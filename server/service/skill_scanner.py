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

The scan is static-only by default; LLM semantic analysis is opt-in via
``SKILL_SCANNER_LLM=1``, which sends analyzer-eligible skill contents to the
configured OpenAI-compatible provider. The model provider
is wired to the same credentials the app already uses for its auxiliary LLM
(the lightweight model tier for simple supporting tasks, so the main model's
quota is untouched):

* ``SKILL_SCANNER_LLM`` (default ``0``) — opt-in LLM semantic analysis.
  Enable it only if your provider supports LangChain's ``json_schema``
  structured-output mode (see the provider limitation note below).
* ``SKILL_SCANNER_TIMEOUT`` — seconds the CLI subprocess may run before it is
  killed (default ``120``); bounds a stalled LLM attempt when the flag is on.
* ``SKILLSPECTOR_PROVIDER`` (default ``openai``) — SkillSpector provider.
* ``SKILLSPECTOR_MODEL`` — SkillSpector model override.
* ``OPENAI_BASE_URL``/``OPENAI_API_KEY`` — forwarded from the app's own
  ``AUXILIARY_LLM_API_BASE``/``AUXILIARY_LLM_API_KEY``/``AUXILIARY_LLM_API_NAME``
  (OpenAI-compatible endpoint, e.g. DeepSeek) unless already set in the
  environment.

Known limitation (structured-output providers)
----------------------------------------------
LLM semantic analysis requires a provider that supports ``json_schema`` strict
structured output. SkillSpector's LLM analyzers hardwire
``with_structured_output(self.response_schema)`` without a ``method`` argument,
which LangChain resolves to ``response_format = {"type": "json_schema", "strict":
true}``. Common Chinese OpenAI-compatible endpoints (DeepSeek, Zhipu GLM at
``open.bigmodel.cn``) ACCEPT the request but never complete it — they stall
indefinitely (>600 s observed) instead of returning a verdict — so the CLI
subprocess is killed by the scan timeout and the scan returns UNAVAILABLE.
For that reason ``SKILL_SCANNER_LLM`` defaults to OFF and is opt-in
(``SKILL_SCANNER_LLM=1``), and the CLI attempt is bounded by
``SKILL_SCANNER_TIMEOUT`` (default ``120`` s). The static rules — YARA,
embedded-prompt detection, etc. — still run and still block
``DO_NOT_INSTALL`` skills regardless; point
``SKILLSPECTOR_PROVIDER``/``OPENAI_BASE_URL``/``SKILLSPECTOR_MODEL`` at a
provider that does support ``json_schema`` structured output (e.g. OpenAI)
to opt in to semantic verdicts.

References
----------
- upstream pyproject: ``data_getter`` pin ``typer>=0.23.0,<0.24`` to dodge a
  ``click`` clash with semgrep (not enforced here — app deps are unaffected).
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
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

#: Env flag that enables LLM semantic analysis. Defaults to OFF (``0``) —
#: opt in with ``SKILL_SCANNER_LLM=1`` (see the provider limitation note in the
#: module docstring). When LLM analysis is enabled, analyzer-eligible skill
#: contents are sent to the configured OpenAI-compatible provider
#: (see :func:`_llm_env`).
_LLM_ENABLED_ENV = os.environ.get("SKILL_SCANNER_LLM", "0").strip().lower() not in {
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

#: Env var overriding the CLI subprocess timeout (seconds), read on every
#: call so operators can tune it without a code change.
_CLI_TIMEOUT_ENV = "SKILL_SCANNER_TIMEOUT"


def _cli_timeout() -> int:
    """Resolve the CLI subprocess timeout in seconds.

    Reads ``SKILL_SCANNER_TIMEOUT`` on every call: unset/empty falls back to
    the module default :data:`_CLI_TIMEOUT`; a valid integer is clamped to at
    least 1; an invalid (non-integer) value logs a warning and falls back to
    the default. Never raises.
    """
    raw = os.environ.get(_CLI_TIMEOUT_ENV, "")
    if not raw:
        return _CLI_TIMEOUT
    try:
        return max(1, int(raw))
    except ValueError:
        logger.warning(
            "Invalid {} value '{}'; falling back to default {}s",
            _CLI_TIMEOUT_ENV,
            raw,
            _CLI_TIMEOUT,
        )
        return _CLI_TIMEOUT


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

    LLM semantic analysis is opt-in (see :func:`_llm_env`); the subprocess
    runs static-only unless ``SKILL_SCANNER_LLM=1``. The subprocess is killed
    after ``SKILL_SCANNER_TIMEOUT`` seconds (default :data:`_CLI_TIMEOUT`).
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
            timeout=_cli_timeout(),
            check=False,
            env=merged_env,
        )
    except subprocess.TimeoutExpired:
        logger.warning("SkillSpector CLI timed out on {}", path)
        return _unavailable("cli")
    except OSError as exc:  # e.g. binary missing mid-run
        logger.warning("SkillSpector CLI failed to launch for {}: {}", path, exc)
        return _unavailable("cli")

    if proc.returncode == _EXIT_ERROR:
        stderr = (proc.stderr or "").strip()[-800:]
        logger.warning(
            "SkillSpector CLI reported an error for {}: {}", path, stderr or proc.stdout[:500]
        )
        return _unavailable("cli")

    # exit 0 (SAFE/CAUTION) or 1 (DO_NOT_INSTALL): parse JSON regardless of the
    # exact exit code — the recommendation lives in the body, not the rc.
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        logger.warning("SkillSpector CLI returned invalid JSON for {}", path)
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
        logger.debug("skillspector python API unavailable: {}", exc)
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
        logger.warning("SkillSpector python API failed for {}: {}", path, exc)
        return _unavailable("python")
    if isinstance(result, dict):
        return _extract_scan_result(result, backend="python")
    logger.warning("SkillSpector python API returned unexpected type for {}", path)
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


# ---------------------------------------------------------------------------
# Verdict-level, content-addressed disk cache
# ---------------------------------------------------------------------------
# Scans are expensive (the CLI may take ~2 min with LLM analysis enabled), but
# their verdict depends only on: the skill's file contents, the scanner
# version, the LLM mode and the backend. Those four inputs are folded into a
# content-addressed key (NO filesystem path: uploads land in random staging
# dirs, and identical content must share a verdict). Verdicts are stored in a
# single JSON file under the runtime data tree and re-served on warm starts.
# Every cache access is fail-open: a broken cache never blocks or slows a scan.

#: Location of the verdict cache (``src/`` is git-ignored runtime data).
_CACHE_PATH: Path = Path(__file__).resolve().parents[2] / "src" / "data" / "skills_scan_cache.json"

#: Memoised scanner-version fingerprints per backend id.
_VERSION_FINGERPRINT_CACHE: dict[str, str] = {}

#: How long (seconds) the ``--version`` probe of the CLI may run.
_VERSION_PROBE_TIMEOUT = 10

#: On-disk schema version of the cache file.
_CACHE_VERSION = 1


def _directory_content_hash(directory: Path) -> str:
    """Content-addressed hash of every file under *directory*.

    Walks the tree in sorted order and chains, per file, its POSIX-relative
    path and the sha256 of its bytes into one final sha256 hex digest, so any
    content change (rename, edit, added file) changes the digest.
    ``__pycache__`` dirs and ``*.pyc`` files are skipped (interpreter noise).
    A nonexistent directory hashes like an empty one (the loop never runs, so
    the result equals the sha256 of empty bytes) and never raises.
    """
    overall = hashlib.sha256()
    if directory.is_dir():
        for root, dirs, files in os.walk(directory):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for name in sorted(files):
                if name.endswith(".pyc"):
                    continue
                full = Path(root) / name
                rel = full.relative_to(directory).as_posix()
                try:
                    file_hash = hashlib.sha256(full.read_bytes()).hexdigest()
                except OSError:
                    file_hash = "unreadable"  # deleted/raced mid-walk; stay deterministic
                overall.update(rel.encode("utf-8") + b"\0" + file_hash.encode("ascii"))
    return overall.hexdigest()


def _cli_version_fingerprint(exe: str) -> str:
    """Version string of the CLI binary, with an exe-staleness fallback.

    Probes ``<exe> --version`` with a short timeout; when the probe fails or
    yields nothing, falls back to ``<mtime>:<size>`` of the binary so a
    replaced CLI still invalidates cached verdicts. Never raises.
    """
    try:
        proc = subprocess.run(
            [exe, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_VERSION_PROBE_TIMEOUT,
            check=False,
        )
        version = (proc.stdout or "").strip()
        first_line = version.splitlines()[0].strip() if version else ""
        if first_line:
            return first_line
    except Exception:
        pass
    try:
        st = os.stat(exe)
        return f"{st.st_mtime}:{st.st_size}"
    except OSError:
        return "unknown"


def _scanner_version_fingerprint(backend: str) -> str:
    """Memoised fingerprint of the scanner installation for *backend*.

    ``"python"`` backends are fingerprinted as ``"python-api"`` (the in-process
    API has no separate binary). ``"cli"`` is fingerprinted by its reported
    version string, falling back to binary staleness. Any probe error fails
    open to the bare backend id — the cache just becomes less precise, never
    wrong in a blocking way.
    """
    cached = _VERSION_FINGERPRINT_CACHE.get(backend)
    if cached is not None:
        return cached
    fingerprint = backend  # fail-open default
    try:
        if backend == "python":
            fingerprint = "python-api"
        elif backend == "cli":
            exe = shutil.which("skillspector")
            if exe:
                fingerprint = _cli_version_fingerprint(exe)
    except Exception as exc:
        logger.debug("SkillSpector version probe failed for {}: {}", backend, exc)
    _VERSION_FINGERPRINT_CACHE[backend] = fingerprint
    return fingerprint


def _llm_fingerprint() -> str:
    """Fingerprint of the LLM-analysis mode (see :func:`_llm_env`)."""
    env = _llm_env()
    if not env:
        return "llm-off"
    return hashlib.sha256(json.dumps(env, sort_keys=True).encode("utf-8")).hexdigest()


def _scan_cache_key(path: Path, backend: str) -> str:
    """Content-addressed cache key for a scan of *path* with *backend*.

    The key is a sha256 over ``"v1"`` + the directory-content hash + the
    scanner-version fingerprint + the LLM-mode fingerprint + the backend id.
    A file input is lifted to its parent dir, mirroring the file→parent
    normalisation in :func:`scan_skill`. No filesystem path is included:
    identical content scanned from different staging dirs shares a verdict.
    """
    directory = path.parent if path.is_file() else path
    digest = hashlib.sha256()
    digest.update(b"v1")
    digest.update(_directory_content_hash(directory).encode("ascii"))
    digest.update(_scanner_version_fingerprint(backend).encode("utf-8"))
    digest.update(_llm_fingerprint().encode("utf-8"))
    digest.update(backend.encode("utf-8"))
    return digest.hexdigest()


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


def _lookup_scan_cache(key: str) -> ScanResult | None:
    """Return the cached verdict for *key*, or ``None`` on any miss/error.

    Fail-open by design: a missing file, corrupt JSON, unknown key or bad
    entry shape is a plain cache miss (at most one warning), never an error
    surfaced to the scan.
    """
    try:
        data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("SkillSpector scan cache unreadable (ignoring): {}", exc)
        return None
    if not isinstance(data, dict) or data.get("version") != _CACHE_VERSION:
        return None
    entries = data.get("entries")
    if not isinstance(entries, dict):
        return None
    entry = entries.get(key)
    if not isinstance(entry, dict) or not isinstance(entry.get("result"), dict):
        return None
    try:
        return _scan_result_from_dict(entry["result"])
    except (ValueError, TypeError) as exc:
        logger.warning("SkillSpector scan cache entry malformed (ignoring): {}", exc)
        return None


def _store_scan_cache(key: str, result: ScanResult) -> None:
    """Persist a SCANNED verdict under *key* (best-effort, fail-open).

    Only ``SCANNED`` results are stored — this deliberately includes
    DO_NOT_INSTALL verdicts (they carry SCANNED status; the rc==1-forced
    hard-gate verdict is post-processed before storage and MUST survive warm
    starts). UNAVAILABLE results are never stored. The file is written
    atomically (tempfile in the same dir + ``os.replace``, mirroring
    :func:`pub_func.atomic_replace`); any error is swallowed so the cache can
    never slow down or break the real scan.
    """
    if result.status is not ScanStatus.SCANNED:
        return
    try:
        path = _CACHE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {"version": _CACHE_VERSION, "entries": {}}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("entries"), dict):
                    data = loaded
            except (OSError, ValueError):
                pass  # corrupt existing cache: start a fresh file
        data["version"] = _CACHE_VERSION
        entries = data.setdefault("entries", {})
        entries[key] = {
            "result": result.to_dict(),
            "cached_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except Exception as exc:
        logger.warning("SkillSpector scan cache write failed (ignoring): {}", exc)


def reset_scan_cache() -> None:
    """Delete the verdict cache file and clear the version-fingerprint memo.

    Best-effort (used by tests and callers that want a forced re-scan); never
    raises.
    """
    _VERSION_FINGERPRINT_CACHE.clear()
    try:
        _CACHE_PATH.unlink(missing_ok=True)
    except OSError as exc:
        logger.debug("SkillSpector scan cache cleanup failed: {}", exc)


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
        logger.warning("Skill scan requested for missing path: {}", p)
        return _unavailable()

    backend = _resolve_backend()
    if backend is None:
        logger.debug(
            "SkillScanner unavailable (neither CLI nor python API present); skipping scan for {}",
            p,
        )
        return _unavailable()

    key = _scan_cache_key(p, backend)
    cached = _lookup_scan_cache(key)
    if cached is not None:
        logger.debug("SkillSpector scan cache hit for {} (key {})", p, key)
        return cached

    def _finish(result: ScanResult) -> ScanResult:
        # Both exit paths (direct CLI verdict and python-API fallback) store
        # under the resolved-backend key. UNAVAILABLE results are ignored by
        # _store_scan_cache; a fallback verdict stored under the CLI key is
        # accepted by design (verdicts are content-valid, and the backend id
        # in the key keeps cli/python verdicts from cross-pollinating).
        _store_scan_cache(key, result)
        return result

    if backend == "cli":
        result = _run_cli(p)
        # Fall through to the in-process API when the CLI could not produce a
        # verdict (e.g. missing binary, error rc, timeout, invalid JSON). A
        # real verdict (SAFE / CAUTION / DO_NOT_INSTALL) is returned as-is.
        if not result.is_unavailable:
            return _finish(result)
        logger.info(
            "SkillScanner CLI could not scan {}; falling back to python API",
            p,
        )
        return _finish(_run_python_api(p))
    return _finish(_run_python_api(p))


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
