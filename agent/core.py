from skills import build_skills_snapshot
from langchain_core.tools import BaseTool
from langchain.agents import create_agent
from langchain.agents.middleware import AgentState
from langgraph.graph.state import CompiledStateGraph
from models import build_main_llm, build_auxiliary_llm
from agent.checkpointer import build_async_sqlite_checkpointer
from models.LLMs.main_llm import max_tokens as main_llm_max_tokens
from config.num import COMPRESSION_TRIGGER_RATIO
from agent.tools import memory_store, build_main_tools
from .checkpointer.thread_safe_checkpointer import ThreadSafeAsyncSqliteSaver
from .middlewares import (
    Summarization,
    ToolCallNormalize,
    MultimodalProcessor,
    ContextEngineHook,
    ToolGuardrails,
    IterationBudget,
    HeartbeatStaleness,
    OutputRepetitionGuard
)
from .middlewares.humanInTheLoop import HumanInTheLoop, HITLConfig
from .middlewares.subagent_completion_drain import SubagentCompletionDrainMiddleware
from .stream_repetition_guard_wrapper import RepetitionGuardWrapper

# # 只有幂等的工具才能并行执行，非幂等串行执行
# patch_tool_node()

# ── Extended state schema ────────────────────────────────────────────────
# Carries ``session_id`` through the graph so that middlewares reading
# ``request.state["session_id"]`` is used by middlewares that need it


class StateSchema(AgentState):
    """Agent state that preserves an ``session_id``."""

    session_id: str


# ── Initialization (explicit, idempotent) ────────────────────────────────
# These three steps used to run at module import time, which made any bare
# ``import agent.core`` (tests, tooling, type checkers) trigger disk I/O and
# tool construction unexpectedly and slowly (AUDIT_REPORT item 26). They now
# live in ``init()``, called once by the service entry point
# (``server/__main__.py``) — importing this module is side-effect-free.

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

    Idempotent: subsequent calls are no-ops.
    """
    global _tools, _initialized
    if _initialized:
        return

    build_skills_snapshot()
    memory_store.load_from_disk()
    _tools = build_main_tools()
    _initialized = True


def get_agent_tools() -> list[BaseTool]:
    return _tools


# Cache of compiled agents, keyed by the asyncio event loop that they were
# built on. Each entry holds a fresh main_llm whose internal openai.AsyncOpenAI
# -> httpx.AsyncClient transport pool is bound to that specific loop.
#
# Why loop-keyed: the previous module-level `_agent: CompiledStateGraph | None`
# singleton embedded ONE loop-bound httpx transport pool and reused it across WS
# turns (and across the subagent daemon thread). Connections in that stale pool
# silently died mid-request on the WS server, surfacing as
# openai.APITimeoutError("Request timed out") at ~17s even though the SDK's
# default 600s deadline had not elapsed (the request is killed by the dead
# pooled connection, not a normal timeout). Verified by a standalone stream with
# the exact same payload (12KB system prompt + full tools schema) that COMPLETED
# IN 8.0s on a fresh in-loop client, while the cached-pool WS path failed.
#
# Keying by loop gives identical behaviour to calling build_main_llm() fresh for
# the current loop (the codebase-wide convention), but still reuses the compiled
# graph for subsequent requests on the same loop to avoid rebuilding it.
_agent: CompiledStateGraph | None = None
_agent_loop = None


async def built_agent(
    temperature: float = 0.8,
    force_rebuild: bool = False,
) -> CompiledStateGraph:
    global _agent, _agent_loop
    import asyncio

    current_loop = asyncio.get_running_loop()

    # Rebuild whenever the loop changes, on first call, or when explicitly
    # requested (force_rebuild). Each rebuild constructs a fresh main_llm ->
    # httpx client bound to the CURRENT loop.
    #
    # Why force_rebuild: the WS server needs a FRESH transport pool every turn.
    # Over many turns on a single event loop the long-lived pooled TCP connection
    # goes stale — DeepSeek's edge reaps an idle keep-alive connection (~15-17s)
    # and the next streaming POST on it dies mid-request, surfacing as
    # ``openai.APITimeoutError("Request timed out")`` far under the SDK deadline.
    # Provably: the exact same payload (12KB system prompt + full tools schema)
    # streams in 8.0s on a fresh in-loop client, while a cached-pool WS call dies
    # at ~16.78s. Closing the pool (AsyncOpenAI.close()) does NOT help — it
    # permanently destroys the client ("Cannot send a request, as the client has
    # been closed"), so rebuilding the graph (hence a fresh client) per turn is
    # the only clean way to reproduce the fresh-client condition. The SQLite
    # checkpointer persists session state independently of the graph object, so a
    # rebuild is safe and cheap relative to the 15-20s LLM call.
    if _agent is None or _agent_loop is not current_loop or force_rebuild:
        checkpointer: ThreadSafeAsyncSqliteSaver = await build_async_sqlite_checkpointer()

        # create table before using
        await checkpointer.setup()

        # Delete all checkpoints but keeps the latest checkpoint
        await checkpointer.aclean_old_checkpoints()

        main_llm = build_main_llm()
        auxiliary_llm = build_auxiliary_llm()

        # Build the agent
        _agent = create_agent(
            model=main_llm.bind(temperature=temperature),
            state_schema=StateSchema,
            checkpointer=checkpointer,
            tools=get_agent_tools(),
            middleware=[
                ContextEngineHook(),
                MultimodalProcessor(),
                IterationBudget(90),
                ToolGuardrails(),
                ToolCallNormalize(),
                SubagentCompletionDrainMiddleware(),
                OutputRepetitionGuard(),
                HeartbeatStaleness(),
                HumanInTheLoop(HITLConfig()),
                Summarization(
                    need_update_system_prompt=True,
                    model=auxiliary_llm,
                    main_llm_context_window=main_llm_max_tokens,
                    trigger=[("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO))],
                    keep=("messages", 10),
                ),
            ],
        )
        # Wrap with RepetitionGuardWrapper for stream-level repetition
        # detection (replaces both the OutputRepetitionGuard middleware and
        # the check_stream_repetition calls in messages.py).
        # phantom_stream_guard=True: the middleware-equipped graph ALWAYS
        # emits before_agent "updates" before any model text on fresh
        # dict-input runs — pre-update model text is physically impossible
        # stream output and historically triggered a false repetition cut
        # that suppressed the real reply.
        _agent = RepetitionGuardWrapper(_agent, phantom_stream_guard=True)
        _agent_loop = current_loop

    return _agent
