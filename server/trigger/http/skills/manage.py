"""Auto-skill deletion and pin endpoints.

Deleting an auto skill is pined-aware: `delete_skill` rejects any skill that
is pinned, and resolves nested `skills/auto/<category>/<skill>/`
paths via `_skill_dir`.

Pinning is curator-aware: pinned skills bypass all automatic transitions and
are rejected by `delete_skill`. The endpoint toggles the `pinned` flag in the
usage record via `pin_skill` / `unpin_skill`, mirroring the delete handler's
name validation and snapshot rebuild.
"""

from server.trigger.core import app
from loguru import logger
from context_engine.curator.usage import (
    delete_skill,
    pin_skill,
    unpin_skill,
)

from server.trigger.http.skills._shared import (
    _json_response,
    _rebuild_snapshot,
    _validate_skill_name,
)


@app.post("/skills/delete")
async def delete_auto_skill_handler(request):
    """Delete an auto skill from disk.

    Accepts a JSON body: {"name": string}. The user-facing client MUST show a
    confirmation dialog before invoking this endpoint, since deletion is
    irreversible. Pinned or fixed skills are rejected by `delete_skill`.
    """
    try:
        body = request.json()
    except Exception:
        body = None

    if not isinstance(body, dict):
        return _json_response(400, {"success": False, "message": "Invalid JSON body"})

    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        return _json_response(400, {"success": False, "message": "Missing or invalid 'name'"})
    name = name.strip()

    err = _validate_skill_name(name)
    if err:
        return _json_response(400, {"success": False, "message": err})

    ok, msg = delete_skill(name)
    logger.info(f"Auto skill delete: name={name}, ok={ok} msg={msg}")
    if not ok:
        return _json_response(400, {"success": False, "message": msg})

    # Rebuild the skills snapshot so the deleted skill disappears from the
    # agent's effective skill prompt.
    _rebuild_snapshot()
    return _json_response(200, {"success": True, "name": name})


@app.post("/skills/pin")
async def pin_auto_skill_handler(request):
    """Pin or unpin an auto skill.

    Accepts a JSON body: {"name": string, "pinned": bool}. When `pinned` is
    true the skill is pinned (curator will never merge or remove it); when
    false it is unpinned. The skill must already exist under `skills/auto/`.
    """
    try:
        body = request.json()
    except Exception:
        body = None

    if not isinstance(body, dict):
        return _json_response(400, {"success": False, "message": "Invalid JSON body"})

    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        return _json_response(400, {"success": False, "message": "Missing or invalid 'name'"})
    name = name.strip()

    pinned = body.get("pinned")
    if not isinstance(pinned, bool):
        return _json_response(
            400,
            {"success": False, "message": "Missing or invalid 'pinned' (must be boolean)"},
        )

    err = _validate_skill_name(name)
    if err:
        return _json_response(400, {"success": False, "message": err})

    ok, msg = pin_skill(name) if pinned else unpin_skill(name)
    logger.info(f"Auto skill pin: name={name}, pinned={pinned}, ok={ok} msg={msg}")
    if not ok:
        return _json_response(400, {"success": False, "message": msg})

    # Rebuild the skills snapshot so the agent's effective skill prompt reflects
    # the new pinned state.
    _rebuild_snapshot()
    return _json_response(200, {"success": True, "name": name, "pinned": pinned})
