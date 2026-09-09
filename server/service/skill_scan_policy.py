"""SkillSpector scan policy: translate verdicts into caller-facing decisions.

The upload endpoint (and ``clawhub_runner``) decide allow/block/advise purely
from these helpers — never from raw :class:`ScanResult` fields.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from server.service.skill_scan_model import ScanResult

#: CLI flag that turns the scanner into a hard gate when the scanner is running
#: but the skill's verdict is `DO_NOT_INSTALL` (see ``scan_skill``).
#: (Kept as a module constant so tests + callers can reason about the policy.)
FAIL_CLOSED_ON_DO_NOT_INSTALL = True


def build_caution_warnings(result: ScanResult) -> list[str]:
    """Build user-facing advisory warnings for a CAUTION verdict.

    Returns an empty list when the result is not a CAUTION (SAFE, UNAVAILABLE,
    or DO_NOT_INSTALL) — in the DO_NOT_INSTALL case the upload is blocked by
    :func:`build_reject_message` instead. Each returned string is a concise,
    human-readable reason the skill was flagged, so the client can surface it
    without blocking the upload.
    """
    if not result.is_caution:
        return []
    finding_titles = [f.title for f in result.findings if f.title]
    score = result.risk_score if result.risk_score is not None else 0
    prefix = f"Skill flagged by security scanner (CAUTION, risk score {score})."
    if not finding_titles:
        return [prefix]
    return [prefix + f" Flags: {', '.join(dict.fromkeys(finding_titles))}."]


def build_reject_message(result: ScanResult) -> str | None:
    """Translate a :class:`ScanResult` into an upload-blocking message.

    Returns ``None`` when the upload may proceed, or the human-readable reason
    to surface in a 400 response when it must be blocked.

    Policy
    ------
    * ``DO_NOT_INSTALL`` (scanner available) -> reject (fail-closed).
    * ``CAUTION`` / ``SAFE`` -> allow (return ``None``).
    * ``UNAVAILABLE`` (scanner not installed / errored) -> allow; this is a
      dev-convenience gate, and the app must keep working when the scanner is
      absent.
    """
    if result.is_unavailable:
        logger.warning("Skill security scanner unavailable; allowing upload without scan verdict")
        return None
    if result.is_do_not_install:
        score = result.risk_score if result.risk_score is not None else 0
        findings = result.findings or []
        detail = findings[0].title if findings else "no detailed findings"
        return (
            f"Skill rejected by security scanner: recommendation "
            f"DO_NOT_INSTALL (risk score {score}). Reason: {detail}."
        )
    # SAFE / CAUTION -> allow.
    return None
