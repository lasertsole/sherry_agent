"""Plan Task 3 spike (RED): interrupt-marker approach verification — hermetic.

Establishes four facts about writing an "interrupted turn" marker into the
LangGraph checkpointer of the REAL ``create_agent`` compiled graph (assembled
exactly like ``agent/core.py``, but with a stubbed LLM and an ``InMemorySaver``
so no network / no API keys / no real provider is involved):

- **FACT A** — ``graph.aupdate_state`` viability: a marker ``AIMessage`` written
  post-cancel merges via the ``add_messages`` reducer and is model-visible on
  the next ``ainvoke``.
- **FACT B** — ToolCallNormalize healing scope: an ``AIMessage`` with
  ``tool_calls`` and no following ``ToolMessage`` (the real shape when a cancel
  lands right after a model super-step) is healed at INPUT time by
  ``ToolCallNormalize.before_model`` (``agent/middlewares/tool_call_normalize.py:12-26``
  -> ``pub_func/transcript_repair.py:158-306``: synthesizes an error-status
  placeholder ToolMessage, :138-155, :270-289). WITHOUT that middleware a
  strict provider rejects the transcript (simulated 400 here). CRITICAL
  finding: when nothing terminates the dangling span, the sanitizer's span
  scan runs to end-of-transcript and silently DROPS the next HumanMessage
  from the model view (and, via the REMOVE_ALL_MESSAGES rewrite, from
  checkpointer state). The marker — itself an AIMessage — terminates the
  span, so marker + input-time healing yields the provider-valid order
  [ai(tool_calls), tool(placeholder), ai(marker), human]. Verdict: heal at
  WRITE time; input-time healing stays as backstop.
- **FACT C** — Summarization survival: within the ``keep`` window the marker
  survives verbatim (immediate-next-turn AC-1 path); once the marker falls into
  the summarized region it disappears from the MODEL-VISIBLE view (metadata
  lost, content survives only if the summarizer LLM happens to echo it —
  non-deterministic), while the checkpointer STATE retains the full history
  (the project ``Summarization`` subclass performs compaction inside
  ``wrap_model_call`` via ``request.override`` — request-scoped, never
  committed to graph state; its ``before_model`` override is log-only,
  ``agent/middlewares/summarization.py:150-162, 466-493``).
- **FACT D** — deterministic-ID idempotency: repeated ``aupdate_state`` calls
  with the SAME message ID upsert (one message, content replaced in place);
  a different ID appends.

Middleware subset note: the production stack in ``agent/core.py:120-136`` also
registers ContextEngineHook / MultimodalProcessor / IterationBudget /
ToolGuardrails / SubagentCompletionDrain / HeartbeatStaleness / HumanInTheLoop /
RepetitionGuardWrapper. Those are excluded here because they bind to real
disk stores (MesMemory SQLite, media files, steering-queue SQLite) or start
timer threads — none of them participate in reducer/update_state semantics or
in ToolCallNormalize/Summarization behavior, which are the objects under test.
Task 6 may reuse this harness; facts here are pinned to langgraph 1.2.5 /
langchain 1.3.9 / langchain-core 1.4.7 (uv.lock).

RED status: all facts in this file are ALREADY established by execution
(green). Task 6 converts the marker-writing procedure exercised here into the
production ``server/service/interrupt_marker.py`` implementation.
"""

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph
from pydantic import Field

import context_engine.store.core as mes_store_core
from agent.middlewares.summarization import Summarization
from agent.middlewares.tool_call_normalize import ToolCallNormalize
from context_engine.store.db import _migrate as mes_migrate
from server.queue import UserInputQueue, UserInputQueueStatus
from server.service.interrupt_marker import write_interrupted_marker

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(60)]

# Versions under test (pinned observations; see uv.lock)
LANGGRAPH_VERSION = "1.2.5"
LANGCHAIN_VERSION = "1.3.9"
LANGCHAIN_CORE_VERSION = "1.4.7"

THREAD_ID = "spike-thread"

# The deterministic ID shape Task 6 plans to write (plan Task 6: `interrupted-turn-{n}`)
MARKER_ID = "interrupted-turn-1"
MARKER_CONTENT = "[interrupted] partial answer text"
MARKER_METADATA = {"interrupted": True, "reason": "cancelled"}

DANGLING_CALL_ID = "call_1"
DANGLING_AI_ID = "ai-dangling-superstep"

# Pinned observation (FACT C / test_fact_c_marker_swallowed_from_model_view):
# after a summarization-triggering turn, the checkpointer state RETAINS the
# marker (the project Summarization subclass compacts the request view only).
# See the verdict doc; flip only if the langchain/langgraph stack changes.
MARKER_IN_STATE_AFTER_SUMMARY_TURN = True


# ---------------------------------------------------------------------------
# Fakes — ONLY the models are fake. The graph is the real create_agent graph.
# ---------------------------------------------------------------------------


def _find_dangling_tool_calls(messages: list[BaseMessage]) -> list[str]:
    """Strict-provider transcript rule: every tool_call of an AIMessage must be
    answered by a ToolMessage carrying its tool_call_id before the next
    AIMessage (this is the rule OpenAI-style providers enforce with HTTP 400).
    """
    dangling: list[str] = []
    pending: list[str] = []
    for m in messages:
        if isinstance(m, AIMessage):
            if pending:
                dangling.extend(pending)
            pending = [tc["id"] for tc in (getattr(m, "tool_calls", None) or [])]
        elif isinstance(m, ToolMessage):
            tid = getattr(m, "tool_call_id", None)
            if tid in pending:
                pending.remove(tid)
    if pending:
        dangling.extend(pending)
    return dangling


class SimulatedProvider400(Exception):
    """Raised by the strict fake provider on dangling tool_calls.

    Real providers (OpenAI etc.) answer such transcripts with HTTP 400
    ("An assistant message with 'tool_calls' must be followed by tool messages
    responding to each 'tool_call_id'").
    """


class RecordingFakeChatModel(BaseChatModel):
    """Fake LLM: records every model-call input, replies canned text."""

    response_text: str = "fake reply"
    received: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "recording-fake"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.received.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.response_text))])


class StrictProviderFakeChatModel(RecordingFakeChatModel):
    """Fake LLM that behaves like a strict provider: HTTP 400 (simulated) when
    the transcript it receives contains unanswered tool_calls."""

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.received.append(list(messages))
        dangling = _find_dangling_tool_calls(self.received[-1])
        if dangling:
            raise SimulatedProvider400(
                f"HTTP 400 (simulated): tool_call ids {dangling} have no following ToolMessage"
            )
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.response_text))])


class FixedSummaryModel(BaseChatModel):
    """Fake auxiliary LLM for Summarization: always returns the same summary."""

    summary_text: str = "SUMMARY-TEXT"
    received: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "fixed-summary-fake"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.received.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.summary_text))])


class _SpikeState(AgentState):
    """Graph state matching agent/core.py StateSchema (session_id carrier)."""

    session_id: str


def _build_graph(
    model: BaseChatModel, middleware: list[AgentMiddleware]
) -> CompiledStateGraph[Any, Any, Any, Any]:
    """Real create_agent graph, assembled like agent/core.py:115-137.

    Hermetic substitutions only: stubbed model, InMemorySaver checkpointer,
    middleware subset (see module docstring).
    """
    return create_agent(
        model=model,
        tools=[],
        middleware=middleware,
        state_schema=_SpikeState,
        checkpointer=InMemorySaver(),
    )


def _config() -> RunnableConfig:
    return {"configurable": {"thread_id": THREAD_ID}}


def _input(messages: list[HumanMessage], session_id: str = "spike-session") -> dict[str, Any]:
    return {"messages": messages, "session_id": session_id}


def _marker(message_id: str = MARKER_ID, content: str = MARKER_CONTENT) -> AIMessage:
    """The marker Task 6 will write: deterministic ID + interrupted metadata."""
    return AIMessage(content=content, id=message_id, metadata=dict(MARKER_METADATA))


def _dangling_ai() -> AIMessage:
    """AIMessage left by a cancel that landed right after a model super-step."""
    return AIMessage(
        content="",
        id=DANGLING_AI_ID,
        tool_calls=[{"name": "echo", "args": {"x": 1}, "id": DANGLING_CALL_ID, "type": "tool_call"}],
    )


async def _state_messages(
    graph: CompiledStateGraph[Any, Any, Any, Any], config: RunnableConfig
) -> list[BaseMessage]:
    snap = await graph.aget_state(config)
    return list(snap.values["messages"])


async def _seed_dangling_state(
    graph: CompiledStateGraph[Any, Any, Any, Any], config: RunnableConfig
) -> None:
    """Turn 1 completes normally, then the cancel shape is appended:
    the transcript ends with an AIMessage that has tool_calls and no result."""
    await graph.ainvoke(_input([HumanMessage("Q1")]), config)
    await graph.aupdate_state(config, {"messages": [_dangling_ai()]})


def _index_of(messages: list[BaseMessage], message_id: str) -> int:
    ids = [getattr(m, "id", None) for m in messages]
    return ids.index(message_id)


# ---------------------------------------------------------------------------
# FACT A — graph.update_state viability
# ---------------------------------------------------------------------------


async def test_fact_a_update_state_merges_marker_via_add_messages_reducer():
    """A1: aupdate_state succeeds and the marker MERGES via the messages
    reducer (existing history preserved, marker appended at the end).

    Note on the plan's pointer to context_engine/store/core.py:25-44: that
    ``add_messages`` is the MesMemory SQLite persistence writer (append-only
    turn batches, no ID dedupe). The reducer governing checkpointer state here
    is LangGraph's own ``langgraph.graph.message.add_messages`` (upsert by ID).
    Both are covered by this spike (MesMemory append semantics -> verdict doc).
    """
    model = RecordingFakeChatModel(response_text="R1")
    graph = _build_graph(model, [ToolCallNormalize()])

    await graph.ainvoke(_input([HumanMessage("Q1")]), _config())
    msgs = await _state_messages(graph, _config())
    assert len(msgs) == 2
    assert isinstance(msgs[0], HumanMessage) and msgs[0].content == "Q1"
    assert isinstance(msgs[1], AIMessage) and msgs[1].content == "R1"

    result = await graph.aupdate_state(_config(), {"messages": [_marker()]})
    assert isinstance(result, dict) and result.get("configurable", {}).get("thread_id") == THREAD_ID

    msgs = await _state_messages(graph, _config())
    # Merged, not replaced: history intact + marker appended last.
    assert len(msgs) == 3
    assert isinstance(msgs[0], HumanMessage) and msgs[0].content == "Q1"
    assert isinstance(msgs[1], AIMessage) and msgs[1].content == "R1"
    assert msgs[-1].id == MARKER_ID
    assert msgs[-1].content == MARKER_CONTENT
    # metadata is a runtime extra on BaseMessage (extra="allow") — getattr is
    # the type-checker-visible way to read it.
    assert getattr(msgs[-1], "metadata").get("interrupted") is True
    assert getattr(msgs[-1], "metadata").get("reason") == "cancelled"


async def test_fact_a_marker_model_visible_on_next_invoke():
    """A2: on the next ainvoke the marker reaches the MODEL as
    [human Q1, ai R1, ai(interrupted-marker), human Q2] — the AC-1 sequence —
    with metadata intact, and stays exactly once in state afterwards
    (ToolCallNormalize's REMOVE_ALL_MESSAGES rewrite preserves it)."""
    model = RecordingFakeChatModel(response_text="R2")
    graph = _build_graph(model, [ToolCallNormalize()])

    await graph.ainvoke(_input([HumanMessage("Q1")]), _config())
    await graph.aupdate_state(_config(), {"messages": [_marker()]})

    await graph.ainvoke(_input([HumanMessage("Q2")]), _config())

    assert model.received, "model was never called"
    last_call = model.received[-1]  # turn 2's model call (received[0] is turn 1's)
    assert last_call[-1].content == "Q2"
    assert last_call[-2].id == MARKER_ID
    assert last_call[-2].content == MARKER_CONTENT
    assert getattr(last_call[-2], "metadata").get("interrupted") is True
    assert getattr(last_call[-2], "metadata").get("reason") == "cancelled"
    # AC-1 sequence: [human Q1, ai(turn-1 reply), ai(interrupted), human Q2]
    # (the canned fake answers "R2" to every call, hence turn-1's reply is "R2")
    assert last_call[0].content == "Q1"
    assert isinstance(last_call[1], AIMessage) and last_call[1].content == "R2"

    # Exactly one marker survives in checkpointer state after the turn.
    msgs = await _state_messages(graph, _config())
    assert sum(1 for m in msgs if m.id == MARKER_ID) == 1
    assert msgs[-1].content == "R2"


# ---------------------------------------------------------------------------
# FACT B — ToolCallNormalize healing scope
# ---------------------------------------------------------------------------


async def test_fact_b_tool_call_healing_drops_trailing_human_when_no_marker():
    """B1 (with ToolCallNormalize, NO marker — the pre-reconciliation state):
    the pair itself is healed (error placeholder synthesized at INPUT time,
    transcript_repair.py:138-155, :270-289), BUT the sanitizer's span scan runs
    to END-OF-TRANSCRIPT (:236-263: messages after an unanswered AIMessage that
    are not ToolMessages only set ``changed=True`` and are discarded) and
    silently DROPS the next HumanMessage from the model view. Observed: the
    model receives [Q1, ai, ai(tool_calls), tool(placeholder)] and never sees
    Q2; the REMOVE_ALL_MESSAGES rewrite (tool_call_normalize.py:16) then
    commits that loss to checkpointer state. This is the hazard that makes
    write-time healing the Task 6 requirement."""
    model = StrictProviderFakeChatModel(response_text="R-after-heal")
    graph = _build_graph(model, [ToolCallNormalize()])

    await _seed_dangling_state(graph, _config())
    await graph.ainvoke(_input([HumanMessage("Q2")]), _config())  # no provider 400...

    last_call = model.received[-1]  # turn 2's model call
    placeholders = [
        m for m in last_call if isinstance(m, ToolMessage) and m.tool_call_id == DANGLING_CALL_ID
    ]
    assert len(placeholders) == 1, f"no synthesized ToolMessage in {last_call!r}"
    placeholder = placeholders[0]
    assert placeholder.status == "error"
    assert "missing" in placeholder.content  # "tool result missing after context trim."
    assert placeholder.id is not None, "synthesized placeholder lost its id"

    # The pair is healed, but the dangling span swallowed the new user turn:
    idx_ai = _index_of(last_call, DANGLING_AI_ID)
    idx_ph = _index_of(last_call, placeholder.id)
    assert idx_ai < idx_ph
    assert not any(isinstance(m, HumanMessage) and m.content == "Q2" for m in last_call), (
        "expected the trailing HumanMessage to be dropped by the span scan "
        "(transcript_repair.py:236-263) — if this fails, healing behavior changed"
    )
    assert last_call[-1].id == placeholder.id, "model saw a transcript ending at the placeholder"

    # The Q2 loss is PERSISTED: the REMOVE_ALL_MESSAGES rewrite committed the
    # healed-but-Q2-less transcript to checkpointer state.
    msgs = await _state_messages(graph, _config())
    persisted = [
        m for m in msgs if isinstance(m, ToolMessage) and m.tool_call_id == DANGLING_CALL_ID
    ]
    assert len(persisted) == 1, "synthesized ToolMessage was not committed to state"
    assert not any(m.content == "Q2" for m in msgs), "Q2 loss was not persisted"
    assert msgs[-1].content == "R-after-heal"


async def test_fact_b_tool_call_no_middleware_strict_provider_400():
    """B2 (causality proof): WITHOUT ToolCallNormalize the dangling pair reaches
    the model and a strict provider rejects it — the simulated HTTP 400."""
    model = StrictProviderFakeChatModel(response_text="never")
    graph = _build_graph(model, [])  # no ToolCallNormalize

    await _seed_dangling_state(graph, _config())
    with pytest.raises(SimulatedProvider400) as exc_info:
        await graph.ainvoke(_input([HumanMessage("Q2")]), _config())
    assert DANGLING_CALL_ID in str(exc_info.value)
    assert model.received, "provider was never reached"  # the 400 fires at the provider boundary


async def test_fact_b_tool_call_marker_after_dangling_pair_keeps_provider_valid_order():
    """B3 (the exact Task 6 combined scenario): cancel right after a model
    super-step + marker written via update_state. Next turn the model receives
    [ai(tool_calls), tool(error placeholder), ai(interrupted-marker), human]
    — the placeholder is synthesized BEFORE the marker, the marker keeps its
    metadata, and no provider 400 occurs."""
    model = StrictProviderFakeChatModel(response_text="R2")
    graph = _build_graph(model, [ToolCallNormalize()])

    await _seed_dangling_state(graph, _config())
    await graph.aupdate_state(_config(), {"messages": [_marker()]})
    await graph.ainvoke(_input([HumanMessage("Q2")]), _config())  # must NOT raise

    last_call = model.received[-1]  # turn 2's model call
    placeholders = [
        m for m in last_call if isinstance(m, ToolMessage) and m.tool_call_id == DANGLING_CALL_ID
    ]
    assert len(placeholders) == 1
    placeholder = placeholders[0]
    assert placeholder.id is not None

    idx_dangling = _index_of(last_call, DANGLING_AI_ID)
    idx_placeholder = _index_of(last_call, placeholder.id)
    idx_marker = _index_of(last_call, MARKER_ID)
    idx_q2 = len(last_call) - 1
    assert last_call[idx_q2].content == "Q2"
    assert idx_dangling < idx_placeholder < idx_marker < idx_q2
    assert getattr(last_call[idx_marker], "metadata").get("interrupted") is True


# ---------------------------------------------------------------------------
# FACT C — Summarization survival
# ---------------------------------------------------------------------------


async def test_fact_c_marker_within_keep_window_survives_verbatim():
    """C1: on the immediate next turn (the AC-1 binding path) the marker sits
    inside the summarizer's ``keep`` window and reaches the model VERBATIM,
    metadata intact. Summarization is registered but not triggered."""
    model = RecordingFakeChatModel(response_text="R2")
    aux = FixedSummaryModel(summary_text="SUMMARY-TEXT")
    graph = _build_graph(
        model,
        [
            ToolCallNormalize(),
            Summarization(model=aux, trigger=[("messages", 50)], keep=("messages", 2)),
        ],
    )

    await graph.ainvoke(_input([HumanMessage("Q1")], session_id="spike-c1"), _config())
    await graph.aupdate_state(_config(), {"messages": [_marker()]})
    await graph.ainvoke(_input([HumanMessage("Q2")], session_id="spike-c1"), _config())

    last_call = model.received[-1]  # turn 2's model call
    assert last_call[-2].id == MARKER_ID
    assert last_call[-2].content == MARKER_CONTENT
    assert getattr(last_call[-2], "metadata").get("interrupted") is True
    assert aux.received == [], "summarizer must not have run"


async def test_fact_c_marker_swallowed_from_model_view_once_summarized():
    """C2: once the marker falls BELOW the keep window it is swallowed from the
    MODEL-VISIBLE view — no marker id, no '[interrupted]' content fragment, no
    metadata. The summary HumanMessage (lc_source='summarization') replaces the
    summarized region. The marker CONTENT is fed to the summarizer LLM as part
    of the summarized transcript, so survival in later context depends on that
    LLM echoing it — non-deterministic with a real auxiliary model.

    Checkpointer STATE retention is pinned by
    MARKER_IN_STATE_AFTER_SUMMARY_TURN (see module docstring)."""
    model = RecordingFakeChatModel(response_text="R2")
    # >= 50 chars: the redesigned _create_summary (summarization.py:785-787)
    # discards summaries shorter than 50 chars into the deterministic static
    # fallback, which echoes AI-message fragments (the marker text) into the
    # model view. A long fixed summary keeps the LLM-path semantics the
    # marker-swallow assertions below were written against.
    aux = FixedSummaryModel(
        summary_text="SUMMARY-TEXT (long enough to clear the 50-char minimum gate)"
    )
    sid = "spike-c2"
    graph = _build_graph(
        model,
        [
            ToolCallNormalize(),
            Summarization(
                model=aux,
                trigger=[("messages", 5)],
                keep=("messages", 2),
                # §9.7 adaptation (T9 pattern): inject the window so the
                # preserve budget is deterministic — 8000 * PRESERVE_RATIO
                # (0.25) = 2000 = MIN_PRESERVE_TOKENS. Pressure
                # (~4115 est tokens / 8000 ~ 0.51) stays below
                # PREEMPTIVE_TRUNCATE_RATIO (0.70), so the ("messages", 5)
                # trigger alone drives compression, as before the redesign.
                main_llm_context_window=8_000,
            ),
        ],
    )

    await graph.ainvoke(_input([HumanMessage("Q1")], session_id=sid), _config())  # 2 msgs
    await graph.aupdate_state(_config(), {"messages": [_marker()]})  # 3 msgs
    await graph.aupdate_state(
        _config(),
        {
            "messages": [
                # ~2053 est tokens each (len // CHARS_PER_TOKEN): the pair must
                # exceed the 2000-token preserve budget; otherwise
                # _determine_cutoff returns 0 (every turn fits the budget) and
                # the redesigned middleware no-ops instead of summarizing
                # (summarization.py:1045-1047 / 1117-1119).
                HumanMessage("filler question " + "x" * 8200),
                AIMessage(content="filler answer " + "y" * 8200),
            ]
        },
    )  # 5 msgs — marker is now the 3rd of 5
    await graph.ainvoke(_input([HumanMessage("Q-last")], session_id=sid), _config())  # 6 >= 5 -> trigger

    assert aux.received, "summarizer never ran"
    # The summarizer LLM is shown the marker content (survival there is its call).
    summarizer_prompt = str(aux.received[0][0].content)
    assert MARKER_CONTENT in summarizer_prompt

    first_call = model.received[0]  # sanity: turn 1's call saw plain Q1
    assert first_call[-1].content == "Q1"

    last_call = model.received[-1]  # final turn's model call (post-trigger)
    ids = [getattr(m, "id", None) for m in last_call]
    assert MARKER_ID not in ids, "marker id leaked into the model view after summarization"
    all_text = " ".join(
        m.content if isinstance(m.content, str) else str(m.content) for m in last_call
    )
    assert "[interrupted]" not in all_text, "marker fragment leaked into the model view"
    # The view now starts with the summary pair. §9.7 tags lc_source on the
    # summary AIMessage (_build_new_messages, summarization.py:839-845); the
    # old middleware tagged the summary HumanMessage instead.
    summary_msg = last_call[0]
    assert isinstance(summary_msg, HumanMessage)
    assert summary_msg.content == "What did we do so far?"
    summary_ai = last_call[1]
    assert isinstance(summary_ai, AIMessage)
    assert summary_ai.additional_kwargs.get("lc_source") == "summarization"
    assert last_call[-1].content == "Q-last"

    # State retention (pinned observation): the project Summarization subclass
    # compacts ONLY the request view (wrap_model_call / request.override,
    # summarization.py:466-493); its before_model override is log-only
    # (:150-162), so checkpointer state keeps the full history incl. marker.
    msgs = await _state_messages(graph, _config())
    assert any(m.id == MARKER_ID for m in msgs) is MARKER_IN_STATE_AFTER_SUMMARY_TURN


# ---------------------------------------------------------------------------
# FACT D — deterministic-ID idempotency
# ---------------------------------------------------------------------------


async def test_fact_d_deterministic_id_upsert_is_idempotent():
    """D1: repeating aupdate_state with the SAME deterministic message ID
    upserts (still exactly one marker, content replaced in place, metadata
    updated); a DIFFERENT ID appends."""
    model = RecordingFakeChatModel(response_text="R1")
    graph = _build_graph(model, [ToolCallNormalize()])

    await graph.ainvoke(_input([HumanMessage("Q1")]), _config())

    await graph.aupdate_state(_config(), {"messages": [_marker()]})
    msgs = await _state_messages(graph, _config())
    assert sum(1 for m in msgs if m.id == MARKER_ID) == 1

    # Same deterministic ID, different content/metadata -> upsert, not append.
    await graph.aupdate_state(
        _config(),
        {
            "messages": [
                _marker(content="rewritten marker body", message_id=MARKER_ID),
            ]
        },
    )
    msgs = await _state_messages(graph, _config())
    markers = [m for m in msgs if m.id == MARKER_ID]
    assert len(markers) == 1, "same deterministic ID duplicated instead of upserting"
    assert markers[0].content == "rewritten marker body"
    assert getattr(markers[0], "metadata").get("interrupted") is True

    # A different deterministic ID appends.
    await graph.aupdate_state(
        _config(),
        {"messages": [_marker(message_id="interrupted-turn-2", content="second marker")]},
    )
    msgs = await _state_messages(graph, _config())
    assert sum(1 for m in msgs if m.id == MARKER_ID) == 1
    assert sum(1 for m in msgs if m.id == "interrupted-turn-2") == 1
    # Appended at the end, order preserved.
    assert msgs[-1].id == "interrupted-turn-2"


# ---------------------------------------------------------------------------
# Task 6 — PRODUCTION path (server/service/interrupt_marker.py)
#
# The facts above are re-exercised THROUGH the production writer. The graph
# and model stay hermetic (T3 harness); ``graph=``/``queue=`` are always
# passed explicitly so the writer never builds the real production agent nor
# touches the default queue's SQLite file.
# ---------------------------------------------------------------------------

PROD_SESSION = "prod-interrupt-session"

# Production id shape: interrupted-{thread_id}-{turn_seq} (turn_seq = 1 here:
# exactly one HumanMessage seeded in each scenario below).
PROD_MARKER_ID = f"interrupted-{THREAD_ID}-1"
PROD_HEAL_ID = f"{PROD_MARKER_ID}-heal-{DANGLING_CALL_ID}"

# Mirrors pub_func/transcript_repair.make_missing_tool_result content.
PROD_HEAL_CONTENT = "tool result missing after context trim."


@pytest.fixture
def mes_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[sqlite3.Connection]:
    """MesMemory on a hermetic tmp SQLite file.

    Patches ``context_engine.store.core._db`` (the module-global connection
    every store operation reads at call time) so the writer's dual-write lands
    in an isolated database.
    """
    conn = sqlite3.connect(
        tmp_path / "mes_memory.db", check_same_thread=False, isolation_level=None
    )
    conn.row_factory = sqlite3.Row
    mes_migrate(conn)
    monkeypatch.setattr(mes_store_core, "_db", conn)
    yield conn
    conn.close()


@pytest.fixture
def queue(tmp_path: Path) -> UserInputQueue:
    """Real store on a hermetic tmp SQLite file (same DB layout as production)."""
    return UserInputQueue(db_path=tmp_path / "user_input_queue.db")


def _interrupted_rows(session_id: str, limit: int = 5) -> list[dict[str, Any]]:
    """MesMemory rows of the session (newest turn first), decoded."""
    return [
        dict(r)
        for r in mes_store_core.get_messages_by_lastest_n_turns(session_id, last_n=limit)
    ]


def _queue_statuses(db_path: Path) -> list[str]:
    """All queue row statuses (created_at ASC), including terminal rows."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT status FROM user_input_queue ORDER BY created_at ASC, id ASC"
        ).fetchall()
        return [str(r[0]) for r in rows]
    finally:
        conn.close()


async def test_production_write_interrupted_marker_binds_reply_sequence(
    queue: UserInputQueue, mes_db: sqlite3.Connection
):
    """AC-1 through the PRODUCTION writer: seed Q1 (cancel landed before any
    reply), write the interrupted marker, then invoke Q2 — the model receives
    EXACTLY [human(Q1), ai(interrupted), human(Q2)] with metadata intact, the
    MesMemory row carries the [interrupted:reason] prefix, and CLAIMED
    cleanup is a no-op on an empty queue."""
    model = RecordingFakeChatModel(response_text="R2")
    graph = _build_graph(model, [ToolCallNormalize()])
    config = _config()

    await graph.aupdate_state(config, {"messages": [HumanMessage("Q1")]})
    await write_interrupted_marker(
        PROD_SESSION, config, "部分回答", "cancelled", graph=graph, queue=queue
    )
    await graph.ainvoke(_input([HumanMessage("Q2")], session_id=PROD_SESSION), config)

    assert model.received, "model was never called"
    last_call = model.received[-1]
    assert len(last_call) == 3, f"expected [human, ai(interrupted), human], got {last_call!r}"
    assert isinstance(last_call[0], HumanMessage) and last_call[0].content == "Q1"
    assert isinstance(last_call[1], AIMessage)
    assert last_call[1].id == PROD_MARKER_ID
    assert last_call[1].content == "[interrupted] 部分回答"
    assert getattr(last_call[1], "metadata").get("interrupted") is True
    assert getattr(last_call[1], "metadata").get("reason") == "cancelled"
    assert isinstance(last_call[2], HumanMessage) and last_call[2].content == "Q2"

    interrupted = [
        r
        for r in _interrupted_rows(PROD_SESSION)
        if r.get("role") == "ai"
        and str(r.get("content", "")).startswith("[interrupted:")
    ]
    assert len(interrupted) == 1
    assert interrupted[0]["content"] == "[interrupted:cancelled] 部分回答"

    assert await queue.list_active(PROD_SESSION) == [], "empty queue: cleanup is a no-op"


async def test_production_heal_write_time_keeps_strict_provider_valid(
    queue: UserInputQueue, mes_db: sqlite3.Connection
):
    """T3 FACT B re-verified THROUGH the production writer: the dangling
    trailing super-step is healed at WRITE time (error placeholder with the
    deterministic ``{marker_id}-heal-{call_id}`` id committed BEFORE the
    marker), so the next turn's strict-provider call sees the valid order
    [ai(tool_calls), tool(placeholder), ai(marker), human] — no simulated 400,
    no input-time re-synthesis, Q2 not dropped by the span scan."""
    model = StrictProviderFakeChatModel(response_text="R2")
    graph = _build_graph(model, [ToolCallNormalize()])
    config = _config()

    await _seed_dangling_state(graph, config)
    await write_interrupted_marker(
        PROD_SESSION, config, "partial answer", "cancelled", graph=graph, queue=queue
    )

    # Write-time heal is already committed to state, ordered before the marker.
    msgs = await _state_messages(graph, config)
    ids = [getattr(m, "id", None) for m in msgs]
    assert PROD_HEAL_ID in ids, f"no deterministic heal placeholder in {ids!r}"
    assert ids.index(PROD_HEAL_ID) < ids.index(PROD_MARKER_ID) == len(msgs) - 1
    heal = next(m for m in msgs if getattr(m, "id", None) == PROD_HEAL_ID)
    assert isinstance(heal, ToolMessage)
    assert heal.tool_call_id == DANGLING_CALL_ID
    assert heal.status == "error"
    assert heal.content == PROD_HEAL_CONTENT

    # Next turn: strict provider must NOT 400 and must see the full valid
    # order, including the new Q2 (write-time healing ends the span, so the
    # input-time scan never drops it — the FACT B1 hazard).
    await graph.ainvoke(_input([HumanMessage("Q2")], session_id=PROD_SESSION), config)
    last_call = model.received[-1]
    idx_dangling = _index_of(last_call, DANGLING_AI_ID)
    idx_heal = _index_of(last_call, PROD_HEAL_ID)
    idx_marker = _index_of(last_call, PROD_MARKER_ID)
    assert idx_dangling < idx_heal < idx_marker < len(last_call) - 1
    assert last_call[-1].content == "Q2"
    # The state-committed placeholder reached the model as-is (no duplicate
    # input-time synthesis for the same call id).
    assert (
        sum(
            1
            for m in last_call
            if isinstance(m, ToolMessage) and m.tool_call_id == DANGLING_CALL_ID
        )
        == 1
    )


async def test_production_idempotent_rewrite_and_claimed_voiding(
    queue: UserInputQueue, mes_db: sqlite3.Connection
):
    """T3 FACT D re-verified THROUGH the production writer: a second call with
    the same state upserts by the deterministic id (one marker, no MesMemory
    duplicate) and STILL voids CLAIMED rows, leaving QUEUED rows untouched for
    the Task 7 drain."""
    model = RecordingFakeChatModel(response_text="R1")
    graph = _build_graph(model, [ToolCallNormalize()])
    config = _config()

    await queue.insert_claimed(PROD_SESSION, '{"text": "in flight"}', "user")
    await queue.enqueue(PROD_SESSION, '{"text": "waiting"}', "user")

    await graph.aupdate_state(config, {"messages": [HumanMessage("Q1")]})
    await write_interrupted_marker(
        PROD_SESSION, config, "partial answer", "cancelled", graph=graph, queue=queue
    )
    msgs_after_first = await _state_messages(graph, config)
    rows_after_first = _interrupted_rows(PROD_SESSION)

    await write_interrupted_marker(
        PROD_SESSION, config, "partial answer", "cancelled", graph=graph, queue=queue
    )

    msgs = await _state_messages(graph, config)
    assert len(msgs) == len(msgs_after_first), "no state growth on the rewrite"
    assert sum(1 for m in msgs if getattr(m, "id", None) == PROD_MARKER_ID) == 1

    rows = _interrupted_rows(PROD_SESSION)
    assert len(rows) == len(rows_after_first), "MesMemory insert skipped on rewrite"
    assert (
        sum(
            1
            for r in rows
            if r.get("role") == "ai" and str(r.get("content", "")).startswith("[interrupted:")
        )
        == 1
    )

    statuses = _queue_statuses(queue._db_path)
    assert statuses == ["VOIDED", "QUEUED"], "CLAIMED voided, QUEUED preserved"
    active = await queue.list_active(PROD_SESSION)
    assert [r.status for r in active] == [UserInputQueueStatus.QUEUED]
