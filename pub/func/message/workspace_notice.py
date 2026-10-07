"""The workspace-notice contract: which injected messages are change notices.

A notice is a message the agent layer INSERTS to explain that the workspace
moved under the model — the working directory was switched or unbound, or the
git branch/HEAD changed while the session was idle. It is a real message in the
transcript (the model must read it), not a system directive that vanishes.

The shape is deliberately tiny and dependency-free, because the readers span the
whole project: the producers live in ``runtime/session`` and the consuming
middleware in ``agent/middlewares/workspace_notice``, while ``pub``'s message
pipeline (turn splitting, summarization labels) must recognise a notice without
importing either. Every producer stamps:

* ``origin`` — which change it announces (``project_dir`` / ``git_head``); the
  chat maps it to a card label and the persistence layer keeps it on the row, so
  a notice renders as a neutral system card rather than a user bubble or an
  assistant answer;
* ``internal`` — the project-wide marker for "injected, not user content" (the
  same flag TaskIntent's directive check and the eviction helpers read);
* ``provenance`` — ``workspace_notice``, so a notice stays identifiable even
  after its origin set grows (a consumer can match on provenance alone).

A notice is NOT a user turn: it is spliced immediately before the human message
it explains, so the human message keeps its place LAST and every "the last
human message is the request" reader is unaffected. The consumers that scan
*all* human messages — turn splitting, the HITL headless-turn check — use
:func:`is_workspace_notice` to skip it.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "GIT_HEAD_NOTICE_ORIGIN",
    "PROJECT_DIR_NOTICE_ORIGIN",
    "WORKSPACE_NOTICE_PROVENANCE",
    "is_workspace_notice",
    "workspace_notice_metadata",
]

#: A working-directory switch / unbinding (``runtime.session.project_dir``).
PROJECT_DIR_NOTICE_ORIGIN = "project_dir"
#: A git branch or HEAD change (``runtime.session.git_head``).
GIT_HEAD_NOTICE_ORIGIN = "git_head"
#: Provenance marker shared by every workspace notice.
WORKSPACE_NOTICE_PROVENANCE = "workspace_notice"

#: Origins that are workspace notices; a new notice kind joins this set.
NOTICE_ORIGINS: frozenset[str] = frozenset({PROJECT_DIR_NOTICE_ORIGIN, GIT_HEAD_NOTICE_ORIGIN})


def workspace_notice_metadata(origin: str) -> dict[str, Any]:
    """The metadata a workspace notice carries (frozen shape).

    :param origin: Which change the notice announces — one of ``NOTICE_ORIGINS``.
    :returns: A fresh dict (callers may hand it straight to a message).
    """
    return {
        "origin": origin,
        "internal": True,
        "provenance": WORKSPACE_NOTICE_PROVENANCE,
    }


def is_workspace_notice(message: Any) -> bool:
    """True when *message* is an injected workspace-change notice.

    Matches on the provenance marker first (forward-compatible with new notice
    kinds), then on the known origins — so notices written before the provenance
    key existed are still recognised.

    :param message: Any message-like object (a ``metadata`` dict is all it needs).
    :returns: ``True`` for a notice, ``False`` for a user message, a model answer
        or any other injected carrier (the subagent-completion carrier carries
        ``internal`` but its own provenance, so it is deliberately NOT a notice).
    """
    meta = getattr(message, "metadata", None) or {}
    if not isinstance(meta, dict) or meta.get("internal") is not True:
        return False
    if str(meta.get("provenance") or "").strip() == WORKSPACE_NOTICE_PROVENANCE:
        return True
    return str(meta.get("origin") or "").strip() in NOTICE_ORIGINS
