"""Persistent operator-scoped tool-approval store (P2-2).

Ports DeepAgents' persisted tool-approval policy: approval decisions
(allow / deny) for an exact ``(operator, session, tool, args)`` call survive
process restarts instead of being lost with the in-memory registers.

Storage is a single JSON file at :func:`config.path.resolve_approval_store_path`
(``SRC_DIR/data/approvals.json`` by default) with an **optimistic byte-revision
CAS**: every read captures a sha256 revision of the raw bytes, every write may
only land when the file still carries that revision, and a losing writer
re-reads and retries a bounded number of times. Writes are atomic
(temp file + ``os.replace``) and the file is created ``0600``; only the args
*hash* is stored — never raw tool arguments or secrets.

The store is operator-scoped: a decision is only visible to the operator that
made it. With no operator in scope — no explicit argument and no
:func:`operator_scope` — :meth:`ToolApprovalStore.evaluate` auto-denies with
an explicit message instead of silently allowing the call.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, UTC
from enum import StrEnum
from pathlib import Path
from typing import Any
from collections.abc import Callable

from loguru import logger

from agent.middlewares.base import args_hash
from config.path import resolve_approval_store_path

from .approval_scope import NO_OPERATOR_MESSAGE, current_operator

STORE_VERSION = 1
_DEFAULT_MAX_CAS_RETRIES = 3
_REASON_MAX_CHARS = 240

# One process-wide lock per store path: the whole read-modify-write is
# serialized inside the process, while cross-process writers are reconciled by
# the revision CAS. Reentrant because _write_if_revision re-acquires it.
_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _path_lock(path: Path) -> threading.RLock:
    """Return the process-wide mutation lock for *path*."""
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(str(path), threading.RLock())


class ApprovalVerdict(StrEnum):
    """Persisted verdict for a tool call.

    ``ALLOW`` / ``DENY`` come from a recorded decision; ``UNKNOWN`` means no
    decision was ever recorded for this exact call.
    """

    ALLOW = "allow"
    DENY = "deny"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ApprovalEvaluation:
    """Result of :meth:`ToolApprovalStore.evaluate`.

    Attributes:
        verdict: The recorded (or auto-denied) decision.
        reason: Human-readable explanation, empty for ``UNKNOWN``.
        auto_denied: ``True`` when no operator was present to decide.
    """

    verdict: ApprovalVerdict
    reason: str = ""
    auto_denied: bool = False


@dataclass(frozen=True)
class _Snapshot:
    """A read of the store file: decoded state plus its byte revision."""

    data: dict[str, Any]
    revision: str  # sha256 of the raw bytes; "" when the file does not exist


def _empty_state() -> dict[str, Any]:
    """Return the initial store document."""
    return {"version": STORE_VERSION, "revision": 0, "approvals": {}}


def _nested(node: Any, *keys: str) -> Any:
    """Walk *keys* through nested dicts, returning ``None`` on any miss."""
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


class ToolApprovalStore:
    """JSON approval store with operator scoping and byte-revision CAS."""

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        max_cas_retries: int = _DEFAULT_MAX_CAS_RETRIES,
    ):
        """Initialize the store.

        Args:
            path: Store file path; defaults to the configured Sherry location.
            max_cas_retries: Bounded read-modify-write CAS attempts per record.
        """
        self.path: Path = Path(path) if path is not None else resolve_approval_store_path()
        self.max_cas_retries = max(1, int(max_cas_retries))
        self.cas_conflicts = 0

    # ── Public API ──────────────────────────────────────────────────────

    def evaluate(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        session_id: str,
        *,
        operator: str | None = None,
    ) -> ApprovalEvaluation:
        """Resolve the persisted decision for one exact tool call.

        No operator in scope auto-denies (with an explicit message); a missing
        record returns ``UNKNOWN`` and leaves the caller's default in place.
        """
        scoped = self._resolve_operator(operator)
        if scoped is None:
            return ApprovalEvaluation(ApprovalVerdict.DENY, NO_OPERATOR_MESSAGE, auto_denied=True)
        record = self._lookup(scoped, session_id, tool_name, tool_args)
        if record is None:
            return ApprovalEvaluation(ApprovalVerdict.UNKNOWN)
        allowed = record.get("decision") == ApprovalVerdict.ALLOW.value
        return ApprovalEvaluation(
            ApprovalVerdict.ALLOW if allowed else ApprovalVerdict.DENY,
            str(record.get("reason") or ""),
        )

    def record(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        session_id: str,
        *,
        allow: bool,
        reason: str = "",
        operator: str | None = None,
    ) -> bool:
        """Persist a decision for one exact tool call via bounded CAS retries.

        Returns ``True`` when the decision landed; ``False`` when no operator is
        in scope (cannot attribute the decision) or the CAS retries are spent.
        """
        scoped = self._resolve_operator(operator)
        if scoped is None:
            logger.debug(
                "tool approval not recorded (no operator): tool={} session={}",
                tool_name,
                session_id,
            )
            return False
        entry = {
            "decision": ApprovalVerdict.ALLOW.value if allow else ApprovalVerdict.DENY.value,
            "reason": str(reason)[:_REASON_MAX_CHARS],
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        key = args_hash(tool_args)

        def _mutate(data: dict[str, Any]) -> None:
            bucket = (
                data.setdefault("approvals", {})
                .setdefault(scoped, {})
                .setdefault(session_id, {})
                .setdefault(tool_name, {})
            )
            bucket[key] = entry

        if not self._mutate_cas(_mutate):
            logger.warning(
                "tool approval CAS exhausted after {} retries: tool={} session={}",
                self.max_cas_retries,
                tool_name,
                session_id,
            )
            return False
        logger.debug(
            "tool approval recorded: tool={} session={} allow={}", tool_name, session_id, allow
        )
        return True

    # ── Internals ───────────────────────────────────────────────────────

    def _resolve_operator(self, operator: str | None) -> str | None:
        """Resolve the operator scope: explicit argument, then ContextVar."""
        if operator is not None:
            return operator.strip() or None
        scoped = current_operator()
        return scoped.strip() or None if isinstance(scoped, str) else None

    def _lookup(
        self, operator: str, session_id: str, tool_name: str, tool_args: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Return the stored record dict for this exact call, or ``None``."""
        snapshot = self._read_snapshot()
        entry = _nested(
            snapshot.data.get("approvals"), operator, session_id, tool_name, args_hash(tool_args)
        )
        return entry if isinstance(entry, dict) else None

    def _read_snapshot(self) -> _Snapshot:
        """Read the raw file bytes, its revision, and the decoded document."""
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return _Snapshot(_empty_state(), "")
        except OSError:
            logger.warning("tool approval store unreadable; treating as empty: {}", self.path)
            return _Snapshot(_empty_state(), "")
        revision = hashlib.sha256(raw).hexdigest()
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            logger.warning("tool approval store corrupt; treating as empty: {}", self.path)
            return _Snapshot(_empty_state(), revision)
        if not isinstance(data, dict):
            logger.warning(
                "tool approval store is not a JSON object; treating as empty: {}", self.path
            )
            return _Snapshot(_empty_state(), revision)
        return _Snapshot(data, revision)

    def _current_revision(self) -> str:
        """Return the byte revision currently on disk (``""`` when missing)."""
        try:
            return hashlib.sha256(self.path.read_bytes()).hexdigest()
        except FileNotFoundError:
            return ""
        except OSError:
            return "<unreadable>"

    def _write_if_revision(self, data: dict[str, Any], revision: str) -> bool:
        """Write atomically only when the on-disk revision matches *revision*."""
        with _path_lock(self.path):
            if self._current_revision() != revision:
                return False
            payload = dict(data)
            payload["version"] = STORE_VERSION
            payload["revision"] = int(payload.get("revision") or 0) + 1
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode(
                    "utf-8"
                )
                tmp_path = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
                with tmp_path.open("wb") as handle:
                    handle.write(encoded)
                os.chmod(tmp_path, 0o600)
                os.replace(tmp_path, self.path)
            except OSError:
                logger.exception("tool approval store write failed: {}", self.path)
                return False
            return True

    def _mutate_cas(self, mutate: Callable[[dict[str, Any]], None]) -> bool:
        """Apply *mutate* with optimistic CAS, retrying on revision conflicts."""
        for _ in range(self.max_cas_retries):
            with _path_lock(self.path):
                snapshot = self._read_snapshot()
                data = copy.deepcopy(snapshot.data)
                mutate(data)
                if self._write_if_revision(data, snapshot.revision):
                    return True
            self.cas_conflicts += 1
        return False
