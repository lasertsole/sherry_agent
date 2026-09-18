"""Live-network e2e: context governance on the REAL agent graph and components.

LIVE-NETWORK, RUN EXPLICITLY. These tests drive the production graph built by
``agent.core.built_agent()`` against the real configured LLMs (main + auxiliary)
and the real components (MesMemory SQLite store, SQLite checkpointer, session
dirs). They write real session data and require a populated ``.env`` with
working endpoints. Run with::

    uv run --no-sync pytest tests/full/test_context_governance_e2e.py -v

Marker decision: deliberately NOT tagged ``llm_e2e``. ``tests/full/`` is
excluded from ``tests/run_tests_split.py`` by construction, so the hermetic CI
gate never collects this file; tagging it ``llm_e2e`` would instead pull it into
the dedicated ``--with-llm-e2e`` CI job, which must not depend on live network
credentials nor write real session data. The file is exercised only by the
explicit command above.

Coverage (features that previously had no e2e coverage):

1. P1-9 human-message eviction — a ~210K-char human message through the real
   graph: eviction file byte-identical to the original, state keeps the FULL
   text plus ``lc_evicted_to``, MesMemory row equals the full text, the real
   model replies from the preview, and the reported input tokens prove the full
   payload never reached the model.
2. P0-2 tool-result eviction — ``python_repl`` prints ~24K chars over 400
   lines; state holds the ``[evicted to: …]`` preview, the eviction file equals
   the MesMemory row, byte for byte.
3. P2-4 ``read_file`` slicing — a 3000-line file is read through the real tool;
   state holds the 4000-char slice + recovery notice, MesMemory holds the full
   JSON result, and no eviction file is written (the source file is on disk).
4. P1-2 overflow tail clip (component level, REAL auxiliary LLM) — a sufficient
   clip recovers with zero LLM invocations; an insufficient clip degrades to
   the real compact route and the auxiliary LLM IS invoked.
5. Chained-summary filtering — ``_create_summary`` with a real auxiliary model:
   the previous summary pair leaves ``<conversation>`` and appears in
   ``<prior-summary>`` instead.

Every test purges the session it created (MesMemory rows, checkpoints, session
folder, registers) and removes any file it wrote, so the module is independently
re-runnable.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from langchain.agents.middleware import ModelRequest
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from loguru import logger

import agent.core as agent_core
from agent.checkpointer.async_sqlite_checkpointer import delete_thread_history
from agent.middlewares.summarization.core import Summarization
from config import SESSIONS_DIR, TEMP_DIR
from context_engine import delete_messages_by_session
from context_engine.store.core import get_history_by_turn_page
from models import build_auxiliary_llm
from pub.func.build_agent_config import build_agent_config
from pub.func.message.eviction import EVICTED_TO_KEY, _READ_FILE_SLICE_NOTICE
from pub.func.message.overflow_clip import CLIP_MARKER
from runtime import clear_all_register_sessions, state_register_mem

pytestmark = [pytest.mark.unit, pytest.mark.timeout(900)]

_CTX_WINDOW = 41_600  # 41600 − 16000 reserve = usable 25600 (compact @ 20480)
_EVICTED_PATH_RE = re.compile(r"\[evicted to: (.*?)\]")
_TOOL_PAYLOAD_CODE = 'for i in range(400):\n    print("line-%04d " % i + "X" * 60)'
_READ_FILE_LINES = 3_000


# ---------------------------------------------------------------------------
# Session / store helpers
# ---------------------------------------------------------------------------


def _new_sid(tag: str) -> str:
    return f"e2e-ctxgov-{tag}-{uuid.uuid4().hex[:8]}"


async def _purge_session(session_id: str) -> None:
    """Delete every trace of a session this module created.

    Mirrors ``server/DAO/messages.py::clear_session`` minus the session-end
    continuity save (a test session has nothing worth carrying forward).
    """
    with contextlib.suppress(Exception):
        delete_messages_by_session(session_id)
    with contextlib.suppress(Exception):
        await delete_thread_history(session_id)
    with contextlib.suppress(Exception):
        shutil.rmtree(Path(SESSIONS_DIR) / session_id, ignore_errors=True)
    with contextlib.suppress(Exception):
        clear_all_register_sessions(session_id=session_id, clear_persistent_states=True)


def _rows(session_id: str) -> list[dict]:
    return get_history_by_turn_page(session_id, turn_page_size=1000)


def _rows_with_role(session_id: str, role: str) -> list[dict]:
    return [row for row in _rows(session_id) if row["role"] == role]


def _evicted_path(preview: str) -> Path:
    match = _EVICTED_PATH_RE.search(preview)
    assert match is not None, f"preview carries no eviction path: {preview[:200]!r}"
    return Path(match.group(1))


# ---------------------------------------------------------------------------
# Real-graph helpers
# ---------------------------------------------------------------------------


async def _real_graph(temperature: float = 0.0) -> Any:
    agent_core.init()
    return await agent_core.built_agent(temperature=temperature)


async def _invoke_with_retry(graph: Any, payload: dict, config: dict, attempts: int = 2) -> dict:
    """One network-error retry, per the e2e discipline (no silent skips)."""
    for attempt in range(1, attempts + 1):
        try:
            return await graph.ainvoke(payload, config)
        except Exception as exc:
            if attempt >= attempts:
                raise
            logger.warning("graph.ainvoke attempt {} failed ({}); retrying once", attempt, exc)
            await asyncio.sleep(5)
    raise AssertionError("unreachable")


def _assistant_text(messages: list[AnyMessage]) -> str:
    parts: list[str] = []
    for message in messages:
        if isinstance(message, AIMessage) and isinstance(message.content, str):
            parts.append(message.content)
    return "\n".join(parts).strip()


def _last_input_tokens(messages: list[AnyMessage]) -> int | None:
    for message in reversed(messages):
        if isinstance(message, AIMessage) and message.usage_metadata:
            value = message.usage_metadata.get("input_tokens")
            if value:
                return int(value)
    return None


async def _run_until_tool(
    graph: Any,
    *,
    prompt: str,
    tool_name: str,
    tag: str,
    is_success: Callable[[str, list[ToolMessage]], bool],
    attempts: int = 3,
) -> tuple[str, dict]:
    """Drive real turns until ``tool_name`` produces an accepted result.

    The live model is non-deterministic; each non-complying attempt is reported
    and retried on a fresh session. Final failure states exactly what the model
    did after the last attempt — no fake success, no skip.
    """
    last_note = "model never called the tool"
    for attempt in range(1, attempts + 1):
        sid = _new_sid(f"{tag}-{attempt}")
        out = await _invoke_with_retry(
            graph,
            {"messages": [HumanMessage(content=prompt, id=f"h-{sid}")], "session_id": sid},
            build_agent_config(sid),
        )
        tool_messages = [
            m for m in out["messages"] if isinstance(m, ToolMessage) and m.name == tool_name
        ]
        if tool_messages and is_success(sid, tool_messages):
            return sid, out
        last_note = _describe_attempt(out, tool_name, tool_messages)
        logger.warning("{} attempt {}/{}: {}", tag, attempt, attempts, last_note)
        await _purge_session(sid)
    pytest.fail(
        f"[{tag}] live model did not produce the expected {tool_name} result "
        f"after {attempts} attempts: {last_note}"
    )


def _describe_attempt(out: dict, tool_name: str, tool_messages: list[ToolMessage]) -> str:
    names = [f"{type(m).__name__}:{getattr(m, 'name', None)}" for m in out.get("messages", [])]
    last_ai = next((m for m in reversed(out.get("messages", [])) if isinstance(m, AIMessage)), None)
    content = (last_ai.content if last_ai and isinstance(last_ai.content, str) else "")[:300]
    observed = [
        f"name={m.name} len={len(str(m.content))} head={str(m.content)[:80]!r}"
        for m in tool_messages
    ]
    return f"messages={names} last_ai={content!r} observed_{tool_name}={observed}"


# ---------------------------------------------------------------------------
# 1. P1-9 human-message eviction across the real graph
# ---------------------------------------------------------------------------


def _human_payload(total_chars: int = 210_000) -> str:
    """Multiline log sample > the 200K threshold, instruction in the last line.

    The final instruction sits inside the tail-5-lines of the head/tail preview,
    so the model sees it even though only the preview reaches the request.
    """
    footer = "只需回复：received"
    body: list[str] = []
    size = 0
    index = 0
    while size < total_chars - len(footer) - 1:
        text = (
            f"2026-09-18T22:57:{index % 60:02d} INFO worker-{index % 7} "
            f"step {index}: processed item {index}\n"
        )
        body.append(text)
        size += len(text)
        index += 1
    return "".join(body) + footer


@pytest.mark.asyncio
async def test_p1_9_human_eviction_real_graph_three_state_split() -> None:
    """A ~210K-char human message: disk == state == MesMemory; model sees preview."""
    sid = _new_sid("p19")
    content = _human_payload()
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

        # (2) state: FULL text + the lc_evicted_to tag (only the model view is cut).
        #     A long user turn also triggers TaskIntent's system-directive
        #     HumanMessage (production behavior), so identify ours by its tag.
        humans = [m for m in out["messages"] if isinstance(m, HumanMessage)]
        tagged = [m for m in humans if m.additional_kwargs.get(EVICTED_TO_KEY)]
        assert len(tagged) == 1, (
            f"expected exactly one tagged (evicted) human, got "
            f"{[(m.id, len(str(m.content))) for m in tagged]}"
        )
        human = tagged[0]
        assert human.content == content, "state must keep the full human text (P1-9 split)"
        assert human.additional_kwargs.get(EVICTED_TO_KEY) == str(files[0])

        # (3) MesMemory: the full text on the archived row (the tag never filters).
        human_rows = _rows_with_role(sid, "human")
        assert any(row["content"] == content for row in human_rows), (
            "MesMemory must archive the full human text; rows="
            f"{[(len(str(row['content']))) for row in human_rows]}"
        )

        # (4) the real model answered from the preview.
        answer = _assistant_text(out["messages"])
        assert answer, "live main LLM returned an empty reply"
        if "received" not in answer.lower():
            logger.warning("P1-9: model did not follow the 'received' instruction: {!r}", answer)

        # (5) token footprint: the full payload (~len/4 tokens) never reached the model.
        input_tokens = _last_input_tokens(out["messages"])
        full_text_tokens = len(content) // 4
        logger.info(
            "P1-9 input_tokens={} full_text_estimate={} preview_chars={}",
            input_tokens,
            full_text_tokens,
            files[0].stat().st_size,
        )
        if input_tokens is not None:
            assert input_tokens < full_text_tokens, (
                f"reported input_tokens={input_tokens} suggests the full "
                f"{len(content)}-char message reached the model "
                f"(local estimate {full_text_tokens} tokens)"
            )
    finally:
        await _purge_session(sid)


# ---------------------------------------------------------------------------
# 2. P0-2 tool-result eviction across the real graph
# ---------------------------------------------------------------------------


def _python_repl_prompt() -> str:
    return (
        "Call the python_repl tool exactly once with this exact code, unchanged:\n\n"
        f"{_TOOL_PAYLOAD_CODE}\n\n"
        "Do not call any other tool. After the tool result comes back, "
        "reply with exactly: DONE"
    )


def _python_repl_success(_sid: str, messages: list[ToolMessage]) -> bool:
    return str(messages[-1].content).startswith("[evicted to: ")


@pytest.mark.asyncio
async def test_p0_2_tool_result_eviction_real_graph() -> None:
    """python_repl prints ~24K chars in 400 lines: preview in state, full on disk."""
    graph = await _real_graph()
    sid, out = await _run_until_tool(
        graph,
        prompt=_python_repl_prompt(),
        tool_name="python_repl",
        tag="p02",
        is_success=_python_repl_success,
    )
    try:
        tool_messages = [
            m for m in out["messages"] if isinstance(m, ToolMessage) and m.name == "python_repl"
        ]
        evicted_message = next(
            m for m in reversed(tool_messages) if str(m.content).startswith("[evicted to: ")
        )
        preview = str(evicted_message.content)
        path = _evicted_path(preview)
        full_text = path.read_text(encoding="utf-8")

        # state holds the preview only — the raw 24K payload never entered it.
        assert len(preview) < 5_000, f"preview unexpectedly large: {len(preview)} chars"
        assert "X" * 60 in full_text
        assert full_text.count("line-") == 400

        # disk == MesMemory, byte for byte.
        tool_rows = _rows_with_role(sid, "tool")
        matching = [row for row in tool_rows if row["content"] == full_text]
        assert matching, (
            "MesMemory must hold the RAW tool result; "
            f"rows={[(len(r['content']), r['content'][:60]) for r in tool_rows]}"
        )
    finally:
        await _purge_session(sid)


# ---------------------------------------------------------------------------
# 3. P2-4 read_file slicing across the real graph
# ---------------------------------------------------------------------------


def _read_file_prompt(relative_path: str) -> str:
    return (
        f"Use the read_file tool to read the file {relative_path} with its default "
        "arguments (do not pass offset/limit). Do not call any other tool. "
        "After the tool result comes back, reply with exactly: DONE"
    )


def _read_file_success(_sid: str, messages: list[ToolMessage]) -> bool:
    return _READ_FILE_SLICE_NOTICE in str(messages[-1].content)


@pytest.mark.asyncio
async def test_p2_4_read_file_slice_real_graph() -> None:
    """read_file slices in place: no eviction file, full JSON result in MesMemory."""
    sid = _new_sid("p24")
    file_name = f"e2e_readfile_{uuid.uuid4().hex[:8]}.txt"
    file_path = Path(TEMP_DIR) / file_name
    file_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"row {i:05d}: " + ("abcdefghij" * 4) for i in range(_READ_FILE_LINES)]
    file_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        graph = await _real_graph()
        sid, out = await _run_until_tool(
            graph,
            prompt=_read_file_prompt(f"temp/{file_name}"),
            tool_name="read_file",
            tag=f"p24-{file_name}",
            is_success=_read_file_success,
        )
        tool_messages = [
            m for m in out["messages"] if isinstance(m, ToolMessage) and m.name == "read_file"
        ]
        sliced = next(
            m for m in reversed(tool_messages) if _READ_FILE_SLICE_NOTICE in str(m.content)
        )
        sliced_text = str(sliced.content)

        # state: first 4000 chars + the recovery notice, with the read_file pointer.
        assert sliced_text.endswith(_READ_FILE_SLICE_NOTICE)
        assert len(sliced_text) == 4_000 + len(_READ_FILE_SLICE_NOTICE)
        assert "read_file" in sliced_text

        # MesMemory: the full JSON result the tool produced (slice happened after).
        tool_rows = _rows_with_role(sid, "tool")
        assert len(tool_rows) == 1
        full_result = tool_rows[0]["content"]
        assert '"total_lines": 3000' in full_result
        assert len(full_result) > 20_000
        assert sliced_text == full_result[:4_000] + _READ_FILE_SLICE_NOTICE

        # P2-4: slicing writes nothing — no eviction file for this session.
        evicted_dir = Path(SESSIONS_DIR) / sid / "evicted"
        assert not evicted_dir.exists() or not any(evicted_dir.iterdir()), (
            f"read_file slicing must not write eviction files: {list(evicted_dir.glob('*'))}"
        )
    finally:
        with contextlib.suppress(OSError):
            file_path.unlink()
        await _purge_session(sid)


# ---------------------------------------------------------------------------
# 4. P1-2 overflow tail clip (component level, real auxiliary LLM)
# ---------------------------------------------------------------------------


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


def _component_middleware(model: _CountingLLM) -> Summarization:
    return Summarization(
        model=model,
        trigger=[("tokens", 80_000)],
        keep=("messages", 10),
        main_llm_context_window=_CTX_WINDOW,
        need_update_system_prompt=False,
    )


def _component_request(messages: list[AnyMessage], sid: str) -> ModelRequest:
    return ModelRequest(
        model=_RequestModelStub(),  # type: ignore[arg-type]
        messages=list(messages),
        state={"session_id": sid, "messages": list(messages)},
    )


def _ai_tool_call(*call_ids: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": "search", "args": {"q": call_id}, "id": call_id} for call_id in call_ids
        ],
    )


def _clip_recoverable_messages() -> list[AnyMessage]:
    """est ~55K; the trailing batch alone can drop it below the 20480 threshold."""
    return [
        HumanMessage(content="h" * 40_000),
        AIMessage(content="a1"),
        HumanMessage(content="q2"),
        _ai_tool_call("c1", "c2", "c3"),
        ToolMessage(content="x" * 60_000, tool_call_id="c1"),
        ToolMessage(content="y" * 60_000, tool_call_id="c2"),
        ToolMessage(content="z" * 60_000, tool_call_id="c3"),
    ]


def _clip_insufficient_messages() -> list[AnyMessage]:
    """est ~55K; the unclippable 100K HumanMessage keeps it over the threshold."""
    return [
        HumanMessage(content="h" * 100_000),
        AIMessage(content="a1"),
        HumanMessage(content="q2"),
        _ai_tool_call("c1", "c2"),
        ToolMessage(content="x" * 60_000, tool_call_id="c1"),
        ToolMessage(content="y" * 60_000, tool_call_id="c2"),
    ]


@contextlib.contextmanager
def _capture_logs(level: str = "INFO"):
    lines: list[str] = []
    handler_id = logger.add(lambda message: lines.append(message.record["message"]), level=level)
    try:
        yield lines
    finally:
        logger.remove(handler_id)


def test_p1_2_sufficient_tail_clip_recovers_without_any_llm_call() -> None:
    """The clip alone recovers the budget: the auxiliary LLM is never invoked."""
    sid = _new_sid("p12-clip")
    counting = _CountingLLM(build_auxiliary_llm())
    middleware = _component_middleware(counting)
    captured: dict = {}

    def handler(request: ModelRequest) -> AIMessage:
        captured["messages"] = list(request.messages)
        return AIMessage(content="ok")

    try:
        with _capture_logs() as lines:
            response = middleware.wrap_model_call(
                _component_request(_clip_recoverable_messages(), sid), handler
            )

        assert response.content == "ok"
        assert counting.prompts == [], (
            f"a sufficient clip must not call the LLM, got {len(counting.prompts)} call(s)"
        )
        clipped = captured["messages"]
        assert all(CLIP_MARKER in str(m.content) for m in clipped[4:])
        assert state_register_mem.get_state(sid, "summarization_compression_count") in (None, 0)
        assert any("route=tail_clip" in line and "trigger=T2" in line for line in lines)
        assert not any("route=compact" in line for line in lines)
    finally:
        clear_all_register_sessions(session_id=sid, clear_persistent_states=True)


def test_p1_2_insufficient_tail_clip_degrades_to_real_llm_compaction() -> None:
    """An insufficient clip degrades to the real compact route (LLM invoked)."""
    sid = _new_sid("p12-degrade")
    counting = _CountingLLM(build_auxiliary_llm())
    middleware = _component_middleware(counting)
    captured: dict = {}

    def handler(request: ModelRequest) -> AIMessage:
        captured["messages"] = list(request.messages)
        return AIMessage(content="ok")

    try:
        with _capture_logs() as lines:
            response = middleware.wrap_model_call(
                _component_request(_clip_insufficient_messages(), sid), handler
            )

        assert response.content == "ok"
        assert counting.prompts, (
            "the degraded route must really invoke the auxiliary LLM for the summary"
        )
        assert any("route=compact_only" in line for line in lines)
        assert not any("route=tail_clip" in line for line in lines)
        assert state_register_mem.get_state(sid, "summarization_compression_count") == 1
        # The compacted request no longer carries the full trailing payload.
        assert len(str(captured["messages"])) < 200_000
    finally:
        clear_all_register_sessions(session_id=sid, clear_persistent_states=True)


# ---------------------------------------------------------------------------
# 5. Chained-summary filtering (real auxiliary LLM)
# ---------------------------------------------------------------------------


def test_chained_summary_filtering_moves_old_summary_to_prior_block(monkeypatch) -> None:
    """_create_summary: old summary leaves <conversation>, enters <prior-summary>."""
    sid = _new_sid("summary-chain")
    counting = _CountingLLM(build_auxiliary_llm())
    middleware = _component_middleware(counting)
    old_summary = "OLD-SUMMARY-SENTINEL: the earlier work fixed the parser and shipped version two."
    messages: list[AnyMessage] = [
        HumanMessage(content="first question about parsers"),
        AIMessage(content=old_summary, additional_kwargs={"lc_source": "summarization"}),
        HumanMessage(content="second question about tests"),
        AIMessage(content="second answer with details"),
    ]
    captured: dict = {}
    original = Summarization._build_summary_prompt

    def spy(self, messages_text: str, previous_summary: str | None, session_id: str = "") -> str:
        prompt = original(self, messages_text, previous_summary, session_id)
        captured["messages_text"] = messages_text
        captured["previous_summary"] = previous_summary
        captured["prompt"] = prompt
        return prompt

    monkeypatch.setattr(Summarization, "_build_summary_prompt", spy)
    try:
        middleware._create_summary(messages, session_id="")

        assert counting.prompts, "the real auxiliary model must be invoked once"
        assert old_summary not in captured["messages_text"], (
            "the previous summary pair must be filtered out of the serialized conversation"
        )
        assert old_summary in (captured["previous_summary"] or "")

        conversation = (
            captured["prompt"].split("<conversation>\n", 1)[1].split("\n</conversation>", 1)[0]
        )
        prior = (
            captured["prompt"].split("<prior-summary>\n", 1)[1].split("\n</prior-summary>", 1)[0]
        )
        assert old_summary not in conversation
        assert old_summary in prior
    finally:
        clear_all_register_sessions(session_id=sid, clear_persistent_states=True)
