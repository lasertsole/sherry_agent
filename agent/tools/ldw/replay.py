"""LDW replay entry: lazy compile, cross-process journal, and degradation result."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from config.features import LDW
from config.path import PLAN_KNOWLEDGE_DIR, ROOT_DIR
from agent.tools.subagent.ldw_shim import InMemoryJournalStore, SqliteWorkflowStore, run_workflow
from agent.tools.subagent.registry.session_keys import normalize_session_key
from agent.tools.todolist.knowledge.identity import resolve_session_plan_identity

from .compiler import build_orchestrate, compile_plan_to_workflow, render_step_result
from .leaf import register_roster
from .scripts import load_ldw_script, write_ldw_script

_REPLAY_LOCKS: dict[str, asyncio.Lock] = {}


def _lock(plan_key: str) -> asyncio.Lock:
    lock = _REPLAY_LOCKS.get(plan_key)
    if lock is None:
        lock = asyncio.Lock()
        _REPLAY_LOCKS[plan_key] = lock
    return lock


def _count_path(plan_key: str) -> Path | None:
    if not plan_key or any(
        ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        for ch in plan_key
    ):
        return None
    return PLAN_KNOWLEDGE_DIR / plan_key / "ldw-replay.json"


def replay_count(plan_key: str) -> int:
    path = _count_path(plan_key)
    if path is None or not path.is_file():
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return max(0, int(payload.get("count") or 0))
    except (OSError, ValueError, TypeError):
        return 0


def _increment_replay_count(plan_key: str) -> int:
    path = _count_path(plan_key)
    if path is None:
        return 0
    count = replay_count(plan_key) + 1
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"count": count}), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        logger.exception("LDW replay: failed to persist replay count for {}", plan_key)
    return count


def _find_flow_id(session_id: str) -> str:
    try:
        from agent.tools.todolist.registry.store_sqlite import get_todos_sync

        for todo in get_todos_sync(session_id) or []:
            flow_id = str(todo.get("flow_id") or "").strip()
            if flow_id:
                return flow_id
    except Exception:
        logger.debug("LDW replay: no flow id from todos for {}", session_id, exc_info=True)
    return ""


def plan_knowledge_from_flow(session_id: str, *, flow_id: str = "") -> dict[str, Any] | None:
    """Read the TaskFlow DAG and turn it into compile input."""
    flow_id = flow_id or _find_flow_id(session_id)
    if not flow_id:
        return None
    try:
        from agent.tools.taskflow.registry.store_sqlite import get_flow_sync

        flow = get_flow_sync(flow_id, session_id)
    except Exception:
        logger.exception("LDW replay: cannot read flow {}", flow_id)
        return None
    if not flow:
        return None
    state = flow.get("state") or {}
    raw_steps = state.get("steps") or []
    if not isinstance(raw_steps, list) or not raw_steps:
        return None
    identity = resolve_session_plan_identity(session_id)
    contract_keys = (
        "extra_tools",
        "response_schema",
        "step_model",
        "judge_criteria",
        "validation_criteria",
        "step_timeout_seconds",
        "retry_policy",
        "skills",
        "expected_params",
        "input_bindings",
        "priority",
        "file_deps",
        "aggregate_deps",
    )
    steps: list[dict[str, Any]] = []
    for step in raw_steps:
        if not isinstance(step, dict):
            continue
        item: dict[str, Any] = {
            "step_id": str(step.get("step_id") or ""),
            "task": str(step.get("task") or ""),
            "functional_role": str(step.get("functional_role") or "general"),
            "depends_on": [str(dep) for dep in (step.get("depends_on") or [])],
        }
        for key in contract_keys:
            if step.get(key) is not None:
                item[key] = step[key]
        steps.append(item)
    return {
        "plan_key": identity.key if identity else "",
        "plan_name": identity.plan_name if identity else "",
        "flow_id": flow_id,
        "steps": steps,
    }


async def _run_ldw(
    plan_key: str,
    meta: dict[str, Any],
    session_id: str,
    variant_input: str,
) -> dict[str, Any]:
    orchestrate = build_orchestrate(meta, variant_input)
    leaf_calls: list[str] = []
    roster = register_roster(meta, session_id, on_leaf_call=leaf_calls.append)
    step_ids = [str(step.get("step_id")) for step in (meta.get("steps") or [])]
    structured_ids = {
        str(step.get("step_id"))
        for step in (meta.get("steps") or [])
        if isinstance(step.get("response_schema"), dict)
    }
    store = None
    try:
        if LDW["journal_path"] and SqliteWorkflowStore is not None:
            path = Path(LDW["journal_path"])
            if not path.is_absolute():
                path = Path(ROOT_DIR) / path
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                store = await SqliteWorkflowStore.open(path)
                journal = store.journal_for(plan_key)
            except Exception:
                logger.exception("LDW replay: sqlite journal unavailable; using memory")
                store = None
                journal = InMemoryJournalStore()
        else:
            journal = InMemoryJournalStore()

        run_error = ""
        try:
            raw_results = await asyncio.wait_for(
                run_workflow(orchestrate, roster=roster, journal=journal),
                timeout=float(LDW["real_replay_timeout_s"]),
            )
        except TimeoutError:
            raw_results = {}
            run_error = f"LDW replay timed out after {LDW['real_replay_timeout_s']}s"
            logger.warning("LDW replay: {} ({})", run_error, plan_key)
        except Exception as exc:  # noqa: BLE001 - a failed workflow degrades, it does not abort
            raw_results = {}
            run_error = f"{type(exc).__name__}: {exc}"
            logger.exception("LDW replay: workflow failed for {}", plan_key)

        results = {
            step_id: (
                None
                if value is None
                else render_step_result(value, structured=step_id in structured_ids)
            )
            for step_id, value in (raw_results or {}).items()
        }
        # A failed/timed-out workflow may return no entries at all: every step
        # that produced no result is a failure candidate for the fallback.
        failed = [step_id for step_id in step_ids if results.get(step_id) is None]
        outcome: dict[str, Any] = {
            "plan_key": plan_key,
            "status": "partial" if failed else "ok",
            "results": results,
            "failed_steps": failed,
            "journal_hits": max(0, len(step_ids) - len(leaf_calls)),
            "leaf_calls": len(leaf_calls),
        }
        if run_error:
            outcome["error"] = run_error
        if failed and LDW["auto_degrade_enabled"]:
            if len(failed) > int(LDW["max_failed_steps"]):
                outcome["degrade_skipped"] = (
                    f"{len(failed)} failed steps exceeds max_failed_steps={LDW['max_failed_steps']}"
                )
            else:
                from .taskflow_adapter import run_taskflow_fallback

                succeeded = {
                    str(step_id): result
                    for step_id, result in results.items()
                    if result is not None
                }
                try:
                    degraded = await run_taskflow_fallback(
                        session_id,
                        plan_key,
                        meta,
                        succeeded,
                        variant_input,
                        journal=journal,
                        repair=bool(LDW["repair_enabled"]),
                    )
                    outcome["degraded"] = degraded
                    if degraded.get("status") == "ok":
                        outcome["status"] = "degraded"
                    else:
                        outcome["status"] = "partial"
                        if degraded.get("error"):
                            outcome["degrade_error"] = str(degraded["error"])
                except Exception as exc:  # noqa: BLE001 - degrade is fail-open
                    logger.exception("LDW replay: TaskFlow degradation failed for {}", plan_key)
                    outcome["degrade_error"] = str(exc)
        return outcome
    finally:
        if store is not None:
            try:
                await store.aclose()
            except Exception:
                logger.debug("LDW replay: sqlite store close failed", exc_info=True)


async def replay_plan(
    session_id: str,
    *,
    plan_key: str = "",
    variant_input: str = "",
    flow_id: str = "",
    force: bool = False,
    degrade: Callable[[str, dict[str, Any]], Awaitable[Any]] | None = None,
) -> dict[str, Any]:
    """Replay one plan through LDW after the replay-count threshold.

    Below the threshold this returns ``status="taskflow"``; the caller should run
    the normal TaskFlow path. A missing script is compiled from the current
    TaskFlow DAG when possible, then retried once.
    """
    if not LDW["enabled"]:
        return {"status": "disabled", "plan_key": plan_key}
    sid = normalize_session_key(session_id)
    identity = resolve_session_plan_identity(sid)
    if not plan_key:
        plan_key = identity.key if identity else ""
    if not plan_key:
        return {"status": "no_plan", "plan_key": ""}

    async with _lock(plan_key):
        count = _increment_replay_count(plan_key)
        if not force and count < int(LDW["min_replays"]):
            return {
                "status": "taskflow",
                "plan_key": plan_key,
                "replay_count": count,
                "min_replays": int(LDW["min_replays"]),
            }

        meta = load_ldw_script(plan_key)
        if meta is None:
            # 预设角色 → 高级: flow replay compilation is global-only. An
            # EXISTING script still replays (the switch governs compilation,
            # not playback); a missing one is not compiled while it is off.
            from runtime.session.extraction import flow_extraction_enabled

            if not flow_extraction_enabled(sid):
                return {
                    "status": "flow_extraction_disabled",
                    "plan_key": plan_key,
                    "replay_count": count,
                }
            knowledge = plan_knowledge_from_flow(sid, flow_id=flow_id)
            if knowledge is None:
                return {"status": "no_workflow", "plan_key": plan_key, "replay_count": count}
            try:
                meta = compile_plan_to_workflow(knowledge)
            except ValueError as exc:
                return {"status": "no_workflow", "plan_key": plan_key, "error": str(exc)}
            write_ldw_script(plan_key, meta)
            meta = load_ldw_script(plan_key) or meta
        try:
            outcome = await _run_ldw(plan_key, meta, sid, variant_input)
        except Exception as exc:  # noqa: BLE001 - replay is fail-open
            logger.exception("LDW replay failed for {}", plan_key)
            return {"status": "error", "plan_key": plan_key, "error": str(exc)}
        outcome["replay_count"] = count
        if outcome.get("status") == "partial" and "degraded" not in outcome and degrade is not None:
            try:
                outcome["degraded"] = await degrade(plan_key, outcome)
            except Exception as exc:  # noqa: BLE001
                logger.exception("LDW degradation failed for {}", plan_key)
                outcome["degrade_error"] = str(exc)
        return outcome
