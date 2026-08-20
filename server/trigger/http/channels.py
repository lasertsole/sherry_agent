import json
from pathlib import Path
from loguru import logger
from server.trigger.core import app
from channels.registry import discover_channel_names, load_channel_class
from config.path import PLUGINS_PATH
from config import API_HOST, API_PORT


def _load_channel_config() -> dict:
    """Load plugins/channels/config.json, returning {} on any failure."""
    config_path = PLUGINS_PATH / "channels" / "config.json"
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f"Failed to load channel config: {e}")
        return {}


def _save_channel_config(config_path: Path, data: dict) -> bool:
    """Atomically persist channel config.json. Returns False on any failure.

    Writes to a temporary file in the same directory then os.replace()s it
    so a failed/interrupted write never leaves a truncated config behind.
    """
    import os
    import tempfile
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(config_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, str(config_path))
        except Exception:
            # Best-effort cleanup of the temp file on failure.
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise
        return True
    except Exception as e:
        logger.error(f"Failed to save channel config: {e}")
        return False


def _channel_icon_url(name: str) -> str:
    """Build an absolute icon URL for a channel, or '' if no icon exists.

    Resolution order — always resolves to at most ONE file, even when the
    icon dir holds many images:
      1. Explicit ``icon`` key in plugins/channels/<name>/config.json
         (e.g. ``"icon": "qq_128.png"``) — the most controllable choice and
         the way to pin a specific file out of several candidates.
      2. Convention file ``<name>_icon_128.<ext>``.
      3. Lexicographically first file in the icon dir (deterministic fallback
         so a multi-icon directory can never resolve ambiguously).
    """
    icon_dir = PLUGINS_PATH / "channels" / name / "icon"
    if not icon_dir.is_dir():
        return ""

    # 1) Explicit per-channel config wins; only the basename is used so the
    #    optional value cannot break out of the icon dir.
    try:
        explicit = _load_channel_local_config(name).get("icon")
        if isinstance(explicit, str) and explicit:
            candidate = icon_dir / Path(explicit).name
            if candidate.is_file():
                return f"http://{API_HOST}:{API_PORT}/channels/{name}/icon/{candidate.name}"
    except Exception:
        pass

    # 2) Canonical convention file.
    for ext in ("png", "jpg", "jpeg", "webp", "svg", "gif"):
        candidate = icon_dir / f"{name}_icon_128.{ext}"
        if candidate.is_file():
            return f"http://{API_HOST}:{API_PORT}/channels/{name}/icon/{candidate.name}"

    # 3) Deterministic fallback: first file by lexicographic order.
    try:
        for entry in sorted(icon_dir.iterdir()):
            if entry.is_file():
                return f"http://{API_HOST}:{API_PORT}/channels/{name}/icon/{entry.name}"
    except Exception:
        pass
    return ""


@app.get("/channels")
async def list_channels_handler(request):
    names = discover_channel_names()
    config = _load_channel_config()
    result = []
    for name in names:
        entry = config.get(name, {})
        enabled = bool(entry.get("enabled", False))
        display_name = name
        try:
            cls = load_channel_class(name)
            display_name = getattr(cls, "display_name", None) or name
        except Exception as e:
            logger.debug(f"Failed to load channel class '{name}': {e}")
        result.append({
            "name": name,
            "display_name": display_name,
            "enabled": enabled,
            # Boolean runtime toggles, persisted in plugins/channels/config.json.
            # heartbeat gate: the heartbeat service also requires a non-empty
            # `receiver` to actually deliver; we only surface the persisted flag here.
            "heartbeat": bool(entry.get("heartbeat", False)),
            "cron": bool(entry.get("cron", False)),
            "icon": _channel_icon_url(name),
        })
    result.sort(key=lambda x: x["name"])
    logger.debug(f"Listed channels: count={len(result)}")
    return {"channels": result}


@app.put("/channels/:channel_name")
async def update_channel_handler(request, path_params):
    """Persist enabled/heartbeat/cron for a channel into plugins/channels/config.json."""
    channel_name = path_params["channel_name"]
    names = discover_channel_names()
    if channel_name not in names:
        logger.warning(f"Unknown channel on update: {channel_name}")
        return {"error": f"Unknown channel: {channel_name}"}, {}, 404

    try:
        body = request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return {"error": "Invalid request body"}, {}, 400

    # Only allow the three boolean toggles we expose in the settings UI.
    allowed = ("enabled", "heartbeat", "cron")
    updates: dict = {}
    for key in allowed:
        if key in body:
            if type(body[key]) is not bool:
                return {"error": f"Field '{key}' must be a boolean"}, {}, 400
            updates[key] = body[key]

    if not updates:
        return {"error": "No updatable fields provided"}, {}, 400

    config_path = PLUGINS_PATH / "channels" / "config.json"
    data = _load_channel_config()
    entry = data.get(channel_name)
    if not isinstance(entry, dict):
        entry = {}
        data[channel_name] = entry
    entry.update(updates)

    if not _save_channel_config(config_path, data):
        return {"error": "Failed to persist channel config"}, {}, 500

    logger.info(f"Updated channel '{channel_name}': {updates}")
    return {
        "channel_name": channel_name,
        "enabled": bool(entry.get("enabled", False)),
        "heartbeat": bool(entry.get("heartbeat", False)),
        "cron": bool(entry.get("cron", False)),
    }


def _channel_config_path(channel_name: str) -> Path:
    """Path to a channel's own config.json under plugins/channels/<name>/config.json.

    Safety: only accepts names found via discover_channel_names(), which scans
    directories that contain a core.py and never returns '..' or path separators.
    """
    return PLUGINS_PATH / "channels" / channel_name / "config.json"


def _load_channel_local_config(channel_name: str) -> dict:
    """Load plugins/channels/<name>/config.json, returning {} on any failure."""
    path = _channel_config_path(channel_name)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f"Failed to load channel local config '{channel_name}': {e}")
        return {}


@app.get("/channels/:channel_name/config")
async def get_channel_config_handler(request, path_params):
    """Return the channel's own config.json contents (plugins/channels/<name>/config.json).

    Exposes the per-channel config (e.g. app_id/receiver for QQ) so the settings
    UI can read it. Returns {} if the file is absent or invalid.
    """
    channel_name = path_params["channel_name"]
    names = discover_channel_names()
    if channel_name not in names:
        logger.warning(f"Unknown channel on get config: {channel_name}")
        return {"error": f"Unknown channel: {channel_name}"}, {}, 404

    config = _load_channel_local_config(channel_name)
    logger.debug(f"Read channel '{channel_name}' local config: {config}")
    return {
        "channel_name": channel_name,
        "config": config,
    }


@app.put("/channels/:channel_name/config")
async def update_channel_config_handler(request, path_params):
    """Persist the channel's own config.json (plugins/channels/<name>/config.json).

    Body must be a JSON object of key/value pairs. Values are stored verbatim
    (strings, numbers, booleans, lists, nested objects all preserved). Written
    atomically via _save_channel_config.
    """
    channel_name = path_params["channel_name"]
    names = discover_channel_names()
    if channel_name not in names:
        logger.warning(f"Unknown channel on update config: {channel_name}")
        return {"error": f"Unknown channel: {channel_name}"}, {}, 404

    try:
        body = request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return {"error": "Invalid request body: expected a JSON object"}, {}, 400

    config_path = _channel_config_path(channel_name)
    if not _save_channel_config(config_path, body):
        return {"error": "Failed to persist channel config"}, {}, 500

    logger.info(f"Updated channel '{channel_name}' local config: keys={list(body.keys())}")
    return {
        "channel_name": channel_name,
        "config": body,
    }


@app.get("/channels/:channel_name/icon/:file_name")
async def channel_icon_handler(request, path_params):
    channel_name = path_params["channel_name"]
    file_name = path_params["file_name"]

    # Path traversal sanitization: only allow a single safe file name.
    if "/" in file_name or "\\" in file_name or ".." in file_name:
        logger.warning(f"Blocked icon path traversal: {channel_name}/{file_name}")
        return {"error": "Invalid icon path"}, {}, 400

    icon_dir = PLUGINS_PATH / "channels" / channel_name / "icon"
    full_path = icon_dir / file_name
    if not full_path.is_file():
        logger.warning(f"Channel icon not found: {channel_name}/{file_name}")
        return {"error": "Icon not found"}, {}, 404

    try:
        from robyn.responses import serve_file
        return serve_file(str(full_path), file_name=file_name)
    except Exception as e:
        logger.warning(f"Failed to serve icon {full_path}: {e}")
        return {"error": "Failed to serve icon"}, {}, 500
