"""System prompt assembly."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

from loguru import logger
from skills.loader import get_skills_text
from config import WORKSPACE_DIR
from config.path import resolve_boulder_path, resolve_plan_path
from workspace import ALL_SYSTEM_FILE_NAMES
from workspace.file_sync import ensure_workspace_system_files

if TYPE_CHECKING:
    from runtime.data_provider import PromptDataProvider

MAX_FILE_CHARS: int = 20_000

# Absolute repo-root path (was cwd-relative); kept as a module attribute so tests can repoint it.
_BOULDER_PATH = resolve_boulder_path()
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


#: Stable capability note appended to every system prompt: what a chat-side
#: file mention looks like and how to reach the file. It belongs to the CACHED
#: half of the prompt (the composer's ``@path`` chips are a UI convention, not
#: live state), so it is a constant, not a per-turn reminder.
FILE_REFERENCE_BLOCK = """## File References

User messages may carry `@path` file references (e.g. `@./src/foo.py`, or a
directory as `@./src/`) — the chat composer turns dragged or right-clicked
workspace files into them. Paths are relative to the session's working
directory; use `read_file` / `search_files` on the named path instead of
guessing a file's content from its name."""


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
        # Deterrence notices: the continuation and transition barriers are enforced in code
        # (agent/core.py + service.update_todos); completion verification is recorded via SisyphusVerifier/EvidenceLedger.
        lines.append(
            "Your todo list is tracked by the continuation system. "
            "Incomplete todos will trigger automatic continuation."
        )
        lines.append(
            "Completion is verified by the Sisyphus contract — unverified claims will be rejected."
        )
        return "\n".join(lines)
    except Exception as error:  # a broken block must not break the prompt
        logger.debug("prompt builder: todo block failed ({}); omitting it", error)
        return ""


def _build_plan_mode_block(session_id: str) -> str:
    """Plan-Then-Execute: the workflow reminder, or the approved plan reference.

    Rebuilt from live state on every call (never frozen), so entering or leaving
    plan mode takes effect on the next model call and a context compression
    cannot lose it. Fail-open: an unreadable register yields no block.
    """
    try:
        from runtime import StateKey, state_register_db, state_register_mem

        status = state_register_mem.get_state(session_id, StateKey.PLAN_STATUS, None)
        if not isinstance(status, str):
            status = ""
        if not status:
            db_status = state_register_db.get_state(session_id, StateKey.PLAN_STATUS, None)
            status = db_status if isinstance(db_status, str) else ""
        if status in ("planning", "needs_replan"):
            replan = status == "needs_replan"
            header = (
                "## Plan Mode (Plan-Then-Execute) — RE-PLAN"
                if replan
                else "## Plan Mode (Plan-Then-Execute)"
            )
            extra = (
                "The approved plan no longer fits execution: revise it with what you have learned."
                if replan
                else ""
            )
            lines = [
                header,
                extra,
                "You are DESIGNING, not executing. Only read-only tools are "
                "available: `read_file`, `search_files`, `message_search`, "
                "`skill_list`, `skill_view`, `question` — plus `exit_plan_mode` to "
                "submit. Write tools (`write_file`, `patch_file`, `terminal`, "
                "`python_repl`) are refused until the user approves the plan.",
                "Work in four phases: (1) explore the relevant files and search "
                "the codebase; (2) design — weigh the options, pick one, note the "
                "risks; (3) write the plan (goal / scope with must-have & "
                "must-not / numbered steps, each with an acceptance criterion / "
                "verification strategy / risks); (4) call `exit_plan_mode` with "
                "the full plan.",
                "A turn in plan mode may ONLY end with a `question` or an `exit_plan_mode` call.",
            ]
            return "\n\n".join(line for line in lines if line)
        if status == "ready":
            path = state_register_mem.get_state(session_id, StateKey.PLAN_FILE_PATH, None)
            if not isinstance(path, str) or not path:
                path = state_register_db.get_state(session_id, StateKey.PLAN_FILE_PATH, None)
            if not isinstance(path, str) or not path:
                return ""
            return (
                "## Approved Plan\n\n"
                "A plan was approved for this session and is saved at "
                f"`{path}`. Execute it (re-read the file with `read_file` when its "
                "details are not in context). If execution shows the plan no "
                "longer fits, call `enter_plan_mode(replan=True)` before drifting "
                "from it."
            )
        return ""
    except Exception:  # noqa: BLE001 - a broken register must not break the prompt
        logger.debug("[prompt_builder] plan-mode block read failed", exc_info=True)
        return ""


def _build_project_dir_block(session_id: str) -> str:
    """Render the session's working directory. Returns "" only on failure.

    Reads the register directly (mem → durable mirror), like the workspace
    snapshot above: a blocking read on this path is pre-existing, and the block
    is what keeps the model from writing into the sherry checkout by accident
    when a session runs unbound.
    """
    try:
        from runtime.session.project_dir import current_project_dir, project_dir_source

        root = current_project_dir(session_id)
        source = project_dir_source(session_id)
        lines = [f"## Current Working Directory: `{root}`"]
        if source == "session":
            lines.append(
                "The user bound this session to that directory. All relative paths in "
                "file tools, terminal commands and path checks resolve against it, and "
                "paths outside it are rejected."
            )
        else:
            # source is "env" (process default configured) or "default" (nothing
            # configured): the session runs against a root the user did NOT pick
            # for it, so say so loudly — a silent fallback is how an agent ends
            # up working inside the sherry repository.
            lines.append(
                "**未绑定项目目录 (no project directory bound)**: this session has no explicit "
                f"project directory, so the process default (`{source}`) is in effect. "
                "Treat that root as read-only context unless the user asks otherwise, and "
                "mention it when you are about to write files there."
            )
        return "\n".join(lines)
    except Exception as error:  # a broken block must not break the prompt
        logger.debug("prompt builder: project-dir block failed ({}); omitting it", error)
        return ""


def _build_project_instructions_block(session_id: str) -> str:
    """Read the project's AGENTS.md (fallback CLAUDE.md) as live instructions.

    The file is user-owned project content, not sherry state: no directory is
    created here. Returns "" when neither file exists or is empty.
    """
    try:
        from runtime.session.project_dir import current_project_dir

        root = Path(current_project_dir(session_id))
        for name in ("AGENTS.md", "CLAUDE.md"):
            path = root / name
            if not path.is_file():
                continue
            content = path.read_text(encoding="utf-8", errors="replace").strip()
            if content:
                return content
    except Exception:
        logger.debug("prompt builder: project instructions read failed", exc_info=True)
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
        plan_ref = active.get("active_plan") or ""
        resolved_plan = resolve_plan_path(plan_ref, session_id)
        plan_display = str(resolved_plan) if resolved_plan is not None else (plan_ref or "?")
        remaining = sum(
            1
            for todo in _read_todos_sync(session_id)
            if todo.get("status") in ("pending", "in_progress")
        )
        return "\n".join(
            [
                "## Active Work",
                f"- Plan: {plan_display}",
                f"- Status: {active.get('status')}",
                f"- Remaining: {remaining} unchecked checkboxes",
            ]
        )
    except Exception as error:  # a broken block must not break the prompt
        logger.debug("prompt builder: boulder block failed ({}); omitting it", error)
        return ""


def _build_taskflow_block(session_id: str) -> str:
    """Render pending TaskFlows for this session. Returns "" on none or failure.

    Reads the session's non-terminal flows from the taskflow registry (the
    registry filters by session id in SQL) and injects a concise summary
    (max 3 flows) so the agent can proactively continue unfinished work on
    session start.
    """
    try:
        provider = _resolve_provider("get_active_flows")
        if provider is None:
            return ""

        mine = provider.get_active_flows(session_id)
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

        lines.append(
            "Use taskflow_inspect(action='summary') to inspect a flow and continue execution."
        )
        return "\n".join(lines)
    except Exception as error:  # a broken block must not break the prompt
        logger.debug("prompt builder: taskflow block failed ({}); omitting it", error)
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
    except Exception as error:  # a broken block must not break the prompt
        logger.debug("prompt builder: continuity block failed ({}); omitting it", error)
        return ""


def _build_knowledge_block(session_id: str) -> str:
    """Inject the current plan's knowledge summary; "" on none or any failure."""
    try:
        provider = _resolve_provider("build_todolist_knowledge_block")
        return provider.build_todolist_knowledge_block(session_id) if provider is not None else ""
    except Exception as error:  # a broken block must not break the prompt
        logger.debug("prompt builder: knowledge block failed ({}); omitting it", error)
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
    #
    # The session's own selection (预设-技能 tab → AGENT_CONFIG["skills"]) is the
    # second source, and it is EXACT: a session that unchecked everything gets an
    # empty index, not every skill back. An explicit argument outranks it (the
    # caller knows best), and `None` on both sides keeps today's behaviour.
    selection = selected_skill_names
    if selection is None and session_id:
        from runtime.session.agent_config_view import session_skill_names

        selection = session_skill_names(session_id)
    if selection is not None:
        # The media chain (image/audio/video → text, text → image) is REQUIRED:
        # a session config may not drop it, and a register value written before
        # the rule existed cannot either — same guard as the required tools.
        from skills.loader import REQUIRED_SKILLS

        selection = sorted(set(selection) | REQUIRED_SKILLS)
    skill_paths: str = get_skills_text(selection, caller_scope="main", exact=selection is not None)

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
                    provider.format_memory_for_system_prompt("facts"),
                )
                if content
            )

    # --- Assembling the final prompt ----------------------------------
    # STABLE content only: persona files + memory + the skills index. The live
    # views (plan mode, working directory, todo list, boulder, TaskFlow board,
    # continuity, knowledge index) are NOT built here any more — they change
    # every turn, and a system prompt whose first tokens change invalidates the
    # provider's prefix cache for everything after it (persona included).
    # ``DynamicReminderMiddleware`` injects them as ``<system-reminder>``
    # messages from live state on each turn's first model call; each live view
    # keeps its builder above (``_build_todo_block`` etc.) — only the delivery
    # changed. This prompt is therefore safe to CACHE per session, which is
    # exactly what ``system_prompt_injection`` does.
    parts = [*file_paths, skill_paths, FILE_REFERENCE_BLOCK]

    # Join with blank-line separators, skipping any empty parts.
    return "\n\n".join(p for p in parts if p)
