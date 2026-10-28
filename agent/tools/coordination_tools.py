"""Cross-agent coordination tools: path claims and the peer findings board.

Both are ADVISORY and opt-in (``COORDINATION``). A claim never refuses a write —
it only adds one line to a peer's tool result — and the findings board is a
read-only lookup plus a small writer, never an injection channel: nothing here
is rendered into the system prompt (live data there would break the provider
prefix cache).

The tools live in their own group so the preset UI can switch them per session;
the builders return ``[]`` when their switch is off, which keeps the default
tool face byte-identical.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated, Literal

from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt.tool_node import InjectedState
from loguru import logger
from pydantic import BaseModel, Field

from agent.tools.pub_base import session_workspace_root
from agent.tools.pub_base.coordination import (
    active_claims,
    claim_paths,
    record_finding,
    release_claims,
    search_findings,
)
from agent.tools.pub_base.path_utils import PathOutOfBoundsError, resolve_workspace_path
from config.features import COORDINATION

__all__ = [
    "build_coordination_tools",
    "claim_notice_for",
    "share_merge_finding",
]

_INTERNAL_PREFIX = "agent:main:"

#: Advisory-notice dedupe: ``(session, path) -> monotonic``.
#:
#: The design intent is "one notice per session per turn", but the AGENT layer
#: cannot see a turn id — it lives in the server's turn scope, and agent must
#: not import server. A per-(session, path) time window delivers the same
#: noise control (four writes to one file produce one line) without inventing a
#: coupling: the notice is advisory, so an extra window across a turn boundary
#: is harmless.
_CLAIM_NOTICE_SEEN: dict[tuple[str, str], float] = {}
_CLAIM_NOTICE_WINDOW_S = 600.0


def _short_session(session_id: str) -> str:
    """A short, human-readable handle for a session id."""
    text = session_id.removeprefix(_INTERNAL_PREFIX)
    return text if len(text) <= 12 else f"…{text[-12:]}"


def _project_root(session_id: str) -> Path | None:
    try:
        root = session_workspace_root(session_id)
    except Exception:  # noqa: BLE001 - coordination is always best-effort
        return None
    return root


def _normalized_paths(session_id: str, root: Path, paths: list[str]) -> tuple[list[str], int]:
    """Resolve and relativize paths; ``(kept, skipped)``.

    A path that escapes the project is SKIPPED (counted, never an error): a
    claim outside the workspace would name a file no peer can write through
    this session anyway.
    """
    kept: list[str] = []
    skipped = 0
    for raw in paths:
        candidate = str(raw or "").strip()
        if not candidate:
            continue
        try:
            resolved = resolve_workspace_path(candidate, root)
        except (PathOutOfBoundsError, OSError):
            skipped += 1
            continue
        try:
            kept.append(str(resolved.relative_to(root)).replace("\\", "/"))
        except ValueError:
            skipped += 1
    return kept, skipped


class ClaimScopeInput(BaseModel):
    """Arguments of ``claim_scope``."""

    paths: list[str] = Field(
        default_factory=list,
        description="Project-relative paths you are about to write (at most "
        f"{COORDINATION['max_paths_per_claim']}).",
    )
    note: str = Field(default="", description="Short reason, e.g. 'refactoring the parser'.")
    release: bool = Field(
        default=False,
        description="Release EVERY claim this session holds in the project instead of claiming.",
    )
    session_id: Annotated[str, InjectedState("session_id")] = ""


class ShareFindingInput(BaseModel):
    """Arguments of ``share_finding``."""

    kind: Literal["FAIL", "FACT", "PATCH"] = Field(
        description="FAIL = tried and it does not work; FACT = confirmed behaviour; "
        "PATCH = this file's interface changed.",
    )
    text: str = Field(description="One or two sentences another agent can act on.")
    files: list[str] = Field(
        default_factory=list, description="Optional project-relative paths this concerns."
    )
    session_id: Annotated[str, InjectedState("session_id")] = ""


class PeersFindingsInput(BaseModel):
    """Arguments of ``peers_findings``."""

    query: str = Field(
        default="",
        description="Words to match (all must appear; matches text or file paths). Empty = newest.",
    )
    kind: str = Field(default="", description="Optional filter: FAIL / FACT / PATCH.")
    limit: int = Field(default=10, description="How many entries to return (capped).")
    session_id: Annotated[str, InjectedState("session_id")] = ""


@tool("claim_scope", args_schema=ClaimScopeInput)
def claim_scope(
    paths: list[str] | None = None,
    note: str = "",
    release: bool = False,
    session_id: str = "",
) -> str:
    """Declare the paths you are about to write, or release your claims.

    Advisory only: a peer writing the same path sees a one-line note in its tool
    result, and nothing is ever refused. Claims expire after
    ``COORDINATION["claim_ttl_seconds"]``. Use ``release=true`` when your task is
    done so peers stop seeing the claim.
    """
    if not COORDINATION["claims_enabled"]:
        return "claim_scope: cross-agent claims are disabled in this deployment."
    session = (session_id or "").strip()
    root = _project_root(session) if session else None
    if root is None:
        return "claim_scope: no project directory is bound to this session."

    if release:
        removed = release_claims(root, session)
        return f"claim_scope: released {removed} claim(s)."

    raw_paths = [str(item) for item in (paths or []) if str(item or "").strip()]
    if not raw_paths:
        return "claim_scope: pass at least one path (or release=true)."
    cap = max(1, int(COORDINATION["max_paths_per_claim"]))
    truncated = max(0, len(raw_paths) - cap)
    kept, skipped = _normalized_paths(session, root, raw_paths[:cap])
    if not kept:
        return (
            "claim_scope: none of the given paths resolve inside the project; nothing was claimed."
        )
    note_text = (note or "").strip()[: int(COORDINATION["max_text_chars"])]
    claimed = claim_paths(
        root,
        session,
        kept,
        note=note_text,
        ttl_seconds=int(COORDINATION["claim_ttl_seconds"]),
    )
    lines = [f"claim_scope: claimed {claimed} path(s)"]
    if skipped:
        lines.append(f" ({skipped} skipped: outside the project)")
    if truncated:
        lines.append(f" ({truncated} dropped: over the {cap}-path cap)")
    lines.append(".")

    others = active_claims(root, kept, exclude_session=session, limit=10)
    if others:
        lines.append(" Peers already claim:")
        for claim in others:
            suffix = f" — {claim.note}" if claim.note else ""
            lines.append(f"\n- {claim.path} by {_short_session(claim.session_id)}{suffix}")
    return "".join(lines)


@tool("share_finding", args_schema=ShareFindingInput)
def share_finding(
    kind: str = "FACT",
    text: str = "",
    files: list[str] | None = None,
    session_id: str = "",
) -> str:
    """Publish a short FAIL / FACT / PATCH finding your peers can query.

    FAIL = "tried this, it does not work"; FACT = "confirmed behaviour";
    PATCH = "this file's interface changed". Keep it to one or two actionable
    sentences: peers find it through ``peers_findings``.
    """
    if not COORDINATION["findings_enabled"]:
        return "share_finding: the peer findings board is disabled in this deployment."
    session = (session_id or "").strip()
    root = _project_root(session) if session else None
    if root is None:
        return "share_finding: no project directory is bound to this session."
    normalized = str(kind or "").strip().upper()
    if normalized not in {"FAIL", "FACT", "PATCH"}:
        return "share_finding: kind must be FAIL, FACT or PATCH."
    body = (text or "").strip()
    if not body:
        return "share_finding: text is required."
    kept, _skipped = _normalized_paths(session, root, [str(item) for item in (files or [])])
    row_id = record_finding(
        root,
        session,
        normalized,
        body,
        files=kept,
        max_text_chars=int(COORDINATION["max_text_chars"]),
    )
    if row_id is None:
        return "share_finding: the findings board is unavailable for this project."
    return f"share_finding: recorded {normalized} finding #{row_id}."


@tool("peers_findings", args_schema=PeersFindingsInput)
def peers_findings(
    query: str = "",
    kind: str = "",
    limit: int = 10,
    session_id: str = "",
) -> str:
    """Read FAIL / FACT / PATCH findings your peers published for this project.

    Every whitespace-separated word must match (text or file path, case
    insensitive). Use it before rediscovering a dead end or re-deriving a fact.
    """
    if not COORDINATION["findings_enabled"]:
        return "peers_findings: the peer findings board is disabled in this deployment."
    session = (session_id or "").strip()
    root = _project_root(session) if session else None
    if root is None:
        return "peers_findings: no project directory is bound to this session."
    capped = max(1, min(int(limit), int(COORDINATION["max_findings_per_query"])))
    kind_filter = str(kind or "").strip().upper()
    try:
        rows = search_findings(root, query=query, kind=kind_filter, limit=capped)
    except Exception as exc:  # noqa: BLE001 - a lookup never breaks a turn
        logger.debug("peers_findings failed (fail-open): {}", exc)
        return "peers_findings: the findings board is unavailable right now."
    if not rows:
        return "peers_findings: no matching findings."
    lines = [f"peers_findings: {len(rows)} matching finding(s):"]
    for row in rows:
        files = f" [{', '.join(row.files)}]" if row.files else ""
        lines.append(f"\n- {row.kind} by {_short_session(row.session_id)}{files}: {row.text}")
    return "".join(lines)


def claim_notice_for(session_id: str, project_root: Path, path: Path) -> str | None:
    """The one-line advisory for a write that overlaps another session's claim.

    Returns ``None`` when the feature is off, nothing overlaps, the same session
    already got a notice this turn, or anything at all goes wrong — a notice is
    never a precondition for a write.
    """
    if not COORDINATION["claims_enabled"]:
        return None
    session = (session_id or "").strip()
    if not session:
        return None
    try:
        relative = str(path.resolve().relative_to(project_root.resolve())).replace("\\", "/")
    except (ValueError, OSError):
        return None
    try:
        others = active_claims(project_root, [relative], exclude_session=session, limit=3)
    except Exception as exc:  # noqa: BLE001 - advisory only
        logger.debug("claim notice skipped: {}", exc)
        return None
    if not others:
        return None
    # One notice per (session, path) inside a short window: a fan-out writing the
    # same file repeatedly stays quiet, a different file still gets its line.
    now = time.monotonic()
    key = (session, relative)
    last = _CLAIM_NOTICE_SEEN.get(key)
    if last is not None and now - last < _CLAIM_NOTICE_WINDOW_S:
        return None
    _CLAIM_NOTICE_SEEN[key] = now
    if len(_CLAIM_NOTICE_SEEN) > 512:  # bounded: prune the expired half
        cutoff = now - _CLAIM_NOTICE_WINDOW_S
        for seen, stamp in list(_CLAIM_NOTICE_SEEN.items()):
            if stamp < cutoff:
                _CLAIM_NOTICE_SEEN.pop(seen, None)
    claim = others[0]
    suffix = f" — {claim.note}" if claim.note else ""
    return (
        f"{relative} is also claimed by {_short_session(claim.session_id)}{suffix}; "
        "coordinate before overwriting (advisory only)"
    )


def share_merge_finding(project_root: Path, session_id: str, files: list[str], text: str) -> None:
    """Record a PATCH finding after a merge (opt-in, fail-open)."""
    if not COORDINATION["findings_enabled"]:
        return
    try:
        record_finding(
            project_root,
            session_id,
            "PATCH",
            text,
            files=files,
            max_text_chars=int(COORDINATION["max_text_chars"]),
        )
    except Exception as exc:  # noqa: BLE001 - a merge never fails on its notice
        logger.debug("merge finding skipped: {}", exc)


def build_coordination_tools() -> list[BaseTool]:
    """The enabled coordination tools (none when both switches are off)."""
    tools: list[BaseTool] = []
    if COORDINATION["claims_enabled"]:
        tools.append(claim_scope)
    if COORDINATION["findings_enabled"]:
        tools.extend([share_finding, peers_findings])
    for built in tools:
        built.handle_tool_error = True
        built.metadata = {"idempotent": False, "side_effect_scope": "workspace"}
    return tools
