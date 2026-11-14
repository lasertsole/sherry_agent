"""Terminal children carry ``SHERRY_SESSION_ID``.

The artifact store is session-scoped (``sherry-artifact://<session>/<name>``),
so a scripted skill running under the terminal tool must know which session it
belongs to. The variable is injected into the SAME scrubbed environment the
child already gets, and it carries no secret, so the scrub contract is
unchanged.
"""

from __future__ import annotations

import asyncio
import os

import pytest

import agent.tools.pub_base.sandbox_guard as sandbox_guard
import agent.tools.terminal.core as terminal
import config
import pub.func.message.eviction as eviction_module
from agent.tools.pub_base.sandbox import SandboxPolicy
from agent.tools.terminal import SafeShellTool

pytestmark = [
    pytest.mark.filterwarnings("ignore:The shell tool has no safeguards"),
]

_SID = "sess-terminal-env"


@pytest.fixture
def sessions_root(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setattr(config, "SESSIONS_DIR", root)
    monkeypatch.setattr(eviction_module, "SESSIONS_DIR", root)
    return root


@pytest.fixture
def tool(monkeypatch):
    """The deterministic bare-spawn path (AUTO + no backend)."""
    monkeypatch.setattr(sandbox_guard, "read_policy", lambda: SandboxPolicy.AUTO)
    monkeypatch.setattr(terminal, "read_policy", lambda: SandboxPolicy.AUTO)
    monkeypatch.setattr(terminal, "get_backend", lambda policy: None)
    return SafeShellTool()


def test_child_env_tags_the_session_and_keeps_the_scrub(monkeypatch):
    monkeypatch.setattr(
        terminal,
        "scrub_env",
        lambda base_env=None: {"PATH": os.environ.get("PATH", ""), "TTI_API_KEY": "secret"},
    )

    env = terminal._child_env(_SID)

    assert env["SHERRY_SESSION_ID"] == _SID
    # The helper adds exactly one key; the scrub result is otherwise untouched.
    assert env["TTI_API_KEY"] == "secret"


def test_child_env_without_a_session_adds_nothing(monkeypatch):
    monkeypatch.setattr(terminal, "scrub_env", lambda base_env=None: {"PATH": "/bin"})

    assert "SHERRY_SESSION_ID" not in terminal._child_env("")


def test_sync_child_reads_the_session_env(tool):
    out = tool._run(["printenv SHERRY_SESSION_ID"], session_id=_SID)

    assert out.strip() == _SID


def test_async_child_reads_the_session_env(tool):
    out = asyncio.run(tool._arun(["printenv SHERRY_SESSION_ID"], session_id=_SID))

    assert out.strip() == _SID


def test_the_real_scrub_keeps_the_session_but_drops_secrets(monkeypatch, tool):
    monkeypatch.setenv("TTI_API_KEY", "sk-super-secret")

    out = tool._run(
        ['printf \'%s|%s\' "$SHERRY_SESSION_ID" "${TTI_API_KEY:-unset}"'],
        session_id=_SID,
    )

    assert out.strip() == f"{_SID}|unset"
