"""Batch-aware ToolNode: file calls are ordered per canonical path.

Adapted from ``xp_graph``'s ``smart_tool_node.py`` but rewritten for the
LangGraph 1.3.9 ToolNode (the branch's ``_get_config_list`` /
``_build_tool_runtimes`` helpers no longer exist). Non-file unsafe calls are a
whole-batch barrier; a file-only batch is split into per-path groups, where a
group containing a write runs in call order and an all-read group may run
concurrently. Different paths never wait for each other.
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.prebuilt.tool_node import (
    ToolNode,
    ToolRuntime,
    get_config_list,
    get_executor_for_config,
)
from langgraph.runtime import Runtime
from loguru import logger

from config.features import TOOL_PARALLEL
from pub.func.message.eviction import (
    EVICTION_PREFIX,
    evict_tool_result,
    hard_clip_tool_result,
)

from .middlewares.context_eviction.core import _rewrite_tool_messages
from .tools.parallel_safety import BatchPlan, plan_batch

__all__ = ["SafeToolNode", "install_safe_tool_node"]


def _log_plan(plan: BatchPlan, tool_calls: list[Any]) -> None:
    if not TOOL_PARALLEL["log_unsafe_batches"]:
        return
    names = [str(call.get("name") or "unknown") for call in tool_calls]
    if plan.barrier:
        logger.warning(
            "SafeToolNode: sequential batch ({}): {}",
            plan.reason or "barrier",
            names,
        )
    else:
        logger.debug(
            "SafeToolNode: {} group(s), file_paths={}, calls={}",
            len(plan.groups),
            list(plan.file_paths),
            names,
        )


# ── Batch-result aggregate size guard ────────────────────────────────────────
# ContextEviction evicts PER call (in ToolNode._arun_one), so N parallel results
# each just under its 20K threshold all survive and their sum blows the context
# window — no middleware sees the batch. The only point with the whole batch in
# hand is here, between gather and _combine_tool_outputs. Purely additive:
# default callers of evict_* are unchanged (force=False), and every failure is
# fail-open (keep the original result).


def _session_id(config: RunnableConfig, runtime: Any, state: Any = None) -> str:
    """Resolve the session for batch eviction; empty means "skip the guard".

    Disk offload needs a session directory, so with no identifiable session the
    guard degrades to a no-op rather than dropping results.

    Resolution is STATE-FIRST: the product invokes its graph with
    ``input={"session_id": …}``, while ``runtime.context`` stays None unless a
    caller passes ``context=`` (it does not). The batch entry points receive the
    graph ``Runtime`` (which carries NO state), so they pass the extracted state
    explicitly; a ``ToolRuntime`` carries ``.state`` itself. ``thread_id`` is NOT
    a session id — ``pub.func.build_agent_config`` puts an INT HASH of the
    session id there, for the checkpointer — so it is never used as a fallback:
    doing that wrote evicted files under ``SESSIONS_DIR/<hash>/``, outside the
    session tree (the preview then pointed at a directory no cleanup owned and
    the session's own path exemption did not cover).
    """
    for candidate in (state, getattr(runtime, "state", None)):
        if isinstance(candidate, dict):
            session_id = candidate.get("session_id")
            if session_id:
                return str(session_id)
    context = getattr(runtime, "context", None)
    if isinstance(context, dict):
        context_sid = context.get("session_id")
    else:
        # user_hooks reads context as an attribute object; support both shapes.
        context_sid = getattr(context, "session_id", None)
    if context_sid:
        return str(context_sid)
    configurable = config.get("configurable") or {} if isinstance(config, dict) else {}
    return str(configurable.get("session_id") or "")


def _extract_tool_message(output: Any) -> ToolMessage | None:
    """The bare ToolMessage in one outputs element (plain or Command-wrapped)."""
    if isinstance(output, ToolMessage):
        return output
    update = getattr(output, "update", None)
    if isinstance(update, dict):
        for msg in update.get("messages", []):
            if isinstance(msg, ToolMessage):
                return msg
    return None


def _msg_content_size(output: Any) -> int:
    """Text char count of an outputs element (0 for non-tool results)."""
    msg = _extract_tool_message(output)
    if msg is None:
        return 0
    content = msg.content
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(
            len(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return 0


def _is_already_evicted(output: Any) -> bool:
    """True when a result already carries the eviction/offload marker."""
    msg = _extract_tool_message(output)
    if msg is None:
        return False
    content = msg.content
    return isinstance(content, str) and content.startswith(EVICTION_PREFIX)


def _evict_one(output: Any, session_id: str) -> Any:
    """Evict one batch result, preferring disk offload, then in-memory clip."""

    def rewrite(msg: ToolMessage) -> ToolMessage:
        try:
            replacement = evict_tool_result(msg, session_id, force=True)
            if replacement is not None:
                return replacement
        except Exception:  # noqa: BLE001 - fail-open
            logger.debug("batch evict_tool_result failed for {}", getattr(msg, "tool_call_id", "?"))
        try:
            clipped = hard_clip_tool_result(msg, force=True)
            if clipped is not None:
                return clipped
        except Exception:  # noqa: BLE001 - fail-open
            logger.debug("batch hard_clip failed for {}", getattr(msg, "tool_call_id", "?"))
        return msg

    return _rewrite_tool_messages(output, rewrite)


def _enforce_batch_size_limit(outputs: list[Any], session_id: str) -> list[Any]:
    """Hold a parallel batch under the aggregate char budget.

    When the sum of all tool-result text exceeds the threshold, evict the
    LARGEST results first (they carry the least marginal value and their
    head/tail preview is enough) until the batch fits. Already-evicted results
    and zero-size elements are skipped; an eviction that fails keeps the
    original. Order and count are preserved — only message content changes.
    """
    threshold = TOOL_PARALLEL.get("batch_result_threshold_chars", 0)
    if threshold <= 0 or len(outputs) <= 1 or not session_id:
        return outputs

    total = sum(_msg_content_size(o) for o in outputs)
    if total <= threshold:
        return outputs

    logger.info(
        "SafeToolNode: batch result size {} chars exceeds threshold {}, "
        "evicting largest first ({} outputs)",
        total,
        threshold,
        len(outputs),
    )
    indices_by_size = sorted(
        range(len(outputs)), key=lambda i: _msg_content_size(outputs[i]), reverse=True
    )
    result = list(outputs)
    for i in indices_by_size:
        if total <= threshold:
            break
        if _is_already_evicted(result[i]):
            continue
        before = _msg_content_size(result[i])
        if before <= 0:
            continue
        result[i] = _evict_one(result[i], session_id)
        total -= max(0, before - _msg_content_size(result[i]))
    return result


class SafeToolNode(ToolNode):
    """ToolNode with a file-aware batch order; all other behaviour is inherited."""

    async def _afunc(self, input: Any, config: RunnableConfig, runtime: Runtime) -> Any:
        tool_calls, input_type = self._parse_input(input)
        if not tool_calls:
            return self._combine_tool_outputs([], input_type)
        config_list = get_config_list(config, len(tool_calls))
        runtimes = self._build_runtimes(tool_calls, config_list, input, runtime)
        session_id = _session_id(config, runtime, runtimes[0].state if runtimes else input)
        plan = plan_batch(
            [dict(call) for call in tool_calls],
            dict(self.tools_by_name),
            runtimes[0].state if runtimes else None,
        )
        _log_plan(plan, list(tool_calls))

        async def run_one(index: int) -> tuple[int, Any]:
            return index, await self._arun_one(tool_calls[index], input_type, runtimes[index])

        if not TOOL_PARALLEL["enforce"]:
            gathered = await asyncio.gather(*(run_one(i) for i in range(len(tool_calls))))
            flat = [out for _, out in gathered]
            if session_id:
                flat = await asyncio.to_thread(_enforce_batch_size_limit, flat, session_id)
            return self._combine_tool_outputs(flat, input_type)

        async def run_group(indices: tuple[int, ...], concurrent: bool) -> list[tuple[int, Any]]:
            if concurrent and len(indices) > 1:
                return list(await asyncio.gather(*(run_one(i) for i in indices)))
            return [await run_one(i) for i in indices]

        group_results = await asyncio.gather(
            *(run_group(group.indices, group.concurrent) for group in plan.groups)
        )
        by_index: dict[int, Any] = {}
        for group in group_results:
            for index, output in group:
                by_index[index] = output
        outputs = [by_index[i] for i in range(len(tool_calls))]
        if session_id:
            outputs = await asyncio.to_thread(_enforce_batch_size_limit, outputs, session_id)
        return self._combine_tool_outputs(outputs, input_type)

    def _func(self, input: Any, config: RunnableConfig, runtime: Runtime) -> Any:
        tool_calls, input_type = self._parse_input(input)
        if not tool_calls:
            return self._combine_tool_outputs([], input_type)
        config_list = get_config_list(config, len(tool_calls))
        runtimes = self._build_runtimes(tool_calls, config_list, input, runtime)
        session_id = _session_id(config, runtime, runtimes[0].state if runtimes else input)
        plan = plan_batch(
            [dict(call) for call in tool_calls],
            dict(self.tools_by_name),
            runtimes[0].state if runtimes else None,
        )
        _log_plan(plan, list(tool_calls))

        if not TOOL_PARALLEL["enforce"]:
            with get_executor_for_config(config) as executor:
                outputs = list(
                    executor.map(
                        self._run_one,
                        tool_calls,
                        [input_type] * len(tool_calls),
                        runtimes,
                    )
                )
            if session_id:
                outputs = _enforce_batch_size_limit(outputs, session_id)
            return self._combine_tool_outputs(outputs, input_type)

        def run_sequential(indices: tuple[int, ...]) -> list[tuple[int, Any]]:
            return [(i, self._run_one(tool_calls[i], input_type, runtimes[i])) for i in indices]

        with get_executor_for_config(config) as executor:
            single_futures: list[tuple[int, Any]] = []
            group_futures: list[Any] = []
            for group in plan.groups:
                if group.concurrent:
                    for index in group.indices:
                        single_futures.append(
                            (
                                index,
                                executor.submit(
                                    self._run_one,
                                    tool_calls[index],
                                    input_type,
                                    runtimes[index],
                                ),
                            )
                        )
                else:
                    group_futures.append(executor.submit(run_sequential, group.indices))
            singles = [(index, future.result()) for index, future in single_futures]
            groups = [future.result() for future in group_futures]
        by_index: dict[int, Any] = dict(singles)
        for group in groups:
            for index, output in group:
                by_index[index] = output
        outputs = [by_index[i] for i in range(len(tool_calls))]
        if session_id:
            outputs = _enforce_batch_size_limit(outputs, session_id)
        return self._combine_tool_outputs(outputs, input_type)

    def _build_runtimes(
        self, tool_calls: list[Any], config_list: list[Any], input: Any, runtime: Any
    ) -> list[ToolRuntime]:
        """Build one ToolRuntime per call, matching ToolNode 1.3.9's inline code."""
        runtimes: list[ToolRuntime] = []
        for call, cfg in zip(tool_calls, config_list, strict=False):
            state = self._extract_state(input, cfg)
            runtimes.append(
                ToolRuntime(
                    state=state,
                    tool_call_id=call["id"],
                    config=cfg,
                    context=runtime.context,
                    store=runtime.store,
                    stream_writer=runtime.stream_writer,
                    tools=list(self.tools_by_name.values()),
                    execution_info=runtime.execution_info,
                    server_info=runtime.server_info,
                )
            )
        return runtimes


def install_safe_tool_node() -> None:
    """Patch the factory's ToolNode once, when the feature is enabled.

    ``create_agent`` has no ``tool_node`` parameter; ``langchain.agents.factory``
    constructs its ToolNode from the module-level name, so replacing that name is
    the only injection point that does not fork ``create_agent``.
    """
    if not TOOL_PARALLEL["enabled"]:
        return
    import langchain.agents.factory as factory

    if factory.ToolNode is SafeToolNode:
        return
    factory.ToolNode = SafeToolNode  # type: ignore[assignment]
    logger.info("SafeToolNode installed (enforce={})", TOOL_PARALLEL["enforce"])
