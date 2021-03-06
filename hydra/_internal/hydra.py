# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
import copy
import logging
import os
import string
from argparse import ArgumentParser
from collections import defaultdict
from typing import Any, Callable, DefaultDict, List, Optional, Sequence, Tuple, Type

from omegaconf import DictConfig, OmegaConf, open_dict

from hydra._internal.singleton import Singleton
from hydra.errors import MissingConfigException
from hydra.types import TaskFunction

from ..plugins import CompletionPlugin, Launcher, SearchPathPlugin, Sweeper
from ..plugins.common.utils import (
    HydraConfig,
    JobReturn,
    JobRuntime,
    configure_log,
    run_job,
    setup_globals,
)
from .config_loader import ConfigLoader
from .config_search_path import ConfigSearchPath
from .plugins import Plugins
from .utils import create_automatic_config_search_path, detect_task_name

log: Optional[logging.Logger] = None


class GlobalHydra(metaclass=Singleton):
    def __init__(self) -> None:
        self.hydra: Optional[Hydra] = None

    def initialize(self, hydra: "Hydra") -> None:
        assert isinstance(hydra, Hydra)
        assert not self.is_initialized(), "GlobalHydra is already initialized"
        self.hydra = hydra

    def is_initialized(self) -> bool:
        return self.hydra is not None

    def clear(self) -> None:
        self.hydra = None

    @staticmethod
    def instance(*args: Any, **kwargs: Any) -> "GlobalHydra":
        return Singleton.instance(GlobalHydra, *args, **kwargs)  # type: ignore


class Hydra:
    @classmethod
    def create_main_hydra_file_or_module(
        cls: Type["Hydra"],
        calling_file: Optional[str],
        calling_module: Optional[str],
        config_dir: Optional[str],
        strict: Optional[bool],
    ) -> "Hydra":
        config_search_path = create_automatic_config_search_path(
            calling_file, calling_module, config_dir
        )
        task_name = detect_task_name(calling_file, calling_module)
        return Hydra.create_main_hydra2(task_name, config_search_path, strict)

    @classmethod
    def create_main_hydra2(
        cls,
        task_name: str,
        config_search_path: ConfigSearchPath,
        strict: Optional[bool],
    ) -> "Hydra":
        assert isinstance(config_search_path, ConfigSearchPath)

        config_loader = ConfigLoader(
            config_search_path=config_search_path, default_strict=strict
        )

        hydra = cls(task_name=task_name, config_loader=config_loader)
        GlobalHydra.instance().initialize(hydra)
        return hydra

    def __init__(self, task_name: str, config_loader: ConfigLoader) -> None:
        """
        :param task_name: task name
        :param config_loader: config loader
        """
        setup_globals()
        self.config_loader = config_loader

        for source in config_loader.get_sources():
            # if specified, make sure main config search path exists
            if source.provider == "main":
                if not source.exists(""):
                    raise MissingConfigException(
                        missing_cfg_file=source.path,
                        message="Primary config dir not found: {}".format(source.path),
                    )

        JobRuntime().set("name", task_name)

    def run(
        self,
        config_file: Optional[str],
        task_function: TaskFunction,
        overrides: List[str],
    ) -> JobReturn:
        cfg = self.compose_config(
            config_file=config_file, overrides=overrides, with_log_configuration=True
        )
        HydraConfig().set_config(cfg)
        return run_job(
            config=cfg,
            task_function=task_function,
            job_dir_key="hydra.run.dir",
            job_subdir_key=None,
        )

    def multirun(
        self,
        config_file: Optional[str],
        task_function: TaskFunction,
        overrides: List[str],
    ) -> Any:
        # Initial config is loaded without strict (individual job configs may have strict).
        cfg = self.compose_config(
            config_file=config_file,
            overrides=overrides,
            strict=False,
            with_log_configuration=True,
        )
        HydraConfig().set_config(cfg)
        sweeper = Plugins.instantiate_sweeper(
            config=cfg, config_loader=self.config_loader, task_function=task_function
        )
        task_overrides = cfg.hydra.overrides.task
        return sweeper.sweep(arguments=task_overrides)

    @staticmethod
    def get_sanitized_hydra_cfg(src_cfg: DictConfig) -> DictConfig:
        cfg = copy.deepcopy(src_cfg)
        for key in list(cfg.keys()):
            if key != "hydra":
                del cfg[key]
        del cfg.hydra["hydra_help"]
        del cfg.hydra["help"]
        return cfg

    def show_cfg(
        self, config_file: Optional[str], overrides: List[str], cfg_type: str
    ) -> None:
        assert cfg_type in ["job", "hydra", "all"]
        cfg = self.compose_config(
            config_file=config_file, overrides=overrides, with_log_configuration=True
        )
        if cfg_type == "job":
            del cfg["hydra"]
        elif cfg_type == "hydra":
            cfg = self.get_sanitized_hydra_cfg(cfg)
        print(cfg.pretty())

    @staticmethod
    def get_shell_to_plugin_map(
        config_loader: ConfigLoader,
    ) -> DefaultDict[str, List[CompletionPlugin]]:
        shell_to_plugin: DefaultDict[str, List[CompletionPlugin]] = defaultdict(list)
        for clazz in Plugins.discover(CompletionPlugin):
            plugin = clazz(config_loader)
            shell_to_plugin[plugin.provides()].append(plugin)

        for shell, plugins in shell_to_plugin.items():
            if len(plugins) > 1:
                raise ValueError(
                    "Multiple plugins installed for {} : {}".format(
                        shell, ",".join([type(plugin).__name__ for plugin in plugins])
                    )
                )

        return shell_to_plugin

    def shell_completion(
        self, config_file: Optional[str], overrides: List[str]
    ) -> None:
        subcommands = ["install", "uninstall", "query"]
        arguments = OmegaConf.from_dotlist(overrides)
        num_commands = sum(1 for key in subcommands if arguments[key] is not None)
        if num_commands != 1:
            raise ValueError(
                "Expecting one subcommand from {} to be set".format(subcommands)
            )

        shell_to_plugin = self.get_shell_to_plugin_map(self.config_loader)

        def find_plugin(cmd: str) -> CompletionPlugin:
            if cmd not in shell_to_plugin:
                raise ValueError(
                    "No completion plugin for '{}' found, available : \n{}".format(
                        cmd, "\n".join(["\t" + x for x in shell_to_plugin.keys()])
                    )
                )
            return shell_to_plugin[cmd][0]

        if arguments.install is not None:
            plugin = find_plugin(arguments.install)
            plugin.install()
        elif arguments.uninstall is not None:
            plugin = find_plugin(arguments.uninstall)
            plugin.uninstall()
        elif arguments.query is not None:
            plugin = find_plugin(arguments.query)
            plugin.query(config_file=config_file)

    @staticmethod
    def format_args_help(args_parser: ArgumentParser) -> str:
        s = ""
        overrides: Any = None
        for action in args_parser._actions:
            if len(action.option_strings) == 0:
                overrides = action
            else:
                s += "{} : {}\n".format(",".join(action.option_strings), action.help)
        s += "Overrides : " + overrides.help
        return s

    def list_all_config_groups(self, parent: str = "") -> Sequence[str]:
        groups: List[str] = []
        for group in self.config_loader.list_groups(parent):
            if parent == "":
                group_name = group
            else:
                group_name = "{}/{}".format(parent, group)
            files = self.config_loader.get_group_options(group_name, file_type="file")
            dirs = self.config_loader.get_group_options(group_name, file_type="dir")
            if len(files) > 0:
                groups.append(group_name)
            if len(dirs) > 0:
                groups.extend(self.list_all_config_groups(group_name))
        return groups

    def format_config_groups(
        self, predicate: Callable[[str], bool], compact: bool = True
    ) -> str:
        groups = [x for x in self.list_all_config_groups() if predicate(x)]
        s = ""
        for group in sorted(groups):
            options = sorted(self.config_loader.get_group_options(group))
            if compact:
                items = ", ".join(options)
                line = "{}: {}".format(group, items)
            else:
                items = "\n".join(["  " + o for o in options])
                line = "{}:\n{}".format(group, items)
            s += line + "\n"

        return s

    def get_help(
        self, help_cfg: DictConfig, cfg: DictConfig, args_parser: ArgumentParser
    ) -> str:
        s = string.Template(help_cfg.template)
        help_text = s.substitute(
            FLAGS_HELP=self.format_args_help(args_parser),
            HYDRA_CONFIG_GROUPS=self.format_config_groups(
                lambda x: x.startswith("hydra/")
            ),
            APP_CONFIG_GROUPS=self.format_config_groups(
                lambda x: not x.startswith("hydra/")
            ),
            CONFIG=cfg.pretty(resolve=False),
        )
        return help_text

    def hydra_help(
        self, config_file: Optional[str], args_parser: ArgumentParser, args: Any
    ) -> None:
        cfg = self.compose_config(
            config_file=config_file,
            overrides=args.overrides,
            with_log_configuration=True,
        )
        help_cfg = cfg.hydra.hydra_help
        cfg = self.get_sanitized_hydra_cfg(cfg)
        help_text = self.get_help(help_cfg, cfg, args_parser)
        print(help_text)

    def app_help(
        self, config_file: Optional[str], args_parser: ArgumentParser, args: Any
    ) -> None:
        cfg = self.compose_config(
            config_file=config_file,
            overrides=args.overrides,
            with_log_configuration=True,
        )
        help_cfg = cfg.hydra.help
        clean_cfg = copy.deepcopy(cfg)
        del clean_cfg["hydra"]
        help_text = self.get_help(help_cfg, clean_cfg, args_parser)
        print(help_text)

    @staticmethod
    def _log_header(header: str, prefix: str = "", filler: str = "-") -> None:
        assert log is not None
        log.debug(prefix + header)
        log.debug(prefix + "".ljust(len(header), filler))

    def _print_plugins(self) -> None:
        assert log is not None
        self._log_header(header="Installed Hydra Plugins", filler="*")
        for plugin_type in [SearchPathPlugin, Sweeper, Launcher]:
            Hydra._log_header(
                header="{}:".format(plugin_type.__name__), prefix="\t", filler="-"
            )
            for plugin in Plugins.discover(plugin_type):
                log.debug("\t\t{}".format(plugin.__name__))

    def _get_padding(self) -> Tuple[int, int, int]:
        provider_pad = 0
        search_path_pad = 0
        file_pad = 0
        for sp in self.config_loader.get_sources():
            assert sp.provider is not None
            assert sp.path is not None
            provider_pad = max(provider_pad, len(sp.provider))
            search_path_pad = max(search_path_pad, len(sp.path))
        for trace in self.config_loader.get_load_history():
            file_pad = max(file_pad, len(trace.filename))

        provider_pad += 1
        search_path_pad += 1
        file_pad += 1
        return provider_pad, search_path_pad, file_pad

    def _print_search_path(self) -> None:
        assert log is not None
        log.debug("")
        self._log_header(header="Hydra config search path", filler="*")

        provider_pad, search_path_pad, file_pad = self._get_padding()
        self._log_header(
            "| {} | {} |".format(
                "Provider".ljust(provider_pad), "Search path".ljust(search_path_pad)
            ),
            filler="-",
        )

        for source in self.config_loader.get_sources():
            assert source.provider is not None
            assert source.path is not None
            log.debug(
                "| {} | {} |".format(
                    source.provider.ljust(provider_pad),
                    source.path.ljust(search_path_pad),
                )
            )

    def _print_composition_trace(self) -> None:
        # Print configurations used to compose the config object
        assert log is not None
        provider_pad, search_path_pad, file_pad = self._get_padding()
        log.debug("")
        self._log_header("Composition trace", filler="*")
        self._log_header(
            "| {} | {} | {} |".format(
                "Provider".ljust(provider_pad),
                "Search path".ljust(search_path_pad),
                "File".ljust(file_pad),
            ),
            filler="-",
        )

        for trace in self.config_loader.get_load_history():
            if trace.path is not None:
                assert trace.provider is not None
                assert trace.filename is not None
                log.debug(
                    "| {} | {} | {} |".format(
                        trace.provider.ljust(provider_pad),
                        trace.path.ljust(search_path_pad),
                        trace.filename.ljust(file_pad),
                    )
                )
            else:
                log.debug("{} : NOT FOUND".format(trace.filename))

    def _print_debug_info(self) -> None:
        self._print_plugins()
        self._print_search_path()
        self._print_composition_trace()

    def compose_config(
        self,
        config_file: Optional[str],
        overrides: List[str],
        strict: Optional[bool] = None,
        with_log_configuration: bool = False,
    ) -> DictConfig:
        """
        :param self:
        :param config_file:
        :param overrides:
        :param with_log_configuration: True to configure logging subsystem from the loaded config
        :param strict: None for default behavior (default to true for config file, false if no config file).
                       otherwise forces specific behavior.
        :return:
        """
        cfg = self.config_loader.load_configuration(
            config_file=config_file, overrides=overrides, strict=strict
        )
        with open_dict(cfg):
            from .. import __version__

            cfg.hydra.runtime.version = __version__
            cfg.hydra.runtime.cwd = os.getcwd()
        if with_log_configuration:
            configure_log(cfg.hydra.hydra_logging, cfg.hydra.verbose)
            global log
            log = logging.getLogger(__name__)
            self._print_debug_info()
        return cfg
