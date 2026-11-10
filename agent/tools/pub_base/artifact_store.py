"""Content-addressed artifact store for media the agent persists.

Aligned with ZCode's derived-media store: a payload is addressed by its SHA-256
prefix, written once (a second write of the same bytes is a no-op), and
referenced by ``sherry-artifact://<session_id>/<name>``. Files live under
``SRC_DIR/<session_id>/artifacts/`` — per session, so deleting a session removes
its artifacts, and a session can never resolve another session's bytes.

The store is fail-open: a write failure returns ``None`` and the caller keeps
its legacy timestamped media copy; an unparsable/invalid URI resolves to
``None``.
"""

from __future__ import annotations

import hashlib
import re
import threading
from pathlib import Path

from loguru import logger

from config import SRC_DIR
from config.features import MEDIA_PIPELINE
from pub.func.validator import is_safe_session_id

from .atomic_write import atomic_write_bytes_no_follow

__all__ = [
    "ARTIFACT_DIR_NAME",
    "ARTIFACT_URI_PREFIX",
    "artifact_path",
    "artifact_uri",
    "find_artifact",
    "is_valid_artifact_name",
    "parse_artifact_uri",
    "resolve_artifact",
    "write_artifact",
]

ARTIFACT_URI_PREFIX = "sherry-artifact://"
ARTIFACT_DIR_NAME = "artifacts"

_DIGEST_LEN = 16
_MEDIA_TYPES = frozenset({"image", "audio", "video", "pdf", "binary"})
_NAME_RE = re.compile(r"^(?P<family>[a-z]+)-(?P<digest>[0-9a-f]{16})(?P<ext>\.[A-Za-z0-9]{1,8})$")
_EXT_RE = re.compile(r"^\.[A-Za-z0-9]{1,8}$")

# One process-level write lock: writes are rare (a content-addressed store only
# pays for the first copy) and a single lock keeps the dedupe map bounded.
_WRITE_LOCK = threading.Lock()


def _store_root(root: Path | None) -> Path:
    return Path(root) if root is not None else SRC_DIR


def _safe_media_type(media_type: str) -> str:
    return media_type if media_type in _MEDIA_TYPES else "binary"


def _safe_extension(ext: str) -> str:
    ext = (ext or "").strip().lower()
    if ext and not ext.startswith("."):
        ext = f".{ext}"
    return ext if _EXT_RE.match(ext) else ".bin"


def _artifact_dir(session_id: str, root: Path | None = None) -> Path | None:
    if not is_safe_session_id(session_id):
        return None
    return _store_root(root) / session_id / ARTIFACT_DIR_NAME


def is_valid_artifact_name(name: str) -> bool:
    """True for ``<family>-<sha256-prefix><ext>`` — the only stored shape."""
    match = _NAME_RE.match(name)
    return bool(match) and match.group("family") in _MEDIA_TYPES


def artifact_uri(session_id: str, name: str) -> str:
    """Build the URI for a stored artifact (no existence check)."""
    return f"{ARTIFACT_URI_PREFIX}{session_id}/{name}"


def _write_name(data: bytes, media_type: str, ext: str) -> str:
    digest = hashlib.sha256(data).hexdigest()[:_DIGEST_LEN]
    return f"{_safe_media_type(media_type)}-{digest}{_safe_extension(ext)}"


def write_artifact(
    session_id: str,
    data: bytes,
    *,
    media_type: str = "binary",
    ext: str = ".bin",
    root: Path | None = None,
) -> str | None:
    """Store *data* and return its ``sherry-artifact://`` URI.

    The same bytes + media type + extension always map to the same file, so a
    repeated write is skipped (deduplicated by existence). ``None`` means the
    store is unavailable or disabled; callers fall back to their legacy path.
    """
    if not data or not MEDIA_PIPELINE.get("artifact_store_enabled", True):
        return None
    directory = _artifact_dir(session_id, root)
    if directory is None:
        return None
    name = _write_name(data, media_type, ext)
    path = directory / name
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("artifact directory unavailable: {}", exc)
        return None
    with _WRITE_LOCK:
        if not path.exists():
            try:
                atomic_write_bytes_no_follow(path, data)
            except OSError as exc:
                logger.warning("artifact write failed: {}", exc)
                return None
    return artifact_uri(session_id, name)


def parse_artifact_uri(uri: str) -> tuple[str, str] | None:
    """Split the URI into ``(session_id, name)``, or ``None`` when invalid."""
    if not isinstance(uri, str) or not uri.startswith(ARTIFACT_URI_PREFIX):
        return None
    session_id, separator, name = uri[len(ARTIFACT_URI_PREFIX) :].partition("/")
    if not separator or not session_id or not name:
        return None
    if not is_safe_session_id(session_id) or not is_valid_artifact_name(name):
        return None
    return session_id, name


def artifact_path(session_id: str, name: str, *, root: Path | None = None) -> Path | None:
    """Resolve a stored artifact to its file, or ``None`` when it cannot."""
    directory = _artifact_dir(session_id, root)
    if directory is None or not is_valid_artifact_name(name):
        return None
    try:
        resolved_dir = directory.resolve()
        candidate = (resolved_dir / name).resolve()
    except OSError:
        return None
    if not candidate.is_relative_to(resolved_dir) or not candidate.is_file():
        return None
    return candidate


def resolve_artifact(uri: str, *, root: Path | None = None) -> Path | None:
    """Resolve a ``sherry-artifact://`` URI to its file, or ``None``."""
    parsed = parse_artifact_uri(uri)
    if parsed is None:
        return None
    return artifact_path(parsed[0], parsed[1], root=root)


def find_artifact(
    session_id: str,
    data: bytes,
    *,
    media_type: str = "binary",
    root: Path | None = None,
) -> Path | None:
    """Return the stored file for *data* if it was already written, else None."""
    if not data:
        return None
    directory = _artifact_dir(session_id, root)
    if directory is None:
        return None
    digest = hashlib.sha256(data).hexdigest()[:_DIGEST_LEN]
    family = _safe_media_type(media_type)
    try:
        for candidate in directory.glob(f"{family}-{digest}.*"):
            if candidate.is_file():
                return candidate.resolve()
    except OSError:
        return None
    return None
