"""Overflow routing and provider-error recovery (T1-T5) for summarization.

Owns the four-route decision, the no-LLM tail clip, the compact executors, the
T3 post-response re-check and the T4/T5 forced-recovery ring.
"""

# allow: SIZE_OK — the bulk is the T1-T5 trigger state machine whose methods
# share the route tables and token estimators; splitting the triggers from the
# dispatch they drive would scatter one cohesive routing unit across modules.

from collections.abc import Awaitable, Callable
from typing import Any, NamedTuple, cast

from langchain.agents import AgentState
from langchain.agents.middleware import (
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langchain.agents.middleware.types import ResponseT
from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.typing import ContextT
from loguru import logger

from config.features import SUMMARIZATION
from pub.func.estimate_tokens import estimate_text_tokens
from pub.func.message.estimate_msg_tokens import estimate_messages_tokens
from pub.func.message.llm_error_classifier import (
    CONTEXT_OVERFLOW,
    PAYLOAD_TOO_LARGE,
    classify_provider_error,
)
from pub.func.message.overflow_clip import ROUTE_TAIL_CLIP, clip_overflow_tail
from pub.func.message.overflow_router import (
    ROUTE_COMPACT_ONLY,
    ROUTE_COMPACT_THEN_TRUNCATE,
    ROUTE_FITS,
    ROUTE_TRUNCATE_TOOL_RESULTS_ONLY,
    compute_pressure,
    decide_route,
    find_truncatable_tool_results,
)
from pub.func.message.tool_args_truncate import truncate_tool_args
from pub.func.message.tool_result_ttl import truncate_to_budget
from runtime import StateKey, state_register_mem

from .state_aliases import (
    _COOLDOWN_ROUNDS_KEY,
    _OVERFLOW_RETRIES_KEY,
    _TURN_ATTEMPTS_KEY,
)

PREEMPTIVE_TRUNCATE_RATIO = SUMMARIZATION["preemptive_truncate_ratio"]
COMPRESSION_TRIGGER_RATIO = SUMMARIZATION["compression_trigger_ratio"]
MIN_ARGS_CHARS_TO_TRUNCATE = SUMMARIZATION["min_args_chars_to_truncate"]
MAX_TOOL_ARGS_CHARS = SUMMARIZATION["max_tool_args_chars"]
PROTECTED_TOOLS = SUMMARIZATION["protected_tools"]
MAX_COMPRESS_ATTEMPTS_PER_TURN = SUMMARIZATION["max_compress_attempts_per_turn"]
MAX_OVERFLOW_RETRIES = SUMMARIZATION["max_overflow_retries"]
TRUNCATE_BUDGET_RATIO = SUMMARIZATION["truncate_budget_ratio"]
COMPRESSION_RESERVE_TOKENS = SUMMARIZATION["compression_reserve_tokens"]


def extract_reported_input_tokens(response: Any) -> int | None:
    """Extract the provider-reported input token count from a wrap return.

    Handles the 3-form handler-return union ``ModelResponse | AIMessage |
    ExtendedModelResponse``:

    - bare ``AIMessage`` (or duck-typed object exposing ``usage_metadata``):
      ``usage_metadata["input_tokens"]``
    - ``ModelResponse``: probes its message body (the ``result`` list);
      the last message carrying usable usage wins
    - ``ExtendedModelResponse``: unwraps ``model_response`` and recurses

    Returns None when missing/malformed (no usage, non-int or bool value,
    value <= 0, plain strings, None). NEVER raises — T3 is a post-response
    re-check that must never break the response path.
    """
    try:
        if response is None:
            return None
        usage = getattr(response, "usage_metadata", None)
        if isinstance(usage, dict):
            value = usage.get("input_tokens")
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return int(value)
            return None
        result = getattr(response, "result", None)
        if isinstance(result, (list, tuple)):
            for msg in reversed(list(result)):
                tokens = extract_reported_input_tokens(msg)
                if tokens is not None:
                    return tokens
        inner = getattr(response, "model_response", None)
        if inner is not None and inner is not response:
            return extract_reported_input_tokens(inner)
        return None
    except Exception:
        return None


_TRIGGER_BY_ERROR_CLASS: dict[str, str] = {
    PAYLOAD_TOO_LARGE: "T4",
    CONTEXT_OVERFLOW: "T5",
}


_RETRY_KEY_BY_ERROR_CLASS: dict[str, str] = {
    PAYLOAD_TOO_LARGE: _OVERFLOW_RETRIES_KEY,
    CONTEXT_OVERFLOW: _OVERFLOW_RETRIES_KEY,
}


class _ForcedRecoveryState(NamedTuple):
    """Bookkeeping threaded through one T4/T5 forced-recovery step."""

    trigger: str
    retry_key: str
    attempt: int
    old_tokens: int


class OverflowMixin:
    """T1-T5 triggers: route decision, dispatch, recovery, post-check."""

    def _get_reported_tokens(self, messages: list[AnyMessage]) -> int:
        last_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
        if last_ai and last_ai.usage_metadata:
            return int(last_ai.usage_metadata.get("total_tokens", 0))
        return 0

    def _check_trigger(self, messages: list[AnyMessage]) -> bool:
        """Check if any trigger condition is met."""
        for trigger_type, threshold in self._trigger:
            if trigger_type == "messages" and len(messages) >= threshold:
                return True
            if trigger_type == "tokens":
                local_est = self._estimate_tokens(messages)
                reported = self._get_reported_tokens(messages)
                effective = max(local_est, reported) if reported > 0 else local_est
                if effective >= threshold:
                    return True
        return False

    def _preemptive_check(self, messages: list[AnyMessage], session_id: str) -> str | None:
        """Pre-prompt token pressure estimation.

        Returns None / 'truncate_only' / 'compact'.
        """
        ctx_window = self._main_llm_context_window
        if not ctx_window or ctx_window <= 0:
            return None

        local_est = self._estimate_tokens(messages)
        reported = self._get_reported_tokens(messages)
        effective = max(local_est, reported) if reported > 0 else local_est
        pressure = effective / ctx_window

        if pressure >= COMPRESSION_TRIGGER_RATIO:
            return "compact"
        if pressure >= PREEMPTIVE_TRUNCATE_RATIO:
            return "truncate_only"
        return None

    def _usable_budget(self) -> int:
        """usable_budget = dynamic context window − COMPRESSION_RESERVE_TOKENS.

        The dynamic window is the constructor-injected ``main_llm_context_window``
        (same source as agent/core.py:11 ``main_llm_max_tokens``) — never a
        hardcoded value; langchain 1.3.9 ``ModelRequest`` has no model_profile
        field.
        """
        ctx = self._main_llm_context_window or 0
        return max(int(ctx) - COMPRESSION_RESERVE_TOKENS, 0)

    def _estimate_system_prompt_tokens(self, session_id: str) -> int:
        prompt = state_register_mem.get_state(session_id, StateKey.SYSTEM_PROMPT, "")
        if isinstance(prompt, str) and prompt:
            return estimate_text_tokens(prompt)
        return 0

    def _decide_overflow_route(self, messages: list[AnyMessage], session_id: str) -> str | None:
        """4-way route decision.

        Returns one of ROUTE_FITS / ROUTE_TRUNCATE_TOOL_RESULTS_ONLY /
        ROUTE_COMPACT_THEN_TRUNCATE / ROUTE_COMPACT_ONLY, or None when no
        dynamic context window is configured. T1/T2 are estimate-driven; the
        reported-usage input belongs to T3.
        """
        ctx_window = self._main_llm_context_window
        if not ctx_window or ctx_window <= 0:
            return None

        usable = self._usable_budget()
        # Pure local estimate (reported_tokens=0 skips the Tier-1 auto-extract):
        # an AIMessage's usage_metadata describes the prompt that produced it, so
        # once newer messages follow it that value is stale and must not drive
        # this estimate-driven preflight — reported usage is T3's input.
        est = estimate_messages_tokens(list(messages), reported_tokens=0)
        system_est = self._estimate_system_prompt_tokens(session_id)
        pressure = compute_pressure(est, None, system_est)
        truncatable = find_truncatable_tool_results(list(messages))
        route = decide_route(pressure, int(ctx_window), usable, truncatable)
        logger.debug(
            "Overflow route decision: est={} system_est={} usable={} "
            "candidates={} route={} session={}",
            est,
            system_est,
            usable,
            len(truncatable),
            route,
            session_id,
        )
        return route

    def _run_budget_truncation(
        self, messages: list[BaseMessage], usable: int
    ) -> tuple[list[BaseMessage], int]:
        """budget truncation over candidate rule.

        Step 1 truncates oversized tool-call args (returns new AIMessages
        via model_copy — the input list is not mutated for AIMessages).
        Step 2 truncates tool results over ``find_truncatable_tool_results``
        candidates (skips the last TRUNCATABLE_RECENT_SKIP messages, >=
        MIN_TOOL_RESULT_TOKENS_TO_TRUNCATE) — that module mutates
        ToolMessages in place. Callers MUST use the returned list for
        ``request.override`` — the args step does NOT touch the input list.
        """
        messages, args_freed = truncate_tool_args(
            list(messages),
            max_args_chars=MAX_TOOL_ARGS_CHARS,
            min_args_chars=MIN_ARGS_CHARS_TO_TRUNCATE,
            protected_tools=set(PROTECTED_TOOLS),
        )
        candidates = find_truncatable_tool_results(list(messages))
        result_freed = truncate_to_budget(
            list(messages), candidates, int(usable * TRUNCATE_BUDGET_RATIO)
        )
        return messages, args_freed + result_freed

    def _fast_tail_clip(
        self,
        messages: list[AnyMessage],
        *,
        usable: int,
        threshold_ratio: float,
        trigger: str,
    ) -> list[AnyMessage] | None:
        """P1-2 no-LLM tail clip: the first move on every overflow path.

        Budget derivation (conservative, reusing the T1–T5 estimators):
        ``est = estimate_messages_tokens(messages, reported_tokens=0)`` is the
        pure local estimate the route decision itself uses (a stale
        ``usage_metadata`` must not drive recovery), and
        ``target = max(est − usable × threshold_ratio, 0)`` is the token
        amount that must be freed. ``target == 0`` means the estimate already
        sits on the line, so the clip takes the maximum eligible tail batch.

        The clip is accepted only when it ALONE drops the estimate below
        ``usable × threshold_ratio``: the caller then returns the stubbed
        request and the existing route never executes — no budget truncation,
        no auxiliary-LLM compaction. An insufficient clip is discarded so the
        existing route keeps operating on the exact original list.
        """
        est = estimate_messages_tokens(list(messages), reported_tokens=0)
        target = est - int(usable * threshold_ratio)
        clipped = clip_overflow_tail(cast("list[BaseMessage]", list(messages)), max(target, 0))
        if clipped is None:
            return None
        new_est = estimate_messages_tokens(list(clipped), reported_tokens=0)
        if new_est >= usable * threshold_ratio:
            logger.debug(
                "Overflow tail clip insufficient: trigger={}, old_tokens={}, "
                "new_tokens={}, target_tokens={} (degrading to the existing route)",
                trigger,
                est,
                new_est,
                target,
            )
            return None
        self._log_route(trigger, ROUTE_TAIL_CLIP, est, new_est, usable)
        return cast("list[AnyMessage]", clipped)

    def _log_route(
        self, trigger: str, route: str, old_tokens: int, new_tokens: int, usable: int
    ) -> None:
        ratio = round(new_tokens / usable, 4) if usable > 0 else 0.0
        logger.info(
            "Context compression: trigger={}, route={}, old_tokens={}, "
            "new_tokens={}, pressure_ratio={}",
            trigger,
            route,
            old_tokens,
            new_tokens,
            ratio,
        )

    def _finish_compact(
        self,
        request: ModelRequest[ContextT],
        route: str,
        session_id: str,
        trigger: str,
        old_tokens: int,
        usable: int,
    ) -> ModelRequest[ContextT]:
        """Shared compaction tail: bookkeeping, optional truncation, route log.

        Reached by both :meth:`_execute_compact` and :meth:`_aexecute_compact`
        after their respective compression call returns.
        """
        self._record_compaction_bookkeeping(session_id)
        if route == ROUTE_COMPACT_THEN_TRUNCATE:
            final_messages = list(request.messages)
            final_messages, _ = self._run_budget_truncation(
                cast("list[BaseMessage]", final_messages), usable
            )
            request = request.override(messages=cast("list[AnyMessage]", final_messages))
        new_tokens = self._estimate_tokens(list(request.messages))
        self._log_route(trigger, route, old_tokens, new_tokens, usable)
        return request

    def _execute_compact(
        self,
        request: ModelRequest[ContextT],
        route: str,
        session_id: str,
        trigger: str,
    ) -> ModelRequest[ContextT]:
        """compact_only / compact_then_truncate execution (sync)."""
        messages: list[AnyMessage] = request.state.get("messages", [])
        old_tokens = self._estimate_tokens(list(messages))
        usable = self._usable_budget()
        try:
            request = self._apply_compression(request, session_id)
        except Exception as e:
            logger.error("Compression failed: {}", e)
            return request
        return self._finish_compact(request, route, session_id, trigger, old_tokens, usable)

    async def _aexecute_compact(
        self,
        request: ModelRequest[ContextT],
        route: str,
        session_id: str,
        trigger: str,
    ) -> ModelRequest[ContextT]:
        """compact_only / compact_then_truncate execution (async)."""
        messages: list[AnyMessage] = request.state.get("messages", [])
        old_tokens = self._estimate_tokens(list(messages))
        usable = self._usable_budget()
        try:
            request = await self._aapply_compression(request, session_id)
        except Exception as e:
            logger.error("Compression failed: {}", e)
            return request
        return self._finish_compact(request, route, session_id, trigger, old_tokens, usable)

    def _prepare_overflow_dispatch(
        self,
        request: ModelRequest[ContextT],
        route: str,
        trigger: str,
    ) -> tuple[ModelRequest[ContextT], str | None]:
        """Run the no-LLM tail clip and the truncate-only track.

        Returns ``(request, compact_route)``: ``compact_route`` is the compact
        route the caller must still execute (``ROUTE_COMPACT_ONLY`` /
        ``ROUTE_COMPACT_THEN_TRUNCATE``), or ``None`` when the request is final.
        Shared by the sync and async dispatchers.
        """
        usable = self._usable_budget()
        messages: list[AnyMessage] = request.state.get("messages", [])

        if route != ROUTE_FITS:
            clipped = self._fast_tail_clip(
                messages,
                usable=usable,
                threshold_ratio=COMPRESSION_TRIGGER_RATIO,
                trigger=trigger,
            )
            if clipped is not None:
                return request.override(messages=clipped), None

        if route == ROUTE_TRUNCATE_TOOL_RESULTS_ONLY:
            old_tokens = self._estimate_tokens(list(messages))
            final_msgs, _ = self._run_budget_truncation(
                cast("list[BaseMessage]", list(messages)), usable
            )
            request = request.override(messages=cast("list[AnyMessage]", final_msgs))
            new_tokens = self._estimate_tokens(list(final_msgs))
            self._log_route(trigger, route, old_tokens, new_tokens, usable)
            # Recheck: truncation freed less than estimated and pressure is
            # still at/above threshold_compact → compact backstop; otherwise
            # pass through WITHOUT compression.
            if usable > 0 and new_tokens >= usable * COMPRESSION_TRIGGER_RATIO:
                return request, ROUTE_COMPACT_THEN_TRUNCATE
            return request, None

        if route in (ROUTE_COMPACT_ONLY, ROUTE_COMPACT_THEN_TRUNCATE):
            return request, route

        return request, None

    def _dispatch_overflow_route(
        self,
        request: ModelRequest[ContextT],
        route: str,
        session_id: str,
        trigger: str = "T2",
    ) -> ModelRequest[ContextT]:
        """Single reusable 4-route executor.

        T1 (before_agent) and T2 (wrap/awrap_model_call) both call this —
        Tasks 6/7 (T3 post-response check, provider-error retry) must reuse
        it instead of copying a second dispatch.

        P1-2: every non-``fits`` route first attempts the no-LLM tail clip.
        When the clip alone recovers the budget the request is returned
        immediately and the route below never executes.
        """
        request, compact_route = self._prepare_overflow_dispatch(request, route, trigger)
        if compact_route is None:
            return request
        return self._execute_compact(request, compact_route, session_id, trigger)

    async def _adispatch_overflow_route(
        self,
        request: ModelRequest[ContextT],
        route: str,
        session_id: str,
        trigger: str = "T2",
    ) -> ModelRequest[ContextT]:
        """Async twin of :meth:`_dispatch_overflow_route` (parity by shape)."""
        request, compact_route = self._prepare_overflow_dispatch(request, route, trigger)
        if compact_route is None:
            return request
        return await self._aexecute_compact(request, compact_route, session_id, trigger)

    def _evaluate_post_response(
        self,
        request: ModelRequest[ContextT],
        response: ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT],
        session_id: str,
        t2_compressed: bool,
    ) -> tuple[int, str, int, int, float] | None:
        """T3 gate: return ``(reported, route, est, usable, pressure)`` or None.

        None means the original response passes through untouched — T2 already
        compressed in this call, the provider reported no usable token count,
        an anti-thrash gate blocks, or the route is ``fits``. Shared by the sync
        and async post-response checks.
        """
        if t2_compressed:
            # T2 dispatched an actual compact in THIS wrap call: exactly
            # one compression per model call (anti-double-compress).
            return None
        reported = extract_reported_input_tokens(response)
        if reported is None:
            return None
        attempts = state_register_mem.get_state(session_id, _TURN_ATTEMPTS_KEY, 0) or 0
        if attempts >= MAX_COMPRESS_ATTEMPTS_PER_TURN:
            return None
        cooldown = state_register_mem.get_state(session_id, _COOLDOWN_ROUNDS_KEY, 0) or 0
        if cooldown > 0:
            # Anti-thrash gate respected: T3 reads the post-tick value.
            return None
        ctx_window = self._main_llm_context_window
        if not ctx_window or ctx_window <= 0:
            return None
        usable = self._usable_budget()
        if usable <= 0:
            return None
        messages = list(request.messages)
        est = self._estimate_tokens(messages)
        system_est = self._estimate_system_prompt_tokens(session_id)
        # reported wins (compute_pressure takes the max) — T3 is
        # real-token driven, NOT estimate-driven like T1/T2.
        pressure = compute_pressure(est, reported, system_est)
        if pressure < usable * COMPRESSION_TRIGGER_RATIO:
            return None
        truncatable = find_truncatable_tool_results(list(messages))
        route = decide_route(pressure, int(ctx_window), usable, truncatable)
        if route == ROUTE_FITS:
            return None
        return reported, route, est, usable, pressure

    def _log_post_response(
        self,
        reported: int,
        route: str,
        est: int,
        usable: int,
        pressure: float,
        request: ModelRequest[ContextT],
    ) -> None:
        new_tokens = self._estimate_tokens(list(request.messages))
        logger.info(
            "Context compression: trigger=T3, reported_input_tokens={}, "
            "route={}, old_tokens={}, new_tokens={}, pressure_ratio={:.2f}",
            reported,
            route,
            est,
            new_tokens,
            pressure / usable,
        )

    def _post_response_check(
        self,
        request: ModelRequest[ContextT],
        response: ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT],
        session_id: str,
        t2_compressed: bool = False,
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        """Post-response real-token re-check (T3).

        Runs AFTER the handler returns inside wrap/awrap_model_call: the
        provider-reported input tokens are the most accurate overflow signal,
        and per-call granularity covers multiple model calls within one turn
        (which the per-turn T1 preflight cannot). Dispatch reuses the
        executors (``_dispatch_overflow_route``) — never a second copy — and
        a failure NEVER loses the original response.
        """
        try:
            evaluated = self._evaluate_post_response(request, response, session_id, t2_compressed)
            if evaluated is None:
                return response
            reported, route, est, usable, pressure = evaluated
            request = self._dispatch_overflow_route(request, route, session_id, trigger="T3")
            self._log_post_response(reported, route, est, usable, pressure, request)
            return response
        except Exception as exc:
            # T3 must never break the response path: original response wins.
            logger.error(
                "Context compression: T3 check failed (response preserved): {}",
                exc,
            )
            return response

    async def _apost_response_check(
        self,
        request: ModelRequest[ContextT],
        response: ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT],
        session_id: str,
        t2_compressed: bool = False,
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        """Async twin of :meth:`_post_response_check` (parity by shape)."""
        try:
            evaluated = self._evaluate_post_response(request, response, session_id, t2_compressed)
            if evaluated is None:
                return response
            reported, route, est, usable, pressure = evaluated
            request = await self._adispatch_overflow_route(request, route, session_id, trigger="T3")
            self._log_post_response(reported, route, est, usable, pressure, request)
            return response
        except Exception as exc:
            logger.error(
                "Context compression: T3 check failed (response preserved): {}",
                exc,
            )
            return response

    def _begin_forced_recovery(
        self,
        request: ModelRequest[ContextT],
        session_id: str,
        error_class: str,
    ) -> tuple[ModelRequest[ContextT], _ForcedRecoveryState | None]:
        """Classify the error and try the no-LLM tail clip before compression.

        Returns ``(request, state)``. When the clip alone recovers the budget
        the retry counter is bumped and ``state`` is ``None`` (the clipped
        request is final); otherwise ``state`` carries the bookkeeping the
        caller needs after its compression call. Shared by both variants.
        """
        trigger = _TRIGGER_BY_ERROR_CLASS[error_class]
        retry_key = _RETRY_KEY_BY_ERROR_CLASS[error_class]
        retries = state_register_mem.get_state(session_id, retry_key, 0) or 0
        attempt = retries + 1
        old_tokens = self._estimate_tokens(list(request.messages))

        usable = self._usable_budget()
        clipped = self._fast_tail_clip(
            list(request.messages),
            usable=usable,
            threshold_ratio=1.0,
            trigger=trigger,
        )
        if clipped is not None:
            state_register_mem.set_state(session_id, retry_key, attempt)
            return request.override(messages=cast("list[AnyMessage]", clipped)), None
        return request, _ForcedRecoveryState(trigger, retry_key, attempt, old_tokens)

    def _finish_forced_recovery(
        self,
        request: ModelRequest[ContextT],
        session_id: str,
        error_class: str,
        state: _ForcedRecoveryState,
    ) -> ModelRequest[ContextT]:
        """Truncate after the compression step, bump the counter, log."""
        usable = self._usable_budget()
        final_messages = list(request.messages)
        final_messages, _ = self._run_budget_truncation(
            cast("list[BaseMessage]", final_messages), usable
        )
        request = request.override(messages=cast("list[AnyMessage]", final_messages))
        new_tokens = self._estimate_tokens(list(final_messages))
        state_register_mem.set_state(session_id, state.retry_key, state.attempt)
        logger.warning(
            "Context compression: trigger={}, attempt={}/{}, error_class={}, "
            "old_tokens={}, new_tokens={}",
            state.trigger,
            state.attempt,
            MAX_OVERFLOW_RETRIES,
            error_class,
            state.old_tokens,
            new_tokens,
        )
        return request

    def _forced_recovery_request(
        self,
        request: ModelRequest[ContextT],
        session_id: str,
        error_class: str,
    ) -> ModelRequest[ContextT]:
        """One forced-compression step for the T4/T5 recovery loop (sync).

        compact_only equivalent + budget truncation, bypassing ALL
        anti-thrash gates by construction (``_should_skip_compression``,
        cooldown rounds and the per-turn attempt cap are simply never
        consulted here — the forced semantics of ``_FORCE_RECOVERY_KEY``
        without setting the key). Reuses ``_apply_compression`` and
        ``_run_budget_truncation`` — no second dispatch copy. Pairing
        invariants stay intact ( truncation is pairing-safe; the
        summary output is a Human/AI pair).

        Does NOT arm the cooldown or count a turn attempt (error recovery
        bypasses those gates by design); it DOES go through
        ``_record_compression`` inside ``_apply_compression`` so the
        session-level compression stats stay truthful. The per-class retry
        counter is incremented AFTER a successful compression step.

        P1-2 fast path: before the compact step the trailing contiguous
        ToolMessage batch is clipped without any LLM call. When the clip
        alone drops the estimate below the usable budget the provider call is
        retried with the stubbed list and compression never runs. It reads
        ``request.messages`` — an already-stubbed request (earlier recovery
        attempt) makes the clip a no-op, so the retry budget cannot be burned
        on identical clips.
        """
        request, state = self._begin_forced_recovery(request, session_id, error_class)
        if state is None:
            return request
        request = self._apply_compression(request, session_id)
        return self._finish_forced_recovery(request, session_id, error_class, state)

    async def _aforced_recovery_request(
        self,
        request: ModelRequest[ContextT],
        session_id: str,
        error_class: str,
    ) -> ModelRequest[ContextT]:
        """Async twin of :meth:`_forced_recovery_request` (parity by shape)."""
        request, state = self._begin_forced_recovery(request, session_id, error_class)
        if state is None:
            return request
        request = await self._aapply_compression(request, session_id)
        return self._finish_forced_recovery(request, session_id, error_class, state)

    def _execute_with_recovery(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
        session_id: str,
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        """Run ``handler`` inside the T4/T5 bounded recovery loop (sync).

        - Non-target errors (``classify_provider_error`` -> None): the
          ORIGINAL exception re-raises untouched — zero retries, zero
          state writes, never swallowed.
        - ``payload_too_large`` (T4) / ``context_overflow`` (T5): forced
          compression + ``request.override(messages=...)`` rebuild +
          handler retry, at most MAX_OVERFLOW_RETRIES times per error
          class (independent session-level counters). When the counter is
          exhausted the ORIGINAL exception re-raises (error-frame
          propagation via the existing messages.py -> turn_runner.py
          chain) — never an empty response.
        - A failure of the forced-compression step itself also propagates
          the ORIGINAL exception (never the compression error).
        - ``_monitor_degradation`` is NOT called here: wrap calls it once,
          AFTER this helper returns, on the final successful response only
          (Metis lock: failed retry calls must not pollute degradation
          statistics). T3 post-response checks run after the recovered
          final response ( wiring preserved).
        """
        while True:
            try:
                return handler(request)
            except BaseException as exc:
                error_class = classify_provider_error(exc)
                if error_class is None:
                    raise
                trigger = _TRIGGER_BY_ERROR_CLASS.get(error_class)
                retry_key = _RETRY_KEY_BY_ERROR_CLASS.get(error_class)
                if trigger is None or retry_key is None:
                    # Unknown future classifier value: treat as non-target.
                    raise
                retries = state_register_mem.get_state(session_id, retry_key, 0) or 0
                if retries >= MAX_OVERFLOW_RETRIES:
                    logger.error(
                        "Context compression: trigger={} retries exhausted "
                        "({}, error_class={}) - propagating original error",
                        trigger,
                        retries,
                        error_class,
                    )
                    raise
                try:
                    request = self._forced_recovery_request(request, session_id, error_class)
                except Exception as compression_exc:
                    logger.error(
                        "Context compression: trigger={} forced compression "
                        "failed ({}) - propagating original error",
                        trigger,
                        compression_exc,
                    )
                    raise exc from compression_exc

    async def _aexecute_with_recovery(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
        session_id: str,
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        """Async twin of :meth:`_execute_with_recovery` (parity by shape)."""
        while True:
            try:
                return await handler(request)
            except BaseException as exc:
                error_class = classify_provider_error(exc)
                if error_class is None:
                    raise
                trigger = _TRIGGER_BY_ERROR_CLASS.get(error_class)
                retry_key = _RETRY_KEY_BY_ERROR_CLASS.get(error_class)
                if trigger is None or retry_key is None:
                    raise
                retries = state_register_mem.get_state(session_id, retry_key, 0) or 0
                if retries >= MAX_OVERFLOW_RETRIES:
                    logger.error(
                        "Context compression: trigger={} retries exhausted "
                        "({}, error_class={}) - propagating original error",
                        trigger,
                        retries,
                        error_class,
                    )
                    raise
                try:
                    request = await self._aforced_recovery_request(request, session_id, error_class)
                except Exception as compression_exc:
                    logger.error(
                        "Context compression: trigger={} forced compression "
                        "failed ({}) - propagating original error",
                        trigger,
                        compression_exc,
                    )
                    raise exc from compression_exc

    def _t1_state_update(self, request: ModelRequest[ContextT]) -> dict[str, Any]:
        """Translate a T1-dispatched request into a before_agent state update.

        ALWAYS clears the state messages first (RemoveMessage with the
        REMOVE_ALL_MESSAGES sentinel) and rebuilds from the dispatched
        request: the add_messages reducer never removes by itself, so a
        plain-list update leaks every message the compression summarized
        away — and a compact that swaps a single huge head message for the
        two-message summary pair even GROWS the list (cutoff=1), which no
        length-based guard can catch. Rebuilding is exact for every track:
        in-place truncation keeps the same ids and content, compact replaces
        the head with the summary pair (same pattern as
        ToolCallNormalize.before_model).
        """
        new_messages = list(request.messages)
        return {
            "messages": cast(
                "list[AnyMessage]",
                [RemoveMessage(id=REMOVE_ALL_MESSAGES), *new_messages],
            )
        }

    def _t1_preflight(self, state: AgentState, session_id: str) -> dict[str, Any] | None:
        messages: list[AnyMessage] = list(state.get("messages", []) or [])
        if not messages:
            return None
        route = self._decide_overflow_route(messages, session_id)
        if route is None or route == ROUTE_FITS:
            return None
        # Cooldown blocks the PROACTIVE compact routes at T1; the cheap
        # truncate track still runs (it is the recovery mechanism itself).
        cooldown = state_register_mem.get_state(session_id, _COOLDOWN_ROUNDS_KEY, 0) or 0
        if cooldown > 0 and route in (ROUTE_COMPACT_ONLY, ROUTE_COMPACT_THEN_TRUNCATE):
            logger.debug(
                "T1 compact route suppressed by cooldown ({} rounds left), session={}",
                cooldown,
                session_id,
            )
            return None
        request = ModelRequest(
            model=self._model,
            messages=cast("list[AnyMessage]", messages),
            state=state,
        )
        request = self._dispatch_overflow_route(request, route, session_id, trigger="T1")
        return self._t1_state_update(request)

    async def _at1_preflight(self, state: AgentState, session_id: str) -> dict[str, Any] | None:
        messages: list[AnyMessage] = list(state.get("messages", []) or [])
        if not messages:
            return None
        route = self._decide_overflow_route(messages, session_id)
        if route is None or route == ROUTE_FITS:
            return None
        cooldown = state_register_mem.get_state(session_id, _COOLDOWN_ROUNDS_KEY, 0) or 0
        if cooldown > 0 and route in (ROUTE_COMPACT_ONLY, ROUTE_COMPACT_THEN_TRUNCATE):
            logger.debug(
                "T1 compact route suppressed by cooldown ({} rounds left), session={}",
                cooldown,
                session_id,
            )
            return None
        request = ModelRequest(
            model=self._model,
            messages=cast("list[AnyMessage]", messages),
            state=state,
        )
        request = await self._adispatch_overflow_route(request, route, session_id, trigger="T1")
        return self._t1_state_update(request)
