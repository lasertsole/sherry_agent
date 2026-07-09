from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from langgraph.prebuilt.tool_node import ToolNode as _OrigToolNode

logger = logging.getLogger(__name__)


def _should_parallelize(tool_calls: list, tools_by_name: dict) -> bool:
    """Return True when a tool-call batch is safe to run concurrently.

    Rules (derived from each tool's ``metadata["idempotent"]`` declaration):
    - All tools idempotent → concurrent
    - Any tool non-idempotent or metadata missing → sequential (safe default)
    """
    if len(tool_calls) <= 1:
        return False

    for tc in tool_calls:
        tool = tools_by_name.get(tc["name"])
        if tool is None:
            return False
        meta = getattr(tool, "metadata", None)
        if not isinstance(meta, dict) or not meta.get("idempotent", False):
            return False

    return True


class SmartToolNode(_OrigToolNode):
    """ToolNode that adds idempotent-based parallel safety classification.

    Native ToolNode blindly runs all tool calls concurrently (asyncio.gather /
    executor.map). This subclass checks each tool's ``metadata["idempotent"]``
    flag: all idempotent → concurrent, any non-idempotent → sequential.
    """

    async def _afunc(self, input, config, runtime):
        tool_calls, input_type = self._parse_input(input)

        if not _should_parallelize(tool_calls, self.tools_by_name):
            logger.debug("smart_tool_node: sequential for %d calls", len(tool_calls))
            config_list = self._get_config_list(config, len(tool_calls))
            tool_runtimes = self._build_tool_runtimes(tool_calls, config_list, input, runtime)
            outputs = []
            for call, tr in zip(tool_calls, tool_runtimes):
                outputs.append(await self._arun_one(call, input_type, tr))
            return self._combine_tool_outputs(outputs, input_type)

        logger.debug("smart_tool_node: concurrent for %d calls", len(tool_calls))
        config_list = self._get_config_list(config, len(tool_calls))
        tool_runtimes = self._build_tool_runtimes(tool_calls, config_list, input, runtime)
        coros = [
            self._arun_one(call, input_type, tr)
            for call, tr in zip(tool_calls, tool_runtimes)
        ]
        outputs = await asyncio.gather(*coros)
        return self._combine_tool_outputs(outputs, input_type)

    def _func(self, input, config, runtime):
        tool_calls, input_type = self._parse_input(input)

        if not _should_parallelize(tool_calls, self.tools_by_name):
            logger.debug("smart_tool_node: sequential for %d calls", len(tool_calls))
            config_list = self._get_config_list(config, len(tool_calls))
            tool_runtimes = self._build_tool_runtimes(tool_calls, config_list, input, runtime)
            outputs = []
            for call, tr in zip(tool_calls, tool_runtimes):
                outputs.append(self._run_one(call, input_type, tr))
            return self._combine_tool_outputs(outputs, input_type)

        logger.debug("smart_tool_node: concurrent for %d calls", len(tool_calls))
        from langgraph.config import get_executor_for_config
        config_list = self._get_config_list(config, len(tool_calls))
        tool_runtimes = self._build_tool_runtimes(tool_calls, config_list, input, runtime)
        with get_executor_for_config(config) as executor:
            outputs = list(
                executor.map(self._run_one, tool_calls, [input_type] * len(tool_calls), tool_runtimes)
            )
        return self._combine_tool_outputs(outputs, input_type)

    @staticmethod
    def _get_config_list(config, n):
        from langgraph.prebuilt.tool_node import get_config_list as _get
        return _get(config, n)

    def _build_tool_runtimes(self, tool_calls, config_list, input, runtime):
        from langgraph.prebuilt.tool_node import ToolRuntime
        runtimes = []
        for call, cfg in zip(tool_calls, config_list):
            state = self._extract_state(input, cfg)
            tr = ToolRuntime(
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
            runtimes.append(tr)
        return runtimes


def patch_tool_node():
    import langchain.agents.factory as _f
    _f.ToolNode = SmartToolNode
    logger.debug("smart_tool_node: patched factory.ToolNode -> SmartToolNode")
