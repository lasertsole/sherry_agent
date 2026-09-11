"""SkillSpector scanner backends: CLI subprocess + in-process Python API.

Each backend turns a skill directory into a normalised :class:`ScanResult`,
degrading to the UNAVAILABLE sentinel instead of raising when the scanner
cannot run. LLM semantic analysis is opt-in via ``SKILL_SCANNER_LLM=1`` and
bounded by ``SKILL_SCANNER_TIMEOUT``.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from config.features import SKILL_SCANNER
from loguru import logger

from server.service.skill_scan_model import _extract_scan_result, ScanResult, ScanStatus

#: Env flag that enables LLM semantic analysis. Defaults to OFF (``0``) —
#: opt in with ``SKILL_SCANNER_LLM=1`` (see the provider limitation note in the
#: ``skill_scanner`` module docstring). When LLM analysis is enabled,
#: analyzer-eligible skill contents are sent to the configured
#: OpenAI-compatible provider (see :func:`_llm_env`).
_LLM_ENABLED_ENV = os.environ.get("SKILL_SCANNER_LLM", "0").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}

#: Expected exit codes from the ``skillspector scan`` command.
#: 0 = SAFE or CAUTION, 1 = DO_NOT_INSTALL, 2 = error (scanner failed to run).
_EXIT_OK = SKILL_SCANNER["exit_ok"]
_EXIT_DO_NOT_INSTALL = SKILL_SCANNER["exit_do_not_install"]
_EXIT_ERROR = SKILL_SCANNER["exit_error"]

#: How long (seconds) the CLI subprocess may run before it is killed.
_CLI_TIMEOUT = SKILL_SCANNER["cli_timeout"]

#: Env var overriding the CLI subprocess timeout (seconds), read on every
#: call so operators can tune it without a code change.
_CLI_TIMEOUT_ENV = SKILL_SCANNER["cli_timeout_env_var"]


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


def _unavailable(backend: str | None = None) -> ScanResult:
    return ScanResult(status=ScanStatus.UNAVAILABLE, backend=backend)


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
        graph = importlib.import_module("skillspector.graph")
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
