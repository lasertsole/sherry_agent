"""Per-working-dir shared KVStore + executor.

All storage classes that share the same db file get one SQLite connection
and one serialising ThreadPoolExecutor.  Reference-counting ensures the
store is closed only after the last user has finalised.

If SNKV marks a connection as corrupted (isCorrupted=1), force_reset()
closes the old KVStore and opens a fresh one without disturbing the
executor or the refcount.  Storage classes use a reset_token to detect
when the underlying connection has been replaced and reopen their column
families accordingly.
"""
from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from snkv import KVStore

_BUSY_TIMEOUT_MS = 5000


@dataclass
class SharedStore:
    kv: KVStore
    executor: ThreadPoolExecutor
    db_path: str
    ref_count: int = 0
    reset_token: int = 0


_registry: dict[str, SharedStore] = {}
_lock = threading.Lock()


def acquire(db_path: str) -> SharedStore:
    """Return (or create) the shared store for *db_path*, incrementing refcount."""
    with _lock:
        if db_path not in _registry:
            kv = KVStore(db_path, busy_timeout=_BUSY_TIMEOUT_MS)
            ex = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix=f"snkv_{os.path.basename(db_path)}",
            )
            _registry[db_path] = SharedStore(kv=kv, executor=ex, db_path=db_path)
        entry = _registry[db_path]
        entry.ref_count += 1
        return entry


def release(db_path: str) -> None:
    """Decrement refcount; close KVStore + executor when the last user releases."""
    with _lock:
        entry = _registry.get(db_path)
        if entry is None:
            return
        entry.ref_count -= 1
        if entry.ref_count <= 0:
            try:
                entry.kv.close()
            except Exception:
                pass
            entry.executor.shutdown(wait=False)
            del _registry[db_path]


def force_reset(db_path: str, current_token: int) -> int:
    """Replace a corrupted KVStore connection with a fresh one.

    Idempotent: only reconnects when the store's reset_token matches
    *current_token*.  Returns the (possibly updated) reset_token so
    callers can detect whether this call or a prior one did the work.
    """
    with _lock:
        entry = _registry.get(db_path)
        if entry is None:
            return 0
        if entry.reset_token != current_token:
            return entry.reset_token
        try:
            entry.kv.close()
        except Exception:
            pass
        entry.kv = KVStore(db_path, busy_timeout=_BUSY_TIMEOUT_MS)
        entry.reset_token += 1
        return entry.reset_token
