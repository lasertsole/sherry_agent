import asyncio
from loguru import logger
from langgraph.runtime import Runtime
from langgraph.typing import ContextT
from typing import override
from context_engine import add_messages
from context_engine.store.core import get_max_turn_num
from typing import Any, cast
from collections.abc import Callable, Awaitable
from workspace.prompt_builder import build_system_prompt
from runtime import state_register_db, state_register_mem
from config.features import CONTEXT_ENGINE_HOOK
from .nudge import _nudge_memory, _nudge_plan_extraction, _PLAN_EXTRACTION_LOCK_KEY
from pub.func import sanitize_tool_use_result_pairing, slice_last_turn
from langchain.agents.middleware import AgentMiddleware, ModelResponse, ModelRequest
from langchain_core.messages import BaseMessage, AIMessage, ToolMessage, SystemMessage
from langchain.agents.middleware.types import ResponseT, ExtendedModelResponse, StateT
from agent.middlewares.base import require_session_id


# Nudge config keys
_NUDGE_MEMORY_COUNT_KEY = "nudge_review_memory_count"
_NUDGE_MEMORY_LOCK_KEY = "nudge_review_memory_lock"
_NUDGE_MEMORY_THRESHOLD = CONTEXT_ENGINE_HOOK["nudge_memory_threshold"]
_PLAN_EXTRACTION_FIRED_KEY = "nudge_plan_extraction_fired"
_PLAN_EXTRACTION_ENABLED = CONTEXT_ENGINE_HOOK["plan_extraction_enabled"]


def _reconcile_denials_for_persistence(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Re-attach denied tool calls so HITL denials survive persistence.

    ``HumanInTheLoop.after_model`` strips rejected tool calls from the AIMessage
    and appends the denial ``ToolMessage`` afterwards, which leaves the denial
    orphaned (no AIMessage carries its ``tool_call_id`` anymore).
    ``sanitize_tool_use_result_pairing`` drops orphaned ToolMessages, so the
    rejection would be lost from MesMemory history.

    This helper runs on the persistence slice only (never on graph state): for
    every error-status ToolMessage whose ``tool_call_id`` no longer appears in
    any AIMessage, the call is re-attached to the closest preceding AIMessage
    (args are unrecoverable after the strip, so ``{}`` is used). The restored
    pair then survives sanitization and is persisted as a normal tool row.
    """
    orphan_ids: set[str] = set()
    for msg in messages:
        if (
            isinstance(msg, ToolMessage)
            and getattr(msg, "status", "") == "error"
            and getattr(msg, "content", "")
        ):
            tc_id = getattr(msg, "tool_call_id", None)
            if isinstance(tc_id, str) and tc_id:
                orphan_ids.add(tc_id)

    if not orphan_ids:
        return messages

    for msg in messages:
        if isinstance(msg, AIMessage):
            for call in getattr(msg, "tool_calls", None) or []:
                call_id = call.get("id") if isinstance(call, dict) else None
                if isinstance(call_id, str):
                    orphan_ids.discard(call_id)

    if not orphan_ids:
        return messages

    out: list[BaseMessage] = []
    n = len(messages)
    for idx, msg in enumerate(messages):
        if isinstance(msg, AIMessage):
            # Peek at the following ToolMessage run (until the next AIMessage)
            # WITHOUT consuming it — every message is still appended below.
            attached: list[dict[str, Any]] = []
            j = idx + 1
            while j < n and not isinstance(messages[j], AIMessage):
                t = messages[j]
                if isinstance(t, ToolMessage):
                    tc_id = getattr(t, "tool_call_id", None)
                    if isinstance(tc_id, str) and tc_id in orphan_ids:
                        attached.append(
                            {
                                "name": getattr(t, "name", None) or "unknown",
                                "args": {},
                                "id": tc_id,
                                "type": "tool_call",
                            }
                        )
                j += 1

            if attached:
                existing = [
                    c for c in (getattr(msg, "tool_calls", None) or []) if isinstance(c, dict)
                ]
                existing_ids = {c.get("id") for c in existing}
                merged = existing + [c for c in attached if c["id"] not in existing_ids]
                msg = msg.model_copy(update={"tool_calls": merged})
                for c in attached:
                    orphan_ids.discard(c["id"])

        out.append(msg)
    return out


def _detect_todo_all_complete(session_id: str) -> bool:
    """Detect when the todo list just became all-complete (fire once).

    Conditions:
    1. todos exist (non-empty)
    2. every todo is ``completed`` or ``cancelled``
    3. plan extraction has not already fired for this completion cycle

    Returns True after setting ``_PLAN_EXTRACTION_FIRED_KEY`` so a later turn
    cannot fire the same completion again; the flag is reset to False whenever
    the todo list is not (or no longer) all-complete. Reads fail open — a
    broken todo store must never break the turn.
    """
    try:
        from agent.tools.todolist.registry.store_sqlite import get_todos_sync

        todos = get_todos_sync(session_id)
    except Exception:
        logger.exception("todo-complete detection failed (fail-open) for {}", session_id)
        return False

    if not todos:
        return False

    all_done = all(t.get("status") in ("completed", "cancelled") for t in todos)
    if not all_done:
        state_register_db.set_state(session_id, _PLAN_EXTRACTION_FIRED_KEY, False)
        return False

    already_fired = state_register_db.get_state(session_id, _PLAN_EXTRACTION_FIRED_KEY, False)
    if already_fired:
        return False

    state_register_db.set_state(session_id, _PLAN_EXTRACTION_FIRED_KEY, True)
    return True


_BACKGROUND_TASKS: set[asyncio.Task] = set()


async def _run_facts_pipeline(session_id: str, turn_num: int) -> None:
    """SESSION plan P2-3: dual-watermark facts extraction (fail-open).

    Skipped on plan-extraction turns: ``_nudge_plan_extraction`` absorbs the
    pending range with its own single LLM pass (the session-memory facts /
    plan-extraction merge decision), so the same turn never runs two extractors.
    """
    try:
        from context_engine.facts.queue import enqueue_turn, process_pending

        await enqueue_turn(session_id, turn_num)
        await process_pending(session_id)
    except Exception:
        logger.exception("facts pipeline failed (fail-open) for {}", session_id)


class ContextEngineHook(AgentMiddleware):
    def __init__(self):
        super().__init__()

    @staticmethod
    def _is_lock(session_id: str) -> bool:
        return state_register_mem.get_state(
            session_id, _NUDGE_MEMORY_LOCK_KEY, False
        ) or state_register_mem.get_state(session_id, _PLAN_EXTRACTION_LOCK_KEY, False)

    @staticmethod
    def _get_and_reload_system_prompt(session_id) -> str:
        system_prompt = state_register_mem.get_state(session_id, "system_prompt", None)

        if system_prompt is None:
            system_prompt = state_register_db.get_state(session_id, "system_prompt", None)

            if system_prompt is None:
                system_prompt = build_system_prompt(session_id=session_id)
                state_register_db.set_state(session_id, "system_prompt", system_prompt)

            state_register_mem.set_state(session_id, "system_prompt", system_prompt)

        return system_prompt

    # ------------------------------------------------------------------
    # Shared: session validation
    # ------------------------------------------------------------------
    @staticmethod
    def _get_session_id_or_raise(state: Any) -> str:
        return require_session_id(state, "Not pass session_id")

    # ------------------------------------------------------------------
    # Shared: system prompt injection (called by both sync and async)
    # ------------------------------------------------------------------
    def _wrap_model_call_impl(
        self,
        request: ModelRequest[ContextT],
    ) -> ModelRequest[ContextT]:
        """Inject system prompt into the request.

        Returns the (possibly overridden) request.
        """
        return request.override(
            system_message=SystemMessage(
                content=self._get_and_reload_system_prompt(
                    self._get_session_id_or_raise(request.state)
                )
            )
        )

    @override
    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        logger.debug("{} wrap_model_call hook fired", type(self).__name__)
        request = self._wrap_model_call_impl(request)
        return handler(request)

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        logger.debug("{} awrap_model_call hook fired", type(self).__name__)
        request = self._wrap_model_call_impl(request)
        return await handler(request)

    # ------------------------------------------------------------------
    # Shared: nudge logic (session validation + count management)
    # ------------------------------------------------------------------
    def _after_agent_impl(
        self, state: StateT
    ) -> tuple[str, str, list[BaseMessage], bool, bool] | None:
        """Validate session, advance the memory counter, and decide nudges.

        Returns
        ``(session_id, system_prompt, messages, need_memory, need_plan_extraction)``
        or None if the agent should bail early (nudge lock active).
        """
        session_id: str = state.get("session_id", "")
        if session_id.strip() == "":
            err_text: str = "Not pass session_id"
            logger.error(err_text)
            raise RuntimeError(err_text)

        messages: list[BaseMessage] = cast("list[BaseMessage]", state["messages"])
        system_prompt: str = self._get_and_reload_system_prompt(session_id)

        nudge_review_memory_count: int = (
            state_register_db.get_state(session_id, _NUDGE_MEMORY_COUNT_KEY, 0) + 1
        )

        # If nudge is locked, skip this turn
        if self._is_lock(session_id):
            state_register_db.set_state(
                session_id, _NUDGE_MEMORY_COUNT_KEY, nudge_review_memory_count
            )
            return None

        need_nudge_review_memory: bool = nudge_review_memory_count >= _NUDGE_MEMORY_THRESHOLD
        need_plan_extraction: bool = _PLAN_EXTRACTION_ENABLED and _detect_todo_all_complete(
            session_id
        )

        logger.debug(
            "nudge_review_memory_count is {}, need_nudge_review_memory is {}",
            nudge_review_memory_count,
            need_nudge_review_memory,
        )
        logger.debug("need_plan_extraction is {}", need_plan_extraction)

        if need_nudge_review_memory:
            state_register_db.set_state(session_id, _NUDGE_MEMORY_COUNT_KEY, 0)
        else:
            state_register_db.set_state(
                session_id, _NUDGE_MEMORY_COUNT_KEY, nudge_review_memory_count
            )

        return (
            session_id,
            system_prompt,
            messages,
            need_nudge_review_memory,
            need_plan_extraction,
        )

    @override
    def after_agent(self, state: StateT, runtime: Runtime[ContextT]) -> dict[str, Any] | None:
        """Sync protocol hook — nudge is dispatched ONLY by ``aafter_agent``.

        LangChain 1.3 builds the after_agent graph node as
        ``RunnableCallable(sync_after_agent, async_after_agent)``: ``ainvoke``
        (all production consumers: the WS/REST stream, auto-turn and every
        child agent) calls the async override, while sync ``invoke`` calls this
        method. The sync path previously dispatched the nudge agents through
        ``run_async()`` — a NEW thread + NEW event loop — which cannot acquire
        the event-loop-bound NUDGE lane semaphore without a cross-loop rebind.
        Since no production caller invokes the main graph synchronously, that
        dispatch is dead code; the counter/lock bookkeeping in
        ``_after_agent_impl`` is kept so the sync hook stays protocol-complete.
        """
        logger.debug("{} after_agent hook fired", type(self).__name__)
        self._after_agent_impl(state)
        return None

    @override
    async def aafter_agent(
        self, state: StateT, runtime: Runtime[ContextT]
    ) -> dict[str, Any] | None:
        logger.debug("{} aafter_agent hook fired", type(self).__name__)
        result = self._after_agent_impl(state)
        if result is None:
            return None

        session_id, system_prompt, messages, need_memory, need_plan_extraction = result

        # Persist last turn messages to MesMemory
        all_messages: list[BaseMessage] = cast("list[BaseMessage]", state["messages"])
        last_turn_messages: list[BaseMessage] = slice_last_turn(all_messages)["messages"]
        # HITL rejections leave the denial ToolMessage orphaned (after_model
        # strips the rejected tool_call); re-pair it so it survives sanitization
        # and is persisted. Persistence copies only — graph state is untouched.
        last_turn_messages = _reconcile_denials_for_persistence(last_turn_messages)
        format_last_turn_messages: list[BaseMessage] = sanitize_tool_use_result_pairing(
            last_turn_messages
        )

        # Sanitize the full message list before feeding any nudge sub-agent. The
        # persist path above already sanitizes its slice; the nudge agents build
        # their own LLM input from the raw `messages` list, so any orphaned
        # ToolMessage (e.g. produced by a HITL reject) would be passed straight
        # to the LLM and trigger a LangChain 400
        # ("Messages with role 'tool' must be a response to a preceding message").
        # Stripping orphans here keeps the nudge path consistent with persist.
        nudge_messages: list[BaseMessage] = sanitize_tool_use_result_pairing(messages)

        # Run persistence and nudge concurrently
        async def _persist() -> None:
            await add_messages(session_id=session_id, messages=format_last_turn_messages)

        async def _nudge() -> None:
            if need_memory:
                await _nudge_memory(session_id, system_prompt, nudge_messages)
            if need_plan_extraction:
                await _nudge_plan_extraction(session_id, system_prompt, nudge_messages)

        await asyncio.gather(_persist(), _nudge())

        # SESSION plan P2-3: enqueue the persisted turn for facts extraction
        # and consume pending ranges. Fire-and-forget — extraction never
        # blocks or breaks the turn (fail-open like every background hook).
        #
        # Facts yield (confirmed decision #2): on a plan-extraction turn the
        # dedicated pipeline is NOT started — _nudge_plan_extraction absorbs the
        # pending range with its single LLM pass and advances the consumed
        # watermark. Turns not yet enqueued are replayed by the next
        # non-plan-extraction turn (the dual watermark is crash-safe), so no
        # interval is lost and no turn ever runs two extractors.
        turn_num = get_max_turn_num(session_id)
        if turn_num > 0 and not need_plan_extraction:
            facts_task = asyncio.create_task(_run_facts_pipeline(session_id, turn_num))
            _BACKGROUND_TASKS.add(facts_task)
            facts_task.add_done_callback(_BACKGROUND_TASKS.discard)

        return None
