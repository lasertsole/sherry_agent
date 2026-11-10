"""Executor-only PTC replay prefill (system-triggered, before the child is built).

A review-passed plan compiles one ``ptc__<plan_key>__<step_id>.py`` per step; a
step subagent for that plan tries it FIRST. A hit completes the step with zero
LLM tokens; a miss falls through to ReAct carrying the completed prefix in its
initial message.
"""

import json
from collections.abc import Callable
from typing import Any

from loguru import logger

from ..types.functional_role import PTC_ROLES
from ..types.registry import SubagentRunRecord
from .inherited_tool_policy import apply_tool_policy


def _ptc_preview(value: Any, limit: int = 500) -> str:
    text = value if isinstance(value, str) else str(value)
    text = " ".join(text.split())
    return text[:limit]


def _render_ptc_progress(calls_log: list[dict[str, Any]]) -> str:
    """Render the completed prefix + first failed call for the React fallback."""
    completed: list[dict[str, Any]] = []
    failed: dict[str, Any] | None = None
    for call in calls_log:
        if not isinstance(call, dict):
            continue
        if call.get("ok") is True and failed is None:
            completed.append(call)
            continue
        if failed is None:
            failed = call

    lines = ["[PTC Pre-run Progress]", "A PTC replay script was attempted for this task."]
    if completed:
        lines.append("")
        lines.append("Completed steps (side effects already applied — DO NOT redo):")
        for index, call in enumerate(completed, start=1):
            lines.append(
                f"  {index}. {call.get('tool', '?')} "
                f"({_ptc_preview(call.get('args', {}), 240)}) → "
                f"{_ptc_preview(call.get('result', ''), 400)}"
            )
    if failed is not None:
        lines.append("")
        lines.append(
            f"Failed at step {len(completed) + 1}: {failed.get('tool', '?')} "
            f"({_ptc_preview(failed.get('args', {}), 240)}) → "
            f"{_ptc_preview(failed.get('error', ''), 500)}"
        )
    elif not completed:
        lines.append("")
        lines.append("PTC produced no tool calls; continue with normal tools.")
    lines.append("")
    lines.append("Continue from the first unfinished step. Do not repeat completed steps.")
    return "\n".join(lines)


def _ptc_tools_map(
    run: SubagentRunRecord,
    tools: list | None,
    effective_deny: list[str],
) -> dict[str, Any]:
    """The role-filtered PTC tool map, without building a child agent."""
    from agent.tools import build_main_tools
    from config.features import PTC

    base_tools = tools if tools is not None else build_main_tools()
    filtered = apply_tool_policy(base_tools, run.inherited_tool_allow, effective_deny)
    allowed = set(PTC["ptc_allowed_tools"])
    return {
        name: tool
        for tool in filtered
        if (name := str(getattr(tool, "name", "") or "")) in allowed and name != "execute_code"
    }


async def _try_ptc_prefill(
    run: SubagentRunRecord,
    tools: list | None,
    effective_deny: list[str],
    output_schema: dict | None,
    *,
    tools_map_builder: Callable[[SubagentRunRecord, list | None, list[str]], dict[str, Any]]
    | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Try the executor-only PTC replay before building the child agent.

    Returns ``(result, progress)``:
    - success: ``result`` carries the stdout + calls_log, ``progress`` is None;
    - failure: ``result`` is None, ``progress`` is the rendered React hint;
    - no replay candidate: both None.

    ``tools_map_builder`` lets the facade (``spawn.core``) supply ITS
    ``_ptc_tools_map`` global, so a monkeypatched facade attribute still feeds the
    replay path as it did before the module split.
    """
    from config.features import PTC

    if not PTC["ptc_prefill_enabled"] or run.functional_role not in PTC_ROLES:
        return None, None
    if not run.step_id:
        return None, None

    from ..registry.session_keys import normalize_session_key
    from agent.tools.todolist.knowledge.identity import resolve_session_plan_identity
    from agent.tools.ptc.scripts import find_step_script, read_step_script

    root_session = normalize_session_key(run.controller_session_key or run.requester_session_key)
    # A nested spawn's immediate requester can itself be a child key. The plan
    # identity is defined on the root main session, so an abnormal key is a
    # fail-open miss rather than a wrong-script hit.
    if not root_session or root_session.startswith("agent:"):
        return None, None
    try:
        identity = resolve_session_plan_identity(root_session)
        if identity is None:
            return None, None
        path = find_step_script(identity.key, run.step_id)
        if path is None:
            return None, None
        script = read_step_script(identity.key, run.step_id)
        if script is None:
            return None, None
        tools_map = (tools_map_builder or _ptc_tools_map)(run, tools, effective_deny)
    except Exception as exc:  # noqa: BLE001 - a lookup failure must not fail the step
        logger.warning("PTC prefill lookup failed for run {}: {}", run.run_id, exc)
        return None, None
    if not tools_map:
        logger.debug("PTC prefill: no role-allowed PTC tools for run {}", run.run_id)
        return None, None

    from agent.tools.ptc.runner import run_ptc

    try:
        raw = await run_ptc(
            script,
            tools_map,
            run.child_session_key,
            PTC,
            cwd=run.spawned_cwd or None,
        )
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("PTC result is not a JSON object")
    except Exception as exc:  # noqa: BLE001 - fail-open to React
        logger.warning("PTC prefill failed before parsing for run {}: {}", run.run_id, exc)
        return None, _render_ptc_progress([])

    calls_log = result.get("calls_log") or []
    if not isinstance(calls_log, list):
        calls_log = []
    try:
        exit_code = int(result.get("exit_code") or 0)
    except (TypeError, ValueError):
        exit_code = -1
    success = (
        result.get("status") == "ok"
        and exit_code == 0
        and str(result.get("output") or "").strip()
        and bool(calls_log)
        and all(isinstance(call, dict) and call.get("ok") is True for call in calls_log)
    )
    if success and output_schema:
        from ..swarm.collector import validate_structured_output

        try:
            valid, error = validate_structured_output(
                str(result.get("output") or ""), output_schema
            )
        except Exception as exc:  # noqa: BLE001 - schema parsing is fail-open
            valid, error = False, f"{type(exc).__name__}: {exc}"
        if not valid:
            logger.info("PTC prefill schema miss for run {}: {}", run.run_id, error)
            success = False
            calls_log = [
                *calls_log,
                {
                    "tool": "output_schema",
                    "args": {},
                    "error": f"PTC output failed the step schema: {error}",
                    "ok": False,
                },
            ]
    if not success:
        # All tool calls may have succeeded while the script still ended in a
        # non-ok status (empty stdout, timeout, budget, …). Give React one
        # synthetic failed entry so the progress names the real stop point.
        if calls_log and all(
            isinstance(call, dict) and call.get("ok") is True for call in calls_log
        ):
            calls_log = [
                *calls_log,
                {
                    "tool": "ptc",
                    "args": {},
                    "error": str(result.get("error") or f"status={result.get('status')}"),
                    "ok": False,
                },
            ]
        logger.info(
            "PTC prefill fell back to React for run {}: status={} exit={} calls={}",
            run.run_id,
            result.get("status"),
            result.get("exit_code"),
            len(calls_log),
        )
        return None, _render_ptc_progress(calls_log)

    logger.info(
        "PTC prefill succeeded for run {} via {} (calls={})",
        run.run_id,
        path,
        len(calls_log),
    )
    return {
        "output": str(result.get("output") or ""),
        "calls_log": calls_log,
        "script": str(path),
    }, None
