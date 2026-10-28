"""Cross-agent coordination: path claims and a peer findings board.

Both surfaces are advisory, opt-in and read-mostly. Claims let a writer say
"these paths are mine for now" (a peer gets one line in its tool result, never a
refusal); the findings board lets siblings share FAIL / FACT / PATCH notes
instead of rediscovering them. Nothing here ever enters the system prompt — the
content changes on every write, and putting live data in the prompt would break
the provider prefix cache.
"""

import os
from typing import TypedDict


def _flag(name: str, default: bool = False) -> bool:
    """Env-driven switch (the file may be read-only in a packaged install)."""
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


class CoordinationConfig(TypedDict):
    """Path claims and the peer findings board (see ``agent/tools/pub_base/coordination.py``)."""

    #: Path claims: ``claim_scope`` becomes available and writes consult it.
    claims_enabled: bool
    #: Peer findings board: ``share_finding`` / ``peers_findings`` become available.
    findings_enabled: bool
    #: How long a claim stays valid without being refreshed.
    claim_ttl_seconds: int
    #: Per-call cap on a claim's path list.
    max_paths_per_claim: int
    #: Per-query cap on findings returned (a caller may ask for fewer).
    max_findings_per_query: int
    #: Cap on one finding's text / one claim's note.
    max_text_chars: int


COORDINATION: CoordinationConfig = {
    # OFF by default: both surfaces add tool-face tokens and a per-project file,
    # so an operator turns them on for a fan-out that actually needs them. The
    # switches are env-overridable because a packaged install's code tree is
    # read-only (`SHERRY_COORDINATION_CLAIMS` / `SHERRY_COORDINATION_FINDINGS`
    # in the .env), and the settings are read at import like every other
    # feature registry — a change takes effect on the next backend start.
    "claims_enabled": _flag("SHERRY_COORDINATION_CLAIMS"),
    "findings_enabled": _flag("SHERRY_COORDINATION_FINDINGS"),
    # Half a work session: long enough for a slow task, short enough that a
    # crashed run's claim stops misleading peers within the hour.
    "claim_ttl_seconds": 1800,
    "max_paths_per_claim": 50,
    "max_findings_per_query": 20,
    "max_text_chars": 2000,
}
