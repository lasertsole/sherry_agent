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


class SafeToolNode(ToolNode):
    """ToolNode with a file-aware batch order; all other behaviour is inherited."""

    async def _afunc(self, input: Any, config: RunnableConfig, runtime: Runtime) -> Any:
        tool_calls, input_type = self._parse_input(input)
        if not tool_calls:
            return self._combine_tool_outputs([], input_type)
        config_list = get_config_list(config, len(tool_calls))
        runtimes = self._build_runtimes(tool_calls, config_list, input, runtime)
        plan = plan_batch(
            [dict(call) for call in tool_calls],
            dict(self.tools_by_name),
            runtimes[0].state if runtimes else None,
        )
        _log_plan(plan, list(tool_calls))

        async def run_one(index: int) -> tuple[int, Any]:
            return index, await self._arun_one(tool_calls[index], input_type, runtimes[index])

        if not TOOL_PARALLEL["enforce"]:
            outputs = await asyncio.gather(*(run_one(i) for i in range(len(tool_calls))))
            return self._combine_tool_outputs([out for _, out in outputs], input_type)

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
        return self._combine_tool_outputs(outputs, input_type)

    def _func(self, input: Any, config: RunnableConfig, runtime: Runtime) -> Any:
        tool_calls, input_type = self._parse_input(input)
        if not tool_calls:
            return self._combine_tool_outputs([], input_type)
        config_list = get_config_list(config, len(tool_calls))
        runtimes = self._build_runtimes(tool_calls, config_list, input, runtime)
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
