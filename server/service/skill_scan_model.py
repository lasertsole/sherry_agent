"""SkillSpector scan data model + result normalisation.

Owns the scan vocabulary (:class:`ScanStatus`, :class:`Severity`,
:class:`ScanFinding`, :class:`ScanResult`) and the coercion of raw
SkillSpector payloads (CLI JSON or Python API dicts, cache entries) into
normalised :class:`ScanResult` values.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import StrEnum
from typing import Any


class ScanStatus(StrEnum):
    """Top-level outcome of a skill scan."""

    #: Scanner ran and returned a verdict (SAFE / CAUTION / DO_NOT_INSTALL).
    SCANNED = "scanned"
    #: Scanner could not run (not installed, subprocess error, timeout, ...).
    UNAVAILABLE = "unavailable"


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class ScanFinding:
    """A single pattern/finding reported by SkillSpector."""

    title: str
    category: str = ""
    severity: Severity | str = Severity.LOW
    description: str = ""
    path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ScanResult:
    """Normalised result of a SkillSpector scan on one skill directory."""

    #: One of "scanned" (a real verdict) or "unavailable" (scanner did not run).
    status: ScanStatus
    #: 0-100 aggregated risk score. ``None`` when status is UNAVAILABLE.
    risk_score: int | None = None
    #: "SAFE" | "CAUTION" | "DO_NOT_INSTALL". ``None`` when unavailable.
    risk_recommendation: str | None = None
    risk_severity: Severity | str | None = None
    findings: list[ScanFinding] = field(default_factory=list)

    #: Backend that produced this result ("cli", "python", or None).
    backend: str | None = None

    @property
    def is_unavailable(self) -> bool:
        return self.status is ScanStatus.UNAVAILABLE

    @property
    def is_do_not_install(self) -> bool:
        return (
            self.status is ScanStatus.SCANNED
            and str(self.risk_recommendation or "").upper() == "DO_NOT_INSTALL"
        )

    @property
    def is_caution(self) -> bool:
        return (
            self.status is ScanStatus.SCANNED
            and str(self.risk_recommendation or "").upper() == "CAUTION"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "risk_score": self.risk_score,
            "risk_recommendation": self.risk_recommendation,
            "risk_severity": (
                self.risk_severity.value
                if isinstance(self.risk_severity, Severity)
                else self.risk_severity
            ),
            "backend": self.backend,
            "findings": [f.to_dict() for f in self.findings],
        }


def _severity(value: Any) -> Severity | str:
    """Coerce a raw severity into a :class:`Severity`, tolerating bad input."""
    if not value:
        return Severity.LOW
    text = str(value).lower()
    for sev in Severity:
        if sev.value in text:
            return sev
    return text


def _normalise_findings(raw: Any) -> list[ScanFinding]:
    """Normalise SkillSpector findings (CLI JSON or Python API) into a list."""
    findings: list[ScanFinding] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            findings.append(
                ScanFinding(
                    title=str(item.get("title") or item.get("rule") or item.get("id") or "Finding"),
                    category=str(item.get("category") or ""),
                    severity=_severity(item.get("severity")),
                    description=str(item.get("description") or item.get("message") or ""),
                    path=str(item.get("file") or item.get("path") or item.get("location") or ""),
                )
            )
    elif isinstance(raw, dict):
        # Some versions nest findings under a key (e.g. "findings" / "results").
        nested = raw.get("findings") or raw.get("results") or raw.get("issues")
        if nested is not None:
            return _normalise_findings(nested)
    return findings


def _extract_scan_result(
    payload: dict[str, Any] | None,
    *,
    backend: str,
) -> ScanResult:
    """Build a normalised :class:`ScanResult` from a SkillSpector dict/JSON."""
    if not isinstance(payload, dict):
        return ScanResult(
            status=ScanStatus.SCANNED,
            risk_score=0,
            risk_recommendation="UNKNOWN",
            backend=backend,
        )
    score = payload.get("risk_score")
    try:
        score = int(score) if score is not None else 0
    except (TypeError, ValueError):
        score = 0
    rec = str(payload.get("risk_recommendation") or "UNKNOWN").upper()
    severity = payload.get("risk_severity")
    return ScanResult(
        status=ScanStatus.SCANNED,
        risk_score=score,
        risk_recommendation=rec,
        risk_severity=_severity(severity),
        findings=_normalise_findings(payload.get("filtered_findings") or payload.get("findings")),
        backend=backend,
    )


def _scan_result_from_dict(data: Any) -> ScanResult:
    """Rebuild a :class:`ScanResult` from its :meth:`ScanResult.to_dict` form.

    ``to_dict`` serialises severity enums to their string values, so severities
    are re-coerced through :func:`_severity` on the way back. Raises
    ``ValueError`` on any malformed shape (the caller fails open).
    """
    if not isinstance(data, dict):
        raise ValueError("cache entry 'result' is not a dict")
    status_raw = data.get("status")
    if status_raw == ScanStatus.SCANNED.value:
        status = ScanStatus.SCANNED
    elif status_raw == ScanStatus.UNAVAILABLE.value:
        status = ScanStatus.UNAVAILABLE
    else:
        raise ValueError(f"unknown scan status in cache: {status_raw!r}")
    findings: list[ScanFinding] = []
    for item in data.get("findings") or []:
        if not isinstance(item, dict):
            continue
        findings.append(
            ScanFinding(
                title=str(item.get("title") or ""),
                category=str(item.get("category") or ""),
                severity=_severity(item.get("severity")),
                description=str(item.get("description") or ""),
                path=str(item.get("path") or ""),
            )
        )
    severity = data.get("risk_severity")
    return ScanResult(
        status=status,
        risk_score=data.get("risk_score"),
        risk_recommendation=data.get("risk_recommendation"),
        risk_severity=_severity(severity) if severity else severity,
        findings=findings,
        backend=data.get("backend"),
    )
