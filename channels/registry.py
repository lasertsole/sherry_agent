"""Auto-discovery for channel plugins under plugins/channels/."""

import pkgutil
import importlib
from loguru import logger
from channels.base import BaseChannel
from config.path import PLUGINS_PATH


def discover_channel_names() -> list[str]:
    """Return all channel module names by scanning plugins/channels/."""
    channel_dir = PLUGINS_PATH / "channels"
    if not channel_dir.is_dir():
        return []

    return [
        name
        for _, name, ispkg in pkgutil.iter_modules([str(channel_dir)])
        if not ispkg
    ]


def _import_path(module_name: str) -> str:
    """Translate a filename stem to an absolute module path for importlib."""
    channel_dir = PLUGINS_PATH / "channels"
    return str(channel_dir / f"{module_name}.py")


def load_channel_class(module_name: str) -> type[BaseChannel]:
    """Import *module_name* from plugins/channels/ and return the first BaseChannel subclass found."""
    from channels.base import BaseChannel as _Base

    spec = importlib.util.spec_from_file_location(
        module_name, _import_path(module_name)
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load spec for channel module {module_name}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    for attr in dir(mod):
        obj = getattr(mod, attr)
        if isinstance(obj, type) and issubclass(obj, _Base) and obj is not _Base:
            return obj
    raise ImportError(f"No BaseChannel subclass in plugins/channels/{module_name}.py")


def discover_plugins() -> dict[str, type[BaseChannel]]:
    """Discover external channel plugins registered via entry_points."""
    from importlib.metadata import entry_points

    plugins: dict[str, type[BaseChannel]] = {}
    for ep in entry_points(group="channels"):
        try:
            cls = ep.load()
            plugins[ep.name] = cls
        except Exception as e:
            logger.warning("Failed to load channel plugin '{}': {}", ep.name, e)
    return plugins


def discover_all() -> dict[str, type[BaseChannel]]:
    """Return all channels: built-in (pkgutil) merged with external (entry_points).

    Built-in channels take priority — an external plugin cannot shadow a built-in name.
    """
    builtin: dict[str, type[BaseChannel]] = {}
    for modname in discover_channel_names():
        try:
            builtin[modname] = load_channel_class(modname)
        except ImportError as e:
            logger.debug("Skipping built-in channel '{}': {}", modname, e)

    external = discover_plugins()
    shadowed = set(external) & set(builtin)
    if shadowed:
        logger.warning("Plugin(s) shadowed by built-in channels (ignored): {}", shadowed)

    return {**external, **builtin}