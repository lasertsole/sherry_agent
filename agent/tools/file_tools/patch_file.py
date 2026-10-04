"""Patch file tool with fuzzy matching (replace mode).

Supports a multi-strategy matching chain inspired by hermes-agent:
exact → line_trimmed → whitespace_normalized → indentation_flexible
→ escape_normalized → trimmed_boundary → unicode_normalized → block_anchor → context_aware

Each strategy is tried in order; the first match wins.  When a non-exact
strategy matches, ``new_string`` is re-indented to preserve the file's
actual indentation pattern.

Concurrency contract: the read → match → write cycle runs under
``file_write_lock`` (in-process per-path lock, then the cross-process ``flock``)
and carries a two-layer CAS: the file must still match the fingerprint taken at
read time (mtime+size, with a content-hash exemption so a no-op touch is not a
conflict), and the atomic write re-asserts the revision immediately before the
replace. A file changed by anyone else is REFUSED with a "re-read and retry"
error — never silently overwritten, which is what used to happen when two
agents patched one file and both reported success.
"""

import hashlib
import json
import difflib
from typing import Annotated, override
from difflib import SequenceMatcher
from pydantic import BaseModel, Field
from langchain_core.callbacks import CallbackManagerForToolRun
from runtime.session.project_dir import current_project_dir
from langchain_core.tools import BaseTool, InjectedToolCallId
from langgraph.prebuilt.tool_node import InjectedState
from agent.tools.pub_base import (
    FileBusyError,
    PathOutOfBoundsError,
    StaleWriteError,
    _extract_session_id,
    atomic_write_bytes_no_follow,
    decode_text,
    display_path,
    encode_text,
    file_revision,
    file_write_lock,
    fuzzy_find_and_replace,
    note_edit,
    read_bytes_no_follow,
    sniff_text_encoding,
    resolve_external_path,
    resolve_workspace_path,
    revision_id,
    safe_error_detail,
)
from agent.tools.todolist.evidence_recorder import mark_evidence_stale
from .snapshot import capture_pre_write, finalize_capture

SessionId = Annotated[str, InjectedState("session_id")]
ToolCallId = Annotated[str, InjectedToolCallId]

# ── Diff helper ──────────────────────────────────────────────────────────


def _unified_diff(old: str, new: str, path: str) -> str:
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    return "".join(diff)


# ── "Did you mean?" hint ─────────────────────────────────────────────────


def _find_closest_lines(
    old_string: str, content: str, context: int = 2, max_results: int = 3
) -> str:
    if not old_string or not content:
        return ""
    old_lines = old_string.splitlines()
    content_lines = content.splitlines()
    if not old_lines or not content_lines:
        return ""
    anchor = next((ln.strip() for ln in old_lines if ln.strip()), "")
    if not anchor:
        return ""
    scored = []
    for i, line in enumerate(content_lines):
        s = line.strip()
        if not s:
            continue
        r = SequenceMatcher(None, anchor, s).ratio()
        if r > 0.3:
            scored.append((r, i))
    if not scored:
        return ""
    scored.sort(key=lambda x: -x[0])
    parts, seen = [], set()
    for _, idx in scored[:max_results]:
        start = max(0, idx - context)
        end = min(len(content_lines), idx + len(old_lines) + context)
        key = (start, end)
        if key in seen:
            continue
        seen.add(key)
        snippet = "\n".join(
            f"{start + j + 1:4d}| {content_lines[start + j]}" for j in range(end - start)
        )
        parts.append(snippet)
    return "\n---\n".join(parts) if parts else ""


# ── LangChain tool ───────────────────────────────────────────────────────


class PatchFileInput(BaseModel):
    file_path: str = Field(description="Path to the file to patch")
    old_string: str = Field(
        description="Text to find in the file (must be unique unless replace_all=True)"
    )
    new_string: str = Field(description="Replacement text")
    replace_all: bool = Field(
        default=False,
        description="If True, replace all occurrences of old_string; otherwise require uniqueness",
    )
    session_id: SessionId = ""
    tool_call_id: ToolCallId = ""


class PatchFileTool(BaseTool):
    name: str = "patch_file"
    args_schema: type[BaseModel] = PatchFileInput
    description: str = (
        "Patch a file by replacing old_string with new_string. "
        "Uses fuzzy matching to handle minor whitespace/indentation differences. "
        "Prefer this over write_file for targeted edits."
    )
    metadata: dict = {"idempotent": False}

    # ── shared core ────────────────────────────────────────────────────────

    def _core(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
        session_id: str = "",
        tool_call_id: str = "",
    ) -> str:
        # redundant: path_guard middleware handles this — kept as the second line of defense
        try:
            root = current_project_dir(session_id)
            resolved = resolve_workspace_path(file_path, root)
        except PathOutOfBoundsError:
            try:
                resolved = resolve_external_path(
                    file_path, session_id=session_id, action_desc="patch file"
                )
            except PathOutOfBoundsError as e:
                return json.dumps({"error": str(e)}, ensure_ascii=False)

        if not resolved.exists():
            return json.dumps(
                {"error": f"File not found: {display_path(resolved, root)}"}, ensure_ascii=False
            )
        if resolved.is_dir():
            return json.dumps(
                {"error": f"Path is a directory: {display_path(resolved, root)}"},
                ensure_ascii=False,
            )

        try:
            # One writer per path at a time (in-process and cross-process), so the
            # read → replace → write cycle below cannot interleave with another
            # agent's patch of the same file.
            with file_write_lock(resolved):
                read_raw, read_stat = read_bytes_no_follow(resolved)
                if (read_encoding := sniff_text_encoding(read_raw)) is None:
                    # Binary or a codec we do not edit: patching would rewrite
                    # the bytes it cannot represent. Refuse, keep the file.
                    return json.dumps(
                        {
                            "error": (
                                "File is binary or not UTF-8/UTF-16 text; "
                                "patch_file edits text only."
                            ),
                            "path": display_path(resolved, root),
                            "hint": "Use terminal (cp / python) for resource files.",
                        },
                        ensure_ascii=False,
                    )
                content = decode_text(read_raw, read_encoding)

                new_content, match_count, strategy, error = fuzzy_find_and_replace(
                    content,
                    old_string,
                    new_string,
                    replace_all,
                )

                if error or match_count == 0:
                    hint = ""
                    if error and error.startswith("Could not find"):
                        closest = _find_closest_lines(old_string, content)
                        if closest:
                            hint = f"\n\nDid you mean one of these sections?\n{closest}"
                    return json.dumps(
                        {
                            "error": (error or "No match found") + hint,
                            "path": display_path(resolved, root),
                            "strategy": strategy,
                        },
                        ensure_ascii=False,
                    )

                # Layer 1 of the CAS: the file must still be the one we read.
                # mtime+size first (cheap); a content hash exempts the case where
                # they moved but the bytes are identical (touch / formatter no-op).
                check_raw, check_stat = read_bytes_no_follow(resolved)
                if (check_stat.st_mtime_ns, check_stat.st_size) != (
                    read_stat.st_mtime_ns,
                    read_stat.st_size,
                ) and hashlib.sha256(check_raw).digest() != hashlib.sha256(read_raw).digest():
                    return json.dumps(
                        {
                            "error": (
                                "File changed on disk since it was read "
                                "(another writer touched it)."
                            ),
                            "path": display_path(resolved, root),
                            "hint": "Re-read the file and re-apply the patch.",
                        },
                        ensure_ascii=False,
                    )

                # The snapshot reuses the bytes this call already read: no
                # second read, and what it stores is exactly what the write
                # below replaces.
                capture = capture_pre_write(
                    resolved, root, before_bytes=read_raw, before_revision=revision_id(read_stat)
                )
                written = encode_text(new_content, read_encoding)
                # Layer 2: the atomic write re-asserts the revision right before
                # the replace, closing the window between the check above and it.
                atomic_write_bytes_no_follow(
                    resolved,
                    written,
                    expected_revision=revision_id(check_stat),
                )
                # A session that already knew the file advances with its own
                # delta (see pub_base/read_state.py): the patch does not invent a
                # license for a session that never read the file, but one that is
                # held stays valid instead of going stale over this edit.
                note_edit(session_id, resolved, file_revision(resolved))
                finalize_capture(
                    capture, session_id=session_id, tool_call_id=tool_call_id, written=written
                )
        except StaleWriteError:
            return json.dumps(
                {
                    "error": "File changed on disk since it was read (another writer touched it).",
                    "path": display_path(resolved, root),
                    "hint": "Re-read the file and re-apply the patch.",
                },
                ensure_ascii=False,
            )
        except FileBusyError as e:
            return json.dumps(
                {
                    "error": str(e),
                    "path": display_path(resolved, root),
                    "hint": "Another process is writing this file; retry shortly.",
                },
                ensure_ascii=False,
            )
        except UnicodeDecodeError as e:
            return json.dumps(
                {"error": f"Failed to read file: {safe_error_detail(e)}"}, ensure_ascii=False
            )
        except OSError as e:
            return json.dumps(
                {"error": f"Failed to write file: {safe_error_detail(e)}"}, ensure_ascii=False
            )

        mark_evidence_stale(file_path, session_id)
        diff = _unified_diff(content, new_content, display_path(resolved, root))

        return json.dumps(
            {
                "success": True,
                "path": display_path(resolved, root),
                "strategy": strategy,
                "matches": match_count,
                "diff": diff,
            },
            ensure_ascii=False,
        )

    @override
    def _run(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
        session_id: str = "",
        tool_call_id: str = "",
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        session_id = session_id or _extract_session_id(run_manager)
        return self._core(file_path, old_string, new_string, replace_all, session_id, tool_call_id)

    @override
    async def _arun(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
        session_id: str = "",
        tool_call_id: str = "",
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        session_id = session_id or _extract_session_id(run_manager)
        return self._core(file_path, old_string, new_string, replace_all, session_id, tool_call_id)


def build_patch_file_tool() -> PatchFileTool:
    tool = PatchFileTool()
    tool.handle_tool_error = True
    return tool
