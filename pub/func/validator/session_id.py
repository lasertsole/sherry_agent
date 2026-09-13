"""Session-id safety validation.

A session id is used to build on-disk paths (``SRC_DIR / session_id / ...``).
It must therefore be a single path segment that cannot escape ``SRC_DIR``.
"""

from config import SRC_DIR


def is_safe_session_id(session_id: str) -> bool:
    """Return True when *session_id* is a single in-tree path segment.

    Rejects empty, ``.``/``..``, and any value containing a path separator,
    then verifies the resolved path still lives under ``SRC_DIR``.
    """
    if not session_id or session_id in (".", ".."):
        return False
    if "/" in session_id or "\\" in session_id:
        return False
    try:
        candidate = (SRC_DIR / session_id).resolve()
    except Exception:
        return False
    return candidate.is_relative_to(SRC_DIR.resolve())
