"""Chunked media upload (ZCode parity): one POST per chunk + a complete call.

``POST /upload/chunk`` stages a bounded piece under ``TEMP_DIR/upload_chunks``;
``POST /upload/complete`` concatenates the pieces, hashes them, and writes the
final file by content hash into the same per-kind directory the whole-body
upload uses (``src/images`` / ``src/audio`` / ``src/video``). Identical bytes
therefore map to one filename and one URL, and the existing static mounts serve
both old and new uploads unchanged.

Every validation failure is a JSON error before any assembly; a partial upload
leaves its staging directory intact for a retry.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

from loguru import logger
from robyn import Response

from config import API_HOST, API_PORT, SRC_DIR, TEMP_DIR
from config.features import HTTP_UPLOAD, SERVER_HTTP
from config.path import is_safe_session_segment
from server.trigger.core import app
from server.trigger.http.helpers import to_text_response

__all__ = ["complete_upload_handler", "upload_chunk_handler"]

_UPLOAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_KIND_DIR: dict[str, str] = {"image": "images", "audio": "audio", "video": "video"}
_KIND_MAX: dict[str, int] = {
    "image": HTTP_UPLOAD["max_image_bytes"],
    "audio": HTTP_UPLOAD["max_audio_bytes"],
    "video": HTTP_UPLOAD["max_video_bytes"],
}
_KIND_EXT: dict[str, dict[str, str]] = {
    "image": SERVER_HTTP["image_content_type_to_ext"],
    "audio": SERVER_HTTP["audio_content_type_to_ext"],
    "video": SERVER_HTTP["video_content_type_to_ext"],
}
_KIND_DEFAULT_EXT: dict[str, str] = {
    "image": SERVER_HTTP["image_default_ext"],
    "audio": SERVER_HTTP["audio_default_ext"],
    "video": SERVER_HTTP["video_default_ext"],
}
_META_NAME = "meta.json"


def _error(status: int, message: str) -> Response:
    return to_text_response(status, {"ok": False, "message": message})


def _request_bytes(request: Any) -> bytes:
    body = request.body
    if isinstance(body, bytes):
        return body
    if isinstance(body, str):
        return body.encode("utf-8")
    return b""


def _chunk_root(session_id: str) -> Path:
    return Path(TEMP_DIR) / "upload_chunks" / session_id


def _kind_for(content_type: str) -> str | None:
    family = content_type.split("/", 1)[0].strip().lower()
    return family if family in _KIND_DIR else None


def _extension(kind: str, content_type: str) -> str:
    normalized = content_type.split(";", 1)[0].strip().lower()
    return _KIND_EXT[kind].get(normalized, _KIND_DEFAULT_EXT[kind])


def _store_final(kind: str, data: bytes, content_type: str) -> tuple[str, str, str]:
    """Write by content hash; return ``(url, filename, sha256)``."""
    digest = hashlib.sha256(data).hexdigest()
    extension = _extension(kind, content_type)
    filename = f"{digest[:16]}{extension}"
    directory = Path(SRC_DIR) / _KIND_DIR[kind]
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename
    if not target.exists():
        target.write_bytes(data)
    url = f"http://{API_HOST}:{API_PORT}/{_KIND_DIR[kind]}/{filename}"
    return url, filename, digest


@app.post("/upload/chunk")
async def upload_chunk_handler(request):
    """Stage one chunk (≤ ``chunk_size_bytes``) for a chunked upload."""
    query = request.query_params
    session_id = str(query.get("session_id", "") or "")
    upload_id = str(query.get("upload_id", "") or "")
    if not is_safe_session_segment(session_id) or not _UPLOAD_ID_RE.match(upload_id):
        return _error(400, "invalid session_id or upload_id")

    try:
        chunk_index = int(query.get("chunk_index", ""))
        total_chunks = int(query.get("total_chunks", ""))
    except (TypeError, ValueError):
        return _error(400, "chunk_index and total_chunks must be integers")
    if total_chunks < 1 or total_chunks > 4096 or not 0 <= chunk_index < total_chunks:
        return _error(400, "invalid chunk range")

    content_type = str(query.get("content_type", "") or "").strip()
    kind = _kind_for(content_type)
    if kind is None:
        return _error(400, "unsupported content_type")

    data = _request_bytes(request)
    chunk_size = int(HTTP_UPLOAD["chunk_size_bytes"])
    if not data:
        return _error(400, "empty chunk")
    if len(data) > chunk_size:
        return _error(413, f"chunk exceeds {chunk_size} bytes")

    upload_dir = _chunk_root(session_id) / upload_id
    chunk_path = upload_dir / str(chunk_index)
    try:
        upload_dir.mkdir(parents=True, exist_ok=True)
        chunk_path.write_bytes(data)
        meta = {
            "filename": str(query.get("filename", "") or ""),
            "content_type": content_type,
            "total_chunks": total_chunks,
        }
        (upload_dir / _META_NAME).write_text(json.dumps(meta), encoding="utf-8")
    except OSError as exc:
        logger.warning("chunk upload staging failed: {}", exc)
        return _error(500, "could not stage the chunk")

    return to_text_response(
        200,
        {
            "ok": True,
            "chunk_index": chunk_index,
            "received": len(data),
        },
    )


@app.post("/upload/complete")
async def complete_upload_handler(request):
    """Assemble the staged chunks by content hash and answer the served URL."""
    query = request.query_params
    session_id = str(query.get("session_id", "") or "")
    upload_id = str(query.get("upload_id", "") or "")
    if not is_safe_session_segment(session_id) or not _UPLOAD_ID_RE.match(upload_id):
        return _error(400, "invalid session_id or upload_id")

    upload_dir = _chunk_root(session_id) / upload_id
    meta_path = upload_dir / _META_NAME
    if not meta_path.is_file():
        return _error(404, "upload not found")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        total_chunks = int(meta["total_chunks"])
    except (OSError, ValueError, KeyError, TypeError):
        return _error(400, "invalid upload metadata")

    content_type = str(meta.get("content_type", ""))
    kind = _kind_for(content_type)
    if kind is None:
        return _error(400, "unsupported content_type")

    pieces: list[bytes] = []
    size = 0
    for index in range(total_chunks):
        path = upload_dir / str(index)
        if not path.is_file():
            return _error(400, f"missing chunk {index}")
        try:
            piece = path.read_bytes()
        except OSError as exc:
            logger.warning("chunk {} unreadable: {}", index, exc)
            return _error(500, "could not read a staged chunk")
        size += len(piece)
        if size > _KIND_MAX[kind]:
            return _error(413, "uploaded file exceeds the size limit")
        pieces.append(piece)

    data = b"".join(pieces)
    try:
        url, filename, digest = _store_final(kind, data, content_type)
    except OSError as exc:
        logger.warning("final upload write failed: {}", exc)
        return _error(500, "could not store the uploaded file")
    shutil.rmtree(upload_dir, ignore_errors=True)
    logger.info(
        "Chunked upload complete: session={} upload={} bytes={} file={}",
        session_id,
        upload_id,
        len(data),
        filename,
    )
    return to_text_response(200, {"ok": True, "url": url, "filename": filename, "sha256": digest})
