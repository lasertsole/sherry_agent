from server.trigger.core import app
from loguru import logger
from context_engine.curator import reset_idle_for_seconds
from context_engine.curator.orchestrator import run_curator_review


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
        result = run_curator_review()
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