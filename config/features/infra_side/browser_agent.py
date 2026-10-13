"""Agent-controllable browser: a loopback Chromium driven over CDP.

The feature is OPT-IN and off by default: with ``enabled`` false no ``browser_*``
tool is registered and the ``/browser/*`` routes answer 404, so a default
install carries neither the schemas nor the routes (and no Chromium process is
ever spawned).

``headless`` follows the environment unless pinned: a host with a DISPLAY gets a
headed window (a person on that desktop can take the page over directly), a
display-less server gets headless — the panel sees the same screencast either
way. Measured on this host with the anti-throttle flags the launcher always
passes: headed ≈ 43 fps, headless ≈ 60 fps, first frame in ~40 ms.

The debug port is deliberately absent from every answer (``status()`` reports the
page count, never the port): the CDP endpoint stays inside the backend process
and is never reported to a client.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TypedDict

from config.features._env import _env_int
from config.path import SRC_DIR


class BrowserAgentConfig(TypedDict):
    """Bounds and switches for the agent's browser."""

    #: 1 = the feature exists (tools registered, routes live). Default 0 (off).
    enabled: int
    #: Env var read for that flag.
    enabled_env_var: str
    #: 1 = headless. Default: 0 when a DISPLAY exists, else 1.
    headless: int
    headless_env_var: str
    #: Explicit Chromium path; "" probes ``executable_candidates`` / PATH.
    executable: str
    executable_env_var: str
    #: Bare names probed on PATH when no explicit path is configured.
    executable_candidates: tuple[str, ...]
    #: Chrome profile directory (cookies / logins persist across restarts).
    user_data_dir: str
    #: Ceiling on live pages; the least-recently-used page beyond it is closed.
    max_pages: int
    #: Seconds a page may sit unused before the sweep closes it (0 = never).
    idle_timeout_s: float
    #: Seconds between idle sweeps.
    idle_sweep_interval_s: float
    #: Seconds allowed for the browser to publish its debug port.
    launch_timeout_s: float
    #: Seconds a navigation may take before it is reported as unfinished.
    nav_timeout_s: float
    #: Seconds any single page operation may take.
    op_timeout_s: float
    #: Refusal threshold for one screenshot (bytes).
    screenshot_max_bytes: int
    #: Screencast JPEG quality and frame bounds (one frame per painted change).
    screencast_quality: int
    screencast_max_width: int
    screencast_max_height: int
    #: Interactive elements a snapshot lists before it flags ``truncated``.
    snapshot_max_elements: int
    #: Page text a snapshot carries (a readable excerpt, not the whole DOM).
    snapshot_max_text_chars: int
    #: 1 = the ``browser_evaluate`` escape hatch registers. Default 0 (off).
    allow_evaluate: int
    allow_evaluate_env_var: str


def _build_browser_agent(env: Mapping[str, str] | None = None) -> BrowserAgentConfig:
    """Build the settings, reading the environment at call time.

    The headless default depends on DISPLAY, so it is computed from the passed
    mapping (not cached from import time): tests and the .env-free CI get the
    headless default whenever no display is present.
    """
    source = env if env is not None else os.environ
    has_display = bool(str(source.get("DISPLAY", "")).strip())
    return {
        "enabled": _env_int("SHERRY_BROWSER_AGENT_ENABLED", 0, source),
        "enabled_env_var": "SHERRY_BROWSER_AGENT_ENABLED",
        "headless": _env_int("SHERRY_BROWSER_HEADLESS", 0 if has_display else 1, source),
        "headless_env_var": "SHERRY_BROWSER_HEADLESS",
        "executable": str(source.get("SHERRY_BROWSER_EXECUTABLE", "") or "").strip(),
        "executable_env_var": "SHERRY_BROWSER_EXECUTABLE",
        "executable_candidates": (
            "chromium",
            "chromium-browser",
            "google-chrome",
            "google-chrome-stable",
        ),
        "user_data_dir": str(SRC_DIR / "data" / "browser-profile"),
        "max_pages": 8,
        "idle_timeout_s": 1800.0,
        "idle_sweep_interval_s": 60.0,
        "launch_timeout_s": 20.0,
        "nav_timeout_s": 15.0,
        "op_timeout_s": 8.0,
        "screenshot_max_bytes": 12 * 1024 * 1024,
        "screencast_quality": 70,
        "screencast_max_width": 1600,
        "screencast_max_height": 1200,
        "snapshot_max_elements": 200,
        "snapshot_max_text_chars": 4000,
        "allow_evaluate": _env_int("SHERRY_BROWSER_ALLOW_EVALUATE", 0, source),
        "allow_evaluate_env_var": "SHERRY_BROWSER_ALLOW_EVALUATE",
    }


BROWSER_AGENT: BrowserAgentConfig = _build_browser_agent()
