"""The meeting HOST profile (plan §2.1 / §2.2 / §9, 阶段 2).

The host coordinates; it never does the participants' work. Its tool surface
is stripped to coordination-only tools (plan: "file/terminal/browser/code-intel
全禁"). This module owns the allow-list and the phase allow-list gate that
keeps the host from speaking on a participant's turn.

The gate is a pure function over the selector's execution plan, so it is testable
without an LLM or a live session.
"""

from __future__ import annotations

from agent.meeting.models import MeetingFlowStage, MeetingPhase

__all__ = [
    "HOST_TOOLS_ALLOWLIST",
    "HOST_PROMPT",
    "filter_host_tools",
    "phase_allows_speaker",
]


#: The only tools the host profile may use (plan §2.1). Everything else —
#: file, terminal, browser, code-intel, TaskFlow mutation — is stripped.
HOST_TOOLS_ALLOWLIST = frozenset(
    {
        "agent_list",
        "ask_agent",
        "memory_search",
        "current_time",
        "output_summary",
        "confirm_meeting_end",
    }
)

HOST_PROMPT = """You are the meeting host. You coordinate; you do not do the
participants' work. Your tools: agent_list, ask_agent, memory_search,
current_time, output_summary, confirm_meeting_end.

Rules:
- You never speak on behalf of a participant — you only organize.
- Speaker order follows business dependencies (frontend before backend,
  backend before the database), never round-robin.
- On a user interjection, classify it and adjust the execution plan.
- You cannot write files, run code, or browse the web.
- At most 40 rounds; summarize proactively as the cap approaches."""


def filter_host_tools(tools: list[dict] | list[str]) -> list[dict] | list[str]:
    """Strip a tool list down to the host allow-list, preserving input shape.

    Tool entries may be plain names or dicts carrying a ``name`` key; unknown
    shapes are dropped. The return type mirrors the input type.
    """
    if tools and isinstance(tools[0], str):
        return [tool for tool in tools if tool in HOST_TOOLS_ALLOWLIST]
    return [
        tool
        for tool in tools
        if isinstance(tool, dict) and tool.get("name") in HOST_TOOLS_ALLOWLIST
    ]


def phase_allows_speaker(phase_id: str | None, agent_id: str, plan: list[MeetingPhase]) -> bool:
    """Whether ``agent_id`` may speak in ``phase_id`` (phase allow-list gate).

    During a non-dispatching stage the host speaks directly, so the gate is not
    consulted. During dispatching, a phase with an empty allow-list means
    "unrestricted" (backwards-compatible with the spike's selector verdicts);
    otherwise the selected speaker must be in the phase's allow-list.
    """
    if not phase_id:
        return True
    phase = next((p for p in plan if p.id == phase_id), None)
    if phase is None or not phase.allowed_agent_ids:
        return True
    return agent_id in phase.allowed_agent_ids


def stage_allows_host_speaking(stage: MeetingFlowStage) -> bool:
    """Non-dispatching stages are host-only (bypass the selector)."""
    return stage is not MeetingFlowStage.DISPATCHING
