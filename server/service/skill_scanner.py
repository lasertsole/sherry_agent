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
import subprocess as subprocess
from pathlib import Path
from typing import Any

import importlib

from loguru import logger
from server.utils.atomic_io import atomic_write_text
from server.service.skill_scan_model import (
    ScanFinding as ScanFinding,
    ScanResult as ScanResult,
    ScanStatus as ScanStatus,
    Severity as Severity,
    _extract_scan_result as _extract_scan_result,
    _normalise_findings as _normalise_findings,
    _scan_result_from_dict,
)
from server.service.skill_scan_backend import (
    _CLI_TIMEOUT as _CLI_TIMEOUT,
    _cli_timeout as _cli_timeout,
    _llm_env,
    _run_cli,
    _run_python_api,
    _unavailable,
)
from server.service.skill_scan_policy import (
    build_caution_warnings as build_caution_warnings,
    build_reject_message as build_reject_message,
)
from server.service.skill_scan_cache import (
    _CACHE_VERSION,
    _VERSION_FINGERPRINT_CACHE,
    _directory_content_hash,
    _scanner_version_fingerprint,
)

# Sentinel used by the CLI/Python backends to signal "scanner could not run".
_UNSET = object()

#: Env flag to disable the scanner entirely (e.g. ``SKILL_SCANNER_ENABLED=0``).
_ENABLED_ENV = os.environ.get("SKILL_SCANNER_ENABLED", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}

#: Location of the verdict cache (``src/`` is git-ignored runtime data).
_CACHE_PATH: Path = Path(__file__).resolve().parents[2] / "src" / "data" / "skills_scan_cache.json"

_BACKEND_CACHE: dict[str, bool | None] = {}


def _is_available(backend: str) -> bool:
    """Probe once whether a given backend is available."""
    if not _ENABLED_ENV:
        return False
    if backend == "cli":
        return shutil.which("skillspector") is not None
    if backend == "python":
        try:
            importlib.import_module("skillspector")

            return True
        except Exception:
            return False
    return False


def _probe_backend(backend: str) -> bool:
    """Memoised availability probe for a backend."""
    cached = _BACKEND_CACHE.get(backend)
    if cached is not None:
        return bool(cached)
    available = _is_available(backend)
    _BACKEND_CACHE[backend] = available
    return available


def _resolve_backend() -> str | None:
    """Return the first available backend, or ``None`` if none is present."""
    if not _ENABLED_ENV:
        return None
    if _probe_backend("cli"):
        return "cli"
    if _probe_backend("python"):
        return "python"
    return None


def reset_backend_cache() -> None:
    """Clear the memoised backend-availability cache (used by tests)."""
    _BACKEND_CACHE.clear()


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
    atomically (:func:`server.utils.atomic_io.atomic_write_text` — tempfile in
    the same dir + ``os.replace``, mirroring :func:`pub.func.atomic_replace`);
    any error is swallowed so the cache can never slow down or break the real
    scan.
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
            "cached_at": datetime.datetime.now(datetime.UTC).isoformat(),
        }
        atomic_write_text(path, json.dumps(data, ensure_ascii=False))
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
