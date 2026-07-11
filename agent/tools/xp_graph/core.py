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
    draft_data = state_register_mem.get_state(session_id, "xp_graph_draft", "")
    if isinstance(draft_data, dict):
        import json
        return json.dumps(draft_data, ensure_ascii=False)
    return str(draft_data) if draft_data else ""


def _handle_rewrite_draft(session_id: str, content: str | None) -> str:
    """Overwrite the draft with raw content string."""
    if not content or not content.strip():
        return "Input content cannot be empty"
    state_register_mem.set_state(session_id, "xp_graph_draft", content)
    return "Draft rewritten successfully"


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

def _write_graph(content: str) -> str:
    """TODO Write to graph (placeholder for future implementation)."""
    return ""


def _retrieve_graph(content: str) -> str:
    """TODO Retrieve graph data (placeholder for future implementation)."""
    return ""


def _dispatch_xp_graph(
    mode: Literal["read_draft", "rewrite_draft", "merge_draft", "update_graph", "retrieve_graph"],
    session_id: str,
    content: str | None,
) -> str:
    """Dispatch xp_graph request to the appropriate handler based on mode."""

    if mode == "read_draft":
        return _handle_read_draft(session_id)

    if mode == "rewrite_draft":
        return _handle_rewrite_draft(session_id, content)

    if mode == "merge_draft":
        return _handle_merge_draft(session_id, content)

    if mode == "update_graph":
        return _write_graph(content)

    if mode == "retrieve_graph":
        return _retrieve_graph(content)

    return f"Unknown mode: {mode}"

# ─── XP Graph Tool ──────────────────────────────────────────────
class XPGraphSchema(BaseModel):
    """Schema for the xp_graph tool.

    Modes:
      - read_draft:      Read the current draft string.
      - rewrite_draft:   Overwrite the draft with a new ExperienceTrace JSON.
      - merge_draft:     Merge an incoming ExperienceTrace JSON into the existing
                         draft (append path/failures, overwrite task, dedup requires).
      - update_graph:    Write new graph data (placeholder for future implementation).
      - retrieve_graph:  Read graph data (placeholder for future implementation).
    """
    mode: Literal[
        "read_draft", "rewrite_draft", "merge_draft",
        "update_graph", "retrieve_graph"
    ] = Field(description="Operation mode")
    content: str | None = Field(
        default=None,
        description="ExperienceTrace JSON (required for rewrite_draft, merge_draft, update_graph; ignored for read_draft and retrieve_graph)"
    )


@tool("xp_graph", args_schema=XPGraphSchema)
def xp_graph(
    mode: Literal["read_draft", "rewrite_draft", "merge_draft", "update_graph", "retrieve_graph"],
    content: str | None = None,
    session_id: Annotated[str, InjectedState("session_id")] = "",
):
    """Experience graph draft tool.

    Manages structured experience traces during task execution.

    Modes:
      - read_draft:     Return the current draft as a string.
      - rewrite_draft:  Overwrite the draft with the given ExperienceTrace JSON.
                        Returns a confirmation message.
      - merge_draft:    Parse the given JSON as ExperienceTrace and merge it into
                        the existing draft (append path/failures, overwrite task,
                        dedup requires).  Returns a confirmation message.
      - update_graph:   (Placeholder) Write graph data.
      - retrieve_graph: (Placeholder) Read graph data.

    When the draft is empty, both rewrite_draft and merge_draft behave the same
    (set the draft to the incoming trace).
    """
    if not session_id or not session_id.strip():
        err_text = "session id can not setting"
        logger.error(err_text)
        raise RuntimeError("session id can not setting")

    return _dispatch_xp_graph(mode, session_id, content)


def build_xp_graph_tool():
    xp_graph.metadata = {"idempotent": False}
    xp_graph.handle_tool_error = True
    return xp_graph