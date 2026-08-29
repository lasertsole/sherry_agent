"""Convenience ``delegate_task`` API for dispatching sub-agent tasks.

This module wraps :func:`agent.tools.subagent.spawn_subagent_direct` with a
developer-friendly synchronous entry point that:

* Fills in sensible defaults (depth, concurrency, timeout, context mode all
  fall back to :class:`~agent.tools.subagent.config.SubagentConfig`).
* Validates ``load_skills`` against the *actual* skill set discovered by
  :func:`skills.loader.scan_skills`, warning (not failing) on unknown names,
  and refuses to inject skills whose frontmatter scope is ``main_only``
  (subagent-invisible skills, e.g. the high-risk auth skills).
* Injects the selected skills into the child's task text as an
  ``<available_skills>`` XML block so the sub-agent is actively steered toward
  the available capabilities.
* Supports both fire-and-forget (:param:`run_in_background=True`) and blocking
  dispatch (:param:`run_in_background=False`) modes.

Import path:

.. code-block:: python

    from agent.tools.subagent import delegate_task

    agent_id = "sub-agent-<task_hash>"
    result = delegate_task(
        agent_id=agent_id,
        task="<clear goal and deliverable>",
        requester_session_key="agent:main:session:<uuid>",
        load_skills=["debugging", "security-review"],  # or []
        run_in_background=True,
    )

The ``load_skills`` decision flow (required by convention): evaluate the
available skill set before dispatch, pass task-appropriate skill names when
relevant, and pass ``[]``/``None`` ONLY when no skill matches the task domain.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Literal

from skills.loader import get_skills_text, scan_skills

from .config import SubagentConfig, get_config
from .registry import get_run
from .spawn import SpawnResult, spawn_subagent_direct
from .types import SpawnMode, ContextMode

logger = logging.getLogger(__name__)


@dataclass
class DelegatedTaskHandle:
    """Pollable handle returned when ``run_in_background=True``.

    Mirrors :class:`SpawnResult` for the spawn outcome and adds helper
    methods for polling the live background run.
    """

    status: Literal["accepted", "forbidden", "error"]
    child_session_key: str | None = None
    run_id: str | None = None
    task_name: str | None = None
    error: str | None = None
    note: str | None = None
    background: bool = True
    #: Populated once the run reaches a terminal state (via poll/result).
    result_text: str | None = None
    terminal_error: str | None = None

    @property
    def accepted(self) -> bool:
        return self.status == "accepted"

    @property
    def forbidden(self) -> bool:
        return self.status == "forbidden"

    def is_running(self) -> bool:
        """Return True while the background run has not reached a terminal state."""
        if not self.accepted or not self.run_id:
            return False
        run = get_run(self.run_id)
        if not run:
            return False
        status = getattr(run.execution, "status", None) or getattr(
            run, "execution_status", "TERMINAL"
        )
        return str(status).upper() != "TERMINAL"

    def poll(self) -> "DelegatedTaskHandle":
        """Refresh terminal result fields from the live registry record (no sleep).

        Returns ``self`` for chaining.
        """
        if not self.accepted or not self.run_id:
            return self
        run = get_run(self.run_id)
        if run and str(
            getattr(run.execution, "status", None) or getattr(run, "execution_status", "")
        ).upper() == "TERMINAL":
            self.result_text, self.terminal_error = _terminal_text(self.run_id)
        return self

    def result(self, timeout: float | None = None, poll_interval: float = 0.25) -> "DelegatedTaskHandle":
        """Block until the run reaches a terminal state; returns ``self`` populated.

        Args:
            timeout: Optional overall wall-clock deadline in seconds. If the run
                is still pending when the deadline passes, returns early with
                whatever state is available (never raises).
            poll_interval: Registry polling cadence in seconds.

        This is safe to call from an already-running event loop.
        """
        if not self.accepted or not self.run_id:
            return self
        try:
            asyncio.get_running_loop()
            in_loop = True
        except RuntimeError:
            in_loop = False
        if in_loop:
            if self.is_running():
                # Poll loop inline without blocking the outer loop.
                import time
                deadline = None if timeout is None else time.monotonic() + timeout
                while self.is_running():
                    if deadline is not None and time.monotonic() >= deadline:
                        break
                    time.sleep(poll_interval)
                self.poll()
            return self
        return _await_outside_loop(self, timeout=timeout, poll_interval=poll_interval)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "child_session_key": self.child_session_key,
            "run_id": self.run_id,
            "task_name": self.task_name,
            "error": self.error,
            "note": self.note,
            "background": self.background,
            "result_text": self.result_text,
            "terminal_error": self.terminal_error,
        }


def _terminal_text(run_id: str) -> tuple[str | None, str | None]:
    """Extract ``(result_text, error)`` from a run record once it is terminal."""
    run = get_run(run_id)
    if not run:
        return None, "run record not found"
    # SubagentRunRecord nests these fields: result lives on ``completion``,
    # outcome/error live on ``execution.outcome`` (a RunOutcome model).
    result_text = getattr(run.completion, "result_text", None)
    err = None
    outcome = getattr(run.execution, "outcome", None)
    if outcome is not None:
        outcome_status = getattr(outcome, "status", None)
        if getattr(outcome, "error", None):
            err = outcome.error
        elif outcome_status is not None and str(getattr(outcome_status, "value", outcome_status)).lower() != "ok":
            err = str(getattr(outcome_status, "value", outcome_status)).lower()
    return result_text, err


def _await_outside_loop(
    handle: DelegatedTaskHandle, timeout: float | None, poll_interval: float
) -> DelegatedTaskHandle:
    """Synchronous poll loop used when the caller has no running event loop."""
    import time

    deadline = None if timeout is None else time.monotonic() + timeout
    while handle.is_running():
        if deadline is not None and time.monotonic() >= deadline:
            break
        time.sleep(poll_interval)
    handle.poll()
    return handle


# ---------------------------------------------------------------------------
# skill injection
# ---------------------------------------------------------------------------


def _validate_load_skills(load_skills: list[str] | None) -> list[str]:
    """Validate requested skills against the real skill set.

    Returns the list of skill names that actually exist and are visible to a
    subagent caller. Unknown names are warned about and dropped; skills whose
    frontmatter scope is ``main_only`` are silently excluded — never fatal.
    (Scope contract: agent/tools/pub_base/skill_utils.py::skill_visible_to.)
    """
    if not load_skills:
        return []
    known = {s["name"]: s for s in scan_skills()}
    resolved: list[str] = []
    for name in load_skills:
        entry = known.get(name)
        if entry is None:
            continue
        scope = str(entry.get("scope") or "").strip().lower()
        if scope == "main_only":
            # main_only skills are invisible to subagent callers; excluded
            # silently, mirroring the previous auth-skill behavior.
            continue
        resolved.append(name)
    unknown = [name for name in load_skills if name not in known]
    if unknown:
        logger.warning(
            "delegate_task: unknown skill(s) ignored: %s. Known skills: %s",
            ", ".join(sorted(unknown)),
            ", ".join(sorted(known)),
        )
    return resolved


def _inject_skills(task: str, load_skills: list[str] | None) -> str:
    """Append selected skills' ``<available_skills>`` XML block to the task text."""
    resolved = _validate_load_skills(load_skills)
    if not resolved:
        return task
    skills_xml = get_skills_text(selected_skill_names=resolved, caller_scope="subagent")
    if not skills_xml:
        return task
    return f"{task}\n\n---\nSelected skills available for this task:\n{skills_xml}\n"


# ---------------------------------------------------------------------------
# internal async dispatch
# ---------------------------------------------------------------------------


async def _dispatch_async(
    *,
    task: str,
    requester_session_key: str,
    agent_id: str,
    task_name: str | None,
    label: str | None,
    thinking: str | None,
    context: ContextMode,
    run_timeout_seconds: float | None,
    output_schema: dict[str, Any] | None,
    model: str | None,
    max_spawn_depth: int | None,
    max_children_per_agent: int | None,
) -> SpawnResult:
    """Run the async spawn pipeline with per-call config overrides applied and
    restored on a throwaway basis (never mutating lasting global state)."""
    cfg: SubagentConfig = get_config()
    prev_max_depth = cfg.max_spawn_depth
    prev_max_children = cfg.max_children_per_agent
    prev_timeout = cfg.run_timeout_seconds
    if max_spawn_depth is not None:
        cfg.max_spawn_depth = max_spawn_depth
    if max_children_per_agent is not None:
        cfg.max_children_per_agent = max_children_per_agent
    if run_timeout_seconds is not None:
        cfg.run_timeout_seconds = run_timeout_seconds
    try:
        return await spawn_subagent_direct(
            task=task,
            requester_session_key=requester_session_key,
            agent_id=agent_id,
            task_name=task_name,
            label=label,
            thinking=thinking,
            spawn_mode=SpawnMode.RUN,
            cleanup="delete",
            context=context,
            run_timeout_seconds=run_timeout_seconds,
            output_schema=output_schema,
            model=model,
        )
    finally:
        cfg.max_spawn_depth = prev_max_depth
        cfg.max_children_per_agent = prev_max_children
        cfg.run_timeout_seconds = prev_timeout


def _to_handle(result: SpawnResult, background: bool) -> DelegatedTaskHandle:
    """Map a :class:`SpawnResult` onto a :class:`DelegatedTaskHandle`."""
    if result.status == "accepted":
        return DelegatedTaskHandle(
            status="accepted",
            child_session_key=result.child_session_key,
            run_id=result.run_id,
            task_name=result.task_name,
            note=result.note,
            background=background,
        )
    return DelegatedTaskHandle(
        status="error" if result.status == "error" else "forbidden",
        error=result.error,
        task_name=result.task_name,
        note=result.note,
        background=background,
    )


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def delegate_task(
    task: str,
    *,
    requester_session_key: str,
    agent_id: str = "sub-agent",
    load_skills: list[str] | None = None,
    run_in_background: bool = True,
    max_spawn_depth: int | None = None,
    max_children_per_agent: int | None = None,
    run_timeout_seconds: float | None = None,
    context_mode: ContextMode | str = ContextMode.ISOLATED,
    model_override: str | None = None,
    task_name: str | None = None,
    label: str | None = None,
    thinking: str | None = None,
    output_schema: dict[str, Any] | None = None,
) -> "DelegatedTaskHandle":
    """Dispatch a single sub-agent task synchronously.

    This is the practical counterpart to the ``sessions_spawn`` tool: it runs
    the full spawn pipeline (validation / depth / concurrency / tool policy /
    registry registration / background async execution) and returns control to
    the caller.

    Args:
        task: Natural-language task description (clear goal + deliverable).
        requester_session_key: Session key of the parent that owns the child.
            **Required.** Typically ``"agent:<id>:session:<uuid>"``.
        agent_id: Logical sub-agent id. Defaults to ``"sub-agent"``; callers
            normally pass a stable per-task handle such as
            ``f"sub-agent-{hash(task)[:8]}"``.
        load_skills: List of skill names to activate for this sub-agent, or
            ``None`` (no skill injection). Skills whose frontmatter scope is
            ``main_only`` are excluded. Unknown names are warned and dropped.
            See module docstring for the required decision flow (pass ``[]``
            ONLY when nothing matches).
        run_in_background: If ``True`` (default), spawn fire-and-forget and
            return a pollable :class:`DelegatedTaskHandle` immediately. If
            ``False``, block the calling thread until the child reaches a
            terminal state and populate ``result_text``.
        max_spawn_depth: Override configured max nesting depth (falls back to
            :data:`SubagentConfig.max_spawn_depth`). Applied per-call.
        max_children_per_agent: Override concurrency cap (falls back to
            :data:`SubagentConfig.max_children_per_agent`). Applied per-call.
        run_timeout_seconds: Wall-clock child timeout (falls back to
            :data:`SubagentConfig.run_timeout_seconds`). Applied per-call.
        context_mode: :class:`ContextMode` or its string name. Only
            ``ISOLATED`` is fully supported for direct dispatch.
        model_override: Optional LLM model override for the child.
        task_name: Optional short display name; auto-derived from *task*.
        label: Optional user-facing label.
        thinking: Optional thinking-level override.
        output_schema: Optional JSON Schema the child output is validated against.

    Returns:
        A :class:`DelegatedTaskHandle`. When ``run_in_background=True`` use
        :meth:`DelegatedTaskHandle.is_running` to poll, or
        :meth:`DelegatedTaskHandle.result` to block for the outcome.

    Raises:
        ValueError: If ``requester_session_key`` is missing or ``task`` empty,
            or if ``context_mode`` is not ``ISOLATED`` (direct dispatch does not
            support context forking yet).
    """
    if not task or not task.strip():
        raise ValueError("delegate_task: `task` must be a non-empty string")
    if not requester_session_key:
        raise ValueError("delegate_task: `requester_session_key` is required")

    # Normalize context_mode: accept ContextMode enum or "isolated"/"fork" string.
    if isinstance(context_mode, ContextMode):
        context = context_mode
    else:
        try:
            context = ContextMode[context_mode.strip().upper()]
        except KeyError:
            raise ValueError(
                f"delegate_task: unknown context_mode {context_mode!r}; "
                f"expected one of {[m.value for m in ContextMode]}"
            ) from None

    if context != ContextMode.ISOLATED:
        raise ValueError(
            "delegate_task: direct dispatch currently supports only "
            "ContextMode.ISOLATED; fork/inherited contexts require sessions_spawn."
        )

    effective_task = _inject_skills(task, load_skills)

    dispatch_kwargs: dict[str, Any] = dict(
        task=effective_task,
        requester_session_key=requester_session_key,
        agent_id=agent_id,
        task_name=task_name,
        label=label,
        thinking=thinking,
        context=context,
        run_timeout_seconds=run_timeout_seconds,
        output_schema=output_schema,
        model=model_override,
        max_spawn_depth=max_spawn_depth,
        max_children_per_agent=max_children_per_agent,
    )

    if run_in_background:
        result = asyncio.run(_dispatch_async(**dispatch_kwargs))
        return _to_handle(result, background=True)

    # Blocking mode: spawn within a fresh event loop, then poll to completion.
    loop = asyncio.new_event_loop()
    try:
        dispatched = loop.run_until_complete(_dispatch_async(**dispatch_kwargs))
        handle = _to_handle(dispatched, background=False)
        if handle.accepted:
            return _await_outside_loop(handle, timeout=None, poll_interval=0.25)
        return handle
    finally:
        loop.close()
