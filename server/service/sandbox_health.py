"""Boot-time sandbox health check + the status payload behind GET /sandbox/status.

The OS sandbox is the only write barrier ``terminal`` / ``python_repl`` /
``execute_code`` have (they never call ``resolve_external_path``). Whether it is
actually available is therefore a security-relevant fact that has to be visible
instead of discoverable: this module resolves the effective policy and backend
once at boot, logs the answer loudly when it is bad, and exposes the same dict
through a read-only route.

``SANDBOX_POLICY=required`` + no usable backend is the fail-closed case: the
tools refuse to execute, so the process is still safe, but a deployment that
meant to sandbox is now silently unable to run commands. ``SANDBOX_STRICT_BOOT=1``
turns that into a startup failure for operators who prefer it that way.
"""

from __future__ import annotations

import os
from typing import Any

from loguru import logger

__all__ = ["sandbox_status", "check_sandbox_at_boot", "STRICT_BOOT_ENV"]

#: Opt-in: refuse to start when REQUIRED is unmet (default: warn only).
STRICT_BOOT_ENV = "SANDBOX_STRICT_BOOT"

#: How to make a backend usable (Linux first, then macOS).
_FIX_HINTS = (
    "(Linux) install bubblewrap and allow user namespaces — "
    "`sudo sysctl kernel.apparmor_restrict_unprivileged_userns=0` on Ubuntu 24.04+, or "
    "`sudo chmod u+s $(command -v bwrap)`; (macOS) ensure /usr/bin/sandbox-exec exists"
)


def sandbox_status() -> dict[str, Any]:
    """The process's sandbox state: ``{policy, backend, available, strict_boot, detail}``."""
    from agent.tools.pub_base.sandbox import SandboxPolicy, get_backend, read_policy

    policy: SandboxPolicy = read_policy()
    backend_name: str | None = None
    available = False
    detail = ""
    if policy is SandboxPolicy.OFF:
        detail = "sandboxing is disabled (SANDBOX_POLICY=off)"
    else:
        try:
            backend = get_backend(policy)
        except RuntimeError as exc:  # REQUIRED + unavailable
            backend = None
            detail = str(exc)
        if backend is not None:
            available = True
            backend_name = type(backend).__name__
    return {
        "policy": policy.value,
        "backend": backend_name,
        "available": available,
        "strict_boot": os.getenv(STRICT_BOOT_ENV, "").strip() in ("1", "true", "yes", "on"),
        "detail": detail,
    }


def check_sandbox_at_boot() -> dict[str, Any]:
    """Log the effective sandbox state at boot; raise only when asked to be strict.

    Always logs the policy + availability at INFO so a log reader can tell what
    the process will do, escalates an unusable REQUIRED to ERROR with fix hints,
    and warns on the AUTO degrade (the bare-run path). With ``SANDBOX_STRICT_BOOT=1``
    the REQUIRED-unmet case raises instead of warning, for deployments that would
    rather not start than run without a sandbox.
    """
    status = sandbox_status()
    logger.info(
        "sandbox: policy={} backend={} available={}",
        status["policy"],
        status["backend"] or "-",
        status["available"],
    )
    if status["available"] or status["policy"] == "off":
        return status
    if status["policy"] == "required":
        message = (
            "SANDBOX_POLICY=required but no sandbox backend is available, so terminal / "
            f"python_repl / execute_code will REFUSE to execute. To fix: {_FIX_HINTS}. "
            "To run without a sandbox instead, set SANDBOX_POLICY=auto."
        )
        if status["strict_boot"]:
            raise RuntimeError(f"{message} (SANDBOX_STRICT_BOOT=1 refuses to start)")
        logger.error(message)
    else:
        logger.warning(
            "sandbox: SANDBOX_POLICY=auto with no available backend — terminal / "
            "python_repl / execute_code run UNSANDBOXED on every call (no HITL path "
            "covers those writes). To fix: {}.",
            _FIX_HINTS,
        )
    return status
