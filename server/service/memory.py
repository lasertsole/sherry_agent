"""Compatibility shim: the legacy full-file memory API.

Long-term memory lives in SQLite now (``server/service/memory_entries.py``).
These two functions preserve the old ``GET/PUT /memory`` contract for existing
clients and scripts: read renders active entries as ``§``-delimited text, write
replaces a target's entries with the parsed body.
"""

from server.service.memory_entries import (
    read_memory_files as read_memory_files,
    write_memory_files as write_memory_files,
)

__all__ = ["read_memory_files", "write_memory_files"]
