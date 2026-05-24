import importlib.util
import logging
import os

from plugins.base import AOLPlugin

log = logging.getLogger("filter.plugins")

_SKIP = {"__init__", "base"}


def load_plugins(plugin_dir: str | None = None) -> list[AOLPlugin]:
    if plugin_dir is None:
        plugin_dir = os.path.dirname(os.path.abspath(__file__))

    plugins = []
    for fname in sorted(os.listdir(plugin_dir)):
        if not fname.endswith(".py") or fname[:-3] in _SKIP:
            continue
        path = os.path.join(plugin_dir, fname)
        try:
            spec   = importlib.util.spec_from_file_location(f"plugins.{fname[:-3]}", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            for attr in dir(module):
                cls = getattr(module, attr)
                if (
                    isinstance(cls, type)
                    and issubclass(cls, AOLPlugin)
                    and cls is not AOLPlugin
                ):
                    instance = cls()
                    plugins.append(instance)
                    log.info(f"loaded plugin: {instance.name} ({fname})")
        except Exception as e:
            log.warning(f"failed to load plugin {fname}: {e}")

    return plugins
