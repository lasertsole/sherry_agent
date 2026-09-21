"""Context compaction middleware (summarization).

This module is the composition root. The middleware class owns the lifecycle
hooks, the session-validation / token-estimation helpers, the durable cooldown
mirror and the thin seams that bind process-level dependencies (system-prompt
rebuild, task-intent re-arm, nudge scheduling, active-plan / taskflow context)
for the responsibility mixins in :mod:`summarization.compression`,
:mod:`summarization.overflow`, :mod:`summarization.summary_generation` and
:mod:`summarization.thrash`.

Behavior and state keys are unchanged from the pre-split module; the
module-level names below are re-exported from the mixin modules so existing
``from agent.middlewares.summarization.core import ...`` imports keep working.
"""

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from langchain.agents import AgentState
from langchain.agents.middleware import (
    AgentMiddleware,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langchain.agents.middleware.types import ResponseT
from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, SystemMessage
from langgraph.runtime import Runtime
from langgraph.typing import ContextT
from loguru import logger

from config.features import SUMMARIZATION
from pub.func.message.estimate_msg_tokens import estimate_msg_tokens, estimate_messages_tokens
from pub.func.message.overflow_router import ROUTE_COMPACT_ONLY, ROUTE_FITS
from runtime import StateKey, state_register_db, state_register_mem
from workspace.prompt_builder import build_system_prompt

from .compression import CompressionMixin
from .overflow import OverflowMixin, extract_reported_input_tokens as extract_reported_input_tokens
from .plan_context import render_plan_context, resolve_active_plan
from .state_aliases import (
    _COMPRESSION_COUNT_KEY as _COMPRESSION_COUNT_KEY,
    _COMPRESSION_INEFFECTIVE_KEY as _COMPRESSION_INEFFECTIVE_KEY,
    _COMPRESSION_LAST_TOKENS_KEY as _COMPRESSION_LAST_TOKENS_KEY,
    _COOLDOWN_ROUNDS_KEY,
    _DEGRADATION_NO_TEXT_KEY as _DEGRADATION_NO_TEXT_KEY,
    _FORCE_RECOVERY_KEY,
    _LAST_STRATEGY_KEY as _LAST_STRATEGY_KEY,
    _LAST_USER_QUESTION_KEY as _LAST_USER_QUESTION_KEY,
    _OVERFLOW_RETRIES_KEY as _OVERFLOW_RETRIES_KEY,
    _PREVIOUS_FILE_OPS_KEY as _PREVIOUS_FILE_OPS_KEY,
    _RECOVERY_ATTEMPTS_KEY as _RECOVERY_ATTEMPTS_KEY,
    _SKIP_LLM_KEY as _SKIP_LLM_KEY,
    _SUMMARY_LC_SOURCE as _SUMMARY_LC_SOURCE,
    _TURN_ATTEMPTS_KEY,
)
from .summarization_components import (
    CompressionEffectivenessTracker,
    MessageTruncator,
    OrphanPairRepairer,
)
from .summary_doc import SummaryDoc as SummaryDoc, cap_summary_doc as cap_summary_doc
from .summary_generation import (
    SummaryGenerationMixin,
    _SUMMARY_CLOSE_TAG as _SUMMARY_CLOSE_TAG,
    _SUMMARY_JSON_RULES as _SUMMARY_JSON_RULES,
    _SUMMARY_OPEN_TAG as _SUMMARY_OPEN_TAG,
    _SUMMARY_PREFIX as _SUMMARY_PREFIX,
    _SUMMARY_PROMPT_FIRST as _SUMMARY_PROMPT_FIRST,
    _SUMMARY_PROMPT_FIRST_STRUCTURED as _SUMMARY_PROMPT_FIRST_STRUCTURED,
    _SUMMARY_PROMPT_UPDATE as _SUMMARY_PROMPT_UPDATE,
    _SUMMARY_PROMPT_UPDATE_STRUCTURED as _SUMMARY_PROMPT_UPDATE_STRUCTURED,
    _SUMMARY_SUFFIX as _SUMMARY_SUFFIX,
    _SUMMARY_TEMPLATE as _SUMMARY_TEMPLATE,
    _SUMMARY_UPDATE_INSTRUCTIONS as _SUMMARY_UPDATE_INSTRUCTIONS,
    _SUMMARY_UPDATE_INSTRUCTIONS_STRUCTURED as _SUMMARY_UPDATE_INSTRUCTIONS_STRUCTURED,
    _build_static_fallback_summary as _build_static_fallback_summary,
    _extract_file_operations as _extract_file_operations,
    _filter_summary_messages as _filter_summary_messages,
    _format_file_ops as _format_file_ops,
    _parse_file_ops_from_summary as _parse_file_ops_from_summary,
    _serialize_for_summary as _serialize_for_summary,
)
from .thrash import ThrashMixin

# Constants used by the retained hooks / cooldown mirror. Kept bound here
# (not imported from a mixin) because they were module-level before the split.
COMPACTION_COOLDOWN_ROUNDS = SUMMARIZATION["compaction_cooldown_rounds"]
MAX_COMPRESS_ATTEMPTS_PER_TURN = SUMMARIZATION["max_compress_attempts_per_turn"]

# Anti-thrash keys that must survive a process restart. The
# CompressionEffectivenessTracker mutates these in ``state_register_mem``
# only; this module mirrors the current values into the SQLite-backed
# ``state_register_db`` and rehydrates them on first access after a restart.
_COOLDOWN_PERSIST_KEYS: tuple[str, ...] = (
    _SKIP_LLM_KEY,
    _COMPRESSION_COUNT_KEY,
    _COMPRESSION_INEFFECTIVE_KEY,
    _COOLDOWN_ROUNDS_KEY,
)

# Sessions already rehydrated in THIS process. A restart resets this set,
# which is exactly the "first access for this session in a new process"
# condition the restore guard needs.
_RESTORED_COOLDOWN_SESSIONS: set[str] = set()


def _rearm_task_intent_after_compact(session_id: str) -> None:
    """Re-arm the task-intent steering after a successful system-prompt rebuild (fail-open).

    Compression drops the already-injected steering context, so the next turn
    must receive the full directive rather than the short reminder.
    """
    try:
        from agent.middlewares.task_intent.core import rearm_after_compact

        rearm_after_compact(session_id)
    except Exception:
        logger.debug("Task-intent re-arm after compact failed for session {}", session_id)


def _get_taskflow_context_sync(session_id: str) -> str:
    """Render this session's active TaskFlow state for the summary prompt.

    Reuses the sync registry read, already scoped to ``session_id`` by SQL.
    Returns "" — never raises — so an unavailable TaskFlow store cannot block
    compression.
    """
    try:
        from agent.tools.taskflow.registry import store_sqlite as taskflow_store
        from agent.tools.taskflow.tools._shared import step_status, steps_summary

        mine = taskflow_store.get_active_flows_sync(session_id)
        if not mine:
            return ""

        lines = ["## Current TaskFlow State (authoritative)"]
        for flow in mine[:3]:
            state = flow.get("state") or {}
            steps = state.get("steps") or []
            counts = steps_summary(steps)
            total = len(steps)
            done = counts.get("done", 0)
            desc = (state.get("description") or "")[:60]

            lines.append(f"### {flow['flow_id']} ({flow['status']})")
            lines.append(f"Desc: {desc}")
            lines.append(
                f"Progress: {done}/{total} done · "
                + " · ".join(
                    f"{s}={counts.get(s, 0)}" for s in ("done", "dispatched", "ready", "blocked")
                )
            )

            # Most recent 2 completed steps, then the first 2 pending ones.
            done_steps = [step for step in steps if step_status(step) == "done"]
            for step in done_steps[-2:]:
                lines.append(f"  ✓ {(step.get('task') or '')[:60]}")

            pending = [step for step in steps if step_status(step) != "done"]
            icons = {"dispatched": "→", "ready": "○", "blocked": "⊘"}
            for step in pending[:2]:
                status = step_status(step)
                icon = icons.get(status, "?")
                lines.append(f"  {icon} {(step.get('task') or '')[:60]}")

            wait = flow.get("wait") or {}
            if wait:
                lines.append(f"Waiting: {wait.get('reason', 'unknown')}")

        return "\n".join(lines)
    except Exception:
        logger.debug("taskflow context unavailable for session {}", session_id)
        return ""


def _get_plan_context_sync(session_id: str) -> str:
    """Render this session's active plan for the summary prompt.

    The plan file path (relative to the repo), the plan name and the open-todo
    summary are injected — never the plan body (the model can ``read_file`` the
    path). Returns "" — never raises — so a broken plan/todo store cannot block
    compression."""
    try:
        plan = resolve_active_plan(session_id)
        if plan is None:
            return ""
        return render_plan_context(plan)
    except Exception:
        logger.debug("plan context unavailable for session {}", session_id)
        return ""


def _schedule_compression_todo_update(session_id: str, discarded_messages: Sequence[Any]) -> None:
    """Fire-and-forget the post-compression todo update (never blocks/raises).

    Call-time import keeps the nudges module's heavier import graph out of
    ``summarization.core``; the scheduler itself gates (feature switch,
    non-empty todo list, per-session lock) and skips when no event loop is
    running (sync compression path).
    """
    try:
        from agent.middlewares.summarization.nudges import schedule_compression_todo_update

        schedule_compression_todo_update(session_id, discarded_messages)
    except Exception:
        logger.exception("compression todo update scheduling failed (fail-open)")


def _schedule_compression_nudges(session_id: str, messages: Sequence[Any]) -> None:
    """Fire-and-forget the compression-time nudge decision (never blocks/raises).

    Memory review and plan extraction moved here from the middleware's
    after-agent hook:
    the scheduler dispatches the memory review on every compression, evaluates
    the plan-extraction single-fire flag, and dispatches under the NUDGE lane
    when no nudge is already in flight. Call-time import keeps the nudges
    module out of the summarization module-load graph.
    """
    try:
        from agent.middlewares.summarization.nudges import schedule_compression_nudges

        schedule_compression_nudges(session_id, messages)
    except Exception:
        logger.exception("compression nudge scheduling failed (fail-open)")


# Message persistence no longer runs here: every model boundary flushes new
# messages to MesMemory via ``MessagePersistenceMiddleware`` (registered in
# ``agent/core.py``), so the compression path only compacts and schedules the
# compression-time nudges below.


class Summarization(
    SummaryGenerationMixin,
    ThrashMixin,
    OverflowMixin,
    CompressionMixin,
    AgentMiddleware,
):
    """Context compaction middleware — written from scratch.

    Does NOT inherit from SummarizationMiddleware. All compression logic
    is self-contained: trigger checking, cutoff determination, summary
    generation, multi-strategy pipeline, degradation monitoring.

    Post-compression format: HumanMessage("What did we do so far?") +
    AIMessage(summary, lc_source="summarization") pair. No consecutive
    same-role messages, no _fix_consecutive_human_messages needed.
    """

    def __init__(
        self,
        model,
        trigger: list | None = None,
        keep: tuple = ("messages", 10),
        main_llm_context_window: int | None = None,
        need_update_system_prompt: bool = False,
        memory_store: Any = None,
        llm_factory: Any = None,
        **kwargs,
    ):
        self._model = model
        self._trigger = trigger or [("tokens", 80_000)]
        self._keep = keep
        self._main_llm_context_window = main_llm_context_window
        self._need_update_system_prompt = need_update_system_prompt
        self._compress_last_turn: bool = False
        self._compaction_just_happened: bool = False
        self._memory_store = memory_store
        self._llm_factory = llm_factory

        self._effectiveness_tracker = CompressionEffectivenessTracker(self._estimate_tokens)
        self._truncator = MessageTruncator()
        self._orphan_repairer = OrphanPairRepairer()

    # ------------------------------------------------------------------
    # Session validation
    # ------------------------------------------------------------------

    @staticmethod
    def _get_session_or_raise(state: AgentState) -> str:
        session_id: str = state.get("session_id", "")
        if session_id.strip() == "":
            err_text = "Not pass session_id"
            logger.error(err_text)
            raise RuntimeError(err_text)
        return session_id

    # ------------------------------------------------------------------
    # Token estimation
    # ------------------------------------------------------------------

    @staticmethod
    def _estimate_msg_tokens(msg: BaseMessage) -> int:
        return estimate_msg_tokens(msg)

    def _estimate_tokens(self, messages: Sequence[BaseMessage]) -> int:
        return estimate_messages_tokens(list(messages))

    # ------------------------------------------------------------------
    # Process-level seams for the responsibility mixins
    # ------------------------------------------------------------------

    def _taskflow_context(self, session_id: str) -> str:
        """Seam bound to the module-level taskflow renderer (patchable)."""
        return _get_taskflow_context_sync(session_id)

    def _plan_context(self, session_id: str) -> str:
        """Seam bound to the module-level plan renderer (patchable)."""
        return _get_plan_context_sync(session_id)

    @staticmethod
    def _resolve_active_plan(session_id: str):
        """Seam bound to ``plan_context.resolve_active_plan`` (patchable)."""
        return resolve_active_plan(session_id)

    def _fire_compression_nudges(
        self,
        session_id: str,
        original_messages,
        messages_to_summarize,
    ) -> None:
        """Dispatch the compression-time nudges through the core module globals."""
        _schedule_compression_nudges(session_id, original_messages)
        _schedule_compression_todo_update(session_id, messages_to_summarize)

    def _rebuild_system_prompt(self, session_id: str) -> str:
        """Reload the memory store and rebuild the system prompt."""
        from agent.tools import memory_store

        memory_store.load_from_disk()
        return build_system_prompt(session_id=session_id)

    def _persist_system_prompt(self, session_id: str, system_prompt: str) -> None:
        """Dual-write the rebuilt prompt and re-arm task-intent steering."""
        state_register_mem.set_state(session_id, StateKey.SYSTEM_PROMPT, system_prompt)
        state_register_db.set_state(session_id, StateKey.SYSTEM_PROMPT, system_prompt)
        _rearm_task_intent_after_compact(session_id)

    # ------------------------------------------------------------------
    # Durable cooldown mirror (state_register_db seam lives here)
    # ------------------------------------------------------------------

    def _record_compaction_bookkeeping(self, session_id: str) -> None:
        """After an ACTUAL compression: arm the cooldown, count the turn attempt."""
        state_register_mem.set_state(session_id, _COOLDOWN_ROUNDS_KEY, COMPACTION_COOLDOWN_ROUNDS)
        attempts = state_register_mem.get_state(session_id, _TURN_ATTEMPTS_KEY, 0) + 1
        state_register_mem.set_state(session_id, _TURN_ATTEMPTS_KEY, attempts)
        # The armed cooldown must survive a restart, or a fresh process
        # immediately re-attempts the compression that just happened. Persist
        # ONLY the rounds key here — the effectiveness-count keys keep their
        # original "persisted on the degraded path only" semantics, while a
        # blanket _persist_cooldown_state() would drag those keys along and
        # leak stale counters across a turn reset.
        state_register_db.set_state(session_id, _COOLDOWN_ROUNDS_KEY, COMPACTION_COOLDOWN_ROUNDS)

    def _persist_cooldown_state(self, session_id: str) -> None:
        for key in _COOLDOWN_PERSIST_KEYS:
            value = state_register_mem.get_state(session_id, key)
            if value is not None:
                state_register_db.set_state(session_id, key, value)

    def _restore_cooldown_state(self, session_id: str) -> None:
        for key in _COOLDOWN_PERSIST_KEYS:
            value = state_register_db.get_state(session_id, key)
            if value is not None:
                state_register_mem.set_state(session_id, key, value)

    def _maybe_restore_cooldown_state(self, session_id: str) -> None:
        if session_id in _RESTORED_COOLDOWN_SESSIONS:
            return
        _RESTORED_COOLDOWN_SESSIONS.add(session_id)
        self._restore_cooldown_state(session_id)

    # ------------------------------------------------------------------
    # before_agent: reset state
    # ------------------------------------------------------------------

    def _before_agent_impl(self, state: AgentState) -> dict[str, Any] | None:
        session_id = state.get("session_id", "")
        if session_id.strip():
            self._reset_turn_state(session_id)
            self._maybe_restore_cooldown_state(session_id)
            # T1 PREFLIGHT: budget-truncate / compact the OVERFLOWED history
            # before the turn starts (4-route decision, truncate track is
            # always allowed; compact routes are cooldown-gated).
            return self._t1_preflight(state, session_id)
        return None

    async def _abefore_agent_impl(self, state: AgentState) -> dict[str, Any] | None:
        session_id = state.get("session_id", "")
        if session_id.strip():
            self._reset_turn_state(session_id)
            self._maybe_restore_cooldown_state(session_id)
            return await self._at1_preflight(state, session_id)
        return None

    def before_agent(self, state: AgentState, runtime: Runtime[ContextT]) -> dict[str, Any] | None:
        logger.debug("Compaction before_agent hook fired")
        return self._before_agent_impl(state)

    async def abefore_agent(
        self, state: AgentState, runtime: Runtime[ContextT]
    ) -> dict[str, Any] | None:
        logger.debug("Compaction abefore_agent hook fired")
        return await self._abefore_agent_impl(state)

    # ------------------------------------------------------------------
    # wrap_model_call (sync)
    # ------------------------------------------------------------------

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        logger.debug("Compaction wrap_model_call hook fired")
        session_id = self._get_session_or_raise(request.state)
        messages: list[AnyMessage] = request.state.get("messages", [])
        self._check_last_turn_ratio(messages, session_id)

        # T2 anti-thrash bookkeeping (EVERY call): tick the cooldown down
        # before anything else. The force flag is read BEFORE the skip check
        # because _should_skip_compression consumes it.
        forced = state_register_mem.get_state(session_id, _FORCE_RECOVERY_KEY, False)
        cooldown_active = self._tick_cooldown(session_id)

        if self._should_skip_compression(session_id):
            self._compress_last_turn = False
            self._compaction_just_happened = False
            response = self._execute_with_recovery(request, handler, session_id)
            self._monitor_degradation(response, session_id)
            return response

        attempts = state_register_mem.get_state(session_id, _TURN_ATTEMPTS_KEY, 0)
        if not forced and (cooldown_active or attempts >= MAX_COMPRESS_ATTEMPTS_PER_TURN):
            # T2 anti-thrash gate: cooldown / per-turn attempt cap suppress
            # the PROACTIVE trigger only (forced recovery is exempt above).
            self._compress_last_turn = False
            if self._compaction_just_happened:
                # T1 compacted earlier this turn: a second compression is
                # exactly the thrash the cooldown prevents, but the rebuilt
                # system prompt must still reach the model (chains without the
                # @dynamic_prompt middleware rely on this one delivering it),
                # and the response still needs degradation monitoring — the
                # flag is left for _monitor_degradation to consume. Identical
                # content is left untouched (no override, no new SystemMessage).
                if self._need_update_system_prompt:
                    rebuilt = state_register_mem.get_state(session_id, StateKey.SYSTEM_PROMPT, "")
                    if rebuilt:
                        existing = request.system_message
                        content_matches = (
                            isinstance(existing, SystemMessage) and existing.content == rebuilt
                        )
                        if not content_matches:
                            request = request.override(
                                system_message=SystemMessage(content=rebuilt)
                            )
            response = self._execute_with_recovery(request, handler, session_id)
            self._monitor_degradation(response, session_id)
            # T3 post-response re-check. Gate path: T2 did NOT
            # dispatch here, so t2_compressed=False — the check re-reads the
            # anti-thrash state itself (post-tick cooldown still > 0 blocks).
            return self._post_response_check(request, response, session_id)

        # T3 anti-double-compress snapshot: turn attempts BEFORE the
        # T2 dispatch; only actual compact executions increment the key, so a
        # bump means T2 compressed in THIS wrap call.
        t2_attempts_before = state_register_mem.get_state(session_id, _TURN_ATTEMPTS_KEY, 0)
        # 4-route decision (upgraded _preemptive_check) → single dispatch
        route = self._decide_overflow_route(messages, session_id)
        if route is not None and route != ROUTE_FITS:
            request = self._dispatch_overflow_route(request, route, session_id, trigger="T2")
        elif self._check_trigger(request.state.get("messages", [])):
            # legacy trigger-clause fallback (e.g. ("messages", N) triggers).
            # P1-2: the tail clip must NOT bypass an explicit trigger-clause
            # compaction — that is a length/token mandate, not pressure
            # recovery — so this path goes straight to the compact executor.
            request = self._execute_compact(request, ROUTE_COMPACT_ONLY, session_id, trigger="T2")

        response = self._execute_with_recovery(request, handler, session_id)
        self._monitor_degradation(response, session_id)
        t2_compressed = (
            state_register_mem.get_state(session_id, _TURN_ATTEMPTS_KEY, 0) > t2_attempts_before
        )
        # T3: post-response real-token re-check; T2-compressed calls
        # skip via the local flag — one compression per model call.
        return self._post_response_check(request, response, session_id, t2_compressed=t2_compressed)

    # ------------------------------------------------------------------
    # awrap_model_call (async)
    # ------------------------------------------------------------------

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        logger.debug("Compaction awrap_model_call hook fired")
        session_id = self._get_session_or_raise(request.state)
        messages: list[AnyMessage] = request.state.get("messages", [])
        self._check_last_turn_ratio(messages, session_id)

        # T2 anti-thrash bookkeeping (EVERY call): tick the cooldown down
        # before anything else. The force flag is read BEFORE the skip check
        # because _should_skip_compression consumes it.
        forced = state_register_mem.get_state(session_id, _FORCE_RECOVERY_KEY, False)
        cooldown_active = self._tick_cooldown(session_id)

        if self._should_skip_compression(session_id):
            self._compress_last_turn = False
            self._compaction_just_happened = False
            response = await self._aexecute_with_recovery(request, handler, session_id)
            self._monitor_degradation(response, session_id)
            return response

        attempts = state_register_mem.get_state(session_id, _TURN_ATTEMPTS_KEY, 0)
        if not forced and (cooldown_active or attempts >= MAX_COMPRESS_ATTEMPTS_PER_TURN):
            # T2 anti-thrash gate: cooldown / per-turn attempt cap suppress
            # the PROACTIVE trigger only (forced recovery is exempt above).
            self._compress_last_turn = False
            if self._compaction_just_happened:
                # T1 compacted earlier this turn: a second compression is
                # exactly the thrash the cooldown prevents, but the rebuilt
                # system prompt must still reach the model (chains without the
                # @dynamic_prompt middleware rely on this one delivering it),
                # and the response still needs degradation monitoring — the
                # flag is left for _monitor_degradation to consume. Identical
                # content is left untouched (no override, no new SystemMessage).
                if self._need_update_system_prompt:
                    rebuilt = state_register_mem.get_state(session_id, StateKey.SYSTEM_PROMPT, "")
                    if rebuilt:
                        existing = request.system_message
                        content_matches = (
                            isinstance(existing, SystemMessage) and existing.content == rebuilt
                        )
                        if not content_matches:
                            request = request.override(
                                system_message=SystemMessage(content=rebuilt)
                            )
            response = await self._aexecute_with_recovery(request, handler, session_id)
            self._monitor_degradation(response, session_id)
            # T3 post-response re-check; see the sync twin.
            return await self._apost_response_check(request, response, session_id)

        # T3 anti-double-compress snapshot: turn attempts BEFORE the
        # T2 dispatch; only actual compact executions increment the key, so a
        # bump means T2 compressed in THIS wrap call.
        t2_attempts_before = state_register_mem.get_state(session_id, _TURN_ATTEMPTS_KEY, 0)
        # 4-route decision (upgraded _preemptive_check) → single dispatch
        route = self._decide_overflow_route(messages, session_id)
        if route is not None and route != ROUTE_FITS:
            request = await self._adispatch_overflow_route(request, route, session_id, trigger="T2")
        elif self._check_trigger(request.state.get("messages", [])):
            # legacy trigger-clause fallback (e.g. ("messages", N) triggers).
            # P1-2: the tail clip must NOT bypass an explicit trigger-clause
            # compaction — that is a length/token mandate, not pressure
            # recovery — so this path goes straight to the compact executor.
            request = await self._aexecute_compact(
                request, ROUTE_COMPACT_ONLY, session_id, trigger="T2"
            )

        response = await self._aexecute_with_recovery(request, handler, session_id)
        self._monitor_degradation(response, session_id)
        t2_compressed = (
            state_register_mem.get_state(session_id, _TURN_ATTEMPTS_KEY, 0) > t2_attempts_before
        )
        # T3: post-response real-token re-check; T2-compressed calls
        # skip via the local flag — one compression per model call.
        return await self._apost_response_check(
            request, response, session_id, t2_compressed=t2_compressed
        )
