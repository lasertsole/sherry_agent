"""Chunked media upload (ZCode parity): begin → sequential chunks → complete.

The client opens an upload with ``POST /upload/begin``, sends every chunk in
index order with ``POST /upload/chunk`` (the server rejects a gap or a replayed
index, mirroring ZCode's ``chunkGap`` / ``chunkConflict``), and finishes with
``POST /upload/complete``, which concatenates, hashes and stores the bytes by
content hash. ``/upload/abort`` cleans a staging directory and
``/upload/status`` reports how many chunks arrived (the resume hook).

Staging lives in ``TEMP_DIR/upload_chunks/<upload_id>/``; the final file lands
in the same per-kind directory the whole-body upload uses, so identical bytes
reuse one filename and URL.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from loguru import logger
from robyn import Response

from config import API_HOST, API_PORT, SRC_DIR, TEMP_DIR
from config.features import HTTP_UPLOAD, SERVER_HTTP
from config.path import is_safe_session_segment
from server.trigger.core import app
from server.trigger.http.helpers import to_text_response

__all__ = [
    "abort_upload_handler",
    "begin_upload_handler",
    "complete_upload_handler",
    "status_upload_handler",
    "upload_chunk_handler",
]

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


def _error(status: int, message: str, *, code: str | None = None) -> Response:
    payload: dict[str, Any] = {"ok": False, "message": message}
    if code is not None:
        payload["code"] = code
    return to_text_response(status, payload)


def _request_bytes(request: Any) -> bytes:
    body = request.body
    if isinstance(body, bytes):
        return body
    if isinstance(body, str):
        return body.encode("utf-8")
    return b""


def _staging_root() -> Path:
    return Path(TEMP_DIR) / "upload_chunks"


def _upload_dir(upload_id: str) -> Path:
    return _staging_root() / upload_id


def _kind_for(content_type: str) -> str | None:
    family = content_type.split("/", 1)[0].strip().lower()
    return family if family in _KIND_DIR else None


def _extension(kind: str, content_type: str) -> str:
    normalized = content_type.split(";", 1)[0].strip().lower()
    return _KIND_EXT[kind].get(normalized, _KIND_DEFAULT_EXT[kind])


def _read_meta(upload_dir: Path) -> dict[str, Any] | None:
    path = upload_dir / _META_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _received_count(upload_dir: Path, total_chunks: int) -> int:
    """Number of contiguous chunks present (0..N-1)."""
    count = 0
    for index in range(total_chunks):
        if not (upload_dir / str(index)).is_file():
            break
        count += 1
    return count


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


@app.post("/upload/begin")
async def begin_upload_handler(request):
    """Open an upload session and return its id plus the first chunk index."""
    query = request.query_params
    session_id = str(query.get("session_id", "") or "")
    if not is_safe_session_segment(session_id):
        return _error(400, "invalid session_id")

    content_type = str(query.get("content_type", "") or "").strip()
    kind = _kind_for(content_type)
    if kind is None:
        return _error(400, "unsupported content_type")

    try:
        total_chunks = int(query.get("total_chunks", ""))
        total_bytes = int(query.get("total_bytes", ""))
    except (TypeError, ValueError):
        return _error(400, "total_chunks and total_bytes must be integers")

    chunk_size = int(HTTP_UPLOAD["chunk_size_bytes"])
    max_bytes = _KIND_MAX[kind]
    max_chunks = math.ceil(max_bytes / chunk_size)
    if total_bytes <= 0 or total_chunks <= 0:
        return _error(400, "empty upload")
    if total_bytes > max_bytes:
        return _error(413, "uploaded file exceeds the size limit")
    if total_chunks > max_chunks:
        return _error(413, "too many chunks")
    if total_chunks != math.ceil(total_bytes / chunk_size):
        return _error(400, "total_chunks does not match total_bytes")

    upload_id = uuid.uuid4().hex
    upload_dir = _upload_dir(upload_id)
    try:
        upload_dir.mkdir(parents=True, exist_ok=False)
        (upload_dir / _META_NAME).write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "filename": str(query.get("filename", "") or ""),
                    "content_type": content_type,
                    "total_chunks": total_chunks,
                    "total_bytes": total_bytes,
                    "received_bytes": 0,
                }
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("upload begin failed: {}", exc)
        return _error(500, "could not create the upload session")

    return to_text_response(200, {"ok": True, "upload_id": upload_id, "next_chunk_index": 0})


@app.post("/upload/chunk")
async def upload_chunk_handler(request):
    """Append one chunk; the index must be exactly the next expected one."""
    query = request.query_params
    upload_id = str(query.get("upload_id", "") or "")
    if not _UPLOAD_ID_RE.match(upload_id):
        return _error(400, "invalid upload_id")

    upload_dir = _upload_dir(upload_id)
    meta = _read_meta(upload_dir)
    if meta is None:
        return _error(404, "upload not found")
    total_chunks = int(meta.get("total_chunks") or 0)
    total_bytes = int(meta.get("total_bytes") or 0)

    try:
        chunk_index = int(query.get("chunk_index", ""))
    except (TypeError, ValueError):
        return _error(400, "chunk_index must be an integer")

    received = _received_count(upload_dir, total_chunks)
    if chunk_index < received:
        return _error(409, "chunk already received", code="chunkConflict")
    if chunk_index > received:
        return _error(409, f"expected chunk {received}", code="chunkGap")

    data = _request_bytes(request)
    chunk_size = int(HTTP_UPLOAD["chunk_size_bytes"])
    if not data:
        return _error(400, "empty chunk")
    if len(data) > chunk_size:
        return _error(413, f"chunk exceeds {chunk_size} bytes")
    received_bytes = int(meta.get("received_bytes") or 0)
    if received_bytes + len(data) > total_bytes:
        return _error(413, "uploaded bytes exceed the declared size")

    try:
        (upload_dir / str(chunk_index)).write_bytes(data)
        meta["received_bytes"] = received_bytes + len(data)
        (upload_dir / _META_NAME).write_text(json.dumps(meta), encoding="utf-8")
    except OSError as exc:
        logger.warning("chunk upload staging failed: {}", exc)
        return _error(500, "could not stage the chunk")

    return to_text_response(200, {"ok": True, "next_chunk_index": chunk_index + 1})


@app.post("/upload/complete")
async def complete_upload_handler(request):
    """Assemble the staged chunks by content hash and answer the served URL."""
    query = request.query_params
    upload_id = str(query.get("upload_id", "") or "")
    if not _UPLOAD_ID_RE.match(upload_id):
        return _error(400, "invalid upload_id")

    upload_dir = _upload_dir(upload_id)
    meta = _read_meta(upload_dir)
    if meta is None:
        return _error(404, "upload not found")

    content_type = str(meta.get("content_type", ""))
    kind = _kind_for(content_type)
    if kind is None:
        return _error(400, "unsupported content_type")
    total_chunks = int(meta.get("total_chunks") or 0)
    total_bytes = int(meta.get("total_bytes") or 0)
    received = _received_count(upload_dir, total_chunks)
    if received != total_chunks or int(meta.get("received_bytes") or 0) != total_bytes:
        return _error(
            400,
            f"missing chunks: received {received} of {total_chunks}",
            code="chunkGap",
        )

    pieces: list[bytes] = []
    size = 0
    for index in range(total_chunks):
        path = upload_dir / str(index)
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
        "Chunked upload complete: upload={} bytes={} file={}",
        upload_id,
        len(data),
        filename,
    )
    return to_text_response(200, {"ok": True, "url": url, "filename": filename, "sha256": digest})


@app.post("/upload/abort")
async def abort_upload_handler(request):
    """Discard a staging directory (idempotent)."""
    upload_id = str(request.query_params.get("upload_id", "") or "")
    if not _UPLOAD_ID_RE.match(upload_id):
        return _error(400, "invalid upload_id")
    shutil.rmtree(_upload_dir(upload_id), ignore_errors=True)
    return to_text_response(200, {"ok": True})


@app.get("/upload/status")
async def status_upload_handler(request):
    """Report the contiguous chunk count (the resume hook)."""
    upload_id = str(request.query_params.get("upload_id", "") or "")
    if not _UPLOAD_ID_RE.match(upload_id):
        return _error(400, "invalid upload_id")
    upload_dir = _upload_dir(upload_id)
    meta = _read_meta(upload_dir)
    if meta is None:
        return _error(404, "upload not found")
    total_chunks = int(meta.get("total_chunks") or 0)
    return to_text_response(
        200,
        {
            "ok": True,
            "received_chunks": _received_count(upload_dir, total_chunks),
            "total_chunks": total_chunks,
        },
    )
