"""System prompt assembly."""

from pathlib import Path
from typing import cast
from skills.loader import get_skills_text
from config import WORKSPACE_DIR, TEMP_DIR
from workspace import ALL_SYSTEM_FILE_NAMES
from workspace.file_sync import ensure_workspace_system_files

MAX_FILE_CHARS: int = 20_000


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    if len(text) > MAX_FILE_CHARS:
        return text[:MAX_FILE_CHARS] + "\n...[truncated]"
    return text


skill_guide_text: str = f"""
补充说明：
1.将<skill_folder>替换成技能文件SKILL.md所在的目录 比如技能文件在 "./skills/text_to_image/SKILL.md", 那么文件目录就在 "./skills/text_to_image"
2.技能生成的临时资源（如图片、语音等）存放在{TEMP_DIR.as_posix()}目录下
"""

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
    # --- Skill block ---------------------------------------------------
    # Compose the skill prompt from the selected skills (or all of them when
    # None). A one-line skill guide is always appended to orient the agent.
    # caller_scope="main": the main agent sees every skill except those
    # frontmatter-scoped "subagent_only".
    skill_paths: str = get_skills_text(selected_skill_names, caller_scope="main")
    skill_paths = f"{skill_paths}\n\n{skill_guide_text}"

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
        from agent.tools.memory import memory_store

        file_paths.extend(
            content
            for content in (
                memory_store.format_for_system_prompt("memory"),
                memory_store.format_for_system_prompt("user"),
            )
            if content
        )

    # --- Assembling the final prompt ----------------------------------
    # Fold static files + memory into one ordered list, then skill block.
    parts = [*file_paths, skill_paths]

    # Join with blank-line separators, skipping any empty parts.
    return "\n\n".join(p for p in parts if p)

