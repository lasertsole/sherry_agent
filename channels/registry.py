"""Auto-discovery for channel plugins under plugins/channels/.

Each channel is a plain directory named after the channel, e.g.
``plugins/channels/qq/``. The actual ``BaseChannel`` subclass lives in ``core.py``
inside the directory, dynamically loaded via importlib:

    plugins/channels/qq/
    └── core.py          # defines / re-exports the BaseChannel subclass

Discovery scans directories (not packages) and only considers folders that
contain a ``core.py`` — no ``__init__.py`` is required. Flat single-file modules
(``qq.py``) are intentionally not supported.
"""

import os
import sys
import shutil
import importlib
import subprocess
import importlib.util
from pathlib import Path
from loguru import logger
from channels.base import BaseChannel
from config.path import PLUGINS_PATH


def _ensure_deps(plugin_dir: Path, plugin_name: str) -> bool:
    """Install plugin-local requirements.txt if present.
    Uses *uv* when available (consistent with the project toolchain), otherwise
    falls back to ``python -m pip``.  Installation is idempotent -- already
    satisfied packages are skipped by pip/uv.

    Returns ``True`` if deps are ready (or no requirements.txt found).
    """
    req_file = plugin_dir / "requirements.txt"
    if not req_file.is_file():
        return True

    if shutil.which("uv"):
        cmd = ["uv", "pip", "install", "-q", "-r", str(req_file)]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "-q", "-r", str(req_file)]

    logger.info("Installing dependencies for channel '{}'...", plugin_name)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        logger.error("Timed out installing dependencies for channel '{}'", plugin_name)
        return False

    if result.returncode != 0:
        logger.error(
            "Failed to install dependencies for channel '{}':\n{}",
            plugin_name, result.stderr.strip(),
        )
        return False

    importlib.invalidate_caches()
    logger.info("Dependencies for channel '{}' ready", plugin_name)
    return True


def discover_channel_names() -> list[str]:
    """Return all channel names by scanning plugins/channels/ subdirectories that contain a core.py."""
    channel_dir = PLUGINS_PATH / "channels"
    if not channel_dir.is_dir():
        return []

    names: list[str] = []
    for entry in os.scandir(channel_dir):
        if not entry.is_dir():
            continue
        if (channel_dir / entry.name / "core.py").is_file():
            names.append(entry.name)
    return sorted(names)


def load_channel_class(module_name: str) -> type[BaseChannel]:
    """Dynamically import ``<module_name>/core.py`` from plugins/channels/ and return the first BaseChannel subclass found."""
    from channels.base import BaseChannel as _Base

    channel_dir = PLUGINS_PATH / "channels"
    core_path = channel_dir / module_name / "core.py"
    if not core_path.is_file():
        raise ImportError(
            f"No channel plugins/channels/{module_name}/core.py"
        )

    if not _ensure_deps(channel_dir / module_name, module_name):
        raise ImportError(
            f"Failed to install dependencies for channel {module_name}"
        )

    spec = importlib.util.spec_from_file_location(
        f"channels.plugin.{module_name}", str(core_path)
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load spec for channel {module_name}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    for attr in dir(mod):
        obj = getattr(mod, attr)
        if isinstance(obj, type) and issubclass(obj, _Base) and obj is not _Base:
            return obj
    raise ImportError(
        f"No BaseChannel subclass in plugins/channels/{module_name}/core.py"
    )


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
    """Return all channels: built-in (directory scan) merged with external (entry_points).

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