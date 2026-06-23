import re
import shutil
from pathlib import Path
from loguru import logger
from config import WORKSPACE_DIR, WORKSPACE_TEMPLATE_DIR, HEARTBEAT_PATH


def _find_heading(lines: list[str], heading: str) -> int:
    """Return the 0-based line index of a ``## <heading>`` section."""
    for i, ln in enumerate(lines):
        if ln.strip() == heading:
            return i
    raise ValueError(f"Could not find '{heading}' section in HEARTBEAT.md")


def _section_content_indices(lines: list[str], heading_idx: int) -> tuple[int, list[int]]:
    """Return (next_heading_idx, content_line_indices) for the section starting at heading_idx.

    ``content_line_indices`` are non-blank, non-HTML-comment lines between the
    heading and the next ``##`` heading (or EOF).
    """
    next_heading_idx = len(lines)
    for i in range(heading_idx + 1, len(lines)):
        if re.match(r"^##\s", lines[i]):
            next_heading_idx = i
            break

    content_indices = [
        j for j in range(heading_idx + 1, next_heading_idx)
        if lines[j].strip() and not lines[j].strip().startswith("<!--")
    ]
    return next_heading_idx, content_indices


def ensure_heartbeat_file_exists() -> None:
    """Ensure HEARTBEAT.md exists."""
    src = WORKSPACE_TEMPLATE_DIR / "HEARTBEAT.md"
    dst = WORKSPACE_DIR / "HEARTBEAT.md"
    if not dst.exists():
        shutil.copy2(src, dst)
        print(f"Copied HEARTBEAT.md from template to {dst}")
    else:
        print("HEARTBEAT.md already exists")

def add_task_to_heartbeat(task_text: str, index: int | None = None) -> str:
    """Add a task item under the ``## Active Tasks`` section in HEARTBEAT.md.

    Args:
        task_text: The task description to add (as a Markdown list item or paragraph).
        index: Optional 0-based insertion position within Active Tasks content lines
               (skipping blanks and HTML comments). ``None`` (default) appends at end.

    Returns:
        The full updated content of HEARTBEAT.md, or raises if the section
        cannot be found.
    """
    ensure_heartbeat_file_exists()
    path = Path(HEARTBEAT_PATH)
    lines = path.read_text(encoding="utf-8").splitlines()

    active_idx = _find_heading(lines, "## Active Tasks")
    _, content_indices = _section_content_indices(lines, active_idx)

    # Build the new task line (ensure it's a bullet item)
    task_line = task_text.strip()
    if not task_line.startswith("-"):
        task_line = f"- [ ] {task_line}"

    if index is not None:
        if not (0 <= index <= len(content_indices)):
            raise IndexError(
                f"Index {index} out of range for {len(content_indices)} task lines"
            )
        insert_at = content_indices[index]
    else:
        # Append: after the last content line, or just after the heading if empty
        insert_at = content_indices[-1] + 1 if content_indices else active_idx + 1

    lines.insert(insert_at, task_line)

    new_content = "\n".join(lines)
    path.write_text(new_content, encoding="utf-8")
    return new_content


def list_active_tasks() -> list[str]:
    """Return the content lines under ``## Active Tasks`` as a list of strings.

    HTML comment lines (``<!-- ... -->``) and blank lines are excluded.
    """
    ensure_heartbeat_file_exists()
    path = Path(HEARTBEAT_PATH)
    lines = path.read_text(encoding="utf-8").splitlines()

    active_idx = _find_heading(lines, "## Active Tasks")
    _, content_indices = _section_content_indices(lines, active_idx)

    return [lines[i] for i in content_indices]


def list_completed_tasks() -> list[str]:
    """Return the content lines under ``## Completed`` as a list of strings.

    HTML comment lines (``<!-- ... -->``) and blank lines are excluded.
    """
    ensure_heartbeat_file_exists()
    path = Path(HEARTBEAT_PATH)
    lines = path.read_text(encoding="utf-8").splitlines()

    completed_idx = _find_heading(lines, "## Completed")
    _, content_indices = _section_content_indices(lines, completed_idx)

    return [lines[i] for i in content_indices]


def remove_tasks_from_completed(task_text: str | list[str] | None = None) -> str:
    """Remove task line(s) from the ``## Completed`` section.

    HTML comment lines and the heading itself are preserved.  Only content
    lines (non-blank, non-HTML-comment) are removed.

    Args:
        task_text: What to remove.
                   - ``None`` (default): remove **all** content lines.
                   - A single string: substring-match, remove matching line(s).
                   - A list of strings: substring-match each, remove all matching.

    Returns:
        The full updated content of HEARTBEAT.md.

    Raises:
        ValueError: If the ``## Completed`` section cannot be found, or if any
                    string in ``task_text`` matches zero lines.
    """
    ensure_heartbeat_file_exists()
    path = Path(HEARTBEAT_PATH)
    lines = path.read_text(encoding="utf-8").splitlines()

    completed_idx = _find_heading(lines, "## Completed")
    _, completed_contents = _section_content_indices(lines, completed_idx)

    if task_text is None:
        indices_to_remove = list(completed_contents)
    elif isinstance(task_text, list):
        indices_to_remove: list[int] = []
        for t in task_text:
            matched = [idx for idx in completed_contents if t.strip() in lines[idx].strip()]
            if not matched:
                error_text: str = f"No task matching '{t}' found in ## Completed"
                logger.error(error_text)
                raise ValueError(error_text)
            indices_to_remove.extend(matched)
        indices_to_remove = list(dict.fromkeys(indices_to_remove))
    else:
        indices_to_remove = [idx for idx in completed_contents if task_text.strip() in lines[idx].strip()]
        if not indices_to_remove:
            error_text: str = f"No task matching '{task_text}' found in ## Completed"
            logger.error(error_text)
            raise ValueError(error_text)

    for idx in reversed(indices_to_remove):
        lines.pop(idx)

    # Compact consecutive blank lines in the section to at most one blank line.
    # Recalculate section boundaries after popping.
    completed_idx = _find_heading(lines, "## Completed")
    section_start = completed_idx + 1
    section_end = len(lines)
    for i in range(section_start, len(lines)):
        if re.match(r"^##\s", lines[i]):
            section_end = i
            break

    new_section: list[str] = []
    prev_blank = False
    for i in range(section_start, section_end):
        is_blank = lines[i].strip() == ""
        if is_blank and prev_blank:
            continue  # squash consecutive blanks
        new_section.append(lines[i])
        prev_blank = is_blank

    lines[section_start:section_end] = new_section

    new_content = "\n".join(lines)
    path.write_text(new_content, encoding="utf-8")
    return new_content


def clear_completed_tasks(task_text: str | list[str] | None = None) -> str:
    """Alias for :func:`remove_tasks_from_completed`.

    Removes task line(s) from the ``## Completed`` section.
    See :func:`remove_tasks_from_completed` for full documentation.
    """
    return remove_tasks_from_completed(task_text)


def move_task_to_completed(task_text: str) -> str:
    """Move a matching task line from ``## Active Tasks`` to ``## Completed``.

    The task line is identified by exact substring match (after stripping) against
    content lines in the Active Tasks section.  The line is removed from Active
    Tasks and inserted before the last content line of Completed (or after the
    Completed heading if that section has no content lines).

    Args:
        task_text: The text to match against task lines (substring match).

    Returns:
        The full updated content of HEARTBEAT.md.

    Raises:
        ValueError: If the section is not found or no line matches ``task_text``.
    """
    ensure_heartbeat_file_exists()
    path = Path(HEARTBEAT_PATH)
    lines = path.read_text(encoding="utf-8").splitlines()

    # Locate both sections
    active_idx = _find_heading(lines, "## Active Tasks")
    completed_idx = _find_heading(lines, "## Completed")

    _, active_contents = _section_content_indices(lines, active_idx)
    _, completed_contents = _section_content_indices(lines, completed_idx)

    # Find the matching line in Active Tasks
    match_idx: int | None = None
    for idx in active_contents:
        if task_text.strip() in lines[idx].strip():
            match_idx = idx
            break

    if match_idx is None:
        error_text: str = f"No task matching '{task_text}' found in ## Active Tasks"
        logger.error(error_text)
        raise ValueError(error_text)

    # Remove from Active Tasks (save the line content)
    removed_line = lines.pop(match_idx)

    # Insert into Completed: before the last content line, or after heading if empty
    if completed_contents:
        # Adjust completed_contents indices — they may have shifted if match_idx
        # came before the completed section (it always does). No adjustment needed.
        insert_at = completed_contents[-1] + 1
    else:
        insert_at = completed_idx + 1

    lines.insert(insert_at, removed_line)

    new_content = "\n".join(lines)
    path.write_text(new_content, encoding="utf-8")
    return new_content