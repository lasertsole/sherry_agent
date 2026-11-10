"""Shared download lifecycle states (2026-11-09 audit L10).

The local-model download panel and the code-embedder panel kept the same four
phases as bare strings; a typo in one comparison silently disabled a state.
One StrEnum is the source of truth (StrEnum values serialize as plain strings,
so the HTTP payload is unchanged).
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["DownloadState"]


class DownloadState(StrEnum):
    IDLE = "idle"
    DOWNLOADING = "downloading"
    READY = "ready"
    ERROR = "error"
