"""Live-network e2e: compression-time nudge cadence on the REAL summary chain.

LIVE-NETWORK, RUN EXPLICITLY. These tests drive the production
``Summarization`` middleware (real auxiliary-LLM summaries via
``build_auxiliary_llm()``), the REAL compression-time nudge agents built by
``summarization/nudges.py`` (main LLM via ``build_main_llm()``), the real
``knowledge`` / ``skill_manage`` / ``memory`` tools, and real SQLite stores
(MesMemory, todos.db, taskflow registry, state registers). They require a
populated ``.env`` with working endpoints. Run with::

    uv run --no-sync pytest tests/full/test_nudge_cadence_e2e.py -v --durations=0

Marker decision: deliberately NOT tagged ``llm_e2e``. ``tests/full/`` is
excluded from ``tests/run_tests_split.py`` by construction, so the hermetic CI
gate never collects this file; tagging it ``llm_e2e`` would instead pull it into
the dedicated ``--with-llm-e2e`` CI job, which must not depend on live network
credentials nor write real session data.

Contract under test (the "nudge cadence" contract): every compression that
actually discards messages dispatches ONE memory review; there is no counter or
threshold that could suppress the 2nd/3rd dispatch; plan extraction is
evaluated independently in the same round when the todo list is all-complete;
an in-flight nudge lock skips dispatch without queueing. Nudge agents, summary
LLM calls, plan-knowledge writes and skill writes are all real — spies only
COUNT and OBSERVE (they wrap the production functions and delegate to them).

Coverage:

1. Single compression → exactly one memory-review dispatch, the real nudge
   agent runs (real main-LLM usage) and attempts a real ``memory`` tool write.
2. Three consecutive compressions (cooldown ticked between them, the production
   gate) → three dispatches; no ``nudge_review_memory_count``-style key exists
   in either state register.
3. Lock semantics: a held ``nudge_review_memory_lock`` skips dispatch and
   queues nothing; after release the next compression dispatches normally.
4. All-complete todos: memory review and plan extraction both fire in the same
   round, sequentially, without suppressing each other.
5. Plan-extraction double output: real knowledge files under
   ``<plan_knowledge>/<plan_key>/`` (``meta.json`` + ``plan-summary.json``) AND
   a real ``skill_manage``-created skill in the auto-skills sandbox.
6. P1-9 interleave: a ~210K-char human message is evicted (full text in state +
   ``lc_evicted_to``, preview in the model view) and then compressed — the
   summary is real, the memory review is dispatched in the same round, the
   TaskFlow context injection still reaches the summary prompt, and the
   eviction file is untouched.
7. P0-2 interleave: a ~25K-char terminal result is evicted (persistence-first
   chain, preview in state) and then compressed — MesMemory keeps the full
   text, the eviction file round-trips byte-identically, and the review fires.
8. Multi-session isolation: A and B each compress; each review sees ONLY its
   own conversation, memory writes are attributed to per-session sandboxes
   with zero cross-pollution, and ``_get_taskflow_context_sync`` per session
   contains only its own flow.
9. Recovery chain: an insufficient tail clip degrades to the real LLM
   compaction, which succeeds, and the memory review is dispatched in the same
   round.

Session sandboxing / cleanup: memory files, plan knowledge and auto-skills are
redirected into the pytest tmp dir; the real session directory, MesMemory rows,
checkpoints, todo rows, taskflow rows, state-register rows, compaction-lock
rows and any pending nudge tasks for every session this module created are
purged in ``finally`` blocks, so the module is independently re-runnable.

Cost note: ``compression_todo_update_enabled`` is turned off for these tests —
the post-compression todo reconciliation fork is a separate nudge path with its
own contract, not asserted here, and disabling it keeps every main-model call
attributable to the cadence/extraction checks above.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shutil
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from langchain.agents.middleware import ModelRequest
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from loguru import logger

import agent.core as agent_core
from agent.checkpointer.async_sqlite_checkpointer import delete_thread_history
from agent.middlewares.context_eviction import ContextEvictionMiddleware
from agent.middlewares.message_persistence import MessagePersistenceMiddleware
from agent.middlewares.summarization import nudges as nudge_mod
from agent.middlewares.summarization.core import Summarization, _get_taskflow_context_sync
from agent.tools import memory as memory_module
from agent.tools.skill_tools import skill_manage
from agent.tools.taskflow.registry.store_sqlite import create_flow, delete_flows_by_session
from agent.tools.todolist.knowledge import knowledge_store
from agent.tools.todolist.knowledge.identity import plan_key
from agent.tools.todolist.registry.store_sqlite import delete_todos_by_session, replace_all
from config import SESSIONS_DIR
from config.features import SUMMARIZATION
from context_engine import delete_messages_by_session
from context_engine.store.core import get_history_by_turn_page
from models import build_auxiliary_llm
from pub.func.message.eviction import EVICTED_TO_KEY
from runtime import clear_all_register_sessions, state_register_db, state_register_mem

pytestmark = [pytest.mark.unit, pytest.mark.timeout(1200)]

_CTX_WINDOW = 20_000  # usable 4000; compact threshold 3200; preserve budget 5000
_BIG_CHARS = 26_000  # ~6.5K estimated tokens: hard overflow, no tail clip
_EVICTED_PATH_RE = re.compile(r"\[evicted to: (.*?)\]")
_SUMMARY_OPEN_TAG = "<summary>"
_TEST_SYSTEM_PROMPT = "You are Sherry, an e2e test session agent. Follow the instructions exactly."


# ---------------------------------------------------------------------------
# Session / store helpers
# ---------------------------------------------------------------------------


def _new_sid(tag: str) -> str:
    return f"e2e-nudge-{tag}-{uuid.uuid4().hex[:8]}"


async def _purge_session(session_id: str) -> None:
    """Delete every trace of a session this module created."""
    with contextlib.suppress(Exception):
        delete_messages_by_session(session_id)
    with contextlib.suppress(Exception):
        await delete_thread_history(session_id)
    with contextlib.suppress(Exception):
        await delete_todos_by_session(session_id)
    with contextlib.suppress(Exception):
        await delete_flows_by_session(session_id)
    with contextlib.suppress(Exception):
        shutil.rmtree(Path(SESSIONS_DIR) / session_id, ignore_errors=True)
    with contextlib.suppress(Exception):
        clear_all_register_sessions(session_id=session_id, clear_persistent_states=True)
    with contextlib.suppress(Exception):
        _drop_compaction_lock(session_id)


def _drop_compaction_lock(session_id: str) -> None:
    from context_engine.store import db as store_db

    db_path = Path(store_db.get_db_path())
    if not db_path.exists():
        return
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    try:
        conn.execute("DELETE FROM compression_locks WHERE session_id = ?", (session_id,))
        conn.commit()
    finally:
        conn.close()


def _rows(session_id: str) -> list[dict]:
    return get_history_by_turn_page(session_id, turn_page_size=1000)


def _rows_with_role(session_id: str, role: str) -> list[dict]:
    return [row for row in _rows(session_id) if row["role"] == role]


def _evicted_path(preview: str) -> Path:
    match = _EVICTED_PATH_RE.search(preview)
    assert match is not None, f"preview carries no eviction path: {preview[:200]!r}"
    return Path(match.group(1))


async def _cancel_pending_nudges() -> None:
    pending = [
        task
        for task in (*nudge_mod._COMPRESSION_NUDGE_TASKS, *nudge_mod._COMPRESSION_TODO_TASKS)
        if not task.done()
    ]
    if not pending:
        return
    for task in pending:
        task.cancel()
    with contextlib.suppress(Exception):
        await asyncio.gather(*pending, return_exceptions=True)


async def _settle_nudge_tasks(timeout_s: float = 900.0) -> None:
    """Wait until every fire-and-forget compression nudge task has finished."""
    deadline = time.monotonic() + timeout_s
    while True:
        pending = [
            task
            for task in (*nudge_mod._COMPRESSION_NUDGE_TASKS, *nudge_mod._COMPRESSION_TODO_TASKS)
            if not task.done()
        ]
        if not pending:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            pytest.fail(f"compression nudge tasks did not finish: {len(pending)} still pending")
        await asyncio.wait(pending, timeout=remaining)


# ---------------------------------------------------------------------------
# Payload builders (small enough for cheap nudge prompts, big enough to compact)
# ---------------------------------------------------------------------------


def _log_blob(tag: str, total_chars: int = _BIG_CHARS) -> str:
    line = f"2026-09-19 INFO [{tag}] worker batch event: item processed and recorded\n"
    return line * max(total_chars // len(line), 1)


def _preference_blob(tag: str, total_chars: int = _BIG_CHARS) -> str:
    tail = (
        "\nThe user said, word for word: 'Always answer me in Chinese, keep replies "
        f"short, and call me 队长 ({tag}).' This is a durable preference.\n"
    )
    body = _log_blob(tag, max(total_chars - len(tail), 0))
    return body + tail


def _human_210k() -> str:
    tail = (
        "\nThe user said, word for word: 'Always answer me in Chinese, keep replies "
        "short, and call me 队长.' This is a durable preference.\n"
    )
    body = _log_blob("p19-eviction", 210_000 - len(tail))
    return body + tail


def _conversation(blob: str, closing: str = "请继续。") -> list[AnyMessage]:
    return [
        HumanMessage(content=blob),
        AIMessage(content="收到，我会按你的要求处理。"),
        HumanMessage(content=closing),
    ]


def _plan_conversation(plan_name: str) -> list[AnyMessage]:
    head = _log_blob(f"plan-{plan_name}")
    tail = (
        f"\nThe plan {plan_name} is complete: the jittered backoff helper landed in "
        "pub/func/retry_utils.py, LLMRetryMiddleware now uses it, and the new timing "
        "tests pass.\n"
    )
    return [
        HumanMessage(content=head + tail),
        AIMessage(content="计划已完成，记录一下经验。"),
        HumanMessage(content="好的，请把完成情况整理成知识。"),
    ]


def _ai_tool_call(*call_ids: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": "search", "args": {"q": call_id}, "id": call_id} for call_id in call_ids
        ],
    )


# ---------------------------------------------------------------------------
# Observing spies: wrap production callables, never replace behavior
# ---------------------------------------------------------------------------


class _NudgeRun:
    """One recorded nudge-agent invocation (observation only)."""

    def __init__(
        self,
        *,
        session_id: str,
        kind: str,
        prompt: str,
        message_count: int,
        marker_hits: dict[str, bool],
        tool_results: list[dict[str, str]],
        ai_texts: list[str],
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        self.session_id = session_id
        self.kind = kind
        self.prompt = prompt
        self.message_count = message_count
        self.marker_hits = marker_hits
        self.tool_results = tool_results
        self.ai_texts = ai_texts
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens

    def tool_names(self) -> list[str]:
        return [item["name"] for item in self.tool_results]

    def tool_content(self, name: str) -> str:
        return "\n".join(item["content"] for item in self.tool_results if item["name"] == name)


class _NudgeRecorder:
    def __init__(self) -> None:
        self.runs: list[_NudgeRun] = []
        self.events: list[tuple[str, str]] = []
        self.memory_done = 0
        self.plan_done = 0
        self.markers: dict[str, str] = {}

    def record(self, *, input: dict, result: Any) -> None:
        messages = list(input.get("messages", []))
        prompt = ""
        if messages:
            last = messages[-1]
            prompt = last.content if isinstance(last.content, str) else str(last.content)
        joined = "\n".join(
            message.content if isinstance(message.content, str) else str(message.content)
            for message in messages
        )
        marker_hits = {name: (text in joined) for name, text in self.markers.items()}
        tool_results: list[dict[str, str]] = []
        ai_texts: list[str] = []
        input_tokens = 0
        output_tokens = 0
        out_messages = result.get("messages", []) if isinstance(result, dict) else []
        for message in out_messages:
            if isinstance(message, ToolMessage):
                tool_results.append(
                    {"name": str(message.name or ""), "content": str(message.content)[:600]}
                )
            elif isinstance(message, AIMessage):
                usage = message.usage_metadata or {}
                input_tokens += int(usage.get("input_tokens", 0) or 0)
                output_tokens += int(usage.get("output_tokens", 0) or 0)
                if isinstance(message.content, str) and message.content.strip():
                    ai_texts.append(message.content[:300])
        self.runs.append(
            _NudgeRun(
                session_id=str(input.get("session_id", "")),
                kind=_classify_prompt(prompt),
                prompt=prompt,
                message_count=len(messages),
                marker_hits=marker_hits,
                tool_results=tool_results,
                ai_texts=ai_texts,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        )


def _classify_prompt(prompt: str) -> str:
    if prompt.startswith("Review the conversation above"):
        return "memory_review"
    if prompt.startswith("You are a knowledge extraction"):
        return "plan_extraction"
    return "other"


class _RecordingAgent:
    """Delegates to the real nudge agent and records its result."""

    def __init__(self, inner: Any, recorder: _NudgeRecorder) -> None:
        self._inner = inner
        self._recorder = recorder

    async def ainvoke(self, input: dict, config: Any = None, **kwargs: Any) -> Any:
        result = await self._inner.ainvoke(input, config, **kwargs)
        self._recorder.record(input=input, result=result)
        return result


class _CountingLLM:
    """Delegates to the real auxiliary model, recording every invocation."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.prompts: list[str] = []

    def invoke(self, prompt: str, config: Any = None) -> Any:
        self.prompts.append(prompt)
        return self._inner.invoke(prompt, config=config)

    async def ainvoke(self, prompt: str, config: Any = None) -> Any:
        self.prompts.append(prompt)
        return await self._inner.ainvoke(prompt, config=config)


class _RequestModelStub:
    model_name = "component-request-stub"


def _model_request(messages: list[AnyMessage], session_id: str) -> ModelRequest:
    return ModelRequest(
        model=_RequestModelStub(),  # type: ignore[arg-type]
        messages=list(messages),
        state={"session_id": session_id, "messages": list(messages)},
    )


# ---------------------------------------------------------------------------
# Sandbox + compression harness
# ---------------------------------------------------------------------------


class _Sandbox:
    """Redirects agent-owned write roots into the pytest tmp dir."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.memory_dir = tmp_path / "memory"
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.plans_dir = tmp_path / "plan_knowledge"
        self.plans_dir.mkdir(parents=True, exist_ok=True)
        self.auto_skills_dir = tmp_path / "skills_auto"
        self.auto_skills_dir.mkdir(parents=True, exist_ok=True)
        self.monkeypatch = monkeypatch
        monkeypatch.setattr(memory_module, "MEMORY_DIR", self.memory_dir)
        monkeypatch.setattr(knowledge_store, "_KNOWLEDGE_ROOT", self.plans_dir)
        monkeypatch.setattr(skill_manage, "AUTO_SKILLS_DIR", self.auto_skills_dir)
        # The post-compression todo reconciliation fork is a separate nudge
        # path with its own contract; disabling it keeps every main-model call
        # here attributable to the cadence / extraction assertions.
        monkeypatch.setitem(SUMMARIZATION, "compression_todo_update_enabled", False)
        agent_core.init()

    def new_session(self, tag: str) -> str:
        sid = _new_sid(tag)
        state_register_mem.set_state(sid, "system_prompt", _TEST_SYSTEM_PROMPT)
        return sid

    def instrument(self, recorder: _NudgeRecorder) -> None:
        monkeypatch = self.monkeypatch
        original_create = nudge_mod._create_nudge_agent

        async def recording_create(system_prompt, allowed_metadata_key=None, tools=None):
            inner = await original_create(
                system_prompt, allowed_metadata_key=allowed_metadata_key, tools=tools
            )
            return _RecordingAgent(inner, recorder)

        monkeypatch.setattr(nudge_mod, "_create_nudge_agent", recording_create)

        original_memory = nudge_mod._nudge_memory
        original_plan = nudge_mod._nudge_plan_extraction

        async def recording_memory(session_id, system_prompt, messages):
            recorder.events.append(("memory", session_id))
            await original_memory(session_id, system_prompt, messages)
            recorder.memory_done += 1

        async def recording_plan(session_id, system_prompt, messages):
            recorder.events.append(("plan", session_id))
            await original_plan(session_id, system_prompt, messages)
            recorder.plan_done += 1

        monkeypatch.setattr(nudge_mod, "_nudge_memory", recording_memory)
        monkeypatch.setattr(nudge_mod, "_nudge_plan_extraction", recording_plan)


class _CompressionHarness:
    """Drives the real Summarization middleware over a synthetic model request."""

    def __init__(self, window: int = _CTX_WINDOW) -> None:
        self.aux = _CountingLLM(build_auxiliary_llm())
        self.middleware = Summarization(
            model=self.aux,
            trigger=[("tokens", 80_000)],
            keep=("messages", 10),
            main_llm_context_window=window,
            need_update_system_prompt=False,
        )

    async def compress(self, session_id: str, messages: list[AnyMessage]) -> list[AnyMessage]:
        """Run one model-call wrap; returns the messages the real LLM would see."""
        captured: dict[str, list[AnyMessage]] = {}

        async def handler(inner: ModelRequest) -> AIMessage:
            captured["messages"] = list(inner.messages)
            return AIMessage(content="ok")

        await self.middleware.awrap_model_call(_model_request(messages, session_id), handler)
        return list(captured.get("messages", []))

    async def tick_cooldown(self, session_id: str, rounds: int) -> None:
        """Count down the post-compression cooldown with fitting (no-op) calls.

        This is the production gate: ``compaction_cooldown_rounds`` successive
        model calls are suppressed before the next proactive compression may
        fire. The tick calls carry tiny messages, so they change nothing else.
        """
        for _ in range(rounds):
            await self.compress(
                session_id, [HumanMessage(content="短消息"), AIMessage(content="ok")]
            )


def _summary_present(messages: list[AnyMessage]) -> bool:
    return any(_SUMMARY_OPEN_TAG in str(message.content) for message in messages)


def _memory_review_problems(
    harness: _CompressionHarness,
    recorder: _NudgeRecorder,
    sid: str,
    final: list[AnyMessage],
    sandbox: _Sandbox,
) -> list[str]:
    problems: list[str] = []
    if not _summary_present(final):
        problems.append("compacted request carries no <summary> pair")
    if not harness.aux.prompts:
        problems.append("the real auxiliary LLM was never invoked for the summary")
    runs = [run for run in recorder.runs if run.session_id == sid and run.kind == "memory_review"]
    if not runs:
        problems.append("the memory-review nudge agent never ran")
        return problems
    run = runs[0]
    if not run.ai_texts and not run.tool_results:
        problems.append("the nudge agent produced neither text nor tool results")
    memory_results = [item for item in run.tool_results if item["name"] == "memory"]
    if not memory_results:
        problems.append(f"the nudge agent never called the memory tool; tools={run.tool_names()}")
        return problems
    if not any('"success": true' in item["content"] for item in memory_results):
        problems.append(f"memory tool returned no success: {memory_results[0]['content'][:200]!r}")
        return problems
    files = [path for path in sandbox.memory_dir.glob("*.md") if path.read_text(encoding="utf-8")]
    if not files:
        problems.append("memory tool reported success but no memory file carries content")
    return problems


# ---------------------------------------------------------------------------
# 1. Single compression → the memory review is dispatched and really runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_compression_dispatches_one_memory_review(sandbox: _Sandbox) -> None:
    recorder = _NudgeRecorder()
    sandbox.instrument(recorder)
    harness = _CompressionHarness()
    reports: list[str] = []

    for attempt in (1, 2):
        sid = sandbox.new_session(f"single-{attempt}")
        try:
            messages = _conversation(_preference_blob(f"single-{attempt}"))
            final = await harness.compress(sid, messages)
            await _settle_nudge_tasks()

            problems = _memory_review_problems(harness, recorder, sid, final, sandbox)
            if not problems:
                runs = [r for r in recorder.runs if r.session_id == sid]
                logger.info(
                    "single-compression memory review ran for {}: tools={} "
                    "input_tokens={} output_tokens={}",
                    sid,
                    runs[0].tool_names(),
                    runs[0].input_tokens,
                    runs[0].output_tokens,
                )
                return
            reports.append(f"attempt {attempt} (sid={sid}): {'; '.join(problems)}")
            logger.warning("single-compression attempt {} not compliant: {}", attempt, problems)
        finally:
            await _cancel_pending_nudges()
            await _purge_session(sid)

    pytest.fail(
        "the live model did not produce a real memory-tool write after 2 attempts:\n"
        + "\n".join(reports)
    )


# ---------------------------------------------------------------------------
# 2. Three consecutive compressions → three dispatches (no counter key)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_three_consecutive_compressions_dispatch_three_reviews(sandbox: _Sandbox) -> None:
    recorder = _NudgeRecorder()
    sandbox.instrument(recorder)
    harness = _CompressionHarness()
    sid = sandbox.new_session("triple")
    try:
        for index in range(3):
            if index > 0:
                await harness.tick_cooldown(sid, 3)
            final = await harness.compress(sid, _conversation(_log_blob(f"triple-{index}")))
            assert _summary_present(final), f"compression {index} produced no summary pair"
            # Let the real in-flight review finish first (case 3 covers the lock
            # itself); this isolates the cadence contract from it.
            await _settle_nudge_tasks()
        assert recorder.memory_done == 3, (
            f"expected one memory review per compression; events={recorder.events}"
        )
        assert [kind for kind, event_sid in recorder.events if event_sid == sid] == ["memory"] * 3
        assert len(harness.aux.prompts) == 3, "each compression must run a real auxiliary summary"

        db_states = state_register_db.get_all_states(sid)
        mem_states = state_register_mem.get_all_states(sid)
        assert "nudge_review_memory_count" not in db_states
        assert "nudge_review_memory_count" not in mem_states
        stale = [key for key in (*db_states, *mem_states) if "nudge" in key and "count" in key]
        assert stale == [], f"a legacy nudge counter survived: {stale}"

        total_in = sum(run.input_tokens for run in recorder.runs if run.session_id == sid)
        total_out = sum(run.output_tokens for run in recorder.runs if run.session_id == sid)
        logger.info(
            "triple compression: reviews={} nudge_input_tokens={} nudge_output_tokens={}",
            recorder.memory_done,
            total_in,
            total_out,
        )
    finally:
        await _cancel_pending_nudges()
        await _purge_session(sid)


# ---------------------------------------------------------------------------
# 3. In-flight lock skips dispatch; release restores the cadence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_lock_skips_dispatch_then_release_dispatches(sandbox: _Sandbox) -> None:
    recorder = _NudgeRecorder()
    sandbox.instrument(recorder)
    harness = _CompressionHarness()
    sid = sandbox.new_session("lock")
    try:
        state_register_mem.set_state(sid, "nudge_review_memory_lock", True)
        held = await harness.compress(sid, _conversation(_log_blob("lock-held")))
        assert _summary_present(held), "compression must still produce a summary while locked"
        assert recorder.memory_done == 0, "a held lock must skip the dispatch"
        assert recorder.events == []
        assert not nudge_mod._COMPRESSION_NUDGE_TASKS, "a held lock must queue nothing"

        state_register_mem.set_state(sid, "nudge_review_memory_lock", False)
        await harness.tick_cooldown(sid, 3)
        released = await harness.compress(sid, _conversation(_log_blob("lock-released")))
        assert _summary_present(released), "second compression produced no summary pair"
        await _settle_nudge_tasks()

        assert recorder.memory_done == 1
        assert recorder.events == [("memory", sid)]
    finally:
        state_register_mem.set_state(sid, "nudge_review_memory_lock", False)
        await _cancel_pending_nudges()
        await _purge_session(sid)


# ---------------------------------------------------------------------------
# Plan-extraction scenario helpers (cases 4 + 5)
# ---------------------------------------------------------------------------

_PLAN_MARKDOWN = """# {plan_name}: jittered exponential backoff for the retry helper

## Goal
Make every LLM retry wait a jittered exponential delay instead of a fixed sleep.

## Steps
1. Add `jittered_backoff(attempt, base=1.5, cap=45.0)` in `pub/func/retry_utils.py`.
2. Wire it into `LLMRetryMiddleware` so every classified retry uses it.
3. Cover cap and jitter bounds with deterministic timing tests.

## What failed first
A fixed `time.sleep(2)` ignored the attempt index and failed the timing assertions.
Root cause: the helper had no attempt parameter, so tests could not observe growth.

## Final approach
`jittered_backoff` multiplies `base * 2 ** (attempt - 1)`, clamps at `cap`, and applies a
bounded random jitter; the middleware passes its retry attempt index through.
"""


class _PlanSetup:
    def __init__(self, sid: str, plan_name: str, plan_file: Path, expected_key: str) -> None:
        self.sid = sid
        self.plan_name = plan_name
        self.plan_file = plan_file
        self.expected_key = expected_key


async def _setup_plan(sandbox: _Sandbox, tag: str) -> _PlanSetup:
    sid = sandbox.new_session(tag)
    plan_name = f"e2e-plan-{tag}-{uuid.uuid4().hex[:6]}"
    plan_file = Path(SESSIONS_DIR) / sid / "plans" / f"{plan_name}.md"
    plan_file.parent.mkdir(parents=True, exist_ok=True)
    plan_file.write_text(_PLAN_MARKDOWN.format(plan_name=plan_name), encoding="utf-8")
    plan_ref = f"{plan_name}.md"
    await replace_all(
        sid,
        [
            {
                "content": "Add jittered_backoff in pub/func/retry_utils.py",
                "status": "completed",
                "plan_ref": plan_ref,
            },
            {
                "content": "Wire jittered_backoff into LLMRetryMiddleware",
                "status": "completed",
                "plan_ref": plan_ref,
            },
            {
                "content": "Add deterministic timing tests for cap and jitter bounds",
                "status": "completed",
                "plan_ref": plan_ref,
            },
        ],
    )
    return _PlanSetup(
        sid=sid, plan_name=plan_name, plan_file=plan_file, expected_key=plan_key(plan_file)
    )


def _plan_output_problems(
    sandbox: _Sandbox, recorder: _NudgeRecorder, setup: _PlanSetup
) -> list[str]:
    problems: list[str] = []
    key_dir = sandbox.plans_dir / setup.expected_key
    meta_path = key_dir / "meta.json"
    if not meta_path.is_file():
        problems.append(f"missing meta.json under plan key dir {key_dir}")
    else:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("key") != setup.expected_key:
            problems.append(f"meta.json key mismatch: {meta.get('key')!r}")
        if meta.get("plan_name") != setup.plan_name:
            problems.append(f"meta.json plan_name mismatch: {meta.get('plan_name')!r}")
    if not (key_dir / "plan-summary.json").is_file():
        problems.append(f"missing plan-summary.json under plan key dir {key_dir}")

    skill_files = sorted(sandbox.auto_skills_dir.rglob("SKILL.md"))
    if not skill_files:
        problems.append(f"no SKILL.md created under {sandbox.auto_skills_dir}")
    else:
        content = skill_files[0].read_text(encoding="utf-8")
        frontmatter = content.split("---")[1] if content.lstrip().startswith("---") else ""
        if "name:" not in frontmatter or "description:" not in frontmatter:
            problems.append(f"created SKILL.md lacks valid frontmatter: {content[:120]!r}")

    plan_runs = [
        run
        for run in recorder.runs
        if run.session_id == setup.sid and run.kind == "plan_extraction"
    ]
    if not plan_runs:
        problems.append("the plan-extraction nudge agent never ran")
        return problems
    tools = {name for run in plan_runs for name in run.tool_names()}
    if "knowledge" not in tools:
        problems.append(f"plan extraction never called the knowledge tool; tools={sorted(tools)}")
    if "skill_manage" not in tools:
        problems.append(f"plan extraction never called skill_manage; tools={sorted(tools)}")
    knowledge_text = "\n".join(run.tool_content("knowledge") for run in plan_runs)
    if "Knowledge written to" not in knowledge_text:
        problems.append(f"knowledge writes failed: {knowledge_text[:300]!r}")
    skill_text = "\n".join(run.tool_content("skill_manage") for run in plan_runs)
    if "created" not in skill_text.lower():
        problems.append(f"skill_manage produced no creation: {skill_text[:300]!r}")
    return problems


# ---------------------------------------------------------------------------
# 4. All-complete todos → memory review + plan extraction in the SAME round
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_complete_todos_dispatch_memory_and_plan_extraction(sandbox: _Sandbox) -> None:
    recorder = _NudgeRecorder()
    sandbox.instrument(recorder)
    harness = _CompressionHarness()
    setup = await _setup_plan(sandbox, "same-round")
    try:
        final = await harness.compress(setup.sid, _plan_conversation(setup.plan_name))
        assert _summary_present(final), "the plan round produced no summary pair"
        await _settle_nudge_tasks()

        assert recorder.memory_done == 1, f"memory review did not complete: {recorder.events}"
        assert recorder.plan_done == 1, f"plan extraction did not complete: {recorder.events}"
        assert [kind for kind, sid in recorder.events if sid == setup.sid] == ["memory", "plan"]
        fired = state_register_db.get_state(setup.sid, "nudge_plan_extraction_fired", False)
        assert fired is True, "the plan-extraction single-fire flag was not set"
    finally:
        await _cancel_pending_nudges()
        await _purge_session(setup.sid)


# ---------------------------------------------------------------------------
# 5. Plan extraction double output: knowledge dir + skill in the auto sandbox
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_extraction_writes_knowledge_and_skill_outputs(sandbox: _Sandbox) -> None:
    recorder = _NudgeRecorder()
    sandbox.instrument(recorder)
    harness = _CompressionHarness()
    reports: list[str] = []

    for attempt in (1, 2):
        setup = await _setup_plan(sandbox, f"output-{attempt}")
        try:
            final = await harness.compress(setup.sid, _plan_conversation(setup.plan_name))
            await _settle_nudge_tasks()
            problems = _plan_output_problems(sandbox, recorder, setup)
            if not problems:
                key_dir = sandbox.plans_dir / setup.expected_key
                skill_files = sorted(sandbox.auto_skills_dir.rglob("SKILL.md"))
                logger.info(
                    "plan extraction outputs: key_dir={} files={} skills={}",
                    key_dir.name,
                    sorted(path.name for path in key_dir.iterdir()),
                    [path.parent.name for path in skill_files],
                )
                assert _summary_present(final)
                return
            reports.append(f"attempt {attempt} (sid={setup.sid}): {'; '.join(problems)}")
            logger.warning("plan-extraction attempt {} missing outputs: {}", attempt, problems)
        finally:
            await _cancel_pending_nudges()
            await _purge_session(setup.sid)

    pytest.fail(
        "the live model did not produce both plan-extraction outputs after 2 attempts:\n"
        + "\n".join(reports)
    )


# ---------------------------------------------------------------------------
# 6. P1-9 evicted human message → compression → same-round review
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p1_9_evicted_human_then_compression_dispatches_review(sandbox: _Sandbox) -> None:
    recorder = _NudgeRecorder()
    sandbox.instrument(recorder)
    harness = _CompressionHarness()
    sid = sandbox.new_session("p19-compress")
    flow_id = f"e2e-flow-p19-{uuid.uuid4().hex[:8]}"
    try:
        await create_flow(
            flow_id,
            {
                "description": "P1-9 compression interleave",
                "steps": [{"task": "keep context", "status": "done"}],
            },
            session_id=sid,
        )
        content = _human_210k()
        human = HumanMessage(content=content, id=f"h-{sid}")
        eviction = ContextEvictionMiddleware()
        update = eviction.before_model({"session_id": sid, "messages": [human]}, None)
        assert update is not None, "the 210K human message was not evicted"
        tagged = update["messages"][0]
        eviction_file = Path(tagged.additional_kwargs[EVICTED_TO_KEY])
        assert eviction_file.read_text(encoding="utf-8") == content

        captured: dict[str, ModelRequest] = {}

        async def handler(inner: ModelRequest) -> AIMessage:
            captured["request"] = inner
            return AIMessage(content="ok")

        await eviction.awrap_model_call(_model_request([tagged], sid), handler)
        preview = str(captured["request"].messages[0].content)
        assert "[evicted to: " in preview, f"model view is not a preview: {preview[:120]!r}"
        assert len(preview) < 5_000, f"evicted preview unexpectedly large: {len(preview)}"

        final = await harness.compress(
            sid, [tagged, AIMessage(content="ack"), HumanMessage(content="继续跟进")]
        )
        await _settle_nudge_tasks()

        assert _summary_present(final), "compression after P1-9 eviction produced no summary"
        assert harness.aux.prompts, "the real auxiliary LLM was never invoked"
        assert flow_id in harness.aux.prompts[-1], (
            "TaskFlow context missing from the summary prompt"
        )
        assert "## Current TaskFlow State" in harness.aux.prompts[-1]
        assert flow_id in _get_taskflow_context_sync(sid), "TaskFlow context lost after compression"
        assert tagged.content == content, "state must keep the full human text"
        assert eviction_file.read_text(encoding="utf-8") == content, "eviction file was damaged"
        assert recorder.memory_done == 1, f"memory review not dispatched: {recorder.events}"
        run = next(run for run in recorder.runs if run.session_id == sid)
        logger.info(
            "P1-9 + compression: preview_chars={} nudge_input_tokens={}",
            len(preview),
            run.input_tokens,
        )
    finally:
        await _cancel_pending_nudges()
        await _purge_session(sid)


# ---------------------------------------------------------------------------
# 7. P0-2 evicted tool result → compression → MesMemory + file intact
# ---------------------------------------------------------------------------


async def _tool_returning(value: Any):
    async def handler(_request: ToolCallRequest) -> Any:
        return value

    return handler


@pytest.mark.asyncio
async def test_p0_2_evicted_tool_result_then_compression_keeps_full_text(sandbox: _Sandbox) -> None:
    recorder = _NudgeRecorder()
    sandbox.instrument(recorder)
    harness = _CompressionHarness()
    sid = sandbox.new_session("p02-compress")
    try:
        full_text = "".join(
            f"burst-{index:04d} " + "Y" * 55 + "\n" for index in range(420)
        )  # ~25.6K chars, 420 lines
        tool_message = ToolMessage(
            content=full_text, name="terminal", tool_call_id="c1", id=f"t-{sid}"
        )
        request = ToolCallRequest(
            tool_call={
                "name": "terminal",
                "args": {"command": "ls"},
                "id": "c1",
                "type": "tool_call",
            },
            tool=None,
            state={"session_id": sid, "messages": []},
            runtime=None,
        )
        eviction = ContextEvictionMiddleware()
        persistence = MessagePersistenceMiddleware()
        tool_handler = await _tool_returning(tool_message)
        result = await eviction.awrap_tool_call(
            request,
            lambda inner: persistence.awrap_tool_call(inner, tool_handler),
        )
        preview = str(result.content)
        assert preview.startswith("[evicted to: "), (
            f"tool result was not evicted: {preview[:120]!r}"
        )
        eviction_file = _evicted_path(preview)
        assert eviction_file.read_text(encoding="utf-8") == full_text

        tool_rows = _rows_with_role(sid, "tool")
        assert any(row["content"] == full_text for row in tool_rows), (
            "MesMemory must hold the RAW tool result before compression"
        )

        messages: list[AnyMessage] = [
            HumanMessage(content=_log_blob("p02-compress")),
            _ai_tool_call("c1"),
            result,
            AIMessage(content="done"),
            HumanMessage(content="继续。"),
        ]
        final = await harness.compress(sid, messages)
        await _settle_nudge_tasks()

        assert _summary_present(final), "compression after P0-2 eviction produced no summary"
        assert recorder.memory_done == 1, f"memory review not dispatched: {recorder.events}"
        assert eviction_file.read_text(encoding="utf-8") == full_text, "eviction file was damaged"
        after_rows = _rows_with_role(sid, "tool")
        assert [row["content"] for row in after_rows] == [full_text], (
            "compression must not rewrite the MesMemory tool row"
        )
    finally:
        await _cancel_pending_nudges()
        await _purge_session(sid)


# ---------------------------------------------------------------------------
# 8. Two sessions compress independently: reviews + TaskFlow contexts isolated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_sessions_compress_isolated_reviews_and_taskflow_context(
    sandbox: _Sandbox,
) -> None:
    recorder = _NudgeRecorder()
    sandbox.instrument(recorder)
    harness = _CompressionHarness()
    sid_a = sandbox.new_session("iso-a")
    sid_b = sandbox.new_session("iso-b")
    marker_a = f"A-MARKER-{uuid.uuid4().hex[:6]}"
    marker_b = f"B-MARKER-{uuid.uuid4().hex[:6]}"
    recorder.markers = {"A": marker_a, "B": marker_b}
    flow_a = f"e2e-flow-a-{uuid.uuid4().hex[:8]}"
    flow_b = f"e2e-flow-b-{uuid.uuid4().hex[:8]}"
    try:
        await create_flow(
            flow_a,
            {
                "description": "session A exclusive flow",
                "steps": [{"task": "A step", "status": "done"}],
            },
            session_id=sid_a,
        )
        await create_flow(
            flow_b,
            {
                "description": "session B exclusive flow",
                "steps": [{"task": "B step", "status": "done"}],
            },
            session_id=sid_b,
        )

        sandbox.monkeypatch.setattr(memory_module, "MEMORY_DIR", sandbox.memory_dir / "session-a")
        final_a = await harness.compress(sid_a, _conversation(_preference_blob(marker_a)))
        assert _summary_present(final_a)
        await _settle_nudge_tasks()

        sandbox.monkeypatch.setattr(memory_module, "MEMORY_DIR", sandbox.memory_dir / "session-b")
        final_b = await harness.compress(sid_b, _conversation(_preference_blob(marker_b)))
        assert _summary_present(final_b)
        await _settle_nudge_tasks()

        assert recorder.memory_done == 2, f"expected one review per session: {recorder.events}"
        run_a = next(run for run in recorder.runs if run.session_id == sid_a)
        run_b = next(run for run in recorder.runs if run.session_id == sid_b)
        assert run_a.marker_hits == {"A": True, "B": False}, run_a.marker_hits
        assert run_b.marker_hits == {"A": False, "B": True}, run_b.marker_hits

        ctx_a = _get_taskflow_context_sync(sid_a)
        ctx_b = _get_taskflow_context_sync(sid_b)
        assert flow_a in ctx_a and flow_b not in ctx_a, f"A context leaked: {ctx_a!r}"
        assert flow_b in ctx_b and flow_a not in ctx_b, f"B context leaked: {ctx_b!r}"

        for directory, foreign in (
            (sandbox.memory_dir / "session-a", marker_b),
            (sandbox.memory_dir / "session-b", marker_a),
        ):
            if directory.exists():
                text = "".join(path.read_text(encoding="utf-8") for path in directory.glob("*.md"))
                assert foreign not in text, f"cross-session memory pollution in {directory}"
    finally:
        await _cancel_pending_nudges()
        await _purge_session(sid_a)
        await _purge_session(sid_b)


# ---------------------------------------------------------------------------
# 9. Insufficient tail clip → real degradation → same-round review
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _capture_logs(level: str = "INFO"):
    lines: list[str] = []
    handler_id = logger.add(lambda message: lines.append(message.record["message"]), level=level)
    try:
        yield lines
    finally:
        logger.remove(handler_id)


def _clip_insufficient_messages() -> list[AnyMessage]:
    """~55K est: the tail clip removes both tool results but cannot recover."""
    return [
        HumanMessage(content="h" * 100_000),
        AIMessage(content="a1"),
        HumanMessage(content="q2"),
        _ai_tool_call("c1", "c2"),
        ToolMessage(content="x" * 60_000, tool_call_id="c1"),
        ToolMessage(content="y" * 60_000, tool_call_id="c2"),
    ]


@pytest.mark.asyncio
async def test_insufficient_tail_clip_degrades_to_compression_and_dispatches_review(
    sandbox: _Sandbox,
) -> None:
    recorder = _NudgeRecorder()
    sandbox.instrument(recorder)
    harness = _CompressionHarness()
    sid = sandbox.new_session("clip-degrade")
    try:
        with _capture_logs(level="DEBUG") as lines:
            final = await harness.compress(sid, _clip_insufficient_messages())
        await _settle_nudge_tasks()

        assert any("Overflow tail clip insufficient" in line for line in lines), (
            "the insufficient tail clip was not attempted"
        )
        assert any("route=compact_only" in line for line in lines), (
            "the degraded route never reached compact_only"
        )
        assert not any("route=tail_clip" in line for line in lines)
        assert harness.aux.prompts, "the degraded route must really invoke the auxiliary LLM"
        assert _summary_present(final), "the degraded compression produced no summary pair"
        assert recorder.memory_done == 1, f"memory review not dispatched: {recorder.events}"
        assert recorder.events == [("memory", sid)]
    finally:
        await _cancel_pending_nudges()
        await _purge_session(sid)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Sandbox:
    return _Sandbox(tmp_path, monkeypatch)
