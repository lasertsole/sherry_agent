import json
import asyncio
from loguru import logger
from langgraph.types import Command
from typing_extensions import override
from runtime import state_register_mem
from langchain.agents import create_agent
from typing import Callable, Awaitable, Any
from langchain_core.messages import HumanMessage
from langchain.agents.middleware import AgentState
from langchain.agents.middleware import AgentMiddleware
from langgraph.prebuilt.tool_node import ToolCallRequest
from langchain_core.messages import BaseMessage, ToolMessage
from typing import Any
from context_engine.xp_graph.core import ExperienceTrace, extract, PathStep

def _parse_nudge_json(raw: str) -> dict[str, Any]:
    """Parse the nudge LLM response into a {nodes, edges} dict.

    Strips markdown fences, tries to extract a JSON object from the string.
    Returns {"nodes": [], "edges": []} on parse failure.
    """
    cleaned = raw.strip()
    # Strip markdown code fences if present
    if cleaned.startswith("```"):
        # Remove opening fence (possibly with language tag)
        first_newline = cleaned.find("\n")
        if first_newline != -1:
            cleaned = cleaned[first_newline + 1 :]
        # Remove trailing fence
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].rstrip()
        elif "```" in cleaned:
            cleaned = cleaned[: cleaned.rfind("```")].rstrip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # Try to find the first {…} block as a fallback
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end > start:
            try:
                data = json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                logger.warning("_parse_nudge_json: failed to extract JSON from nudge output")
                return {"nodes": [], "edges": []}
        else:
            logger.warning("_parse_nudge_json: no JSON found in nudge output")
            return {"nodes": [], "edges": []}
    if not isinstance(data, dict):
        return {"nodes": [], "edges": []}
    return data


async def _persist_nudge_xpgraph(data: dict[str, Any], session_id: str) -> None:
    """Take parsed {nodes, edges} dict and persist via extract().

    Constructs an ExperienceTrace describing what was found (the task,
    the extraction action, and a summary of nodes/edges). Falls back
    to empty ExperienceTrace if data is empty.
    """
    nodes = data.get("nodes", []) or []
    edges = data.get("edges", []) or []
    if not nodes and not edges:
        logger.debug("_persist_nudge_xpgraph: no nodes/edges to persist, skipping")
        return

    node_summary = "; ".join(f"{n.get('type','?')}:{n.get('name','?')}" for n in nodes[:5])
    draft = ExperienceTrace(
        task="nudge: extract reusable experience from conversation",
        path=[PathStep(
            tool="nudge",
            input=f"extract XpGraph data: {len(nodes)} nodes, {len(edges)} edges",
            trigger="post-conversation nudge review",
        )],
        failures=[],
        requires=None,
    )
    try:
        result = await extract(experience_trace=draft, session_id=session_id)
        logger.debug(
            "_persist_nudge_xpgraph: persisted {} nodes, {} edges [{}]",
            len(result.nodes),
            len(result.edges),
            node_summary,
        )
    except Exception:
        logger.exception("_persist_nudge_xpgraph: extract() failed")


_MEMORY_REVIEW_PROMPT = (
    "Review the conversation above and consider saving to memory if appropriate.\n\n"
    "Focus on:\n"
    "1. Has the user revealed things about themselves — their persona, desires, "
    "preferences, or personal details worth remembering?\n"
    "2. Has the user expressed expectations about how you should behave, their work "
    "style, or ways they want you to operate?\n\n"
    "If something stands out, save it using the memory tool. "
    "If nothing is worth saving, just say 'Nothing to save.' and stop."
)

_SKILL_REVIEW_PROMPT = (
    "Review the conversation above and extract reusable experience as "
    "XpGraph nodes. Be ACTIVE — most sessions produce at least one "
    "experience node. A pass that does nothing is a missed learning "
    "opportunity, not a neutral outcome.\n\n"
    "XpGraph has three node types. Pick the right one per finding:\n"
    "  • SKILL — a reusable strategy, technique, workflow, or operation "
    "that a future agent can follow directly. Name: `tool-action` "
    "(e.g. `conda-env-create`, `docker-buildx-bake`). Content: plain "
    "text steps, commands, or instructions.\n"
    "  • EVENT — a one-time error, pitfall, or notable incident. "
    "Name: `phenomenon-context` (e.g. `importerror-libgl1`, "
    "`timeout-large-file-upload`). Content: the symptom, root cause, "
    "and fix used.\n"
    "  • TASK — a task or topic that appeared. Name: `verb-object` "
    "(e.g. `deploy-bilibili-mcp`). Content: high-level summary.\n\n"
    "Signals to extract from (any one warrants action):\n"
    "  • User corrected your style, tone, format, legibility, or "
    "verbosity. Frustration signals like 'stop doing X', 'this is too "
    "verbose', 'don't format like this', 'why are you explaining', "
    "'just give me the answer', 'you always do Y and I hate it', or "
    "an explicit 'remember this' are FIRST-CLASS SKILL node signals. "
    "Extract the preference as a SKILL node so the next session starts "
    "already knowing.\n"
    "  • User corrected your workflow, approach, or sequence of steps. "
    "Extract the correction as a SKILL node with the proper approach.\n"
    "  • Non-trivial technique, fix, workaround, debugging path, or "
    "tool-usage pattern emerged that a future session would benefit "
    "from. Extract it as a SKILL or EVENT node.\n"
    "  • A task was completed with non-trivial effort. Extract it as "
    "a TASK node linked to the SKILL/EVENT nodes used.\n\n"
    "Capture edges between nodes when you extract multiple:\n"
    "  • USED_SKILL — TASK → SKILL: this task used this skill\n"
    "  • SOLVED_BY — EVENT → SKILL: this error was solved by this "
    "skill\n"
    "  • REQUIRES — SKILL → SKILL: prerequisite dependency\n"
    "  • PATCHES — SKILL → SKILL: new skill corrects an old one\n"
    "  • CONFLICTS_WITH — SKILL ↔ SKILL: mutual exclusion\n\n"
    "Node naming rules:\n"
    "  • Lowercase, hyphenated, class-level names.\n"
    "  • NOT a specific PR number, error string, feature codename, "
    "library-alone name, or session artifact.\n"
    "  • If the name only makes sense for today's session, rethink it.\n\n"
    "Do NOT extract as nodes (these become persistent self-imposed "
    "constraints that bite you later when the environment changes):\n"
    "  • Environment-dependent failures: missing binaries, fresh-install "
    "errors, post-migration path mismatches, 'command not found', "
    "unconfigured credentials, uninstalled packages.\n"
    "  • Negative claims about tools or features ('browser tools do not "
    "work', 'X tool is broken'). These harden into refusals.\n"
    "  • Session-specific transient errors that resolved before the "
    "conversation ended. If retrying worked, the lesson is the retry "
    "pattern, not the original failure.\n"
    "  • One-off task narratives. A user asking 'summarize today's "
    "market' is not a reusable experience.\n"
    "  • User persona/facts about the user — those go to MEMORY, not "
    "xp_graph.\n\n"
    "Output format — respond with a JSON object containing extracted "
    "nodes and edges:\n"
    "{\n"
    '  "nodes": [\n'
    '    {"type": "SKILL|EVENT|TASK", "name": "...", "description": '
    '"one-line summary", "content": "detailed knowledge"}\n'
    "  ],\n"
    '  "edges": [\n'
    '    {"type": "USED_SKILL|SOLVED_BY|...", "from_name": "...", '
    '"to_name": "...", "instruction": "..."}\n'
    "  ]\n"
    "}\n\n"
    "If genuinely nothing stood out, respond with "
    '{"nodes": [], "edges": []} — but do NOT treat this as the '
    "default. Most sessions produce at least one node."
)

_COMBINED_REVIEW_PROMPT = (
    "Review the conversation above and update two things, then "
    "output ONLY valid JSON — nothing else:\n\n"
    "**Memory**: who the user is. Did the user reveal persona, "
    "desires, preferences, personal details, or expectations about "
    "how you should behave? Save facts about the user and durable "
    "preferences with the memory tool.\n\n"
    "**Experience (XpGraph)**: extract reusable experience as XpGraph "
    "nodes. Be ACTIVE — most sessions produce at least one node. A "
    "pass that does nothing is a missed learning opportunity, not a "
    "neutral outcome.\n\n"
    "XpGraph node types:\n"
    "  • SKILL — reusable strategy, technique, workflow. Name: "
    "`tool-action` (e.g. `conda-env-create`). Content: steps/instructions.\n"
    "  • EVENT — one-time error or pitfall. Name: "
    "`phenomenon-context` (e.g. `importerror-libgl1`). Content: "
    "symptom, root cause, fix.\n"
    "  • TASK — a task that was done. Name: `verb-object` "
    "(e.g. `deploy-bilibili-mcp`). Content: high-level summary.\n\n"
    "Signals that warrant extraction (any one is enough):\n"
    "  • User corrected your style, tone, format, legibility, "
    "verbosity, or approach. Frustration is a FIRST-CLASS SKILL node "
    "signal — extract the preference as a SKILL node.\n"
    "  • Non-trivial technique, fix, workaround, or debugging path "
    "emerged. Extract as SKILL or EVENT.\n"
    "  • A task was completed with non-trivial effort. Extract as TASK.\n\n"
    "Capture edges between nodes when you extract multiple:\n"
    "  • USED_SKILL — TASK → SKILL\n"
    "  • SOLVED_BY — EVENT → SKILL\n"
    "  • REQUIRES — SKILL → SKILL\n"
    "  • PATCHES — SKILL → SKILL\n"
    "  • CONFLICTS_WITH — SKILL ↔ SKILL\n\n"
    "Node naming rules:\n"
    "  • Lowercase, hyphenated, class-level names.\n"
    "  • NOT a specific PR number, error string, feature codename, "
    "library-alone name, or session artifact.\n\n"
    "Do NOT extract as nodes:\n"
    "  • Environment-dependent failures (missing binaries, uninstalled "
    "packages, path mismatches).\n"
    "  • Negative claims about tools ('this tool is broken').\n"
    "  • Session-specific transient errors that resolved.\n"
    "  • One-off task narratives.\n\n"
    "CRITICAL — Your response must be ONLY a JSON object with the "
    "XpGraph nodes and edges. Do NOT add any text, markdown, or "
    "explanations. If you used the memory tool, fine — but your "
    "final message to me must be ONLY this JSON:\n"
    "{\n"
    '  "nodes": [\n'
    '    {"type": "SKILL|EVENT|TASK", "name": "...", "description": '
    '"one-line summary", "content": "detailed knowledge"}\n'
    "  ],\n"
    '  "edges": [\n'
    '    {"type": "USED_SKILL|...", "from_name": "...", '
    '"to_name": "...", "instruction": "..."}\n'
    "  ]\n"
    "}\n\n"
    "Act on whichever of the two dimensions has real signal. If "
    'genuinely nothing stands out on either, respond with '
    '{"nodes": [], "edges": []} — but do NOT treat this as the '
    "default. Most sessions produce at least one node."
)

class _NudgeLimitTool(AgentMiddleware):
    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        logger.debug("{} awrap_tool_call hook fired", type(self).__name__)
        tool_name: str = request.tool_call.get("name", "unknown")

        if not self._is_nudge_allowed(request.tool):
            return ToolMessage(
                content=(
                    f"Tool [{tool_name}] is not allowed during nudge phase. "
                    "Execution has been skipped. Please reconsider your approach."
                ),
                tool_call_id=request.tool_call["id"],
                name=tool_name,
                status="error",
            )

        return await handler(request)

    @staticmethod
    def _is_nudge_allowed(tool: Any) -> bool:
        if tool is not None and isinstance(getattr(tool, "metadata", None), dict):
            return bool(tool.metadata.get("nudge", False))
        return False

class StateSchema(AgentState):
    """Agent state that preserves an ``session_id``."""
    session_id: str

async def _create_nudge_agent(system_prompt: str):
    from agent import get_agent_tools
    from models import build_main_llm
    from agent.middlewares import ToolGuardrails, IterationBudget
    from agent.tools.xp_graph import build_xp_graph_tool

    # Exclude xp_graph_tool to prevent recursive extract() calls.
    # Nudge already persists XpGraph data via _persist_nudge_xpgraph()
    # after the agent returns, so the tool is unnecessary and dangerous.
    xp_tool = build_xp_graph_tool()
    all_tools = get_agent_tools()
    nudge_tools = [t for t in all_tools if t.name != xp_tool.name]

    main_llm = build_main_llm()  # Create a fresh LLM instance for the current event loop
    return create_agent(
        model=main_llm,
        state_schema=StateSchema,
        system_prompt=system_prompt,
        middleware=[_NudgeLimitTool(), ToolGuardrails(), IterationBudget()],
        tools=nudge_tools
    )

async def _nudge_memory(session_id: str, system_prompt: str, messages: list[BaseMessage]) -> None:
    state_register_mem.set_state(session_id, "nudge_review_memory_lock", True)
    try:
        _agent = await _create_nudge_agent(system_prompt)
        res = await _agent.ainvoke(input={"session_id": session_id, "messages": [*messages, HumanMessage(content=_MEMORY_REVIEW_PROMPT)]})
        logger.debug("nudge memory res is {}", res["messages"][-1])
    finally:
        state_register_mem.set_state(session_id, "nudge_review_memory_lock", False)


async def _nudge_skill(session_id: str, system_prompt: str, messages: list[BaseMessage]) -> None:
    state_register_mem.set_state(session_id, "nudge_review_skill_lock", True)
    try:
        _agent = await _create_nudge_agent(system_prompt)
        res = await _agent.ainvoke(input={"session_id": session_id, "messages": [*messages, HumanMessage(content=_SKILL_REVIEW_PROMPT)]})
        raw = res["messages"][-1].content if hasattr(res["messages"][-1], "content") else str(res["messages"][-1])
        logger.debug("nudge skill raw output: {}", raw)
        data = _parse_nudge_json(raw)
        await _persist_nudge_xpgraph(data, session_id)
    finally:
        state_register_mem.set_state(session_id, "nudge_review_skill_lock", False)


async def _nudge_combined(session_id: str, system_prompt: str, messages: list[BaseMessage]) -> None:
    state_register_mem.set_state(session_id, "nudge_review_memory_lock", True)
    state_register_mem.set_state(session_id, "nudge_review_skill_lock", True)
    try:
        _agent = await _create_nudge_agent(system_prompt)
        res = await _agent.ainvoke(input={"session_id": session_id, "messages": [*messages, HumanMessage(content=_COMBINED_REVIEW_PROMPT)]})
        raw = res["messages"][-1].content if hasattr(res["messages"][-1], "content") else str(res["messages"][-1])
        logger.debug("nudge combined raw output: {}", raw)
        data = _parse_nudge_json(raw)
        await _persist_nudge_xpgraph(data, session_id)
    finally:
        state_register_mem.set_state(session_id, "nudge_review_memory_lock", False)
        state_register_mem.set_state(session_id, "nudge_review_skill_lock", False)