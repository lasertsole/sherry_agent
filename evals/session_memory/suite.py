"""Eval suite for the session-memory subsystem (SESSION plan capabilities).

Scores the session-memory stack end to end under the eval sandbox:

- P0-2  cooldown survives a simulated restart and suppresses re-compaction
- P0-3  compaction lock: mutual exclusion, release, re-acquire
- P1-1  checkpoint restore recovers the exact pre-compaction context
- P1-2  crash-retry replay writes once; fresh content never deduped
- P1-3  ineligible messages leave the history projection
- P2-3  dual-watermark facts extraction (real auxiliary LLM)
- P2-5  semantic search ranking (real local embed model)

Deterministic checks assert correctness; LLM/embedding-backed checks assert
grounded behaviour. Usage: uv run python evals/evals.py session_memory
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from collections.abc import Callable

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals.sandbox import EvalSandbox  # noqa: E402
from langchain_core.messages import BaseMessage, HumanMessage  # noqa: E402

_TIMEOUT_PER_CHECK_S = 120.0


def _sid(case: str) -> str:
    return f"evals-sm-{case}-{uuid.uuid4().hex[:8]}"


def _make_summarization():
    from types import SimpleNamespace

    import agent.middlewares.summarization as summarization_module

    calls: list[object] = []

    class _RecordingModel:
        _llm_type = "fake"

        def invoke(self, prompt, config=None):
            calls.append(prompt)
            return SimpleNamespace(text="summary")

        async def ainvoke(self, prompt, config=None):
            calls.append(prompt)
            return SimpleNamespace(text="summary")

    middleware = summarization_module.Summarization(
        model=_RecordingModel(),
        trigger=[("tokens", 500)],
        keep=("messages", 10),
        main_llm_context_window=8000,
        need_update_system_prompt=False,
    )
    return middleware, calls


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


async def check_cooldown_restart_survival(sid: str) -> dict[str, Any]:
    """P0-2: an armed cooldown survives a simulated restart and re-arms off."""
    import agent.middlewares.summarization as summarization_module
    from runtime import state_register_mem

    middleware, _ = _make_summarization()
    middleware._record_compaction_bookkeeping(sid)
    armed = state_register_mem.get_state(sid, summarization_module._COOLDOWN_ROUNDS_KEY)

    # Simulate a process restart: volatile memory is wiped, durable state stays.
    state_register_mem.clear_session(sid)
    summarization_module._RESTORED_COOLDOWN_SESSIONS.discard(sid)
    middleware._maybe_restore_cooldown_state(sid)
    suppressed_after_restart = middleware._tick_cooldown(sid)  # True = still cooling

    # Exhaust the rehydrated rounds: proactive compression re-enables.
    while state_register_mem.get_state(sid, summarization_module._COOLDOWN_ROUNDS_KEY, 0) or 0 > 0:
        middleware._tick_cooldown(sid)
    re_enabled = not middleware._tick_cooldown(sid)

    passed = (
        armed == summarization_module.COMPACTION_COOLDOWN_ROUNDS
        and suppressed_after_restart
        and re_enabled
    )
    return {"passed": passed, "armed": armed, "suppressed_after_restart": suppressed_after_restart}


async def check_compaction_lock_mutual_exclusion(sid: str) -> dict[str, Any]:
    """P0-3: a held lock excludes a second holder; release re-enables it."""
    from agent.middlewares.compaction_lock import CompactionLock, CompactionLockError

    lock = CompactionLock()
    excluded = False
    async with lock.acquire(sid, timeout_s=5.0):
        try:
            async with lock.acquire(sid, timeout_s=0.4):
                excluded = False  # second holder got in: mutual exclusion broken
        except CompactionLockError:
            excluded = True
    async with lock.acquire(sid, timeout_s=5.0):
        released = True
    return {"passed": excluded and released, "mutual_exclusion": excluded, "releasable": released}


async def check_checkpoint_restore_roundtrip(sid: str) -> dict[str, Any]:
    """P1-1: restore recovers the exact pre-compaction context, no data loss."""
    from context_engine.store import add_messages
    from context_engine.store.core import (
        create_compaction_checkpoint,
        get_history_by_turn_page,
        get_max_turn_num,
        mark_messages_compacted,
        restore_compaction_checkpoint,
    )

    await add_messages(sid, [HumanMessage(content="before compaction")])
    pre_turn = get_max_turn_num(sid)
    checkpoint_id = create_compaction_checkpoint(
        sid, pre_compaction_turn=pre_turn, post_compaction_turn=pre_turn
    )
    mark_messages_compacted(sid, from_turn=1, checkpoint_id=checkpoint_id)
    await add_messages(sid, [HumanMessage(content="after compaction")])

    restore_compaction_checkpoint(sid, checkpoint_id)
    rows = get_history_by_turn_page(sid)
    contents = [row["content"] for row in rows]

    passed = contents == ["before compaction"]
    return {"passed": passed, "active_contents": contents}


async def check_idempotent_replay(sid: str) -> dict[str, Any]:
    """P1-2: replaying flushed messages writes once; fresh content is kept."""
    from context_engine.store import add_messages
    from context_engine.store.core import get_max_turn_num

    batch: list[BaseMessage] = [HumanMessage(content="idempotent replay probe")]
    await add_messages(sid, batch)
    turns_after_first = get_max_turn_num(sid)
    await add_messages(sid, batch)  # crash-retry replay of the same objects
    turns_after_replay = get_max_turn_num(sid)

    await add_messages(sid, [HumanMessage(content="a brand new turn")])
    turns_after_fresh = get_max_turn_num(sid)

    passed = (
        turns_after_first == 1
        and turns_after_replay == turns_after_first
        and turns_after_fresh == turns_after_first + 1
    )
    return {
        "passed": passed,
        "turns": [turns_after_first, turns_after_replay, turns_after_fresh],
    }


async def check_context_eligible_filter(sid: str) -> dict[str, Any]:
    """P1-3: ineligible messages leave the projection but stay on disk."""
    from context_engine.store import add_messages
    from context_engine.store.core import get_history_by_turn_page

    eligible = HumanMessage(content="eligible context row")
    ineligible = HumanMessage(content="ineligible progress row")
    ineligible.additional_kwargs["context_eligible"] = False
    await add_messages(sid, [eligible, ineligible])

    projected = get_history_by_turn_page(sid)
    everything = get_history_by_turn_page(sid, only_eligible=False)

    projected_texts = [str(r["content"]) for r in projected]
    passed = (
        any("eligible context row" in t for t in projected_texts)
        and not any("ineligible progress row" in t for t in projected_texts)
        and len(everything) == len(projected) + 1
    )
    return {"passed": passed, "projected": len(projected), "stored": len(everything)}


async def check_facts_extraction_llm(sid: str, tiered) -> dict[str, Any]:
    """P2-3: the real auxiliary LLM extracts durable facts from a fixture turn."""
    from context_engine.facts.queue import enqueue_turn, process_pending
    from context_engine.store import add_messages
    from context_engine.store.core import get_max_turn_num

    fixture: BaseMessage = HumanMessage(
        content="Important project decision: the team adopted uv for dependency "
        "management and SQLite WAL mode for storage."
    )
    await add_messages(sid, [fixture])
    turn = get_max_turn_num(sid)
    await enqueue_turn(sid, turn)
    written = await process_pending(sid, tiered_store=tiered)

    facts = tiered.read_facts()
    joined = json.dumps(facts, ensure_ascii=False).lower()
    passed = written >= 1 and ("uv" in joined or "wal" in joined)
    return {"passed": passed, "facts_written": written, "facts": facts}


async def check_semantic_search_ranking(sid: str) -> dict[str, Any]:
    """P2-5: the real embed model ranks the topical message first."""
    from context_engine.embeddings import semantic_search
    from context_engine.store import add_messages

    await add_messages(sid, [HumanMessage(content="fixed the docker networking bug")])
    await add_messages(sid, [HumanMessage(content="booked a vacation to okinawa")])

    matches = await semantic_search("docker networking", session_id=sid, limit=2)
    passed = bool(matches) and "docker" in str(matches[0]["content"]).lower()
    return {
        "passed": passed,
        "top_score": matches[0]["score"] if matches else None,
        "top_content": str(matches[0]["content"])[:120] if matches else "",
    }


async def _run_checks(results_dir: Path) -> list[dict[str, Any]]:
    from agent.tools.memory import MemoryStore
    from agent.tools.memory_tiered import TieredMemoryStore

    tiered = TieredMemoryStore(memory_store=MemoryStore(), facts_dir=results_dir / "facts")
    checks: list[dict[str, Any]] = []

    async def run(name: str, fn: Callable, *args) -> None:
        started = time.monotonic()
        try:
            detail = await fn(*args)
            detail["case"] = name
            detail["latency_s"] = round(time.monotonic() - started, 2)
        except Exception as exc:  # noqa: BLE001 — a failed check is a data point
            detail = {
                "case": name,
                "passed": False,
                "error": f"{type(exc).__name__}: {exc}",
                "latency_s": round(time.monotonic() - started, 2),
            }
        checks.append(detail)
        print(f"  [{name}] passed={detail['passed']} ({detail['latency_s']}s)", flush=True)

    # Deterministic infrastructure checks (stubbed models, no LLM cost).
    await run("cooldown_restart_survival", check_cooldown_restart_survival, _sid("cooldown"))
    await run(
        "compaction_lock_mutual_exclusion", check_compaction_lock_mutual_exclusion, _sid("lock")
    )
    await run(
        "checkpoint_restore_roundtrip", check_checkpoint_restore_roundtrip, _sid("checkpoint")
    )
    await run("idempotent_replay", check_idempotent_replay, _sid("idempotent"))
    await run("context_eligible_filter", check_context_eligible_filter, _sid("eligible"))

    # Real-model checks: auxiliary LLM extraction + local embed ranking.
    await run("facts_extraction_llm", check_facts_extraction_llm, _sid("facts"), tiered)
    await run("semantic_search_ranking", check_semantic_search_ranking, _sid("semantic"))

    return checks


def main() -> None:
    """Run the session-memory eval suite under the sandbox and report."""
    run_id = time.strftime("%Y%m%d_%H%M%S")
    results_dir = REPO_ROOT / "evals" / "results" / "session_memory" / run_id
    results_dir.mkdir(parents=True)

    sandbox = EvalSandbox(results_dir)
    sandbox.apply()
    started = time.monotonic()
    try:
        checks = asyncio.run(_run_checks(results_dir))
    finally:
        sandbox.restore()

    passed = sum(1 for c in checks if c.get("passed"))
    aggregate = {
        "checks_total": len(checks),
        "checks_passed": passed,
        "pass_rate": round(passed / len(checks), 4) if checks else 0.0,
        "duration_s": round(time.monotonic() - started, 2),
    }
    report = {"run_id": run_id, "aggregate": aggregate, "checks": checks}
    (results_dir / "eval_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"\n[evals] session_memory aggregate: {aggregate}", flush=True)
    print(f"[evals] report: {results_dir}", flush=True)
    sys.stdout.flush()
    # Child checkpoint connections keep the loop alive after asyncio.run (same
    # known hang tests/conftest.py works around); the report is on disk.
    os._exit(0)


if __name__ == "__main__":
    main()
