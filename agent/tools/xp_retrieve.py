from loguru import logger
from pydantic import BaseModel, Field
from models import  build_auxiliary_llm
from typing import Any, TypedDict, Annotated
from langchain_core.tools import tool, BaseTool
from langgraph.prebuilt.tool_node import InjectedState
from context_engine.xp_graph import assemble_context, build_config, get_db, Recaller


class RecallResult(TypedDict):
    nodes: list[Any]
    edges: list[Any]

class XPRetrieveSchema(BaseModel):
    query: str = Field(description="Query string used to search the experience knowledge graph for relevant methods, skills, or error solutions")

@tool("xp_retrieve", args_schema=XPRetrieveSchema)
async def xp_retrieve_tool(query: str, role: Annotated[str, InjectedState("session_id")] = "")-> str:
    """Search the experience knowledge graph for relevant methods, then summarize them into a readable answer.

    Recalls similar historical tasks, reusable skills, and past error solutions
    from the experience knowledge graph, then filters and organizes the results
    into a concise answer. Returns "No relevant methods found." when nothing
    matches the query."""
    db = get_db(role)

    recaller = Recaller(db, build_config(role))
    rec: RecallResult = await recaller.recall(query)
    nodes = rec["nodes"]
    edges = rec["edges"]

    if not nodes:
        return "No relevant methods found."

    assemble_result = assemble_context(
        db,
        recalled_nodes=nodes,
        recalled_edges=edges
    )
    xml = assemble_result["xml"]

    llm = build_auxiliary_llm(temperature=0.0)
    filter_prompt = (
        "You are an experience knowledge graph query assistant. Below is the recalled node and edge data (XML format).\n\n"
        "Graph structure:\n"
        "- Three node types: SKILL (reusable operational method), TASK (historical task), EVENT (historical error and solution)\n"
        "- Nodes are grouped by community; knowledge within the same community is related\n"
        "- Edge types:\n"
        "  · USED_SKILL: A TASK used a SKILL\n"
        "  · SOLVED_BY: An EVENT was resolved by a SKILL\n"
        "  · REQUIRES: One SKILL depends on another SKILL\n"
        "  · PATCHES: A newer SKILL corrects an older one\n"
        "  · CONFLICTS_WITH: Two SKILLs are mutually exclusive\n\n"
        "Filter the XML data below based on the user's question and return only the relevant methods.\n"
        f"User question: {query}\n\n"
        "Recalled knowledge graph data:\n"
        f"{xml}\n\n"
        "Rules:\n"
        "1. Keep only nodes and edges directly relevant to the user's question\n"
        "2. If relevant content is found, present it in clear, concise language\n"
        "3. If no relevant method is found, reply with exactly: No relevant methods found.\n"
        "4. Do not fabricate information — base your answer solely on the recalled data"
    )
    try:
        response = await llm.ainvoke(filter_prompt)
        if hasattr(response, 'content'):
            raw = response.content
            result = raw[0] if isinstance(raw, list) else raw
        else:
            result = str(response)
        result = str(result).strip()
        if "No relevant methods found." in result:
            return "No relevant methods found."
        return result
    except Exception as e:
        logger.error(f"[xp_retrieve] LLM filtering failed: {e}")
        return xml or "No relevant methods found."

def build_xp_retrieve_tool()-> BaseTool:
    xp_retrieve_tool.handle_tool_error = True
    xp_retrieve_tool.metadata = {"idempotent": True}
    return xp_retrieve_tool