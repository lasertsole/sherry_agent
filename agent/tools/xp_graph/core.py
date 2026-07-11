from loguru import logger
from typing import Literal, Annotated
from pydantic import BaseModel, Field
from langchain_core.tools import tool
from runtime import state_register_mem
from langgraph.prebuilt.tool_node import InjectedState


# ─── ExperienceTrace Models ─────────────────────────────────────
class Fix(BaseModel):
    """A fix strategy for a failure."""
    strategy: Literal["parameter", "approach", "workaround"] = Field(
        description="Type of fix strategy"
    )
    description: str = Field(description="Description of the fix")
    tool: str | None = Field(default=None, description="Tool used for the fix")


class Failure(BaseModel):
    """A failure encountered during task execution."""
    symptom: str = Field(description="What went wrong (symptom)")
    cause: str = Field(description="Root cause of the failure")
    fixes: list[Fix] = Field(default=[], description="Fixes applied or attempted")
    abandoned: bool = Field(default=False, description="Whether this failure path was abandoned")


class PathStep(BaseModel):
    """A step in the execution path."""
    tool: str = Field(description="Tool or skill used")
    input: str = Field(description="Input or command executed")
    output: str | None = Field(default=None, description="Output or result")


class ExperienceTrace(BaseModel):
    """Structured trace of an agent's experience during task execution.
    
    This is the core data structure for the draft system. It captures:
    - The task being performed
    - The execution path (tools used, inputs, outputs)
    - Failures encountered and fixes applied
    - Dependencies on other skills
    """
    task: str = Field(description="Description of the task being performed")
    path: list[PathStep] = Field(default=[], description="Execution path steps")
    failures: list[Failure] = Field(default=[], description="Failures encountered")
    requires: list[str] | None = Field(default=None, description="Required skills or prerequisites")


def _handle_read_draft(session_id: str) -> str:
    """Read the current draft for a session."""

    draft_data: ExperienceTrace = state_register_mem.get_state(session_id, "xp_graph_draft", None)
    if draft_data is not None:
        import json
        return json.dumps(draft_data, ensure_ascii=False)
    return "draft is empty"


def _handle_rewrite_draft(session_id: str, content: str | None) -> str:
    """Overwrite the draft with a parsed ExperienceTrace."""
    if not content or not content.strip():
        return "Input content cannot be empty"
    try:
        import json
        trace = ExperienceTrace(**json.loads(content))
        state_register_mem.set_state(session_id, "xp_graph_draft", trace.model_dump())
        return "Draft rewritten successfully"
    except Exception as e:
        logger.warning("rewrite_draft failed: {}", e)
        return f"Rewrite failed: {e}"


def _handle_merge_draft(session_id: str, content: str | None) -> str:
    """Parse incoming JSON as ExperienceTrace and merge into existing draft."""
    if not content or not content.strip():
        return "Input content cannot be empty"
    try:
        import json
        incoming = ExperienceTrace(**json.loads(content))

        # Read existing trace from state register
        draft_data = state_register_mem.get_state(session_id, "xp_graph_draft", None)
        existing: ExperienceTrace | None = None
        if draft_data is not None:
            if isinstance(draft_data, dict):
                existing = ExperienceTrace(**draft_data)
            elif isinstance(draft_data, str):
                existing = ExperienceTrace(**json.loads(draft_data))

        # Append-mode merge logic
        if existing is None:
            merged = incoming.model_copy(deep=True)
        else:
            merged = existing.model_copy(deep=True)
            merged.task = incoming.task
            merged.path = merged.path + incoming.path
            merged.failures = merged.failures + incoming.failures
            if incoming.requires:
                if merged.requires is None:
                    merged.requires = list(incoming.requires)
                else:
                    seen = set(merged.requires)
                    for req in incoming.requires:
                        if req not in seen:
                            merged.requires.append(req)
                            seen.add(req)
        state_register_mem.set_state(session_id, "xp_graph_draft", merged.model_dump())
        return "Draft merged successfully"
    except Exception as e:
        logger.warning("merge_draft failed: {}", e)
        return f"Merge failed: {e}"

def _handle_write_graph() -> str:
    """Write to graph (placeholder for future implementation)."""
    return ""


def _handle_retrieve_graph(query: str | None = None) -> str:
    """Retrieve knowledge from the graph using a natural language query.

    Uses the Recaller's dual-path recall (precise + generalized) to find
    relevant nodes and edges. Returns formatted XML context string.

    Args:
        query: Natural language search query.

    Returns:
        Formatted knowledge context string, or empty string if query is empty.
    """
    if not query or not query.strip():
        return "Query is empty. Please provide a natural language query string for retrieval."

    try:
        import asyncio
        from .base import get_instance

        instance = get_instance("default")
        loop = asyncio.get_event_loop()
        result = loop.run_until_complete(instance.assemble(query))
        if result and "system_prompt_addition" in result:
            return result["system_prompt_addition"]
        return ""
    except Exception as e:
        logger.warning("retrieve_graph failed: {}", e)
        return ""


def _resolve_draft_content(
    experience_trace: ExperienceTrace | None,
    query: str | None,
) -> str | None:
    """Resolve draft content from experience_trace or query string.

    `experience_trace` takes precedence. Falls back to `query` as JSON string.
    """
    if experience_trace is not None:
        import json
        return json.dumps(experience_trace.model_dump(), ensure_ascii=False)
    return query


def _dispatch_xp_graph(
    mode: Literal["read_draft", "rewrite_draft", "merge_draft", "update_graph", "retrieve_graph"],
    session_id: str,
    experience_trace: ExperienceTrace | None = None,
    query: str | None = None,
) -> str:
    """Dispatch xp_graph request to the appropriate handler based on mode.

    Args:
        mode: Operation mode.
        session_id: Current session ID.
        experience_trace: Structured ExperienceTrace for draft operations.
        query: Natural language query for retrieve_graph, or JSON string fallback for drafts.
    """

    if mode == "read_draft":
        return _handle_read_draft(session_id)

    elif mode == "rewrite_draft":
        content = _resolve_draft_content(experience_trace, query)
        return _handle_rewrite_draft(session_id, content)

    elif mode == "merge_draft":
        content = _resolve_draft_content(experience_trace, query)
        return _handle_merge_draft(session_id, content)

    elif mode == "update_graph":
        return _handle_write_graph()

    elif mode == "retrieve_graph":
        return _handle_retrieve_graph(query)

    return f"Unknown mode: {mode}"

# ─── XP Graph Tool ──────────────────────────────────────────────
class XPGraphSchema(BaseModel):
    """Schema for the xp_graph tool.

    Two independent payload fields:
      - `experience_trace`: Structured ExperienceTrace object for draft operations
        (rewrite_draft / merge_draft).
      - `query`: Natural language string for knowledge retrieval (retrieve_graph mode).
        Also accepted as JSON-serialized ExperienceTrace string for draft operations
        when `experience_trace` is not provided (legacy compatibility).

    Modes:
      - read_draft:      Read the current draft string.
      - rewrite_draft:   Overwrite the draft with a new ExperienceTrace.
                         Reads from `experience_trace` first, falls back to `query` JSON.
      - merge_draft:     Merge an incoming ExperienceTrace into the existing draft.
                         Reads from `experience_trace` first, falls back to `query` JSON.
      - update_graph:    Write new graph data (placeholder for future implementation).
      - retrieve_graph:  Retrieve knowledge from the graph using `query` as the search string.
    """
    mode: Literal[
        "read_draft", "rewrite_draft", "merge_draft",
        "update_graph", "retrieve_graph"
    ] = Field(description="Operation mode")
    experience_trace: ExperienceTrace | None = Field(
        default=None,
        description=(
            "Structured ExperienceTrace object for draft operations (rewrite_draft / merge_draft)."
            " When both `experience_trace` and `query` are provided, `experience_trace` takes"
            " precedence for draft modes."
        ),
    )
    query: str | None = Field(
        default=None,
        description=(
            "Natural language query string for knowledge retrieval via `retrieve_graph` mode."
            " Also accepted as JSON-serialized ExperienceTrace string for draft operations"
            " when `experience_trace` is not provided (legacy compatibility)."
        ),
    )


@tool("xp_graph", args_schema=XPGraphSchema)
def xp_graph(
    mode: Literal["read_draft", "rewrite_draft", "merge_draft", "update_graph", "retrieve_graph"],
    experience_trace: ExperienceTrace | None = None,
    query: str | None = None,
    role: Annotated[str, InjectedState("role")] = "",
    session_id: Annotated[str, InjectedState("session_id")] = "",
):
    """Experience graph draft tool.

    Manages structured experience traces during task execution and retrieves
    knowledge from the experience graph.

    Modes:
      - read_draft:     Return the current draft as a string.
      - rewrite_draft:  Overwrite the draft with the given ExperienceTrace.
                        Reads from `experience_trace` first, falls back to `query` as JSON.
                        Returns a confirmation message.
      - merge_draft:    Merge an incoming ExperienceTrace into the existing draft
                        (append path/failures, overwrite task, dedup requires).
                        Reads from `experience_trace` first, falls back to `query` as JSON.
                        Returns a confirmation message.
      - update_graph:   (Placeholder) Write graph data.
      - retrieve_graph: Retrieve knowledge from the graph using `query` as the
                        natural language search string.

    When the draft is empty, both rewrite_draft and merge_draft behave the same
    (set the draft to the incoming trace).
    """
    if not session_id or not session_id.strip():
        err_text = "session id can not setting"
        logger.error(err_text)
        raise RuntimeError("session id can not setting")

    return _dispatch_xp_graph(mode, session_id, experience_trace, query)


def build_xp_graph_tool():
    xp_graph.metadata = {"idempotent": False}
    xp_graph.handle_tool_error = True
    return xp_graph