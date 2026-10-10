import asyncio
import contextlib
import os
import threading
from typing import Any

from langchain_core.tools import BaseTool
from langchain.agents import create_agent
from langchain.agents.middleware import AgentState
from langgraph.graph.state import CompiledStateGraph
from loguru import logger
from models import build_main_llm, build_auxiliary_llm
from agent.checkpointer import build_async_sqlite_checkpointer
from models.LLMs.main_llm import build_fallback_chain
from models.LLMs.main_llm import max_tokens as main_llm_max_tokens
from config.features import (
    ITERATION_BUDGET,
    LLM_CLIENT_DEFAULTS,
    SUMMARIZATION,
    assert_max_token_valid,
)
from agent.tools import memory_store, build_main_tools
from .checkpointer.thread_safe_checkpointer import ThreadSafeAsyncSqliteSaver
from .middlewares import (
    Summarization,
    ToolCallNormalize,
    PathGuard,
    MultimodalProcessor,
    system_prompt_injection,
    ToolGuardrails,
    ContextEvictionMiddleware,
    IterationBudget,
    HeartbeatStaleness,
    OutputRepetitionGuard,
    MaxTokensBoostMiddleware,
    MessagePersistenceMiddleware,
    LLMRetryMiddleware,
    validate_required_middleware,
    _MAIN_REQUIRED,
)
from .middlewares.humanInTheLoop import HumanInTheLoop, HITLConfig
from .middlewares.workspace_notice import WorkspaceNoticeMiddleware
from .middlewares.tool_selection import ToolSelectionMiddleware
from .middlewares.subagent_completion_drain import SubagentCompletionDrainMiddleware
from .middlewares.task_intent import TaskIntentMiddleware
from .middlewares.thinking_control import ThinkingControlMiddleware
from .middlewares.todo_continuation import TodoContinuationEnforcer
from agent.wrapper.registry import apply_graph_wrappers
from agent.prompt_data_provider import register_prompt_data_provider
from agent.skill_write_provider import register_skill_write_provider
from .wrapper.context_limit import ContextLimitGuardWrapper

COMPRESSION_TRIGGER_RATIO = SUMMARIZATION["compression_trigger_ratio"]

# ── Extended state schema ────────────────────────────────────────────────
# Carries ``session_id`` through the graph so that middlewares reading
# ``request.state["session_id"]`` is used by middlewares that need it


class StateSchema(AgentState):
    """Agent state that preserves an ``session_id``."""

    session_id: str


# ── Initialization (explicit, idempotent) ────────────────────────────────
# These three steps live in ``init``, called once by the service entry point
# (``server/__main__.py``): importing this module stays side-effect-free, so a
# bare ``import agent.core`` (tests, tooling, type checkers) triggers no disk
# I/O and no tool construction.

_tools: list[BaseTool] = []
_initialized: bool = False


def init() -> None:
    """One-time agent initialization; called by the service entry point.

    - Rebuilds the skill snapshot at server start to keep the skills prompt
      stable throughout this server run, ensuring reliable model prefix
      caching.
    - Loads memory markdown files from disk; they stay unchanged until
      compression is triggered during this server run.
    - Builds the main tool list.
    - Registers the prompt data provider so workspace/context_engine can read
      prompt data without importing the agent package.
    - Registers the skill write provider so the curator can create/write
      skills without importing the agent package.

    Idempotent: subsequent calls are no-ops.
    """
    global _tools, _initialized
    if _initialized:
        return

    from skills import build_skills_snapshot

    build_skills_snapshot()
    memory_store.load_from_disk()
    _tools = build_main_tools()
    register_prompt_data_provider()
    register_skill_write_provider()
    _initialized = True
    logger.info(
        "agent init: skills snapshot built, {} main tool(s) registered, providers wired",
        len(_tools),
    )


def get_agent_tools() -> list[BaseTool]:
    return _tools


# Cache of the compiled agent for the CURRENT event loop: ONE slot plus the loop
# it was built on. A request on the same loop reuses it; a request on another
# loop — or ``force_rebuild=True`` — replaces it. (The earlier design kept a
# per-loop dict; a single slot is what actually ships, because a rebuild is
# required whenever the loop differs and the extra entries would only pin more
# loop-bound clients alive.)
#
# Why the loop matters: the graph embeds a main_llm whose openai.AsyncOpenAI ->
# httpx.AsyncClient transport pool is bound to the loop it was built on. Reusing
# a foreign loop's pool dies mid-request as
# ``openai.APITimeoutError("Request timed out")`` at ~17s, far under the SDK's
# 600s deadline — the request is killed by the dead pooled connection. Verified
# with a standalone stream (same 12KB prompt + full tools schema) completing in
# 8.0s on a fresh in-loop client while the cached-pool path failed at ~16.78s.
#
# Lifecycle: every rebuild opens a NEW checkpointer (own aiosqlite connection +
# non-daemon worker thread + file handle), so a replaced graph must be closed or
# every turn leaks one connection. Callers register a live use with
# ``hold_agent`` / ``release_agent``; a replaced graph is closed as soon as its
# last holder drops it, and a graph that is still the cache stays open.
class _LoopSafeLock(asyncio.Lock):
    """``asyncio.Lock`` that binds to whichever loop acquires it.

    The build lock is module-level and ``built_agent`` is called from the server
    loop, the subagent daemon thread and in-process callers; a plain
    ``asyncio.Lock`` created at import time would refuse the second loop
    ("is bound to a different event loop"). Same override as the checkpointer's.
    """

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        return asyncio.get_running_loop()


_agent: CompiledStateGraph | None = None
_agent_loop = None
_agent_lock = _LoopSafeLock()
_agent_holders: dict[int, int] = {}  # id(graph) -> live holders
_agent_holders_lock = threading.Lock()
_release_tasks: set[asyncio.Task[None]] = set()


def hold_agent(graph: Any) -> None:
    """Register a live use of *graph* (pair every call with ``release_agent``)."""
    if graph is None:
        return
    with _agent_holders_lock:
        _agent_holders[id(graph)] = _agent_holders.get(id(graph), 0) + 1


def release_agent(graph: Any) -> None:
    """Drop one live use; an orphaned, unheld graph is closed here.

    The graph still sitting in the cache slot is kept: it is not orphaned, the
    next same-loop caller reuses it.
    """
    if graph is None:
        return
    with _agent_holders_lock:
        key = id(graph)
        remaining = _agent_holders.get(key, 0) - 1
        if remaining > 0:
            _agent_holders[key] = remaining
            return
        _agent_holders.pop(key, None)
    if graph is _agent:
        return
    close_agent_graph(graph)


def close_agent_graph(graph: Any) -> None:
    """Close a graph's owned resources (its checkpointer connection), async-safe.

    Fail-open and never blocking: without a running loop there is nothing to
    schedule on (process teardown closes the fds anyway), and a checkpointer
    without ``aclose`` is left alone.
    """
    checkpointer = getattr(graph, "checkpointer", None)
    aclose = getattr(checkpointer, "aclose", None)
    if not callable(aclose):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(_aclose_checkpointer_quietly(checkpointer, aclose))
    _release_tasks.add(task)
    task.add_done_callback(_release_tasks.discard)


async def _aclose_checkpointer_quietly(checkpointer: Any, aclose: Any) -> None:
    try:
        await aclose()
    except Exception:
        logger.exception("failed to close a replaced agent graph's checkpointer")


@contextlib.asynccontextmanager
async def agent_lease(*, force_rebuild: bool = False):
    """A HELD graph for one bounded operation (``hold_agent``/``release_agent``).

    Every caller that uses a graph beyond the immediate call must lease it: a
    concurrent rebuild replaces the cached graph, and an unheld replaced graph is
    closed at once — which would pull the checkpointer out from under a live
    read.
    """
    graph = await built_agent(force_rebuild=force_rebuild)
    hold_agent(graph)
    try:
        yield graph
    finally:
        release_agent(graph)


def _agent_holder_count(graph: Any) -> int:
    with _agent_holders_lock:
        return _agent_holders.get(id(graph), 0)


def _assert_max_token() -> None:
    """MAX_TOKEN guard, runtime second line of defense.

    Even if the server booted with a valid .env, a mid-run edit that drops
    either value below 128K still refuses to build the graph. TokenGuardError
    propagates to the caller (server/service/messages.py surfaces it as a WS
    error chunk).
    """
    _main_raw = os.getenv("MAIN_LLM_MAX_TOKEN", "").strip()
    _aux_raw = os.getenv("AUXILIARY_LLM_MAX_TOKEN", "").strip()
    _main_val = int(_main_raw) if _main_raw else None
    _aux_val = int(_aux_raw) if _aux_raw else LLM_CLIENT_DEFAULTS["aux_remote_max_tokens"]
    assert_max_token_valid("MAIN_LLM_MAX_TOKEN", _main_val)
    assert_max_token_valid("AUXILIARY_LLM_MAX_TOKEN", _aux_val)


def _build_middlewares(
    *,
    fallback_chain: Any,
    auxiliary_llm: Any,
    main_llm_context_window: int,
    compression_trigger_ratio: float,
    temperature: float | None = None,
) -> list[Any]:
    """Assemble the middleware pipeline.

    ORDER IS A CONTRACT: LangChain executes ``after_model`` nodes in reverse
    registration order and ``wrap_model_call`` layers outermost-first, so the
    positions below are load-bearing. Pinned by
    ``tests/agent/core/test_middleware_order.py``.
    """
    drain_middleware = SubagentCompletionDrainMiddleware()
    return [
        # Todo-continuation: registered FIRST so its after_agent hook runs LAST —
        # after_agent hooks execute in REVERSE list order, so the first
        # registered middleware sits closest to END (README "Hook
        # Ordering Semantics"). It must observe the truly finished turn.
        TodoContinuationEnforcer(),
        # @dynamic_prompt middleware INSTANCE (not a constructor):
        # the outermost wrap_model_call layer in this list.
        system_prompt_injection,
        # Working-directory change notice: a before_agent node, so it runs once
        # per turn and BEFORE every before_model hook. The notice is spliced in
        # FRONT of the turn's human message (the one other middlewares rely on
        # staying last: TaskIntent's steering check, ContextEviction's
        # trailing-message tag), which is why it sits next to the prompt
        # injection — the prompt renders the CURRENT root, this layer explains
        # why it moved.
        WorkspaceNoticeMiddleware(),
        # Per-session tool set (预设-工具 tab): narrows request.tools before the
        # model is bound and refuses a disabled tool at execution. Registered as
        # a real middleware (not gated) because it HAS no switch of its own — it
        # is what applies the switch, and an unset config is a no-op.
        ToolSelectionMiddleware(),
        MultimodalProcessor(),
        IterationBudget(ITERATION_BUDGET["main_agent_max_iterations"]),
        ToolGuardrails(),
        # Registered directly after ToolGuardrails, i.e. OUTER relative
        # to PathGuard / HITL / MessagePersistenceMiddleware in the wrap
        # chain (first registered = outermost). MessagePersistence
        # stays innermost and persists the RAW tool result the moment
        # the handler returns; this layer then swaps in the preview on
        # the way out, so graph state only ever holds the preview while
        # MesMemory keeps the full text, read_file results are
        # sliced instead of offloaded, and its before_model hook
        # tags an oversized trailing HumanMessage after
        # MultimodalProcessor's before_agent ran (before_agent chain
        # precedes the model loop), and wrap_model_call truncates only
        # the model view — state keeps the full human text.
        ContextEvictionMiddleware(),
        ToolCallNormalize(),
        PathGuard(),
        drain_middleware,
        TaskIntentMiddleware(),
        OutputRepetitionGuard(),
        MaxTokensBoostMiddleware(),
        # Per-session thinking toggle: swaps the per-call model for the
        # thinking on/off variant when the client flag is set (inner relative
        # to MaxTokensBoost — the boost retries must re-apply on top of the
        # swapped model, and this layer only ever changes request.model).
        ThinkingControlMiddleware(temperature=temperature),
        HeartbeatStaleness(),
        HumanInTheLoop(HITLConfig()),
        # Registered directly after HITL so its after_model node runs
        # FIRST — langchain 1.3.9 chains after_model nodes in reverse
        # registration order (factory.py: model -> after_model[-1] ->
        # ... -> after_model[0]). The AI message is therefore persisted
        # before HITL strips denied tool calls or raises a
        # GraphInterrupt, and no other hook can skip the flush.
        MessagePersistenceMiddleware(),
        # Between HITL and Summarization: INNER relative to
        # MaxTokensBoost (it only sees genuine truncations) and OUTER
        # relative to Summarization (the retry loop wraps the
        # T4/T5 overflow recovery from outside).
        LLMRetryMiddleware(fallback_chain=fallback_chain),
        Summarization(
            need_update_system_prompt=True,
            model=auxiliary_llm,
            main_llm_context_window=main_llm_context_window,
            trigger=[("tokens", int(main_llm_context_window * compression_trigger_ratio))],
        ),
    ]


async def _build_graph(
    *,
    temperature: float,
    main_llm_context_window: int,
    compression_trigger_ratio: float,
) -> CompiledStateGraph:
    """Compile the agent graph and apply the wrapper chain."""
    checkpointer: ThreadSafeAsyncSqliteSaver = await build_async_sqlite_checkpointer()

    # create table before using
    await checkpointer.setup()

    # Delete all checkpoints but keeps the latest checkpoint
    await checkpointer.aclean_old_checkpoints()

    main_llm = build_main_llm()
    auxiliary_llm = build_auxiliary_llm()
    fallback_chain = build_fallback_chain()

    # Assemble the pipeline, then fail fast BEFORE the expensive create_agent()
    # build if a safety-critical middleware has been silently removed.
    agent_middleware = _build_middlewares(
        fallback_chain=fallback_chain,
        auxiliary_llm=auxiliary_llm,
        main_llm_context_window=main_llm_context_window,
        compression_trigger_ratio=compression_trigger_ratio,
        temperature=temperature,
    )
    validate_required_middleware(agent_middleware, chain="main", entries=_MAIN_REQUIRED)
    logger.debug(
        "_build_graph: {} middleware validated (chain=main), building the agent",
        len(agent_middleware),
    )

    # Build the agent
    compiled = create_agent(
        model=main_llm.bind(temperature=temperature),
        state_schema=StateSchema,
        checkpointer=checkpointer,
        tools=get_agent_tools(),
        middleware=agent_middleware,
    )
    # Wrap with the pluggable graph-wrapper chain (agent/wrapper/registry.py).
    # Defaults, innermost first:
    #
    # 1. RepetitionGuardWrapper: stream-level repetition
    # detection (in addition to the OutputRepetitionGuard middleware
    # registered above; it owns the stream seam end to end, and the
    # middleware exposes no separate stream helper).
    # phantom_stream_guard=True: the middleware-equipped graph ALWAYS
    # emits before_agent "updates" before any model text on fresh
    # dict-input runs — pre-update model text is physically impossible
    # stream output and historically triggered a false repetition cut
    # that suppressed the real reply.
    #
    # 2. ContextLimitGuardWrapper: context-window guard OUTSIDE the
    # repetition wrapper — the guard sees chunks before repetition
    # filtering, capturing real usage_metadata at model-call boundaries
    # and enforcing the mid-stream output budget.
    return apply_graph_wrappers(compiled)


async def built_agent(
    temperature: float = 0.8,
    force_rebuild: bool = False,
) -> ContextLimitGuardWrapper:
    """Return the compiled, wrapped agent bound to the current event loop.

    Thin orchestrator over the builder steps: token gate → per-loop cache
    check → ``_build_graph``.
    """
    global _agent, _agent_loop

    _assert_max_token()

    current_loop = asyncio.get_running_loop()

    # Rebuild whenever the loop changes, on first call, or when explicitly
    # requested (force_rebuild). Each rebuild constructs a fresh main_llm ->
    # httpx client bound to the CURRENT loop.
    #
    # Why force_rebuild: the WS server asks for a fresh transport pool every
    # turn — the same stale pooled connection as above, reached one turn later
    # rather than on a foreign loop. No other state lives in the graph object
    # (the checkpointer persists sessions independently), so a rebuild is safe
    # and cheap next to the LLM call it precedes.
    # The lock covers the check-and-build so two concurrent callers cannot both
    # build (and orphan one graph each); it is loop-safe because callers may sit
    # on different loops.
    async with _agent_lock:
        if _agent is not None and _agent_loop is current_loop and not force_rebuild:
            return _agent
        reason = (
            "first-call"
            if _agent is None
            else ("loop-change" if _agent_loop is not current_loop else "force_rebuild")
        )
        logger.info(
            "built_agent: rebuilding graph (reason={}, loop={:#x})",
            reason,
            id(current_loop),
        )
        previous = _agent
        _agent = await _build_graph(
            temperature=temperature,
            main_llm_context_window=main_llm_max_tokens,
            compression_trigger_ratio=COMPRESSION_TRIGGER_RATIO,
        )
        _agent_loop = current_loop
        logger.debug("built_agent: graph rebuilt and cached")

    # A replaced graph is closed as soon as nobody is using it: `release_agent`
    # of its last holder does the closing when this call leaves it held.
    if previous is not None and _agent_holder_count(previous) == 0:
        close_agent_graph(previous)

    return _agent
