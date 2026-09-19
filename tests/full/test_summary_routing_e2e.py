"""Live-network e2e: summary routing on the REAL compression pipeline.

LIVE-NETWORK, RUN EXPLICITLY. These tests drive the production
``Summarization`` middleware over real auxiliary-LLM calls
(``build_auxiliary_llm()`` with ``with_structured_output(SummaryDoc,
method="json_mode")``), real SQLite stores (todos.db, state registers), and the
real agent graph (``agent.core.built_agent()``) for the P1-9 eviction cases.
They require a populated ``.env`` with working endpoints. Run with::

    uv run --no-sync pytest -m llm_e2e tests/full/test_summary_routing_e2e.py -v --durations=0

Marker policy: tagged ``llm_e2e``, so a bare ``pytest`` run deselects it via the
``pyproject.toml`` addopts and the hermetic CI gate never collects this file;
``tests/run_tests_split.py`` additionally ``--ignore``s ``tests/full/`` outright,
so the dedicated ``--with-llm-e2e`` job does not pick it up either. The tag is
what keeps the file restricted to explicit invocation.

Coverage (Part 0 = structured_output compression; Part 1 = plan context +
``active_plan_notes``):

1. Plan-active compression — the summary prompt carries the authoritative plan
   block (path + name + open todos), the rendered document has an
   ``## Active Plan Notes`` section carrying the plan lesson, and
   ``latest_user_request`` is the closing user message verbatim.
2. Five chained compressions (cooldown ticked between them) — notes inherited
   from the first document survive all five later compressions verbatim; a new
   lesson appended mid-chain is added while the existing entries stay untouched
   and no duplicate is created.
3. Cap — a chain carrying more than ``active_plan_notes_max_items`` notes is
   stored capped to the tail and the rendered Markdown carries the
   ``(N earlier items omitted for brevity)`` annotation.
4. Completion — once every todo is ``completed``, the next compression drops the
   section and clears the array (a dead plan leaves no residue).
5. P1-9 eviction + compression — a ~210K-char human message is evicted through
   the real graph; the compression then reports the eviction pointer in
   ``evicted_refs[]``, the disk copy stays byte-identical to the original, the
   MesMemory row keeps the full text, and ``latest_user_request`` carries the
   verbatim anchor plus the ``[evicted to: …]`` pointer.
6. Multi-message burst — two oversized user turns (each evicted on its own
   turn) plus a final synthesis question: the summary keeps the last question
   verbatim while both older eviction pointers travel in ``evicted_refs[]``.
7. No active plan — no plan context is injected into the prompt and the
   rendered document has no ``## Active Plan Notes`` section.

Every test purges the session it created (MesMemory rows, checkpoints, session
folder incl. plan files, todo rows, register states) so the module is
independently re-runnable. The post-compression todo fork and the compression
nudges are kept from firing (an in-flight nudge lock is simulated, the todo
fork switch is off) so every main/aux model call is attributable to the
routing assertions above — their own contracts are covered by
``test_nudge_cadence_e2e.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

import pytest
from langchain.agents.middleware import ModelRequest
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from loguru import logger

import agent.core as agent_core
from agent.checkpointer.async_sqlite_checkpointer import delete_thread_history
from agent.middlewares.summarization.core import Summarization
from agent.middlewares.summarization.summary_doc import (
    ACTIVE_PLAN_NOTES_MAX_ITEMS,
    SummaryDoc,
    cap_summary_doc,
    render_summary_markdown,
)
from agent.tools.todolist.registry.store_sqlite import delete_todos_by_session, replace_all
from config import SESSIONS_DIR
from config.features import SUMMARIZATION
from context_engine import delete_messages_by_session
from context_engine.store.core import get_history_by_turn_page
from models import build_auxiliary_llm
from pub.func.message.eviction import EVICTED_TO_KEY
from runtime import clear_all_register_sessions, state_register_db, state_register_mem

pytestmark = [pytest.mark.llm_e2e, pytest.mark.timeout(1200)]

# A small window keeps the auxiliary prompts cheap: usable 4000, compact
# threshold 3200, preserve budget 5000 — the same knobs test_nudge_cadence_e2e
# uses for its real compression harness.
_CTX_WINDOW = 20_000
_BIG_CHARS = 26_000
_EVICTED_PATH_RE = re.compile(r"\[evicted to: (.*?)\]")
_SUMMARY_OPEN_TAG = "<summary>"


# ---------------------------------------------------------------------------
# Session / store helpers
# ---------------------------------------------------------------------------


def _new_sid(tag: str) -> str:
    return f"e2e-sumroute-{tag}-{uuid.uuid4().hex[:8]}"


async def _purge_session(session_id: str) -> None:
    """Delete every trace of a session this module created."""
    with contextlib.suppress(Exception):
        delete_messages_by_session(session_id)
    with contextlib.suppress(Exception):
        await delete_thread_history(session_id)
    with contextlib.suppress(Exception):
        await delete_todos_by_session(session_id)
    with contextlib.suppress(Exception):
        shutil.rmtree(Path(SESSIONS_DIR) / session_id, ignore_errors=True)
    with contextlib.suppress(Exception):
        clear_all_register_sessions(session_id=session_id, clear_persistent_states=True)


@pytest.fixture(autouse=True)
def _no_compression_forks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the post-compression todo fork out of these tests (cost control)."""
    monkeypatch.setitem(SUMMARIZATION, "compression_todo_update_enabled", False)


def _rows_with_role(session_id: str, role: str) -> list[dict]:
    return [
        row
        for row in get_history_by_turn_page(session_id, turn_page_size=1000)
        if row["role"] == role
    ]


# ---------------------------------------------------------------------------
# Plan seeding (real plan file + real todos.db)
# ---------------------------------------------------------------------------


class _PlanSeed:
    def __init__(self, sid: str, plan_name: str, plan_file: Path, todos: list[dict]) -> None:
        self.sid = sid
        self.plan_name = plan_name
        self.plan_file = plan_file
        self.todos = todos


async def _seed_plan(sid: str, tag: str, *, active: bool = True) -> _PlanSeed:
    """Write a real session plan file and real todo rows associated with it."""
    plan_name = f"e2e-plan-{tag}-{uuid.uuid4().hex[:6]}"
    plan_file = Path(SESSIONS_DIR) / sid / "plans" / f"{plan_name}.md"
    plan_file.parent.mkdir(parents=True, exist_ok=True)
    plan_file.write_text(
        f"# {plan_name}\n\n## Goal\nRoute plan lessons into the summary document.\n",
        encoding="utf-8",
    )
    plan_ref = f"{plan_name}.md"
    state_register_db.set_state(sid, "plan_ref", plan_ref)
    todos = [
        {"content": "wire the plan notes array", "status": "completed", "plan_ref": plan_ref},
        {
            "content": "keep the anchor verbatim",
            "status": "completed" if not active else "pending",
            "plan_ref": plan_ref,
        },
        {
            "content": "drop the section when the plan finishes",
            "status": "completed" if not active else "in_progress",
            "plan_ref": plan_ref,
        },
    ]
    await replace_all(sid, todos)
    return _PlanSeed(sid=sid, plan_name=plan_name, plan_file=plan_file, todos=todos)


async def _complete_plan(seed: _PlanSeed) -> None:
    plan_ref = f"{seed.plan_name}.md"
    await replace_all(
        seed.sid,
        [
            {
                "content": todo["content"],
                "status": "completed",
                "plan_ref": plan_ref,
            }
            for todo in seed.todos
        ],
    )


# ---------------------------------------------------------------------------
# Compression harness (real Summarization + real auxiliary LLM)
# ---------------------------------------------------------------------------


class _RequestModelStub:
    model_name = "component-request-stub"


def _model_request(messages: list[AnyMessage], session_id: str) -> ModelRequest:
    return ModelRequest(
        model=_RequestModelStub(),  # type: ignore[arg-type]
        messages=list(messages),
        state={"session_id": session_id, "messages": list(messages)},
    )


class _CompressionHarness:
    """Drives the real middleware over a synthetic model request."""

    def __init__(self, window: int = _CTX_WINDOW) -> None:
        self.middleware = Summarization(
            model=build_auxiliary_llm(),
            trigger=[("tokens", 80_000)],
            keep=("messages", 10),
            main_llm_context_window=window,
            need_update_system_prompt=False,
        )

    async def compress(self, session_id: str, messages: list[AnyMessage]) -> list[AnyMessage]:
        """Run one model-call wrap; returns the messages the real LLM would see.

        The production nudge lock is held for the session: a real in-flight
        nudge suppresses the dispatch (production semantics), which keeps the
        main-LLM cost of this file attributable to the routing assertions. The
        nudge contracts themselves are covered by ``test_nudge_cadence_e2e.py``.
        """
        state_register_mem.set_state(session_id, "nudge_review_memory_lock", True)
        captured: dict[str, list[AnyMessage]] = {}

        async def handler(request: ModelRequest) -> AIMessage:
            captured["messages"] = list(request.messages)
            return AIMessage(content="ok")

        await self.middleware.awrap_model_call(_model_request(messages, session_id), handler)
        return list(captured.get("messages", []))

    def new_turn(self, session_id: str) -> None:
        """Mirror the real turn reset (``before_agent``) without T1 preflight.

        A production turn resets the per-turn compression attempt counter; the
        chain below spans several turns, so each compression starts from a
        fresh turn. T1 preflight itself is covered by the dedicated
        summarization trigger tests.
        """
        self.middleware._reset_turn_state(session_id)

    async def tick_cooldown(self, session_id: str, rounds: int = 3) -> None:
        """Count the post-compression cooldown down with fitting (no-op) calls."""
        for _ in range(rounds):
            await self.compress(
                session_id, [HumanMessage(content="短消息"), AIMessage(content="ok")]
            )


def _summary_present(messages: list[AnyMessage]) -> bool:
    return any(_SUMMARY_OPEN_TAG in str(message.content) for message in messages)


def _doc_from_messages(middleware: Summarization, messages: list[AnyMessage]) -> SummaryDoc | None:
    return middleware._extract_previous_doc(messages)


def _rendered_from_messages(messages: list[AnyMessage]) -> str:
    for message in messages:
        if _SUMMARY_OPEN_TAG in str(message.content):
            return str(message.content)
    return ""


async def _compress_until(
    harness: _CompressionHarness,
    session_id: str,
    messages: list[AnyMessage],
    predicate: Any,
    tag: str,
    attempts: int = 2,
) -> tuple[list[AnyMessage], SummaryDoc]:
    """Compress until the real document satisfies *predicate* (≤2 attempts)."""
    reports: list[str] = []
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            harness.new_turn(session_id)
            await harness.tick_cooldown(session_id)
        final = await harness.compress(session_id, messages)
        doc = _doc_from_messages(harness.middleware, final)
        if doc is not None and predicate(doc):
            return final, doc
        reports.append(
            f"attempt {attempt}: summary_present={_summary_present(final)} "
            f"doc={None if doc is None else doc.model_dump()}"
        )
        logger.warning("{} attempt {}/{} not compliant: {}", tag, attempt, attempts, reports[-1])
    pytest.fail(
        f"[{tag}] real auxiliary compression did not produce the expected SummaryDoc "
        f"after {attempts} attempts:\n" + "\n".join(reports)
    )


async def _acreate_until(
    middleware: Summarization,
    session_id: str,
    messages: list[AnyMessage],
    predicate: Any,
    tag: str,
    attempts: int = 2,
) -> SummaryDoc:
    """Call ``_acreate_summary`` until it returns a compliant SummaryDoc (≤2 tries)."""
    reports: list[str] = []
    for attempt in range(1, attempts + 1):
        try:
            doc = await middleware._acreate_summary(list(messages), session_id=session_id)
        except Exception as exc:
            doc = exc
        if isinstance(doc, SummaryDoc) and predicate(doc):
            return doc
        reports.append(f"attempt {attempt}: {type(doc).__name__} {doc!r:.400}")
        logger.warning("{} attempt {}/{} not compliant: {}", tag, attempt, attempts, reports[-1])
        if attempt < attempts:
            await asyncio.sleep(3)
    pytest.fail(
        f"[{tag}] real auxiliary compression did not produce the expected SummaryDoc "
        f"after {attempts} attempts:\n" + "\n".join(reports)
    )


def _capture_structured_prompts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Spy on the real prompt builder; returns the captured prompts (observe only)."""
    prompts: list[str] = []
    original = Summarization._build_structured_summary_prompt

    def spy(self, messages_text, previous_doc, previous_summary, session_id=""):
        prompt = original(self, messages_text, previous_doc, previous_summary, session_id)
        prompts.append(prompt)
        return prompt

    monkeypatch.setattr(Summarization, "_build_structured_summary_prompt", spy)
    return prompts


# ---------------------------------------------------------------------------
# Conversation builders
# ---------------------------------------------------------------------------


def _log_blob(tag: str, total_chars: int = _BIG_CHARS) -> str:
    line = f"2026-09-19 INFO [{tag}] worker batch event: item processed and recorded\n"
    return line * max(total_chars // len(line), 1)


def _lesson_plan_conversation(lesson: str, anchor: str) -> list[AnyMessage]:
    """A turn whose summarized message carries both the anchor and the lesson.

    Both must sit in the *summarized* slice (first 2000 chars of the blob):
    the auxiliary model never sees the preserved tail, and only this slice is
    serialized into ``<conversation>``.
    """
    head = f"最新请求 {anchor}\n计划执行教训（请原文保留到计划结束）：{lesson}\n" + _log_blob(
        "plan-lesson"
    )
    return [
        HumanMessage(content=head),
        AIMessage(content="收到，已记录这条计划教训。"),
        HumanMessage(content="继续。"),
    ]


# ===========================================================================
# 1. Plan-active compression: plan context + notes + verbatim anchor
# ===========================================================================


@pytest.mark.asyncio
async def test_plan_active_compression_keeps_lesson_and_anchor(monkeypatch) -> None:
    harness = _CompressionHarness()
    sid = _new_sid("plan-active")
    prompts = _capture_structured_prompts(monkeypatch)
    seed = await _seed_plan(sid, "active")
    lesson = "PLAN-LESSON-1 修改 widget 前先运行 test_widget_17，否则回归不暴露"
    anchor = "ANCHOR-1 请继续执行计划，并把这条教训保留到计划结束。"
    try:
        final, doc = await _compress_until(
            harness,
            sid,
            _lesson_plan_conversation(lesson, anchor),
            lambda d: (
                any("PLAN-LESSON-1" in note for note in d.active_plan_notes)
                and anchor in d.latest_user_request
            ),
            tag="plan-active",
        )
        assert _summary_present(final)

        # (1) the plan context really reached the compression prompt.
        assert prompts, "the real prompt builder was never invoked"
        prompt = prompts[0]
        assert "## Active Plan (authoritative)" in prompt
        assert f"- Plan: {seed.plan_name}" in prompt
        assert f"workspace/sessions/{sid}/plans/{seed.plan_name}.md" in prompt
        assert "keep the anchor verbatim" in prompt
        assert "- Todo progress: 1/3 done" in prompt

        # (2) the lesson landed in the typed array and the rendered section.
        assert doc.active_plan_notes, "active_plan_notes must not be empty"
        assert any("PLAN-LESSON-1" in note for note in doc.active_plan_notes)
        rendered = render_summary_markdown(doc)
        assert "## Active Plan Notes" in rendered
        assert "PLAN-LESSON-1" in rendered

        # (3) the conversation anchor is verbatim (no truncation/paraphrase).
        assert anchor in doc.latest_user_request
        assert anchor in rendered

        # (4) Part 0 carrier: the rendered chain carries the typed document.
        assert doc == _doc_from_messages(harness.middleware, final)
        payload = final[1].additional_kwargs.get("summary_doc")
        assert isinstance(payload, dict)
        assert payload.get("active_plan_notes") == cap_summary_doc(doc).active_plan_notes
        logger.info(
            "plan-active notes={} latest={!r}",
            doc.active_plan_notes,
            doc.latest_user_request,
        )
    finally:
        await _purge_session(sid)


# ===========================================================================
# 2. Five chained compressions: verbatim carry-forward + append
# ===========================================================================


@pytest.mark.asyncio
async def test_five_chained_compressions_preserve_notes_verbatim() -> None:
    harness = _CompressionHarness()
    sid = _new_sid("chain5")
    await _seed_plan(sid, "chain")
    lesson1 = "PLAN-LESSON-1 修改 widget 前先运行 test_widget_17，否则回归不暴露"
    lesson2 = "PLAN-LESSON-2 先跑 ruff 再跑基于测试，能提前发现导入错误"
    try:
        current, doc1 = await _compress_until(
            harness,
            sid,
            _lesson_plan_conversation(lesson1, "请继续。CHAIN-ANCHOR-1"),
            lambda d: any("PLAN-LESSON-1" in note for note in d.active_plan_notes),
            tag="chain-first",
        )
        initial_notes = list(doc1.active_plan_notes)
        assert initial_notes
        assert _summary_present(current)

        seen = [initial_notes]
        for step in range(2, 7):
            harness.new_turn(sid)
            await harness.tick_cooldown(sid)
            lesson = lesson2 if step == 4 else "继续按计划推进。"
            marker = "PLAN-LESSON-2" if step == 4 else ""
            turn = [
                HumanMessage(content=f"{lesson}\n" + _log_blob(f"chain-step-{step}")),
                AIMessage(content="继续执行中。"),
                HumanMessage(content=f"继续第 {step} 步。CHAIN-ANCHOR-{step}"),
            ]
            current, doc = await _compress_until(
                harness,
                sid,
                [*current, *turn],
                lambda d, m=marker: (
                    all(note in d.active_plan_notes for note in initial_notes)
                    and (not m or any(m in note for note in d.active_plan_notes))
                ),
                tag=f"chain-step-{step}",
                attempts=2,
            )
            # FIFO never touches this array: carried entries keep their order.
            assert doc.active_plan_notes[: len(initial_notes)] == initial_notes, (
                f"step {step} lost/reordered carried notes: {doc.active_plan_notes}"
            )
            seen.append(list(doc.active_plan_notes))

        assert any("PLAN-LESSON-2" in note for note in seen[-1]), (
            f"the new mid-chain lesson never landed: {seen[-1]}"
        )
        assert len(seen[-1]) > len(initial_notes), (
            f"the new lesson replaced instead of appending: {seen[-1]}"
        )

        # The chain stores the typed document and renders each note exactly once.
        stored = _doc_from_messages(harness.middleware, current)
        assert stored is not None
        assert stored.active_plan_notes == seen[-1]
        rendered = render_summary_markdown(stored)
        for note in initial_notes:
            assert rendered.count(f"- {note}") == 1
        assert rendered.count("## Active Plan Notes") == 1
        logger.info("chain5 notes={}", stored.active_plan_notes)
    finally:
        await _purge_session(sid)


# ===========================================================================
# 3. Cap: >max notes → tail kept, annotation rendered
# ===========================================================================


@pytest.mark.asyncio
async def test_notes_cap_evicts_oldest_with_annotation() -> None:
    middleware = Summarization(
        model=build_auxiliary_llm(),
        trigger=[("tokens", 80_000)],
        keep=("messages", 10),
        main_llm_context_window=_CTX_WINDOW,
        need_update_system_prompt=False,
    )
    sid = _new_sid("cap")
    await _seed_plan(sid, "cap")
    carried = [f"CAP-NOTE-{index:02d} 计划教训条目（构造）" for index in range(21)]
    prior = SummaryDoc(
        latest_user_request="继续执行。CAP-ANCHOR",
        goal="exercise the notes cap",
        active_plan_notes=carried,
    )
    # Seed the chain carrier directly (pre-cap document shape) so the carried
    # count really exceeds the cap before this compression runs.
    prior_pair: list[AnyMessage] = [
        HumanMessage(
            content="What did we do so far?",
            additional_kwargs={"lc_source": "summarization"},
        ),
        AIMessage(
            content=f"<summary>\n{render_summary_markdown(prior)}\n</summary>",
            additional_kwargs={"lc_source": "summarization", "summary_doc": prior.model_dump()},
        ),
    ]
    messages = [
        *prior_pair,
        HumanMessage(content="继续执行计划。\n" + _log_blob("cap")),
        AIMessage(content="继续执行中。"),
        HumanMessage(content="继续。CAP-ANCHOR-2"),
    ]
    try:
        doc = await _acreate_until(
            middleware,
            sid,
            messages,
            lambda d: len(d.active_plan_notes) > ACTIVE_PLAN_NOTES_MAX_ITEMS,
            tag="cap",
        )
        assert doc.active_plan_notes[: len(carried)] == carried, (
            "carried entries must survive the compression verbatim"
        )
        merged = list(doc.active_plan_notes)
        omitted = len(merged) - ACTIVE_PLAN_NOTES_MAX_ITEMS
        assert omitted >= 1

        capped_pair = middleware._build_new_messages(doc)
        stored = middleware._extract_previous_doc(capped_pair)
        assert stored is not None
        assert len(stored.active_plan_notes) == ACTIVE_PLAN_NOTES_MAX_ITEMS
        assert stored.active_plan_notes == merged[omitted:]
        rendered = capped_pair[1].content
        assert f"({omitted} earlier items omitted for brevity)" in rendered
        assert carried[0] not in rendered
        assert merged[-1] in rendered
        logger.info(
            "cap: merged={} stored={} omitted={}",
            len(merged),
            len(stored.active_plan_notes),
            omitted,
        )
    finally:
        await _purge_session(sid)


# ===========================================================================
# 4. Completion: all todos done → section dropped, array cleared
# ===========================================================================


@pytest.mark.asyncio
async def test_completed_plan_clears_notes_and_section() -> None:
    harness = _CompressionHarness()
    sid = _new_sid("done")
    seed = await _seed_plan(sid, "done")
    lesson = "PLAN-LESSON-DONE 计划完成前的教训：先备份再迁移"
    try:
        current, doc1 = await _compress_until(
            harness,
            sid,
            _lesson_plan_conversation(lesson, "请继续。DONE-ANCHOR-1"),
            lambda d: bool(d.active_plan_notes),
            tag="done-first",
        )
        assert doc1.active_plan_notes
        assert "## Active Plan Notes" in render_summary_markdown(doc1)

        # The plan's todos all became completed: the resolver deactivates it.
        await _complete_plan(seed)
        harness.new_turn(sid)
        await harness.tick_cooldown(sid)
        # The big message sits before the closing turn so it lands in the
        # summarized slice (the last human message is always preserved).
        follow_up: list[AnyMessage] = [
            HumanMessage(content="计划已完成。\n" + _log_blob("done-follow-up")),
            AIMessage(content="收到。"),
            HumanMessage(content="继续后续工作。DONE-ANCHOR-2"),
        ]
        current, doc2 = await _compress_until(
            harness,
            sid,
            [*current, *follow_up],
            lambda d: d.active_plan_notes == [],
            tag="done-second",
        )
        assert doc2.active_plan_notes == []
        rendered = _rendered_from_messages(current)
        assert "## Active Plan Notes" not in rendered
        stored = _doc_from_messages(harness.middleware, current)
        assert stored is not None and stored.active_plan_notes == []
        logger.info("completed plan: notes cleared")
    finally:
        await _purge_session(sid)


# ===========================================================================
# 5. P1-9 eviction + compression: three-way consistency
# ===========================================================================


def _evicted_path(preview: str) -> Path:
    match = _EVICTED_PATH_RE.search(preview)
    assert match is not None, f"preview carries no eviction path: {preview[:200]!r}"
    return Path(match.group(1))


def _pre_response_slice(messages: list[AnyMessage]) -> list[AnyMessage]:
    """The message list as it stood at the model call of the current turn.

    Compression runs inside ``wrap_model_call``, so the newest user request has
    no reply yet; a post-turn transcript would mislead the auxiliary model into
    treating the request as already resolved. Internal injections (TaskIntent
    steering, completion carriers) are skipped when locating the request.
    """
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, HumanMessage):
            continue
        kwargs = getattr(message, "additional_kwargs", {}) or {}
        metadata = getattr(message, "metadata", {}) or {}
        if kwargs.get("internal") or metadata.get("internal"):
            continue
        return list(messages[: index + 1])
    raise AssertionError("no user HumanMessage in the message list")


def _oversized_payload(anchor: str, tag: str, total_chars: int = 210_000) -> str:
    """A >200K-char human message whose request anchor sits in the first 2000 chars."""
    head = (
        f"E2E 请求锚点（请原文引用）：{anchor}。"
        "请基于下面粘贴的日志做分析，最后只需回复“received”。\n"
    )
    body: list[str] = []
    size = 0
    index = 0
    while size < total_chars - len(head) - 64:
        line = (
            f"2026-09-19T08:{index % 60:02d} INFO [{tag}] worker-{index % 5} "
            f"step {index}: processed item {index}\n"
        )
        body.append(line)
        size += len(line)
        index += 1
    return head + "".join(body) + "（日志结束）"


async def _real_graph() -> Any:
    agent_core.init()
    return await agent_core.built_agent(temperature=0.0)


async def _invoke_with_retry(graph: Any, payload: dict, config: dict, attempts: int = 2) -> dict:
    for attempt in range(1, attempts + 1):
        try:
            return await graph.ainvoke(payload, config)
        except Exception as exc:
            if attempt >= attempts:
                raise
            logger.warning("graph.ainvoke attempt {} failed ({}); retrying once", attempt, exc)
            await asyncio.sleep(5)
    raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_p1_9_eviction_then_compression_three_way_consistency() -> None:
    from pub.func.build_agent_config import build_agent_config

    sid = _new_sid("evict")
    anchor = "EVICT-ANCHOR-9137"
    content = _oversized_payload(anchor, "evict")
    middleware = Summarization(
        model=build_auxiliary_llm(),
        trigger=[("tokens", 80_000)],
        keep=("messages", 10),
        main_llm_context_window=_CTX_WINDOW,
        need_update_system_prompt=False,
    )
    try:
        graph = await _real_graph()
        out = await _invoke_with_retry(
            graph,
            {"messages": [HumanMessage(content=content, id=f"h-{sid}")], "session_id": sid},
            build_agent_config(sid),
        )

        # (1) disk: exactly one human eviction file, byte-identical to the input.
        evicted_dir = Path(SESSIONS_DIR) / sid / "evicted"
        files = sorted(evicted_dir.glob("human-*.md"))
        assert len(files) == 1, f"expected exactly one human eviction file, got {files}"
        assert files[0].read_text(encoding="utf-8") == content

        # (2) MesMemory: the full text on the archived row.
        human_rows = _rows_with_role(sid, "human")
        assert any(row["content"] == content for row in human_rows)

        # (3) state: the tagged human keeps the full text (only the view is cut).
        tagged = [
            message
            for message in out["messages"]
            if isinstance(message, HumanMessage) and message.additional_kwargs.get(EVICTED_TO_KEY)
        ]
        assert len(tagged) == 1
        assert tagged[0].content == content

        # (4) compression: pointer in the typed array, anchor + pointer verbatim.
        doc = await _acreate_until(
            middleware,
            sid,
            _pre_response_slice(out["messages"]),
            lambda d: str(files[0]) in d.evicted_refs and anchor in d.latest_user_request,
            tag="evict",
        )
        assert str(files[0]) in doc.evicted_refs
        assert anchor in doc.latest_user_request
        assert f"[evicted to: {files[0]}]" in doc.latest_user_request
        rendered = render_summary_markdown(doc)
        assert "## Evicted References" in rendered
        assert str(files[0]) in rendered
        assert anchor in rendered
        logger.info(
            "eviction + compression: refs={} latest={!r}",
            doc.evicted_refs,
            doc.latest_user_request,
        )
    finally:
        await _purge_session(sid)


# ===========================================================================
# 6. Multi-message burst: last request verbatim + older eviction pointers
# ===========================================================================


@pytest.mark.asyncio
async def test_multi_message_burst_keeps_last_request_and_evicted_refs() -> None:
    from pub.func.build_agent_config import build_agent_config

    sid = _new_sid("burst")
    content_a = _oversized_payload("BURST-ANCHOR-A-2201", "burst-a")
    content_b = _oversized_payload("BURST-ANCHOR-B-4719", "burst-b")
    synthesis = "综合提问：请把前面两条日志的结论合并成一句话。BURST-ANCHOR-C"
    middleware = Summarization(
        model=build_auxiliary_llm(),
        trigger=[("tokens", 80_000)],
        keep=("messages", 10),
        main_llm_context_window=_CTX_WINDOW,
        need_update_system_prompt=False,
    )
    try:
        graph = await _real_graph()
        for index, text in enumerate((content_a, content_b, synthesis), start=1):
            out = await _invoke_with_retry(
                graph,
                {
                    "messages": [HumanMessage(content=text, id=f"h-{sid}-{index}")],
                    "session_id": sid,
                },
                build_agent_config(sid),
            )

        evicted_dir = Path(SESSIONS_DIR) / sid / "evicted"
        files = sorted(evicted_dir.glob("human-*.md"))
        assert len(files) == 2, f"expected two human eviction files, got {files}"
        on_disk = {path.read_text(encoding="utf-8") for path in files}
        assert on_disk == {content_a, content_b}, "each eviction file must be byte-identical"
        human_rows = _rows_with_role(sid, "human")
        for text in (content_a, content_b, synthesis):
            assert any(row["content"] == text for row in human_rows)

        doc = await _acreate_until(
            middleware,
            sid,
            _pre_response_slice(out["messages"]),
            lambda d: (
                all(str(path) in d.evicted_refs for path in files)
                and d.latest_user_request.strip() == synthesis.strip()
            ),
            tag="burst",
        )
        assert all(str(path) in doc.evicted_refs for path in files)
        assert doc.latest_user_request.strip() == synthesis
        assert "[evicted to:" not in doc.latest_user_request  # the last request is live
        rendered = render_summary_markdown(doc)
        assert "## Latest Unresolved User Request" in rendered
        assert synthesis in rendered
        assert "## Evicted References" in rendered
        for path in files:
            assert str(path) in rendered
        logger.info("burst: refs={} latest={!r}", doc.evicted_refs, doc.latest_user_request)
    finally:
        await _purge_session(sid)


# ===========================================================================
# 7. No active plan: nothing injected, no notes section
# ===========================================================================


@pytest.mark.asyncio
async def test_no_active_plan_injects_nothing(monkeypatch) -> None:
    harness = _CompressionHarness()
    sid = _new_sid("no-plan")
    prompts = _capture_structured_prompts(monkeypatch)
    try:
        # No plan_ref state, no todos with a plan_ref -> the resolver returns None.
        final, doc = await _compress_until(
            harness,
            sid,
            _lesson_plan_conversation("PLAN-LESSON-NO 无计划时的噪音", "请继续。NO-PLAN-ANCHOR"),
            lambda d: d.active_plan_notes == [],
            tag="no-plan",
        )
        assert prompts
        assert "## Active Plan (authoritative)" not in prompts[0]
        assert doc.active_plan_notes == []
        rendered = _rendered_from_messages(final)
        assert "## Active Plan Notes" not in rendered
        assert _summary_present(final)
        logger.info("no active plan: no block injected, no notes section")
    finally:
        await _purge_session(sid)
