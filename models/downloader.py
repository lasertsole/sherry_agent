"""Small streaming-download helpers for local model prefetch.

The code embedder has its own ONNX downloader; this module is the generic
counterpart used by the 模型配置 local-model download buttons (GGUF/residual
files). It streams to ``<dest>.part`` and atomically replaces the target so a
crashed download never leaves a truncated model file behind.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path


def probe_size(url: str, timeout_seconds: float) -> int:
    """Best-effort Content-Length via HEAD; 0 when the server omits it."""
    import httpx

    try:
        response = httpx.head(url, follow_redirects=True, timeout=timeout_seconds)
        response.raise_for_status()
        return int(response.headers.get("content-length") or 0)
    except Exception:  # noqa: BLE001 - a probe is advisory; the stream reports its own total
        return 0


def stream_download(
    url: str,
    dest: Path,
    *,
    timeout_seconds: float,
    chunk_bytes: int,
    progress: Callable[[int, int], None] | None = None,
) -> int:
    """Stream ``url`` into ``dest``, resuming a leftover ``.part`` when possible.

    Returns the complete number of bytes in ``dest``. A failed transfer keeps
    its partial file so the next call can send a Range request instead of
    restarting a multi-GB model from zero (a server that ignores Range answers
    200, which restarts the file cleanly).
    """
    import httpx

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(f"{dest.name}.part")
    resume_from = part.stat().st_size if part.is_file() else 0
    headers = {"Range": f"bytes={resume_from}-"} if resume_from else {}
    try:
        with httpx.stream(
            "GET",
            url,
            headers=headers,
            follow_redirects=True,
            timeout=httpx.Timeout(timeout_seconds),
        ) as response:
            if response.status_code == 416:
                part.unlink(missing_ok=True)
                raise RuntimeError("download resume was rejected (HTTP 416); retry the download")
            response.raise_for_status()
            # A server that ignores Range answers 200 with the WHOLE body.
            if resume_from and response.status_code != 206:
                resume_from = 0
            reported_total = int(response.headers.get("content-length") or 0)
            total = resume_from + reported_total if reported_total > 0 else 0
            downloaded = resume_from
            if progress is not None:
                progress(downloaded, total)
            with part.open("ab" if resume_from else "wb") as handle:
                for chunk in response.iter_bytes(chunk_size=chunk_bytes):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    downloaded += len(chunk)
                    if progress is not None:
                        progress(downloaded, total)
        part.replace(dest)
    except Exception:
        # Keep the partial file for a resumed retry; only a 416 (handled above)
        # deletes it because the remote no longer accepts that offset.
        raise
    return downloaded
