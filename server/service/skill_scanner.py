"""SkillSpector security scanner for agent skills.

This module provides a thin, graceful-degradation wrapper around
`NVIDIA/SkillSpector <https://github.com/NVIDIA/SkillSpector>`_ so that skills
uploaded or shipped with the project can be scanned for malicious patterns,
prompt injection, data exfiltration risks and other supply-chain hazards
*before* they are installed/activated.

Integration order (first working backend wins):

1. **CLI backend** — the ``skillspector`` console command (installed in an
   isolated tool environment via ``uv tool install``). Preferred because it
   keeps SkillSpector's heavy dependency tree (``boto3``, ``yara-python``,
   ``langchain-*``, ``openai``, ...) out of the application's own virtualenv.
2. **Python-API backend** — ``from skillspector import graph`` (library form).
   Used when the library is importable in-process.

If neither backend is available, ``scan_skill`` returns an "unavailable"
sentinel instead of raising. Callers decide policy on that sentinel (the
upload endpoint allows + logs a warning; a config flag can flip this to
fail-closed).

By default the scan runs with LLM semantic analysis enabled (``use_llm=True``
/ without ``--no-llm``), which sends analyzer-eligible skill contents to the
configured OpenAI-compatible provider. Disable it with the
``SKILL_SCANNER_LLM=0`` env flag to get a static-only scan. The model provider
is wired to the same credentials the app already uses for its auxiliary LLM
(the lightweight model tier for simple supporting tasks, so the main model's
quota is untouched):

* ``SKILL_SCANNER_LLM`` (default ``1``) — enable/disable LLM semantic analysis.
  Leave ``0`` unless your provider supports LangChain's ``json_schema``
  structured-output mode (see the DeepSeek note below).
* ``SKILLSPECTOR_PROVIDER`` (default ``openai``) — SkillSpector provider.
* ``SKILLSPECTOR_MODEL`` — SkillSpector model override.
* ``OPENAI_BASE_URL``/``OPENAI_API_KEY`` — forwarded from the app's own
  ``AUXILIARY_LLM_API_BASE``/``AUXILIARY_LLM_API_KEY``/``AUXILIARY_LLM_API_NAME``
  (OpenAI-compatible endpoint, e.g. DeepSeek) unless already set in the
  environment.

Known limitation (DeepSeek)
---------------------------
The app's default ``AUXILIARY_LLM_*`` provider is DeepSeek. SkillSpector's LLM
analyzers hardwire ``with_structured_output(self.response_schema)`` without a
``method`` argument, which LangChain resolves to ``response_format =
{"type": "json_schema", "strict": true}`` — a format DeepSeek rejects with
``This response_format type is unavailable now`` (DeepSeek only supports plain
``json_object``). There is no SkillSpector env knob to switch the structured-
output method, so the CLI subprocess falls back to a static-only scan for
every DeepSeek-backed upload (the static rules — YARA, embedded-prompt
detection, etc. — still run and still block ``DO_NOT_INSTALL`` skills). The
``SKILL_SCANNER_LLM=1`` flag therefore costs a rejected API round-trip per scan
and adds no semantic verdict. Either leave the flag ``0`` for DeepSeek, or point
``SKILLSPECTOR_PROVIDER``/``OPENAI_BASE_URL``/``SKILLSPECTOR_MODEL`` at a
provider that does support ``json_schema`` structured output (e.g. OpenAI).

References
----------
- upstream pyproject: ``data_getter`` pin ``typer>=0.23.0,<0.24`` to dodge a
  ``click`` clash with semgrep (not enforced here — app deps are unaffected).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any

from loguru import logger

# Sentinel used by the CLI/Python backends to signal "scanner could not run".
_UNSET = object()

#: Env flag to disable the scanner entirely (e.g. ``SKILL_SCANNER_ENABLED=0``).
_ENABLED_ENV = os.environ.get("SKILL_SCANNER_ENABLED", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}

#: Env flag that enables LLM semantic analysis. Defaults to ON (``1``); set to
#: ``0``/``false``/... for a static-only scan (no external network call).
#: When LLM analysis is enabled, analyzer-eligible skill contents are sent to
#: the configured OpenAI-compatible provider (see :func:`_llm_env`).
_LLM_ENABLED_ENV = os.environ.get("SKILL_SCANNER_LLM", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}


def _llm_env() -> dict[str, str]:
    """Env vars forwarded to the SkillSpector subprocess for LLM analysis.

    The CLI reads ``SKILLSPECTOR_PROVIDER`` / ``SKILLSPECTOR_MODEL`` /
    ``OPENAI_BASE_URL`` / ``OPENAI_API_KEY`` from its own subprocess env. To
    avoid duplicating credentials, these are derived from the app's
    ``AUXILIARY_LLM_*`` settings (the lightweight model tier used for simple
    auxiliary tasks) unless the caller pre-set them explicitly. The auxiliary
    LLM is a deliberate choice: skill scanning is a low-stakes supporting task,
    so it should not consume the main model's quota.

    Returns an empty dict when LLM analysis is disabled so the subprocess runs
    static-only (never leaks env vars or sends contents to a provider).
    """
    if not _LLM_ENABLED_ENV:
        return {}
    env = {
        "SKILLSPECTOR_PROVIDER": "openai",
        "OPENAI_BASE_URL": os.environ.get(
            "AUXILIARY_LLM_API_BASE", os.environ.get("OPENAI_BASE_URL", "")
        ),
        "OPENAI_API_KEY": os.environ.get(
            "AUXILIARY_LLM_API_KEY", os.environ.get("OPENAI_API_KEY", "")
        ),
    }
    model = os.environ.get("SKILLSPECTOR_MODEL") or os.environ.get("AUXILIARY_LLM_API_NAME") or ""
    if model:
        env["SKILLSPECTOR_MODEL"] = model
    # Keep any explicit overrides the operator set (e.g. OPENAI_BASE_URL for a
    # local Ollama endpoint) instead of always clobbering them with the app's.
    for key in ("SKILLSPECTOR_PROVIDER", "OPENAI_BASE_URL", "OPENAI_API_KEY"):
        explicit = os.environ.get(key)
        if explicit:
            env[key] = explicit
    if not env.get("OPENAI_BASE_URL") or not env.get("OPENAI_API_KEY"):
        logger.warning(
            "SkillSpector LLM analysis requested but AUXILIARY_LLM_API_BASE/KEY "
            + "are missing; static-only scan will run for the subprocess"
        )
        return {}
    return env


#: CLI flag that turns the scanner into a hard gate when the scanner is running
#: but the skill's verdict is `DO_NOT_INSTALL` (see ``scan_skill``).
#: (Kept as a module constant so tests + callers can reason about the policy.)
FAIL_CLOSED_ON_DO_NOT_INSTALL = True

#: Expected exit codes from the ``skillspector scan`` command.
#: 0 = SAFE or CAUTION, 1 = DO_NOT_INSTALL, 2 = error (scanner failed to run).
_EXIT_OK = 0
_EXIT_DO_NOT_INSTALL = 1
_EXIT_ERROR = 2

#: How long (seconds) the CLI subprocess may run before it is killed.
_CLI_TIMEOUT = 120


class ScanStatus(str, Enum):
    """Top-level outcome of a skill scan."""

    #: Scanner ran and returned a verdict (SAFE / CAUTION / DO_NOT_INSTALL).
    SCANNED = "scanned"
    #: Scanner could not run (not installed, subprocess error, timeout, ...).
    UNAVAILABLE = "unavailable"


class Severity(str, Enum):
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


def _is_available(backend: str) -> bool:
    """Probe once whether a given backend is available."""
    if not _ENABLED_ENV:
        return False
    if backend == "cli":
        return shutil.which("skillspector") is not None
    if backend == "python":
        try:
            import skillspector  # noqa: F401  (guarded import is the probe)

            return True
        except Exception:
            return False
    return False


_BACKEND_CACHE: dict[str, bool | None] = {}


def _probe_backend(backend: str) -> bool:
    """Memoised availability probe for a backend."""
    cached = _BACKEND_CACHE.get(backend)
    if cached is not None:
        return bool(cached)
    available = _is_available(backend)
    _BACKEND_CACHE[backend] = available
    return available


def _run_cli(path: Path) -> ScanResult:
    """Scan using the ``skillspector`` CLI (json output).

    LLM semantic analysis is enabled by default (see :func:`_llm_env`); pass
    ``--no-llm`` only when the ``SKILL_SCANNER_LLM`` flag is off.
    """
    exe = shutil.which("skillspector")
    if not exe:
        return _unavailable("cli")
    cmd = [exe, "scan", str(path), "--format", "json"]
    extra_env = _llm_env()
    if not extra_env:
        cmd.append("--no-llm")
    merged_env = {**os.environ, **extra_env}
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_CLI_TIMEOUT,
            check=False,
            env=merged_env,
        )
    except subprocess.TimeoutExpired:
        logger.warning("SkillSpector CLI timed out on %s", path)
        return _unavailable("cli")
    except OSError as exc:  # e.g. binary missing mid-run
        logger.warning("SkillSpector CLI failed to launch for %s: %s", path, exc)
        return _unavailable("cli")

    if proc.returncode == _EXIT_ERROR:
        stderr = (proc.stderr or "").strip()[-800:]
        logger.warning(
            "SkillSpector CLI reported an error for %s: %s", path, stderr or proc.stdout[:500]
        )
        return _unavailable("cli")

    # exit 0 (SAFE/CAUTION) or 1 (DO_NOT_INSTALL): parse JSON regardless of the
    # exact exit code — the recommendation lives in the body, not the rc.
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        logger.warning("SkillSpector CLI returned invalid JSON for %s", path)
        return _unavailable("cli")
    result = _extract_scan_result(payload, backend="cli")
    if proc.returncode == _EXIT_DO_NOT_INSTALL:
        result.risk_recommendation = "DO_NOT_INSTALL"
    return result


def _run_python_api(path: Path) -> ScanResult:
    """Scan using the in-process ``skillspector.graph`` API (static-only)."""
    try:
        from skillspector import graph  # type: ignore[import-not-found]
    except Exception as exc:
        logger.debug("skillspector python API unavailable: %s", exc)
        return _unavailable("python")
    try:
        result = graph.invoke(
            {
                "input_path": str(path),
                "output_format": "json",
                "use_llm": False,
            }
        )
    except Exception as exc:
        logger.warning("SkillSpector python API failed for %s: %s", path, exc)
        return _unavailable("python")
    if isinstance(result, dict):
        return _extract_scan_result(result, backend="python")
    logger.warning("SkillSpector python API returned unexpected type for %s", path)
    return _unavailable("python")


def _unavailable(backend: str | None = None) -> ScanResult:
    return ScanResult(status=ScanStatus.UNAVAILABLE, backend=backend)


def _resolve_backend() -> str | None:
    """Return the first available backend, or ``None`` if none is present."""
    if not _ENABLED_ENV:
        return None
    if _probe_backend("cli"):
        return "cli"
    if _probe_backend("python"):
        return "python"
    return None


def scan_skill(path: str | os.PathLike[str]) -> ScanResult:
    """Run a static security scan against a skill directory.

    Preferred backend is the ``skillspector`` CLI; the in-process Python API is
    used as a fallback. If neither is available the returned
    :class:`ScanResult` has ``status == UNAVAILABLE`` and the policy is left to
    the caller.

    Parameters
    ----------
    path
        A path to either a ``SKILL.md`` file or the directory containing it.
        SkillSpector accepts both; a file path is converted to its parent dir
        so the whole skill (not just the single SKILL.md) is scanned.
    """
    p = Path(path)
    if p.is_file():
        p = p.parent
    if not p.exists():
        logger.warning("Skill scan requested for missing path: %s", p)
        return _unavailable()

    backend = _resolve_backend()
    if backend == "cli":
        result = _run_cli(p)
        # Fall through to the in-process API when the CLI could not produce a
        # verdict (e.g. missing binary, error rc, timeout, invalid JSON). A
        # real verdict (SAFE / CAUTION / DO_NOT_INSTALL) is returned as-is.
        if not result.is_unavailable:
            return result
        logger.info(
            "SkillScanner CLI could not scan %s; falling back to python API",
            p,
        )
        return _run_python_api(p)
    if backend == "python":
        return _run_python_api(p)
    logger.debug(
        "SkillScanner unavailable (neither CLI nor python API present); skipping scan for %s",
        p,
    )
    return _unavailable()


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


# Allow an explicit re-probe for tests/clients that install the tool at runtime.
def reset_backend_cache() -> None:
    """Clear the memoised backend-availability cache (used by tests)."""
    _BACKEND_CACHE.clear()
