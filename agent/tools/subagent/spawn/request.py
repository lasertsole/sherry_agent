"""The immutable request object a subagent spawn is built from.

The audit (2026-11-09 H4) replaced ``spawn_subagent_direct``'s 22-parameter
signature with :class:`SpawnRequest`: one frozen dataclass owns the fields and
their defaults, and the orchestration receives it as a single argument.

``spawn_subagent_direct`` still accepts keyword arguments through
:meth:`SpawnRequest.from_legacy` — dozens of existing call sites, tests and
scripts use that shape, and a hard cutover would be a compatibility break with
no functional gain. New callers should construct the request directly.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Literal

__all__ = ["SpawnRequest"]


@dataclass(frozen=True)
class SpawnRequest:
    """Everything one subagent spawn needs, with the historical defaults.

    Field defaults are byte-for-byte the old function-signature defaults; the
    spawn phase code binds them to locals and never mutates the request.
    """

    task: str
    requester_session_key: str
    agent_id: str = "main"
    task_name: str | None = None
    label: str | None = None
    thinking: str | None = None
    cleanup: Literal["delete", "keep"] = "delete"
    attachments: list[dict] | None = None
    cwd: str | None = None
    completion_owner_key: str | None = None
    expects_completion_message: bool = True
    run_timeout_seconds: float | None = None
    swarm_group_id: str | None = None
    output_schema: dict | None = None
    model: str | None = None
    launch_fingerprint: str | None = None
    goal_max_turns: int | None = None
    functional_role_hint: str | None = None
    extra_tools: list[str] | None = None
    #: Per-expert domain persona appended to the child system prompt (expert
    #: panel / meeting modes carry it from the expert registry).
    persona_prompt: str | None = None
    #: Expert's skill whitelist — the dispatch path merges it into the step's
    #: ``skills`` (SKILL.md prepend); carried here for spawn diagnostics.
    skills_whitelist: list[str] | None = None
    isolation: bool = False
    flow_id: str | None = None
    step_id: str | None = None
    #: High-risk keyword approval carried into the child: after a human approves
    #: a child's ``[NEEDS_APPROVAL]`` bubble, the replacement spawn receives
    #: ``{"keyword": ..., "tool": ..., "args_hash"?: ...}`` and its ToolGuardrails
    #: lets that keyword through (per-spawn, never a permanent allow).
    approval_context: dict | None = None

    @classmethod
    def from_legacy(cls, **kwargs: Any) -> SpawnRequest:
        """Build a request from the historical keyword-argument call shape.

        Unknown names raise ``TypeError`` (mirroring the old signature) so a
        typo or a removed field fails loudly instead of being silently dropped.
        """
        known = {field.name for field in fields(cls)}
        unknown = sorted(set(kwargs) - known)
        if unknown:
            raise TypeError(
                "spawn_subagent_direct() got unexpected keyword argument(s): " + ", ".join(unknown)
            )
        return cls(**kwargs)

    def as_kwargs(self) -> dict[str, Any]:
        """The field dict (for diagnostics and compatibility helpers)."""
        return {field.name: getattr(self, field.name) for field in fields(self)}
