import asyncio

from config.features import CURATOR_DEFAULTS, TOOLS_TIMEOUTS
from server.trigger.core import app
from server.trigger.http.helpers import bad_request, ok, read_body
from loguru import logger
from context_engine.curator import reset_idle_for_seconds
from context_engine.curator.orchestrator import run_curator_review
from context_engine.curator.usage import list_archived, restore_skill
from context_engine.curator.config import (
    get_interval_override_days,
    set_interval_override_days,
    get_effective_interval_hours,
    get_last_maintenance_at,
)
from context_engine.curator.state import load_state

# Valid range for the auto-maintenance interval override (days).
_INTERVAL_MIN_DAYS = CURATOR_DEFAULTS["http_interval_min_days"]
_INTERVAL_MAX_DAYS = CURATOR_DEFAULTS["http_interval_max_days"]

# Mirrors the canonical skill-name limit so the route stays agent-free.
_MAX_SKILL_NAME_LENGTH = TOOLS_TIMEOUTS["skill_manage_max_name_length"]

_SEPARATOR_CHARS = ("/", "\\", "\x00", "\n", "\r")


def _validate_restore_name(name: str) -> str | None:
    """Reject names that cannot be a directory name under the archive root.

    Defense in depth only: ``restore_skill`` matches names against directory
    entries inside ``skills/.archive/`` and re-checks the bundled/hub guard,
    so a separator could never resolve a path. Mirrors the shape rules of the
    canonical validator without importing the agent layer.
    """
    if len(name) > _MAX_SKILL_NAME_LENGTH:
        return f"Skill name exceeds {_MAX_SKILL_NAME_LENGTH} characters."
    if name.startswith(".") or any(char in name for char in _SEPARATOR_CHARS):
        return f"Invalid skill name '{name}'."
    return None


@app.get("/curator/settings")
async def get_curator_settings_handler(request):
    """
    Return the currently effective curator settings:
      - auto_interval_days: the UI-configured interval (days, 1..5) or None if
        falling back to the curator.interval_hours setting in sherry.jsonc.
      - interval_hours: the effective interval in hours.
      - last_run_at / last_maintenance_at: ISO timestamps (may be None).
    """
    try:
        state = load_state()
        return {
            "success": True,
            "auto_interval_days": get_interval_override_days(),
            "interval_hours": get_effective_interval_hours(),
            "last_run_at": state.get("last_run_at"),
            "last_maintenance_at": get_last_maintenance_at(),
        }
    except Exception as e:
        logger.exception("Failed to load curator settings: {}", e)
        return {"success": False, "error": str(e)}, {}, 500


@app.put("/curator/settings")
async def put_curator_settings_handler(request):
    """
    Configure the auto-maintenance interval override.

    Accepts a JSON body: {"auto_interval_days": <int|null>}. A literal null
    clears the override and falls back to the curator.interval_hours setting
    in sherry.jsonc (used by the client's "restore default" button). Any other
    non-null value must
    be an integer within the valid 1..5 day range, otherwise a 400 is
    returned — this rejects empty strings, floats, and out-of-range values.

    Returns the persisted override along with the now-effective interval in
    hours.
    """
    try:
        body = request.json()
    except Exception:
        body = None

    if not isinstance(body, dict) or "auto_interval_days" not in body:
        return {"success": False, "error": "Missing 'auto_interval_days'"}, {}, 400

    raw = body.get("auto_interval_days")

    if raw is None:
        # Explicit null -> restore default (legitimate "reset" action).
        days = None
    elif isinstance(raw, bool) or not isinstance(raw, int):
        # Bool is an int subclass; reject it too. Only true integers (not
        # floats like 2.5, not strings like "") satisfy this branch.
        return (
            {
                "success": False,
                "error": (
                    "auto_interval_days must be a whole number "
                    f"between {_INTERVAL_MIN_DAYS} and {_INTERVAL_MAX_DAYS} (or null)"
                ),
            },
            {},
            400,
        )
    elif not (_INTERVAL_MIN_DAYS <= raw <= _INTERVAL_MAX_DAYS):
        return (
            {
                "success": False,
                "error": f"auto_interval_days must be between {_INTERVAL_MIN_DAYS} and {_INTERVAL_MAX_DAYS} days",
            },
            {},
            400,
        )
    else:
        days = raw

    stored = set_interval_override_days(days)
    return {
        "success": True,
        "auto_interval_days": stored,
        "interval_hours": get_effective_interval_hours(),
        "last_maintenance_at": get_last_maintenance_at(),
    }


@app.post("/curator/run")
async def run_curator_handler(request):
    """
    Force-trigger a curator review/maintenance run.

    This directly invokes `run_curator_review` (the forced entry point) rather
    than `maybe_run_curator` (which respects the idle-heuristics scheduler and
    may silently no-op). It returns the execution result so the client can
    reflect the outcome (e.g. transition counts, summary).

    Reset the curator idle counter before running: a manual maintenance is a
    user-initiated action, so the inactivity clock used by the background
    auto-run loop is zeroed to avoid an immediate duplicate auto-trigger.
    """
    try:
        # run_curator_review is a synchronous blocking call (includes an LLM
        # call), so it must run in a thread pool to avoid blocking the asyncio
        # event loop and slowing down other concurrent requests.
        result = await asyncio.to_thread(run_curator_review)
        logger.debug(f"Curator force-run completed: {result}")
        # Failures at the LLM layer do not raise; they are flagged via the
        # "error" field in the result. This must be recognized explicitly,
        # otherwise the frontend would wrongly report "maintenance complete".
        if result.get("error"):
            logger.warning(f"Curator force-run LLM failed: {result['error']}")
            return {"success": False, "error": result["error"], "result": result}
        return {"success": True, "result": result}
    except Exception as e:
        logger.exception("Curator force-run failed: {}", e)
        return {"success": False, "error": str(e)}, {}, 500
    finally:
        reset_idle_for_seconds()


@app.post("/curator/restore")
async def restore_archived_skill_handler(request):
    """
    Restore an archived skill back into ``skills/auto/``.

    Accepts a JSON body ``{"name": "<skill>"}``. The restore itself runs in
    the curator layer (``context_engine.curator.usage.restore_skill``), which
    moves the directory out of ``skills/.archive/`` and flips the curator-side
    usage record back to ``active`` so the skill re-enters lifecycle
    management. Failures (unknown name, bundled/hub shadow, occupied
    destination) return 400 with the curator's message.
    """
    body = read_body(request)
    if body is None:
        return bad_request("Invalid JSON body")

    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        return bad_request("Missing or invalid 'name'")
    name = name.strip()

    err = _validate_restore_name(name)
    if err:
        return bad_request(err)

    restored, msg = restore_skill(name)
    logger.info(f"Curator restore: name={name}, ok={restored} msg={msg}")
    if not restored:
        return bad_request(msg)

    return ok({"success": True, "message": msg, "name": name})


@app.get("/curator/archived")
async def list_archived_skills_handler(request):
    """
    List the skill directory names currently under ``skills/.archive/``.

    Directory names are reported verbatim (timestamped collision entries keep
    their ``<name>-<timestamp>`` suffix) and can be passed back to
    ``POST /curator/restore``.
    """
    names = list_archived()
    return ok({"success": True, "archived": names, "count": len(names)})
