"""Patch file tool with fuzzy matching (replace mode).

Supports a multi-strategy matching chain inspired by hermes-agent:
exact → line_trimmed → whitespace_normalized → indentation_flexible
→ escape_normalized → trimmed_boundary → unicode_normalized → block_anchor → context_aware

Each strategy is tried in order; the first match wins.  When a non-exact
strategy matches, ``new_string`` is re-indented to preserve the file's
actual indentation pattern.
"""
import difflib
import json
import re
from difflib import SequenceMatcher
from typing import Optional, Type

from langchain_core.callbacks import CallbackManagerForToolRun
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from tools.pub_base import resolve_path, fuzzy_find_and_replace

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

def _find_closest_lines(old_string: str, content: str, context: int = 2, max_results: int = 3) -> str:
    if not old_string or not content:
        return ""
    old_lines = old_string.splitlines()
    content_lines = content.splitlines()
    if not old_lines or not content_lines:
        return ""
    anchor = next((l.strip() for l in old_lines if l.strip()), "")
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
            f"{start + j + 1:4d}| {content_lines[start + j]}"
            for j in range(end - start)
        )
        parts.append(snippet)
    return "\n---\n".join(parts) if parts else ""


# ── LangChain tool ───────────────────────────────────────────────────────

class PatchFileInput(BaseModel):
    file_path: str = Field(description="Path to the file to patch")
    old_string: str = Field(description="Text to find in the file (must be unique unless replace_all=True)")
    new_string: str = Field(description="Replacement text")
    replace_all: bool = Field(
        default=False,
        description="If True, replace all occurrences of old_string; otherwise require uniqueness",
    )


class PatchFileTool(BaseTool):
    name: str = "patch_file"
    args_schema: Type[BaseModel] = PatchFileInput
    description: str = (
        "Patch a file by replacing old_string with new_string. "
        "Uses fuzzy matching to handle minor whitespace/indentation differences. "
        "Prefer this over write_file for targeted edits."
    )

    def _run(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
        run_manager: Optional[CallbackManagerForToolRun] = None,
    ) -> str:
        resolved = resolve_path(file_path)

        if not resolved.exists():
            return json.dumps({"error": f"File not found: {file_path}"}, ensure_ascii=False)
        if resolved.is_dir():
            return json.dumps({"error": f"Path is a directory: {file_path}"}, ensure_ascii=False)

        try:
            content = resolved.read_text(encoding="utf-8")
        except Exception as e:
            return json.dumps({"error": f"Failed to read file: {e}"}, ensure_ascii=False)

        if content.startswith("\ufeff"):
            content = content[1:]

        new_content, match_count, strategy, error = fuzzy_find_and_replace(
            content, old_string, new_string, replace_all,
        )

        if error or match_count == 0:
            hint = ""
            if error and error.startswith("Could not find"):
                closest = _find_closest_lines(old_string, content)
                if closest:
                    hint = f"\n\nDid you mean one of these sections?\n{closest}"
            return json.dumps({
                "error": (error or "No match found") + hint,
                "path": file_path,
                "strategy": strategy,
            }, ensure_ascii=False)

        try:
            resolved.write_text(new_content, encoding="utf-8")
        except Exception as e:
            return json.dumps({"error": f"Failed to write file: {e}"}, ensure_ascii=False)

        diff = _unified_diff(content, new_content, file_path)

        return json.dumps({
            "success": True,
            "path": file_path,
            "strategy": strategy,
            "matches": match_count,
            "diff": diff,
        }, ensure_ascii=False)


def build_patch_file_tool() -> PatchFileTool:
    tool = PatchFileTool()
    tool.handle_tool_error = True
    return tool
