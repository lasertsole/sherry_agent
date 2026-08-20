import asyncio

from server.trigger.core import app
from loguru import logger
from context_engine.curator import reset_idle_for_seconds
from context_engine.curator.orchestrator import run_curator_review
from context_engine.curator.config import (
    get_interval_override_days,
    set_interval_override_days,
    get_effective_interval_hours,
    get_last_maintenance_at,
)
from context_engine.curator.state import load_state

# Valid range for the auto-maintenance interval override (days).
_INTERVAL_MIN_DAYS = 1
_INTERVAL_MAX_DAYS = 5


@app.get("/curator/settings")
async def get_curator_settings_handler(request):
    """
    Return the currently effective curator settings:
      - auto_interval_days: the UI-configured interval (days, 1..5) or None if
        falling back to curator.yaml's interval_hours.
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
    clears the override and falls back to curator.yaml's interval_hours (used
    by the client's "restore default" button). Any other non-null value must
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
        return {
            "success": False,
            "error": (
                "auto_interval_days must be a whole number "
                f"between {_INTERVAL_MIN_DAYS} and {_INTERVAL_MAX_DAYS} (or null)"
            ),
        }, {}, 400
    elif not (_INTERVAL_MIN_DAYS <= raw <= _INTERVAL_MAX_DAYS):
        return {
            "success": False,
            "error": f"auto_interval_days must be between {_INTERVAL_MIN_DAYS} and {_INTERVAL_MAX_DAYS} days",
        }, {}, 400
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
        # run_curator_review 是同步阻塞调用（含 LLM 调用），必须放到线程池执行，
        # 避免阻塞 asyncio 事件循环、拖慢其他并发请求。
        result = await asyncio.to_thread(run_curator_review)
        logger.debug(f"Curator force-run completed: {result}")
        # LLM 层失败时未抛出异常，而是通过结果里的 error 字段标记。
        # 必须显式识别，否则前端会误报「维护完成」。
        if result.get("error"):
            logger.warning(f"Curator force-run LLM failed: {result['error']}")
            return {"success": False, "error": result["error"], "result": result}
        return {"success": True, "result": result}
    except Exception as e:
        logger.exception("Curator force-run failed: {}", e)
        return {"success": False, "error": str(e)}, {}, 500
    finally:
        reset_idle_for_seconds()