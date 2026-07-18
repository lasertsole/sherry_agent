"""Tests for commander integration with xp_graph tool.

Tests cover:
1. Commander system prompt includes xp_graph instructions
2. xp_graph returns relevant methods when nodes exist
3. xp_graph returns "No relevant methods found." when no nodes match
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock


class TestCommanderSystemPrompt:
    """Verify the commander system prompt includes xp_graph instructions."""

    def test_commander_system_prompt_contains_xp_graph(self):
        """The system prompt should instruct the commander to call xp_graph before starting."""
        from agent.tools.subagent.commander.core import get_commander_system_prompt

        prompt = get_commander_system_prompt()

        # Check the tool is documented
        assert "xp_graph" in prompt, "System prompt must mention xp_graph tool"

        # Check Step -1 exists (pre-task historical experience retrieval)
        assert "Step -1" in prompt, "System prompt must have Step -1 for pre-task xp_graph"
        assert "Retrieve Historical Experience" in prompt

        # Check "when stuck" guidance exists
        assert "Stuck? Call xp_graph" in prompt or "当遇到困难" in prompt

    def test_commander_build_includes_xp_graph_tool(self):
        """The commander agent should have xp_graph in its tool list."""
        from langchain_core.tools import BaseTool

        # The middlewares are imported inside build_commander to avoid circular deps.
        # We mock them at the actual import locations.
        with patch("agent.tools.subagent.commander.core.build_main_llm") as mock_llm, \
             patch("agent.tools.subagent.commander.core.build_todo_writer_tool") as mock_todo, \
             patch("agent.tools.subagent.commander.core.build_worker_tool") as mock_worker, \
             patch("agent.tools.subagent.commander.core.build_xp_graph_tool") as mock_xp, \
             patch("agent.middlewares.ToolCallNormalize"), \
             patch("agent.middlewares.IterationBudget"), \
             patch("agent.middlewares.ToolGuardrails"), \
             patch("agent.tools.subagent.commander.core.CommanderSummarization"), \
             patch("agent.tools.subagent.commander.core.TODOManager"), \
             patch("agent.tools.subagent.commander.core.InMemorySaver"), \
             patch("agent.tools.subagent.commander.core.create_agent") as mock_create:

            mock_llm.return_value = MagicMock()
            mock_todo.return_value = MagicMock(spec=BaseTool)
            mock_worker.return_value = MagicMock(spec=BaseTool)
            mock_xp.return_value = MagicMock(spec=BaseTool, name="xp_graph")
            mock_create.return_value = MagicMock()

            from agent.tools.subagent.commander.core import build_commander
            build_commander()

            # Verify create_agent was called with 3 tools including xp_graph
            call_kwargs = mock_create.call_args.kwargs
            tools = call_kwargs.get("tools", [])
            tool_names = [getattr(t, "name", str(t)) for t in tools]
            assert any("xp_graph" in n for n in tool_names), \
                f"xp_graph tool not found in tools list: {tool_names}"


class TestXpGraphDirectly:
    """Direct unit tests for xp_graph tool behavior."""

    @pytest.mark.asyncio
    async def test_xp_graph_returns_methods_when_nodes_found(self):
        """When recall finds nodes, xp_graph should assemble context and return filtered result."""
        mock_db = MagicMock()
        mock_nodes = [MagicMock(name="node1"), MagicMock(name="node2")]
        mock_edges = [MagicMock(name="edge1")]

        with patch("agent.tools.xp_retrieve.get_db", return_value=mock_db), \
             patch("agent.tools.xp_retrieve.resolve_db_path") as mock_path, \
             patch("agent.tools.xp_retrieve.Recaller") as mock_recaller_cls, \
             patch("agent.tools.xp_retrieve.build_embed_model"), \
             patch("agent.tools.xp_retrieve.build_auxiliary_llm") as mock_aux_llm, \
             patch("agent.tools.xp_retrieve.assemble_context") as mock_assemble:

            mock_path.return_value.as_posix.return_value = "/tmp/test.db"

            mock_recaller = MagicMock()
            mock_recaller.recall = AsyncMock(return_value={"nodes": mock_nodes, "edges": mock_edges})
            mock_recaller_cls.return_value = mock_recaller

            mock_assemble.return_value = {"xml": "<node><name>test-skill</name></node>"}

            mock_llm = MagicMock()
            mock_response = MagicMock()
            mock_response.content = "Here is the method for deploying: use Docker build then kubectl apply."
            mock_llm.ainvoke = AsyncMock(return_value=mock_response)
            mock_aux_llm.return_value = mock_llm

            from agent.tools.xp_graph import xp_graph_tool
            result = await xp_graph_tool.ainvoke({"query": "deploy to kubernetes", "role": "test"})

            assert "deploy" in result.lower() or "docker" in result.lower() or "kubectl" in result.lower(), \
                f"Expected method content in result, got: {result}"

    @pytest.mark.asyncio
    async def test_xp_graph_returns_no_methods_when_no_nodes(self):
        """When recall finds nothing, xp_graph should return 'No relevant methods found.'"""
        mock_db = MagicMock()

        with patch("agent.tools.xp_retrieve.get_db", return_value=mock_db), \
             patch("agent.tools.xp_retrieve.resolve_db_path") as mock_path, \
             patch("agent.tools.xp_retrieve.Recaller") as mock_recaller_cls, \
             patch("agent.tools.xp_retrieve.build_embed_model"), \
             patch("agent.tools.xp_retrieve.build_auxiliary_llm"):

            mock_path.return_value.as_posix.return_value = "/tmp/test.db"

            mock_recaller = MagicMock()
            mock_recaller.recall = AsyncMock(return_value={"nodes": [], "edges": []})
            mock_recaller_cls.return_value = mock_recaller

            from agent.tools.xp_graph import xp_graph_tool
            result = await xp_graph_tool.ainvoke({"query": "nonsense query that matches nothing", "role": "test"})

            assert result == "No relevant methods found.", \
                f"Expected 'No relevant methods found.', got: {result}"

    @pytest.mark.asyncio
    async def test_xp_graph_llm_fallback_on_error(self):
        """When LLM filtering fails, xp_graph should fall back to raw XML or 'No relevant methods found.'"""
        mock_db = MagicMock()
        mock_nodes = [MagicMock(name="node1")]
        mock_edges = []

        with patch("agent.tools.xp_retrieve.get_db", return_value=mock_db), \
             patch("agent.tools.xp_retrieve.resolve_db_path") as mock_path, \
             patch("agent.tools.xp_retrieve.Recaller") as mock_recaller_cls, \
             patch("agent.tools.xp_retrieve.build_embed_model"), \
             patch("agent.tools.xp_retrieve.build_auxiliary_llm") as mock_aux_llm, \
             patch("agent.tools.xp_retrieve.assemble_context") as mock_assemble:

            mock_path.return_value.as_posix.return_value = "/tmp/test.db"

            mock_recaller = MagicMock()
            mock_recaller.recall = AsyncMock(return_value={"nodes": mock_nodes, "edges": mock_edges})
            mock_recaller_cls.return_value = mock_recaller

            mock_assemble.return_value = {"xml": "<node><name>fallback-skill</name></node>"}

            mock_llm = MagicMock()
            mock_llm.ainvoke = AsyncMock(side_effect=Exception("LLM unavailable"))
            mock_aux_llm.return_value = mock_llm

            from agent.tools.xp_graph import xp_graph_tool
            result = await xp_graph_tool.ainvoke({"query": "test query", "role": "test"})

            assert "fallback-skill" in result or result == "No relevant methods found.", \
                f"Expected fallback XML or no-methods message, got: {result}"
