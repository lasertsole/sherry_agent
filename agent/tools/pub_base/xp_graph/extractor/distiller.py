"""
Distiller — Task-end distillation of high-signal experiences from
subagent execution into the knowledge graph.

Called from SubagentManager._run_subagent() after the task completes.
Uses auxiliary_llm to produce structured Node/Edge objects from:
  - The original task and final result
  - Commander's draft notes (strategy-level)
  - Worker output summaries (operation-level)

Strategy-level insights go into the "default" (commander) knowledge graph.
Operation-level insights go into the "worker" knowledge graph.
"""

import json
from typing import Any
from loguru import logger
from pydantic import BaseModel, Field
from models import auxiliary_llm
from langchain_core.messages import HumanMessage
from ..type import NodeType, EdgeType
from agent.tools.subagent.draft import get_drafts, clear_drafts

# ─── Pydantic models for structured output ──────────────────────

class DistillNode(BaseModel):
    type: str = Field(description="TASK, SKILL, or EVENT")
    name: str = Field(description="Concise verb-object name, e.g. 'parallel-research-deploy'")
    description: str = Field(description="One-line summary")
    content: str = Field(description="Detailed reusable knowledge (2-5 sentences)")

class DistillEdge(BaseModel):
    from_node: str = Field(description="Source node name")
    to_node: str = Field(description="Target node name")
    type: str = Field(description="USED_SKILL / SOLVED_BY / REQUIRES / PATCHES / CONFLICTS_WITH")
    instruction: str = Field(description="When/why this relationship applies")

class DistillResult(BaseModel):
    nodes: list[DistillNode] = Field(default_factory=list)
    edges: list[DistillEdge] = Field(default_factory=list)

# ─── Prompts ────────────────────────────────────────────────────

_STRATEGY_DISTILL_PROMPT = """\
You are an experience distiller for a task COMMANDER. Given the task execution summary
and strategy-level notes, extract reusable STRATEGY knowledge into a knowledge graph.

## Input
- Original Task: {task}
- Final Result: {result}
- Strategy Notes (from drafts and pre-compaction distillation):
{notes}

## Extraction Rules

### Nodes
- TASK: The type of task being solved (e.g., "multi-component-integration")
- SKILL: A reusable strategy/approach (e.g., "parallel-independent-decomposition")
- EVENT: A one-time error or pitfall (e.g., "dependency-cycle-timeout")

### Edges (strict direction rules)
- TASK → SKILL via USED_SKILL: "This type of task uses this strategy"
- EVENT/SKILL → SKILL via SOLVED_BY: "This problem was solved by this approach" (condition required)
- SKILL → SKILL via REQUIRES: "This strategy requires that prerequisite"
- SKILL → SKILL via PATCHES: "Newer approach corrects older one"

### Naming Convention
- TASK: verb-object (e.g., "deploy-bilibili-mcp")
- SKILL: tool-operation (e.g., "conda-env-isolation")
- EVENT: phenomenon-tool (e.g., "port-conflict-springboot")

### Content Template
- TASK: What was requested, key constraints, outcome
- SKILL: Step-by-step approach, why it works, when to use it
- EVENT: Error symptoms, root cause, workaround

Maximum 5 nodes and 5 edges. Only extract genuinely reusable knowledge — skip task-specific details that won't generalize.
Output as JSON matching the DistillResult schema."""

_OPERATION_DISTILL_PROMPT = """\
You are an experience distiller for a task WORKER. Given the subtask execution summary
and operation-level notes, extract reusable OPERATION knowledge into a knowledge graph.

## Input
- Subtask: {task}
- Result: {result}
- Operation Notes (from drafts and pre-compaction distillation):
{notes}

## Extraction Rules

### Nodes
- TASK: The type of subtask being executed (e.g., "npm-dependency-install")
- SKILL: A reusable tool/technique (e.g., "npm-ci-clean-install")
- EVENT: A one-time error or gotcha (e.g., "node-version-mismatch")

### Edges (strict direction rules)
- TASK → SKILL via USED_SKILL: "This type of subtask uses this tool/technique"
- EVENT/SKILL → SKILL via SOLVED_BY: "This problem was solved by this technique" (condition required)
- SKILL → SKILL via REQUIRES: "This technique requires that prerequisite"

### Naming Convention
- TASK: verb-object (e.g., "install-python-deps")
- SKILL: tool-operation (e.g., "pip-requirements-fix")
- EVENT: phenomenon-tool (e.g., "encoding-error-windows")

### Content Template
- TASK: What was done, specific commands, parameters used
- SKILL: Exact tool commands, flags, configuration, why they work
- EVENT: Error message, environment details, exact fix applied

Maximum 5 nodes and 5 edges. Only extract genuinely reusable knowledge — skip task-specific file paths or content.
Output as JSON matching the DistillResult schema."""


async def distill(
    task: str,
    result: str,
    session_id: str,
    commander_session_id: str,
    level: str = "strategy",
) -> DistillResult:
    """Distill experiences from a completed subagent task.

    Args:
        task: Original task description
        result: Final result text
        session_id: Master session ID (for commander drafts)
        commander_session_id: Commander session ID
        level: "strategy" for commander, "operation" for worker
    """
    drafts = get_drafts(commander_session_id)
    notes_text = "\n".join(f"- {d}" for d in drafts) if drafts else "(no notes captured)"

    prompt_template = _STRATEGY_DISTILL_PROMPT if level == "strategy" else _OPERATION_DISTILL_PROMPT
    prompt = prompt_template.format(task=task, result=result[:2000], notes=notes_text)

    try:
        llm_with_structure = auxiliary_llm.with_structured_output(DistillResult)
        distill_result: DistillResult = await llm_with_structure.ainvoke([HumanMessage(content=prompt)])
        return distill_result
    except Exception as e:
        logger.warning(f"[distiller:{level}] structured output failed, trying json_mode: {e}")
        try:
            llm_with_json = auxiliary_llm.bind(response_format={"type": "json_object"})
            response = await llm_with_json.ainvoke([HumanMessage(content=prompt + "\n\nRespond with valid JSON.")])
            data = json.loads(response.content if isinstance(response.content, str) else str(response.content))
            return DistillResult(**data)
        except Exception as e2:
            logger.error(f"[distiller:{level}] distillation failed: {e2}")
            return DistillResult()
    finally:
        clear_drafts(commander_session_id)


async def distill_and_ingest(
    task: str,
    result: str,
    session_id: str,
    commander_session_id: str,
) -> None:
    """Full pipeline: distill strategy + operation experiences, ingest into respective knowledge graphs.

    Strategy-level experiences go into the "default" (commander) graph.
    Operation-level experiences go into the "worker" graph.
    """
    from ..core import get_instance

    strategy_result = await distill(task, result, session_id, commander_session_id, level="strategy")
    if strategy_result.nodes:
        commander_memory = get_instance("default")
        await commander_memory.ingest_experiences(commander_session_id, strategy_result.nodes)
        logger.info(f"[distiller] ingested {len(strategy_result.nodes)} strategy nodes + {len(strategy_result.edges)} edges")

    operation_result = await distill(task, result, session_id, commander_session_id, level="operation")
    if operation_result.nodes:
        worker_memory = get_instance("worker")
        await worker_memory.ingest_experiences(commander_session_id, operation_result.nodes)
        logger.info(f"[distiller] ingested {len(operation_result.nodes)} operation nodes + {len(operation_result.edges)} edges")
