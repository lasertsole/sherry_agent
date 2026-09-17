"""System prompt assembly."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

from loguru import logger
from skills.loader import get_skills_text
from config import WORKSPACE_DIR
from workspace import ALL_SYSTEM_FILE_NAMES
from workspace.file_sync import ensure_workspace_system_files

if TYPE_CHECKING:
    from runtime.data_provider import PromptDataProvider

MAX_FILE_CHARS: int = 20_000

# Active-work pointer written by the ulw-execute orchestration flow.
_BOULDER_PATH = Path(".omo/boulder.json")
_ACTIVE_WORK_STATUSES = frozenset({"active", "paused"})

_TODO_ICONS: dict[str, str] = {
    "pending": "○",
    "in_progress": "◐",
    "completed": "●",
    "cancelled": "✕",
}

# First miss per provider method is logged once (mirrors runtime.hooks consumers).
_provider_misses_logged: set[str] = set()


def _resolve_provider(method: str) -> PromptDataProvider | None:
    """Resolve the prompt data provider, logging the first miss per method.

    The provider is registered by ``agent.core.init()`` at server boot; a
    process that never assembled the agent (unit tests, tooling) degrades the
    dynamic prompt blocks to empty instead of importing across package
    boundaries.
    """
    from runtime import data_provider

    provider = data_provider.get_prompt_data_provider()
    if provider is None and method not in _provider_misses_logged:
        _provider_misses_logged.add(method)
        logger.debug(
            "prompt builder: prompt data provider is not registered; '{}' degrades to empty",
            method,
        )
    return provider


def _read_todos_sync(session_id: str) -> list[dict]:
    """Read the session todo list synchronously via the prompt data provider."""
    provider = _resolve_provider("get_todos")
    return provider.get_todos(session_id) if provider is not None else []


def _build_todo_block(session_id: str) -> str:
    """Render the session todo list. Returns "" when empty or on any failure."""
    try:
        todos = _read_todos_sync(session_id)
        if not todos:
            return ""
        lines = ["## Current Todo List"]
        for todo in todos:
            icon = _TODO_ICONS.get(todo.get("status", ""), "○")
            tag_parts = [todo.get("category") or "quick"]
            delegation = todo.get("delegation")
            if delegation and delegation != "self":
                tag_parts.append(delegation)
            # DAG state lives in TaskFlow; only the flow/step pointer is shown.
            flow_id = todo.get("flow_id")
            step_id = todo.get("step_id")
            if flow_id:
                tag_parts.append(f"flow:{flow_id}")
            if step_id:
                tag_parts.append(step_id)
            tag = f"({', '.join(tag_parts)})"
            content = todo.get("content", "")
            priority = todo.get("priority") or "medium"
            lines.append(f"- [{icon}] {tag} {content} ({priority})")
        # Deterrence notices: E3 continuation and the E4 transition barrier are enforced in code
        # (agent/core.py + service.update_todos); E5 verification is recorded via SisyphusVerifier/EvidenceLedger.
        lines.append(
            "Your todo list is tracked by the continuation system. "
            "Incomplete todos will trigger automatic continuation."
        )
        lines.append(
            "Completion is verified by the Sisyphus contract — unverified claims will be rejected."
        )
        return "\n".join(lines)
    except Exception:
        return ""


def _build_boulder_block(session_id: str) -> str:
    """Render the active/paused work pointer. Returns "" on none or any failure."""
    try:
        if not _BOULDER_PATH.exists():
            return ""
        with _BOULDER_PATH.open("r", encoding="utf-8") as handle:
            boulder = json.load(handle)
        if not isinstance(boulder, dict):
            return ""
        works = boulder.get("works")
        if not isinstance(works, dict):
            return ""
        active = works.get(boulder.get("active_work_id") or "")
        if not isinstance(active, dict) or active.get("status") not in _ACTIVE_WORK_STATUSES:
            # active_work_id stale or missing: fall back to any active/paused work.
            active = next(
                (
                    work
                    for work in works.values()
                    if isinstance(work, dict) and work.get("status") in _ACTIVE_WORK_STATUSES
                ),
                None,
            )
        if not isinstance(active, dict):
            return ""
        remaining = sum(
            1
            for todo in _read_todos_sync(session_id)
            if todo.get("status") in ("pending", "in_progress")
        )
        return "\n".join(
            [
                "## Active Work",
                f"- Plan: {active.get('active_plan') or '?'}",
                f"- Status: {active.get('status')}",
                f"- Remaining: {remaining} unchecked checkboxes",
            ]
        )
    except Exception:
        return ""


def _build_taskflow_block(session_id: str) -> str:
    """Render pending TaskFlows for this session. Returns "" on none or failure.

    Scans the taskflow registry for non-terminal flows whose
    ``state['creator_session_key']`` matches this session and injects a concise
    summary (max 3 flows) so the agent can proactively continue unfinished work
    on session start.
    """
    try:
        provider = _resolve_provider("get_active_flows")
        if provider is None:
            return ""

        creator_key = provider.requester_session_key(session_id)
        active_flows = provider.get_active_flows()

        # Filter: only flows created by THIS session.
        mine = [
            flow
            for flow in active_flows
            if (flow.get("state") or {}).get("creator_session_key") == creator_key
        ]
        if not mine:
            return ""

        lines = ["## Pending TaskFlows"]
        for flow in mine[:3]:  # max 3 flows
            state = flow.get("state") or {}
            steps = state.get("steps") or []
            counts = provider.steps_summary(steps)
            total = len(steps)
            done = counts.get("done", 0)
            desc = state.get("description", "")[:60]
            status = flow.get("status", "?")

            # Find the next actionable step (first non-done).
            pending = [s for s in steps if provider.step_status(s) not in ("done",)]
            next_hint = ""
            if pending:
                next_step = pending[0]
                next_id = next_step.get("step_id", "?")
                next_task = (next_step.get("task", "") or "")[:40]
                next_hint = f' | next: {next_id} "{next_task}"'

            lines.append(
                f'- [{status}] {flow["flow_id"]}: "{desc}" | {done}/{total} steps done{next_hint}'
            )

        lines.append("Use taskflow_summary to inspect a flow and continue execution.")
        return "\n".join(lines)
    except Exception:
        return ""


def _build_continuity_block(session_id: str) -> str:
    """Render the last session's end state for cross-session continuity.

    Injects the previous session's tail AI reply + related TaskFlow ids so the
    agent can proactively continue unfinished work on session start. Returns ""
    on none or any failure (fail-open).
    """
    try:
        provider = _resolve_provider("build_continuity_prompt")
        return provider.build_continuity_prompt(session_id) if provider is not None else ""
    except Exception:
        return ""


def _build_knowledge_block(session_id: str) -> str:
    """Inject the current plan's knowledge summary; "" on none or any failure."""
    try:
        provider = _resolve_provider("build_todolist_knowledge_block")
        return provider.build_todolist_knowledge_block(session_id) if provider is not None else ""
    except Exception:
        return ""


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    if len(text) > MAX_FILE_CHARS:
        return text[:MAX_FILE_CHARS] + "\n...[truncated]"
    return text


_WORKSPACE_STATE_KEY = "workspace"


def _read_static_files(selected_file_names: list[str] | None) -> list[str]:
    """Read the static workspace files into a list of text blocks.

    Used for caching under ``state_register_db`` key ``workspace``. Dynamic
    content (memory_store) is intentionally excluded so it stays fresh.
    """
    # Lazy-ensure the persona system files exist before reading them. Only the
    # missing ones are copied in from the language template directory; existing
    # user-authored files are never overwritten.
    ensure_workspace_system_files()
    if selected_file_names is not None:
        return [_read_text(WORKSPACE_DIR / f) for f in selected_file_names]
    return [_read_text(WORKSPACE_DIR / f) for f in ALL_SYSTEM_FILE_NAMES]


def build_system_prompt(
    selected_file_names: list[str] | None = None,
    selected_skill_names: list[str] | None = None,
    session_id: str | None = None,
) -> str:
    """Assemble the system prompt from persona files, memory, live blocks and skills."""
    # --- Skill block ---------------------------------------------------
    # Compose the skill prompt from the selected skills (or all of them when
    # None). caller_scope="main": the main agent sees every skill except those
    # frontmatter-scoped "subagent_only".
    skill_paths: str = get_skills_text(selected_skill_names, caller_scope="main")

    # --- Workspace persona block --------------------------------------
    # Why the workspace snapshot is frozen per session:
    # The persona (from workspace static files) is fixed when a session starts, so
    # mid-conversation edits to those files must NOT alter an in-flight session's
    # identity. Freezing the snapshot per session_id keeps the persona consistent
    # for the whole conversation, while a brand-new session reads the latest files.
    if session_id:
        from runtime import state_register_db

        # Try to load this session's previously frozen snapshot.
        # `get_state` may return anything (Any); only trust a real list.
        raw = cast("object", state_register_db.get_state(session_id, _WORKSPACE_STATE_KEY, None))
        if isinstance(raw, list):
            # Cache hit: reuse the frozen snapshot, but COPY it so the later
            # memory append does not mutate the cached value (which would leak
            # memory into the snapshot and duplicate it on the next call).
            file_paths = list(cast("list[str]", raw))
        else:
            # Cache miss: this is the first build for the session, so snapshot
            # the static files ONCE and persist them. Store the original list
            # untouched; we only append memory to a working copy afterwards.
            snapshot = _read_static_files(selected_file_names)
            _ = state_register_db.set_state(session_id, _WORKSPACE_STATE_KEY, snapshot)
            file_paths = list(snapshot)
    else:
        # No session -> no caching. Always re-read the current file contents.
        file_paths = _read_static_files(selected_file_names)

    # --- Dynamic memory block -----------------------------------------
    # Memory is NOT frozen: it must stay live so the current conversation
    # reflects fresh "memory"/"user" state. Only appended when the caller did
    # not filter to explicit files. `None` results are dropped.
    if selected_file_names is None:
        provider = _resolve_provider("format_memory_for_system_prompt")
        if provider is not None:
            file_paths.extend(
                content
                for content in (
                    provider.format_memory_for_system_prompt("memory"),
                    provider.format_memory_for_system_prompt("user"),
                )
                if content
            )

    # --- Todo + boulder + taskflow + continuity blocks ----------------
    # Rebuilt from live state on every call, so they survive context
    # compression; skipped entirely when there is no session to scope them to.
    # The taskflow/continuity blocks follow the memory-block rule: only injected
    # when the caller did not filter to explicit files.
    if session_id:
        blocks = [
            _build_todo_block(session_id),
            _build_boulder_block(session_id),
            _build_taskflow_block(session_id) if selected_file_names is None else "",
            _build_knowledge_block(session_id) if selected_file_names is None else "",
            _build_continuity_block(session_id) if selected_file_names is None else "",
        ]
    else:
        blocks = []

    # --- Assembling the final prompt ----------------------------------
    # Fold static files + memory + live blocks into one ordered list, then skills.
    parts = [*file_paths, *blocks, skill_paths]

    # Join with blank-line separators, skipping any empty parts.
    return "\n\n".join(p for p in parts if p)
