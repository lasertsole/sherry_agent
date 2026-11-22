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
from pathlib import Path
from typing import override
from pydantic import BaseModel, Field, model_validator
from loguru import logger

from langchain_core.callbacks import (
    AsyncCallbackManagerForToolRun,
    CallbackManagerForToolRun,
)
from langchain_core.tools import BaseTool
from agent.tools.pub_base import SessionId
from agent.tools.pub_base.shadow_git import ensure_checkpoint
from agent.tools.pub_base import (
    tool_error,
    FileBusyError,
    StaleWriteError,
    resolve_tool_session_id,
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
    resolve_tool_path,
    revision_id,
    safe_error_detail,
)
from agent.tools.pub_base.evidence_recorder import invalidates_evidence

from .patch_apply import apply_multi_file_patch, closest_lines


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
    """Backward-compatible alias for the shared hint helper (patch_apply)."""
    return closest_lines(old_string, content, context=context, max_results=max_results)


# ── LangChain tool ───────────────────────────────────────────────────────


class PatchFileInput(BaseModel):
    """Two modes: str_replace (default) and a multi-file patch document.

    ``patch`` non-empty selects the multi-file mode and the three single-file
    arguments are ignored; otherwise all three are required.
    """

    file_path: str | None = Field(
        default=None, description="Path to the file to patch (str_replace mode)"
    )
    old_string: str | None = Field(
        default=None,
        description="Text to find in the file (must be unique unless replace_all=True)",
    )
    new_string: str | None = Field(default=None, description="Replacement text")
    replace_all: bool = Field(
        default=False,
        description="If True, replace all occurrences of old_string; otherwise require uniqueness",
    )
    patch: str | None = Field(
        default=None,
        description=(
            "Multi-file patch document starting with '*** Begin Patch' and ending with "
            "'*** End Patch'. Supports *** Add File / *** Update File / *** Delete File / "
            "*** Move to, with '@@' hunks inside an Update. When set, file_path/old_string/"
            "new_string are ignored."
        ),
    )
    session_id: SessionId = ""

    @model_validator(mode="after")
    def _require_one_mode(self) -> "PatchFileInput":
        if (self.patch or "").strip():
            return self
        missing = [
            name
            for name in ("file_path", "old_string", "new_string")
            if getattr(self, name) is None
        ]
        if missing:
            raise ValueError(
                "str_replace mode requires " + ", ".join(missing) + " (or pass `patch`)"
            )
        return self


class PatchFileTool(BaseTool):
    name: str = "patch_file"
    args_schema: type[BaseModel] = PatchFileInput
    description: str = (
        "Patch a file by replacing old_string with new_string. "
        "Uses fuzzy matching to handle minor whitespace/indentation differences. "
        "Prefer this over write_file for targeted edits. "
        "For multi-file changes, pass `patch` instead: a document starting with "
        "'*** Begin Patch' and ending with '*** End Patch', supporting "
        "'*** Add File', '*** Update File' (with '@@' hunks of context/+/- lines), "
        "'*** Delete File' and '*** Move to'. The whole patch is preflight-checked "
        "(paths, file existence, hunk matching) before any file is written; a "
        "rejected patch changes nothing."
    )
    metadata: dict = {
        "idempotent": False,
        "side_effect_scope": "workspace",
        # A successful source edit is real progress: ToolGuardrails releases
        # every block it accumulated from the stale pre-edit probing so the
        # agent can verify the new candidate instead of being locked out.
        "clears_guardrail_blocks": True,
    }

    # ── shared core ────────────────────────────────────────────────────────

    @invalidates_evidence()
    def _core(
        self,
        file_path: str | None,
        old_string: str | None,
        new_string: str | None,
        replace_all: bool = False,
        session_id: str = "",
        patch: str | None = None,
    ) -> str:
        if (patch or "").strip():
            # Multi-file mode: parse → preflight → commit (see patch_apply).
            # The single-file arguments are ignored when a patch is present.
            return apply_multi_file_patch(str(patch), session_id)
        if file_path is None or old_string is None or new_string is None:
            return tool_error(
                "str_replace mode requires file_path, old_string and new_string (or pass `patch`)"
            )

        root, resolved, error = resolve_tool_path(file_path, session_id, "patch file", mode="write")
        if error is not None or resolved is None or root is None:
            return error or tool_error("Could not resolve the path")

        # One shadow snapshot before the first patch of this turn (fail-open).
        ensure_checkpoint(
            session_id, reason=f"before_write:patch_file:{display_path(resolved, root)}"
        )

        if not resolved.exists():
            return tool_error(f"File not found: {display_path(resolved, root)}")
        if resolved.is_dir():
            return tool_error(f"Path is a directory: {display_path(resolved, root)}")

        try:
            # One writer per path at a time (in-process and cross-process), so the
            # read → replace → write cycle below cannot interleave with another
            # agent's patch of the same file.
            with file_write_lock(resolved):
                outcome = self._apply_patch(
                    resolved, root, old_string, new_string, replace_all, session_id
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
            return tool_error(f"Failed to read file: {safe_error_detail(e)}")
        except OSError as e:
            return tool_error(f"Failed to write file: {safe_error_detail(e)}")

        if isinstance(outcome, str):
            return outcome
        content, new_content, strategy, match_count = outcome
        # Keep an ALREADY-RUNNING language server on the new bytes. Fail-open:
        # no server / a dead pipe costs nothing, and the edit has already landed.
        try:
            from agent.tools.pub_base.lsp_sync import sync_edited_file

            sync_edited_file(str(resolved), new_content, session_id)
        except Exception:  # noqa: BLE001 - advisory only
            logger.debug("LSP edit sync skipped", exc_info=True)
        diff = _unified_diff(content, new_content, display_path(resolved, root))

        payload: dict[str, object] = {
            "success": True,
            "path": display_path(resolved, root),
            "strategy": strategy,
            "matches": match_count,
            "diff": diff,
        }
        # Advisory only (opt-in): a peer's claim never blocks a patch.
        try:
            from agent.tools.coordination_tools import claim_notice_for

            notice = claim_notice_for(session_id, root, resolved)
        except Exception:  # noqa: BLE001 - advisory only
            notice = None
        if notice:
            payload["claim_notice"] = notice
        return json.dumps(payload, ensure_ascii=False)

    def _apply_patch(
        self,
        resolved: Path,
        root: Path,
        old_string: str,
        new_string: str,
        replace_all: bool,
        session_id: str,
    ) -> str | tuple[str, str, str, int]:
        """Replace the text under the path lock.

        Returns the error JSON when the patch cannot be applied, else
        ``(content, new_content, strategy, match_count)`` for the caller's diff.
        """
        read_raw, read_stat = read_bytes_no_follow(resolved)
        if (read_encoding := sniff_text_encoding(read_raw)) is None:
            # Binary or a codec we do not edit: patching would rewrite
            # the bytes it cannot represent. Refuse, keep the file.
            return json.dumps(
                {
                    "error": (
                        "File is binary or not UTF-8/UTF-16 text; patch_file edits text only."
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
                        "File changed on disk since it was read (another writer touched it)."
                    ),
                    "path": display_path(resolved, root),
                    "hint": "Re-read the file and re-apply the patch.",
                },
                ensure_ascii=False,
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
        return content, new_content, strategy, match_count

    @override
    def _run(
        self,
        file_path: str | None = None,
        old_string: str | None = None,
        new_string: str | None = None,
        replace_all: bool = False,
        session_id: str = "",
        patch: str | None = None,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        session_id = resolve_tool_session_id(session_id, run_manager)
        return self._core(file_path, old_string, new_string, replace_all, session_id, patch)

    @override
    async def _arun(
        self,
        file_path: str | None = None,
        old_string: str | None = None,
        new_string: str | None = None,
        replace_all: bool = False,
        session_id: str = "",
        patch: str | None = None,
        run_manager: AsyncCallbackManagerForToolRun | None = None,
    ) -> str:
        import asyncio

        session_id = resolve_tool_session_id(session_id, run_manager)
        return await asyncio.to_thread(
            self._core, file_path, old_string, new_string, replace_all, session_id, patch
        )


def build_patch_file_tool() -> PatchFileTool:
    tool = PatchFileTool()
    tool.handle_tool_error = True
    return tool
