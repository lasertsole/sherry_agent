import re
from pathlib import Path

def has_traversal_component(path_str: str) -> bool:
    """Return True if *path_str* contains traversal components (e.g. ``..``
    or Windows dot-only names like ``...`` / ``....`` that resolve to parent).

    Quick check for obvious traversal attempts before doing full resolution.
    """
    parts = Path(path_str).parts
    return any(re.compile(r"\.{2,}").fullmatch(p) for p in parts)

def validate_within_dir(path: Path, root: Path) -> str | None:
    """Ensure *path* resolves to a location within *root*.

    Returns an error message string if validation fails, or ``None`` if the
    path is safe.  Uses ``Path.resolve()`` to follow symlinks and normalize
    ``..`` components.

    Usage::

        error = validate_within_dir(user_path, allowed_root)
        if error:
            return json.dumps({"error": error})
    """
    try:
        resolved = path.resolve()
        root_resolved = root.resolve()
        resolved.relative_to(root_resolved)
    except (ValueError, OSError) as exc:
        return f"Path escapes allowed directory: {exc}"
    return None