import os
import re
import yaml
import json
import shutil
import tempfile
from pathlib import Path
from loguru import logger
from typing import Literal, Type, Any
from pydantic import BaseModel, Field
from typing_extensions import override
from langchain_core.tools import BaseTool
from config import AUTO_SKILLS_DIR, ROOT_DIR
from tools.pub_base import fuzzy_find_and_replace, format_no_match_hint
from pub_func import atomic_replace, has_traversal_component, validate_within_dir

_MAX_NAME_LENGTH: int = 64
_MAX_DESCRIPTION_LENGTH: int = 1024
_MAX_SKILL_CONTENT_CHARS = 100_000   # ~36k tokens at 2.75 chars/token
_VALID_NAME_RE = re.compile(r'^[a-z0-9][a-z0-9._-]*$')
# Subdirectories allowed for write_file/remove_file
_ALLOWED_SUBDIRS = {"references", "templates", "scripts", "assets"}
_MAX_SKILL_FILE_BYTES = 1_048_576    # 1 MiB per supporting file

class SkillManageSchema(BaseModel):
    """Schema for skill_manage tool arguments."""
    action: Literal["create", "patch", "edit", "delete", "write_file", "remove_file"] = Field(
        description="The action to perform."
    )
    name: str = Field(
        description=(
            "Skill name (lowercase, hyphens/underscores, max 64 chars). "
            "Must match an existing skill for patch/edit/delete/write_file/remove_file."
        )
    )
    content: str | None = Field(
        default=None,
        description=(
            "Full SKILL.md content (YAML frontmatter + markdown body). "
            "Required for 'create' and 'edit'. For 'edit', read the skill "
            "first with skill_view() and provide the complete updated text."
        )
    )
    old_string: str | None = Field(
        default=None,
        description=(
            "Text to find in the file (required for 'patch'). Must be unique "
            "unless replace_all=true. Include enough surrounding context to "
            "ensure uniqueness."
        )
    )
    new_string: str | None = Field(
        default=None,
        description=(
            "Replacement text (required for 'patch'). Can be empty string "
            "to delete the matched text."
        )
    )
    replace_all: bool | None = Field(
        default=None,
        description="For 'patch': replace all occurrences instead of requiring a unique match (default: false)."
    )
    category: str | None = Field(
        default=None,
        description=(
            "Optional category/domain for organizing the skill (e.g., 'devops', "
            "'data-science', 'mlops'). Creates a subdirectory grouping. "
            "Only used with 'create'."
        )
    )
    file_path: str | None = Field(
        default=None,
        description=(
            "Path to a supporting file within the skill directory. "
            "For 'write_file'/'remove_file': required, must be under references/, "
            "templates/, scripts/, or assets/. "
            "For 'patch': optional, defaults to SKILL.md if omitted."
        )
    )
    file_content: str | None = Field(
        default=None,
        description="Content for the file. Required for 'write_file'."
    )
    absorbed_into: str | None = Field(
        default=None,
        description=(
            "For 'delete' only — declares intent so the curator can "
            "tell consolidation from pruning without guessing. "
            "Pass the umbrella skill name when this skill's content "
            "was merged into another (the target must already exist). "
            "Pass an empty string when the skill is truly stale and "
            "being pruned with no forwarding target. Omitting the arg "
            "on delete is supported for backward compatibility but "
            "downstream tooling (e.g. cron-job skill reference "
            "rewriting) will have to guess at intent."
        )
    )

def _write_file(name: str, file_path: str, file_content: str) -> dict[str, Any]:
    """Add or overwrite a supporting file within any skill directory."""
    err = _validate_file_path(file_path)
    if err:
        return {"success": False, "error": err}

    if not file_content and file_content != "":
        return {"success": False, "error": "file_content is required."}

    # Check size limits
    content_bytes = len(file_content.encode("utf-8"))
    if content_bytes > _MAX_SKILL_FILE_BYTES:
        return {
            "success": False,
            "error": (
                f"File content is {content_bytes:,} bytes "
                f"(limit: {_MAX_SKILL_FILE_BYTES:,} bytes / 1 MiB). "
                f"Consider splitting into smaller files."
            ),
        }
    err = _validate_content_size(file_content, label=file_path)
    if err:
        return {"success": False, "error": err}

    skill_dir = _find_skill(name)
    if not skill_dir:
        return {"success": False, "error": f"Skill '{name}' not found. Create it first with action='create'."}

    target, err = _resolve_skill_target(skill_dir, file_path)
    if err:
        return {"success": False, "error": err}
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(target, file_content)

    return {
        "success": True,
        "message": f"File '{file_path}' written to skill '{name}'.",
        "path": str(target),
    }


def _remove_file(name: str, file_path: str) -> dict[str, Any]:
    """Remove a supporting file from any skill directory."""
    err = _validate_file_path(file_path)
    if err:
        return {"success": False, "error": err}

    skill_dir = _find_skill(name)
    if not skill_dir:
        return {"success": False, "error": f"Skill '{name}' not found."}

    target, err = _resolve_skill_target(skill_dir, file_path)
    if err:
        return {"success": False, "error": err}
    if not target.exists():
        # List what's actually there for the model to see
        available = []
        for subdir in _ALLOWED_SUBDIRS:
            d = skill_dir / subdir
            if d.exists():
                for f in d.rglob("*"):
                    if f.is_file():
                        available.append(str(f.relative_to(skill_dir)))
        return {
            "success": False,
            "error": f"File '{file_path}' not found in skill '{name}'.",
            "available_files": available if available else None,
        }

    target.unlink()

    # Clean up empty subdirectories
    parent = target.parent
    if parent != skill_dir and parent.exists() and not any(parent.iterdir()):
        parent.rmdir()

    return {
        "success": True,
        "message": f"File '{file_path}' removed from skill '{name}'.",
    }


def _find_skill(name: str) -> Path | None:
    for skill_md in AUTO_SKILLS_DIR.glob("**/SKILL.md"):
        if skill_md.parent.name == name:
            return skill_md.parent
    return None

def _resolve_skill_dir(name: str, category: str = None) -> Path:
    """Build the directory path for a new skill, optionally under a category."""
    if category:
        return AUTO_SKILLS_DIR / category / name
    return AUTO_SKILLS_DIR / name

def _atomic_write_text(file_path: Path, content: str, encoding: str = "utf-8") -> None:
    """
    Atomically write text content to a file.
    
    Uses a temporary file in the same directory and os.replace() to ensure
    the target file is never left in a partially-written state if the process
    crashes or is interrupted.
    
    Args:
        file_path: Target file path
        content: Content to write
        encoding: Text encoding (default: utf-8)
    """
    file_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        dir=str(file_path.parent),
        prefix=f".{file_path.name}.tmp.",
        suffix="",
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
        atomic_replace(temp_path, file_path)
    except Exception:
        # Clean up temp file on error
        try:
            os.unlink(temp_path)
        except OSError:
            logger.error("Failed to remove temporary file %s during atomic write", temp_path, exc_info=True)
        raise

def _resolve_skill_target(skill_dir: Path, file_path: str) -> tuple[Path | None, str | None]:
    """Resolve a supporting-file path and ensure it stays within the skill directory."""
    target = skill_dir / file_path
    error = validate_within_dir(target, skill_dir)
    if error:
        return None, error
    return target, None

def _containing_skills_root(skill_path: Path) -> Path:
    """Return the skills root directory (local or external_dirs entry) that
    contains ``skill_path``.  Falls back to the local ``SKILLS_DIR`` if no
    match is found (defensive — callers should have located the skill via
    ``_find_skill`` first).
    """

    try:
        resolved = skill_path.resolve()
    except OSError:
        resolved = skill_path

    from tools.pub_base import get_all_auto_skills_dirs

    try:
        resolved = skill_path.resolve()
    except OSError:
        resolved = skill_path

    for root in get_all_auto_skills_dirs():
        try:
            resolved.relative_to(root.resolve())
            return root
        except (ValueError, OSError):
            continue
    return AUTO_SKILLS_DIR

# =============================================================================
# Validation helpers
# =============================================================================

def _validate_name(name: str) -> str | None:
    """Validate a skill name. Returns error message or None if valid."""
    if not name:
        return "Skill name is required."
    if len(name) > _MAX_NAME_LENGTH:
        return f"Skill name exceeds {_MAX_NAME_LENGTH} characters."
    if not _VALID_NAME_RE.match(name):
        return (
            f"Invalid skill name '{name}'. Use lowercase letters, numbers, "
            f"hyphens, dots, and underscores. Must start with a letter or digit."
        )
    return None

def _validate_category(category: str | None) -> str | None:
    """Validate an optional category name used as a single directory segment."""
    if category is None:
        return None
    if not isinstance(category, str):
        return "Category must be a string."

    category = category.strip()
    if not category:
        return None
    if "/" in category or "\\" in category:
        return (
            f"Invalid category '{category}'. Use lowercase letters, numbers, "
            "hyphens, dots, and underscores. Categories must be a single directory name."
        )
    if len(category) > _MAX_NAME_LENGTH:
        return f"Category exceeds {_MAX_NAME_LENGTH} characters."
    if not _VALID_NAME_RE.match(category):
        return (
            f"Invalid category '{category}'. Use lowercase letters, numbers, "
            "hyphens, dots, and underscores. Categories must be a single directory name."
        )
    return None

def _validate_frontmatter(content: str) -> str | None:
    """
    Validate that SKILL.md content has proper frontmatter with required fields.
    Returns error message or None if valid.
    """
    if not content.strip():
        return "Content cannot be empty."

    if not content.startswith("---"):
        return "SKILL.md must start with YAML frontmatter (---). See existing skills for format."

    end_match = re.search(r'\n---\s*\n', content[3:])
    if not end_match:
        return "SKILL.md frontmatter is not closed. Ensure you have a closing '---' line."

    yaml_content = content[3:end_match.start() + 3]

    try:
        parsed = yaml.safe_load(yaml_content)
    except yaml.YAMLError as e:
        return f"YAML frontmatter parse error: {e}"

    if not isinstance(parsed, dict):
        return "Frontmatter must be a YAML mapping (key: value pairs)."

    if "name" not in parsed:
        return "Frontmatter must include 'name' field."
    if "description" not in parsed:
        return "Frontmatter must include 'description' field."
    if len(str(parsed["description"])) > _MAX_DESCRIPTION_LENGTH:
        return f"Description exceeds {_MAX_DESCRIPTION_LENGTH} characters."

    body = content[end_match.end() + 3:].strip()
    if not body:
        return "SKILL.md must have content after the frontmatter (instructions, procedures, etc.)."

    return None

def _validate_content_size(content: str, label: str = "SKILL.md") -> str | None:
    """Check that content doesn't exceed the character limit for agent writes.

    Returns an error message or None if within bounds.
    """
    if len(content) > _MAX_SKILL_CONTENT_CHARS:
        return (
            f"{label} content is {len(content):,} characters "
            f"(limit: {_MAX_SKILL_CONTENT_CHARS:,}). "
            f"Consider splitting into a smaller SKILL.md with supporting files "
            f"in references/ or templates/."
        )
    return None

def _validate_file_path(file_path: str) -> str | None:
    """
    Validate a file path for write_file/remove_file.
    Must be under an allowed subdirectory and not escape the skill dir.
    """

    if not file_path:
        return "file_path is required."

    normalized = Path(file_path)

    # Prevent path traversal (checked before any allow-listing so the SKILL.md
    # exception below can never be reached by a traversal-laden path).

    if has_traversal_component(file_path):
        return "Path traversal ('..') is not allowed."

    # SKILL.md is the canonical skill file and lives at the skill root, not
    # under an allowed subdirectory. Accept its two natural spellings —
    # 'SKILL.md' and '<skill-name>/SKILL.md' — so callers can target the main
    # file. The traversal guard above still applies, so this can't escape.
    if normalized.parts and normalized.name == "SKILL.md":
        if len(normalized.parts) == 1 or len(normalized.parts) == 2:
            return None

    # Must be under an allowed subdirectory
    if not normalized.parts or normalized.parts[0] not in _ALLOWED_SUBDIRS:
        allowed = ", ".join(sorted(_ALLOWED_SUBDIRS))
        return f"File must be under one of: {allowed}. Got: '{file_path}'"

    # Must have a filename (not just a directory)
    if len(normalized.parts) < 2:
        return f"Provide a file path, not just a directory. Example: '{normalized.parts[0]}/myfile.md'"

    return None

# =============================================================================
# Core actions
# =============================================================================

def _create_skill(name: str, content: str, category: str = None) -> dict[str, Any]:
    """Create a new user skill with SKILL.md content."""
    # Validate name
    err = _validate_name(name)
    if err:
        return {"success": False, "error": err}

    err = _validate_category(category)
    if err:
        return {"success": False, "error": err}

    # Validate content
    err = _validate_frontmatter(content)
    if err:
        return {"success": False, "error": err}

    err = _validate_content_size(content)
    if err:
        return {"success": False, "error": err}

    # Check for name collisions across all directories
    existing = _find_skill(name)
    if existing:
        return {
            "success": False,
            "error": f"A skill named '{name}' already exists at {existing.as_posix()}."
        }

    # Create the skill directory
    skill_dir = _resolve_skill_dir(name, category)
    skill_dir.mkdir(parents=True, exist_ok=True)

    # Write SKILL.md atomically
    skill_md = skill_dir / "SKILL.md"
    _atomic_write_text(skill_md, content)

    result = {
        "success": True,
        "message": f"Skill '{name}' created.",
        "path": str(skill_dir.relative_to(AUTO_SKILLS_DIR)),
        "skill_md": str(skill_md),
    }
    if category:
        result["category"] = category
    result["hint"] = (
        "To add reference files, templates, or scripts, use "
        "skill_manage(action='write_file', name='{}', file_path='references/example.md', file_content='...')".format(name)
    )
    return result

def _edit_skill(name: str, content: str) -> dict[str, Any]:
    """Replace the SKILL.md of any existing skill (full rewrite)."""
    err = _validate_name(name)
    if err:
        return {"success": False, "error": err}

    err = _validate_frontmatter(content)
    if err:
        return {"success": False, "error": err}

    err = _validate_content_size(content)
    if err:
        return {"success": False, "error": err}

    skill_dir: Path | None = _find_skill(name)
    if not skill_dir:
        return {"success": False, "error": f"Skill '{name}' not found in folder '{AUTO_SKILLS_DIR.as_posix()}'"}

    skill_md = skill_dir / "SKILL.md"

    _atomic_write_text(skill_md, content)

    return {
        "success": True,
        "message": f"Skill '{name}' updated (full rewrite).",
        "path": skill_dir.as_posix(),
    }

def _patch_skill(
    name: str,
    old_string: str,
    new_string: str,
    file_path: str = None,
    replace_all: bool = False,
) -> dict[str, Any]:
    """Targeted find-and-replace within a skill file.

    Defaults to SKILL.md. Use file_path to patch a supporting file instead.
    Requires a unique match unless replace_all is True.
    """
    if not old_string:
        return {"success": False, "error": "old_string is required for 'patch'."}
    if new_string is None:
        return {"success": False, "error": "new_string is required for 'patch'. Use an empty string to delete matched text."}

    skill_dir = _find_skill(name)
    if not skill_dir:
        return {"success": False, "error": f"Skill '{name}' not found in folder '{AUTO_SKILLS_DIR.as_posix()}'"}

    if file_path:
        # Patching a supporting file
        err = _validate_file_path(file_path)
        if err:
            return {"success": False, "error": err}
        target, err = _resolve_skill_target(skill_dir, file_path)
        if err:
            return {"success": False, "error": err}
    else:
        # Patching SKILL.md
        target = skill_dir / "SKILL.md"

    if not target.exists():
        return {"success": False, "error": f"File not found: {target.relative_to(skill_dir)}"}

    content = target.read_text(encoding="utf-8")

    # Use the same fuzzy matching engine as the file patch tool.
    # This handles whitespace normalization, indentation differences,
    # escape sequences, and block-anchor matching — saving the agent
    # from exact-match failures on minor formatting mismatches.

    new_content, match_count, _strategy, match_error = fuzzy_find_and_replace(
        content, old_string, new_string, replace_all
    )
    if match_error:
        # Show a short preview of the file so the model can self-correct
        preview = content[:500] + ("..." if len(content) > 500 else "")
        err_msg = match_error
        try:
            err_msg += format_no_match_hint(match_error, match_count, old_string, content)
        except Exception:
            pass
        return {
            "success": False,
            "error": err_msg,
            "file_preview": preview,
        }
 
    # Check size limit on the result
    target_label = "SKILL.md" if not file_path else file_path
    err = _validate_content_size(new_content, label=target_label)
    if err:
        return {"success": False, "error": err}

    # If patching SKILL.md, validate frontmatter is still intact
    if not file_path:
        err = _validate_frontmatter(new_content)
        if err:
            return {
                "success": False,
                "error": f"Patch would break SKILL.md structure: {err}",
            }

    _atomic_write_text(target, new_content)

    return {
        "success": True,
        "message": f"Patched {'SKILL.md' if not file_path else file_path} in skill '{name}' ({match_count} replacement{'s' if match_count > 1 else ''}).",
    }

def _delete_skill(name: str, absorbed_into: str | None = None) -> dict[str, Any]:
    """Delete a skill.

    ``absorbed_into`` declares intent:
      - ``None`` / missing  → caller didn't declare (legacy / non-curator path);
        accepted for backward compat but logs a warning because the curator
        classification pipeline can't tell consolidation from pruning without it.
      - ``""`` (empty)      → explicit "truly pruned, no forwarding target".
      - ``"<skill-name>"``  → content was absorbed into that umbrella; the
        target must exist on disk. Validated here so the model can't claim an
        umbrella that doesn't exist.
    """
    err = _validate_name(name)
    if err:
        return {"success": False, "error": err}

    skill_dir = _find_skill(name)
    if not skill_dir:
        return {"success": False, "error": f"Skill '{name}' not found."}

    # Validate absorbed_into target when declared non-empty
    if absorbed_into is not None and isinstance(absorbed_into, str) and absorbed_into.strip():
        target_name = absorbed_into.strip()
        if target_name == name:
            return {
                "success": False,
                "error": f"absorbed_into='{target_name}' cannot equal the skill being deleted.",
            }
        target = _find_skill(target_name)
        if not target:
            return {
                "success": False,
                "error": (
                    f"absorbed_into='{target_name}' does not exist. "
                    f"Create or patch the umbrella skill first, then retry the delete."
                ),
            }

    skills_root = _containing_skills_root(skill_dir)
    shutil.rmtree(skill_dir)

    # Clean up empty category directories (don't remove the skills root itself)
    parent = skill_dir.parent
    if parent != skills_root and parent.exists() and not any(parent.iterdir()):
        parent.rmdir()

    message = f"Skill '{name}' deleted."
    if absorbed_into is not None and isinstance(absorbed_into, str) and absorbed_into.strip():
        message += f" Content absorbed into '{absorbed_into.strip()}'."

    return {
        "success": True,
        "message": message,
    }

class SkillManage(BaseTool):
    name: str = "skill_manage"
    description: str = (
        "Manage skills (create, update, delete). Skills are your procedural "
        "memory — reusable approaches for recurring task types. "
        f"New skills go to {AUTO_SKILLS_DIR.relative_to(ROOT_DIR)}; existing skills can be modified wherever they live.\n\n"
        "Actions: create (full SKILL.md + optional category), "
        "patch (old_string/new_string — preferred for fixes), "
        "edit (full SKILL.md rewrite — major overhauls only), "
        "delete, write_file, remove_file.\n\n"
        "On delete, pass `absorbed_into=<umbrella>` when you're merging this "
        "skill's content into another one, or `absorbed_into=\"\"` when you're "
        "pruning it with no forwarding target. This lets the curator tell "
        "consolidation from pruning without guessing, so downstream consumers "
        "(cron jobs that reference the old skill name, etc.) get updated "
        "correctly. The target you name in `absorbed_into` must already "
        "exist — create/patch the umbrella first, then delete.\n\n"
        "Create when: complex task succeeded (5+ calls), errors overcome, "
        "user-corrected approach worked, non-trivial workflow discovered, "
        "or user asks you to remember a procedure.\n"
        "Update when: instructions stale/wrong, OS-specific failures, "
        "missing steps or pitfalls found during use. "
        "If you used a skill and hit issues not covered by it, patch it immediately.\n\n"
        "After difficult/iterative tasks, offer to save as a skill. "
        "Skip for simple one-offs. Confirm with user before creating/deleting.\n\n"
        "Good skills: trigger conditions, numbered steps with exact commands, "
        "pitfalls section, verification steps. Use skill_view() to see format examples.\n\n"
        "Pinned skills are protected from deletion only — skill_manage(action='delete') "
        "will refuse with a message pointing the user to `hermes curator unpin <name>`. "
        "Patches and edits go through on pinned skills so you can still improve them as "
        "pitfalls come up; pin only guards against irrecoverable loss."
    )
    args_schema: Type[BaseModel] = SkillManageSchema

    @override
    def _run(
        self,
        action: str,
        name: str,
        content: str | None,
        old_string: str | None,
        new_string: str | None,
        replace_all: bool | None,
        category: str | None,
        file_path: str | None,
        file_content: str | None,
        absorbed_into: str | None,
        **kwargs: Any
    ) -> Any:
        if action == "create":
            if not content:
                return "content is required for 'create'. Provide the full SKILL.md text (frontmatter + body)."
            result = _create_skill(name, content, category)

        elif action == "edit":
            if not content:
                return "content is required for 'edit'. Provide the full updated SKILL.md text."
            result = _edit_skill(name, content)

        elif action == "patch":
            if not old_string:
                return "old_string is required for 'patch'. Provide the text to find."
            if new_string is None:
                return "new_string is required for 'patch'. Use empty string to delete matched text."
            result = _patch_skill(name, old_string, new_string, file_path, replace_all)

        elif action == "delete":
            result = _delete_skill(name, absorbed_into=absorbed_into)

        elif action == "write_file":
            if not file_path:
                return "file_path is required for 'write_file'. Example: 'references/api-guide.md'"
            if file_content is None:
                return "file_content is required for 'write_file'."
            result = _write_file(name, file_path, file_content)

        elif action == "remove_file":
            if not file_path:
                return "file_path is required for 'remove_file'."
            result = _remove_file(name, file_path)

        else:
            result = {"success": False, "error": f"Unknown action '{action}'. Use: create, edit, patch, delete, write_file, remove_file"}

        if result.get("success"):
            try:
                from skills import build_skills_snapshot
                build_skills_snapshot()
            except Exception:
                pass
            # Curator telemetry: bump patch_count on edit/patch/write_file (the actions
            # that mutate an existing skill's guidance), drop the record on delete.
            # Only mark a skill as agent-created when the background self-improvement
            # review fork creates it — foreground `skill_manage(create)` calls are
            # user-directed, and those skills belong to the user (the curator must
            # not touch them). Best-effort; telemetry failures never break the tool.
            try:
                from tools.pub_base import bump_patch, forget, mark_agent_created, is_background_review
                if action == "create":
                    if is_background_review():
                        mark_agent_created(name)
                elif action in {"patch", "edit", "write_file", "remove_file"}:
                    bump_patch(name)
                elif action == "delete":
                    # A recoverable curator archive (routed through archive_skill)
                    # keeps its usage record as STATE_ARCHIVED so `hermes curator
                    # status`/`restore` still see it. Only a hard delete forgets.
                    if not result.get("_archived"):
                        forget(name)
            except Exception:
                pass

        return json.dumps(result, ensure_ascii=False)

    @override
    async def _arun(
        self,
        action: str,
        name: str,
        content: str | None,
        old_string: str | None,
        new_string: str | None,
        replace_all: bool | None,
        category: str | None,
        file_path: str | None,
        file_content: str | None,
        absorbed_into: str | None,
        **kwargs: Any
    ) -> Any:
        return self._run(
            action,
            name,
            content,
            old_string,
            new_string,
            replace_all,
            category,
            file_path,
            file_content,
            absorbed_into,
        )

def build_skill_manage_tool()-> SkillManage:
    tool: SkillManage = SkillManage()
    tool.handle_tool_error = True
    return tool