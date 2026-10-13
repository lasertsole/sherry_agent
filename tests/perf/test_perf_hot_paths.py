"""Hot-path performance guards.

What each case protects:

* token estimation — every prompt-size decision (eviction, summarization
  pressure, the context ring) runs through it, so it must stay linear in the
  text it is handed;
* turn slicing — the history paginator re-splits long conversations;
* descendant-run lookup — the subagent tree walk, whose BFS frontier must not be
  re-scanned per node;
* the taskflow store round-trip plus its status summary — the DAG tools hit that
  on every step transition;
* lane acquire/release — every dispatch point pays it;
* the context-usage accounting — the toolbar ring polls it.

Design notes live in ``tests/perf/bench.py``: growth ratios, not milliseconds.
"""

from __future__ import annotations

import json
from time import sleep
from uuid import uuid4
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent.tools.subagent.registry import clear as clear_registry
from agent.tools.subagent.registry import memory as registry_memory
from agent.tools.subagent.registry import queries as registry_queries
from agent.tools.subagent.types.registry import SubagentRunRecord
from agent.tools.taskflow.registry import store_sqlite as flow_store
from agent.tools.taskflow.tools._shared import new_step, steps_summary
from config.features import SUBAGENT_INFRA
from pub.func.estimate_tokens import estimate_json_tokens, estimate_text_tokens
from pub.func.message.turn_utils import split_into_turns
from runtime.lane.core import LaneType, get_lane_manager
from tests.perf.bench import MAX_GROWTH_FACTOR, scaled, timed_async

pytestmark = [pytest.mark.perf, pytest.mark.regression]

# Prose with the CJK + ASCII mix real transcripts have.
_PROSE_UNIT = "上下文治理 concurrency lane 512 tokens の推定 " * 40
_TOOL_SCHEMA = {
    "name": "taskflow_run_task",
    "description": "Register and dispatch one DAG step " * 12,
    "parameters": {
        "type": "object",
        "properties": {f"field_{i}": {"type": "string"} for i in range(20)},
    },
}


def test_text_token_estimation_grows_linearly():
    scaled(
        "estimate_text_tokens",
        lambda factor: estimate_text_tokens(_PROSE_UNIT * (5 * factor)),
        ceiling_seconds=2.0,
    )


def test_tool_schema_estimation_grows_linearly():
    """The ring's tool estimate walks one serialized schema per tool."""
    serialized = json.dumps(_TOOL_SCHEMA)

    scaled(
        "estimate_json_tokens (tool schemas)",
        lambda factor: estimate_json_tokens(serialized * (200 * factor)),
        ceiling_seconds=2.0,
    )


def _history(turns: int) -> list[Any]:
    messages: list[Any] = []
    for i in range(turns):
        messages.append(HumanMessage(content=f"question {i} " * 20))
        messages.append(AIMessage(content=f"answer {i} " * 40))
    return messages


def test_turn_slicing_grows_linearly():
    histories = {scale: _history(200 * scale) for scale in (1, 10)}

    scaled(
        "split_into_turns",
        lambda factor: split_into_turns(histories[factor]),
        ceiling_seconds=2.0,
    )


# ---------------------------------------------------------------------------
# Subagent registry tree walk
# ---------------------------------------------------------------------------


def _seed_registry(runs: int) -> None:
    """One requester with `runs` children, in memory only (no SQLite).

    Non-terminal records, so the store's retention cap never evicts a seed.
    """
    clear_registry()
    for i in range(runs):
        registry_memory.set_run(
            SubagentRunRecord(
                run_id=f"run-{i}",
                child_session_key=f"child-{i}",
                requester_session_key="agent:main:session:perf",
                task=f"task {i}",
            )
        )


#: The realistic worst case is a full registry: sizes derive from the cap so a
#: future cap change cannot silently turn this into an equality comparison.
_CAP = max(2, int(SUBAGENT_INFRA["registry_max_retained_runs"]))
_SMALL = max(1, _CAP // 10)


def test_descendant_run_lookup_is_not_quadratic():
    """A BFS frontier is a deque, not ``list.pop(0)`` on a growing list."""
    for size in (_SMALL, _CAP):
        _seed_registry(size)
        assert len(registry_queries.list_descendant_runs("agent:main:session:perf")) == size

    try:
        scaled(
            "list_descendant_runs",
            lambda factor: len(registry_queries.list_descendant_runs("agent:main:session:perf")),
            prepare=lambda factor: _seed_registry(_SMALL * factor),
            ceiling_seconds=3.0,
        )
    finally:
        clear_registry()


# ---------------------------------------------------------------------------
# TaskFlow store
# ---------------------------------------------------------------------------


def test_flow_state_codec_and_summary_grow_linearly(monkeypatch: pytest.MonkeyPatch):
    """The step-shaped work inside a store round trip: encode, decode, summarise.

    The round trip itself cannot carry a ratio budget — ``create_flow`` costs
    ~10ms before it touches a single step (connection setup and commit on a
    container filesystem), which swamps the step work and pins the ratio near 1x
    (measured: 100 → 1000 steps moved 14.3ms → 21.0ms). These three are the parts
    that actually scale with the DAG, so they get the growth budget.
    """
    states = {
        scale: {
            "description": "perf",
            "steps": [new_step(f"s{i}", f"task {i}") for i in range(100 * scale)],
        }
        for scale in (1, 10)
    }

    import json as _json

    def _round_trip(scale: int) -> None:
        state = states[scale]
        decoded = _json.loads(_json.dumps(state))
        steps_summary(decoded["steps"])

    scaled("flow state json round-trip + steps_summary", _round_trip, ceiling_seconds=2.0)


@pytest.mark.asyncio
async def test_flow_store_roundtrip_stays_in_the_same_order_of_magnitude(isolated_db):
    """A loose ceiling on the I/O round trip, in case an O(n²) ever lands in it."""

    async def _one_round(count: int) -> None:
        flow_id = f"flow-perf-{count}-{uuid4().hex[:8]}"
        steps = [new_step(f"step-{i}", f"task {i}") for i in range(count)]
        await flow_store.create_flow(
            flow_id,
            {"description": "perf", "steps": steps, "results": []},
            session_id="sess-perf",
        )
        flow = await flow_store.get_flow(flow_id, session_id="sess-perf")
        assert flow is not None
        steps_summary(flow["state"]["steps"])

    for count in (0, 1_000):
        await _one_round(count)  # warm, then measure the same work

    elapsed = await timed_async(lambda: _one_round(1_000), repeats=3, warmup=0)
    elapsed_ms = elapsed * 1000
    print(f"[perf] taskflow 1000-step create+get: {elapsed_ms:.1f}ms")
    assert elapsed_ms < 2_000, f"a 1000-step round trip took {elapsed_ms:.0f}ms"


# ---------------------------------------------------------------------------
# Lanes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lane_acquire_release_stays_cheap():
    """Every dispatch point pays an acquire; it must not accumulate cost."""
    lane = get_lane_manager().get_lane(LaneType.MAIN)

    async def _round_trips(count: int) -> None:
        for _ in range(count):
            await lane.acquire(warn_ms=60_000)
            lane.release()

    rounds = 2_000
    median_seconds = await timed_async(lambda: _round_trips(rounds), repeats=3, warmup=1)
    per_acquire_ms = median_seconds / rounds * 1000
    print(f"[perf] lane acquire+release: {per_acquire_ms:.3f}ms each")

    assert per_acquire_ms < 5.0, f"lane acquire costs {per_acquire_ms:.2f}ms"


# ---------------------------------------------------------------------------
# Context-usage accounting (the toolbar ring's endpoint)
# ---------------------------------------------------------------------------


def _long_prompt(blocks: int) -> str:
    """A prompt that grows with `blocks` in both of its parts.

    Growing only the skill index left the fixed persona cost dominating, so the
    measured ratio read ~1x and the case could not have caught anything.
    """
    index = ["<available_skills>"]
    for i in range(20 * blocks):
        index.append(
            f"  <skill>\n    <name>skill_{i}</name>\n    <description>description</description>\n"
            "  </skill>"
        )
    index.append("</available_skills>")
    return "persona and memory block " * (200 * blocks) + "\n\n" + "\n".join(index)


def test_context_usage_accounting_grows_linearly(monkeypatch: pytest.MonkeyPatch):
    from server.service import context_usage_service as service

    monkeypatch.setattr(service, "_reported_prompt_tokens", lambda sid: 100_000)
    monkeypatch.setattr(service, "_tool_schema_tokens", lambda: 8_000)
    monkeypatch.setattr(service, "_cache_hit_ratio", lambda sid: None)

    def _account(blocks: int) -> dict:
        monkeypatch.setattr(service, "_system_prompt_text", lambda sid: _long_prompt(blocks))
        return service.get_context_usage("sess-perf")

    usage = _account(1)
    assert usage["skills"] > 0 and usage["total"] == 100_000

    scaled("get_context_usage", _account, ceiling_seconds=3.0)


# ---------------------------------------------------------------------------
# Guard the harness itself: a quadratic function must fail the ratio
# ---------------------------------------------------------------------------


def test_the_ratio_guard_accepts_linear_and_rejects_quadratic():
    """Sanity: without this the growth budgets above could be vacuous."""
    # Manufactured costs, so the control is instant and exactly shaped: sleep is
    # wall time like any other, and perf_counter measures it.
    linear = lambda factor: sleep(0.002 * factor)  # noqa: E731 - inline by design
    quadratic = lambda factor: sleep(0.0002 * factor * factor)  # noqa: E731 - inline by design

    scaled("control (linear)", linear, repeats=3, warmup=0)
    with pytest.raises(AssertionError, match="superlinear growth"):
        scaled(
            "control (quadratic)",
            quadratic,
            max_factor=MAX_GROWTH_FACTOR,
            repeats=3,
            warmup=0,
        )
