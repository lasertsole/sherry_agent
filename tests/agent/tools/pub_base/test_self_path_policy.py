"""Sherry's own files: the read/write allow-lists and their two gates.

The hole: nothing configured means the session's project directory IS the
checkout, so the in-root test called the whole source tree (and the credentials
beside it) safe — ``patch_file`` could rewrite ``agent/core.py`` and
``read_file`` could read ``.models`` with no approval.

Covered here:

* the policy itself (write whitelist vs the broader read whitelist) — the
  acceptance matrix, including the session-scoped plans / evicted boundary;
* the file-tool gate (``resolve_tool_path``): in-root is no longer the end of
  the story, so a write to the source tree raises (or asks the operator);
* the shell screen: ``cat .models`` and ``echo x > agent/core.py`` are stopped,
  while reading source and writing temp stay allowed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import config.path as paths
from agent.middlewares.path_guard.core import _screen_command_paths
from agent.tools.pub_base.self_path_policy import classify_self_path

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


@pytest.fixture()
def repo(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway 'checkout': the self roots are this tmp tree."""
    root = tmp_path / "checkout"
    for rel in (
        "agent",
        "config",
        "src/data",
        "src/checkpoints",
        "src/images",
        "src/audio",
        "src/video",
        "src/rag/uploads",
        "temp",
        "static",
        "logs/output",
        "models/LLMs",
        "workspace/memory",
        "workspace/knowledge",
        "workspace/template",
        "workspace/sessions/s1/plans",
        "workspace/sessions/s1/evicted",
        "workspace/sessions/s2/plans",
        "mcp_servers",
        "skills/auto",
        "plugins",
    ):
        (root / rel).mkdir(parents=True, exist_ok=True)
    for name in (".env", ".models", ".network", "sherry.jsonc"):
        (root / name).write_text("{}", encoding="utf-8")
    monkeypatch.setattr(paths, "ROOT_DIR", root, raising=False)
    monkeypatch.setattr(paths, "APP_HOME", root, raising=False)
    monkeypatch.setattr(paths, "SRC_DIR", root / "src", raising=False)
    monkeypatch.setattr(paths, "TEMP_DIR", root / "temp", raising=False)
    monkeypatch.setattr(paths, "STATIC_DIR", root / "static", raising=False)
    monkeypatch.setattr(paths, "LOG_DIR", root / "logs" / "output", raising=False)
    monkeypatch.setattr(paths, "SESSIONS_DIR", root / "workspace" / "sessions", raising=False)
    monkeypatch.setattr(paths, "WORKSPACE_DIR", root / "workspace", raising=False)
    monkeypatch.setattr(paths, "MCP_SERVERS_DIR", root / "mcp_servers", raising=False)
    monkeypatch.setattr(paths, "USER_SKILLS_DIR", root / "skills" / "auto", raising=False)
    monkeypatch.setattr(paths, "CODE_INTEL_DIR", root / ".codeintel", raising=False)
    monkeypatch.setattr(paths, "MODELS_DIR", root / "models", raising=False)
    return root


def _decide(root: Path, rel: str, mode: str, sid: str = "") -> str:
    return "deny" if classify_self_path(root / rel, sid, mode) else "allow"


class TestWriteWhitelist:
    @pytest.mark.parametrize(
        "rel",
        [
            ".sherry/codeintel/index.db",
            "temp/x.txt",
            "static/a.png",
            "src/images/u.png",
            "src/audio/a.wav",
            "src/video/v.mp4",
            "src/rag/uploads/doc.txt",
        ],
    )
    def test_the_harmless_directories_are_writable(self, repo: Path, rel: str):
        assert _decide(repo, rel, "write") == "allow"

    @pytest.mark.parametrize(
        "rel",
        [
            "agent/core.py",
            "config/path.py",
            ".models",
            ".network",
            ".env",
            "sherry.jsonc",
            "src/data/auth.db",
            "src/checkpoints/sqlite.db",
            "workspace/memory/MEMORY.md",
            "workspace/AGENTS.md",
            "logs/output/a.log",
            "mcp_servers/tool.json",
            "skills/auto/x/SKILL.md",
        ],
    )
    def test_source_config_and_runtime_state_are_not(self, repo: Path, rel: str):
        assert _decide(repo, rel, "write") == "deny"

    def test_the_current_sessions_products_are_writable_but_others_are_not(self, repo: Path):
        assert _decide(repo, "workspace/sessions/s1/plans/p.md", "write", "s1") == "allow"
        assert _decide(repo, "workspace/sessions/s1/uploads/u.bin", "write", "s1") == "allow"
        assert _decide(repo, "workspace/sessions/s2/x.txt", "write", "s1") == "deny"

    def test_without_a_session_only_the_global_areas_are_writable(self, repo: Path):
        assert _decide(repo, "workspace/sessions/s1/plans/p.md", "write", "") == "deny"
        assert _decide(repo, "temp/x.txt", "write", "") == "allow"


class TestReadWhitelist:
    def test_the_source_tree_is_readable(self, repo: Path):
        # Reading source neither breaks the runtime nor leaks a secret.
        assert _decide(repo, "agent/core.py", "read") == "allow"
        assert _decide(repo, "config/path.py", "read") == "allow"
        assert _decide(repo, "models/LLMs/main_llm.py", "read") == "allow"

    @pytest.mark.parametrize(
        "rel",
        [
            ".models",
            ".network",
            ".env",
            "sherry.jsonc",
            "src/data/auth.db",
            "src/checkpoints/sqlite.db",
            "workspace/memory/USER.md",
            "workspace/memory/FACTS.md",
            "workspace/knowledge/plan.json",
            "workspace/AGENTS.md",
            "logs/output/a.log",
            "mcp_servers/tool.json",
        ],
    )
    def test_credentials_memory_and_runtime_state_are_not(self, repo: Path, rel: str):
        assert _decide(repo, rel, "read") == "deny"

    def test_the_current_sessions_plans_are_readable_but_history_is_not(self, repo: Path):
        assert _decide(repo, "workspace/sessions/s1/plans/p.md", "read", "s1") == "allow"
        # evicted/ holds discarded transcript: writable nowhere, readable never.
        assert _decide(repo, "workspace/sessions/s1/evicted/e.txt", "read", "s1") == "deny"
        assert _decide(repo, "workspace/sessions/s2/plans/p.md", "read", "s1") == "deny"

    def test_model_weights_are_denied_without_the_source_dir(self, repo: Path):
        (repo / "models" / "auxiliary_llm.gguf").write_bytes(b"x")
        assert _decide(repo, "models/auxiliary_llm.gguf", "read") == "deny"


class TestUserProjectIsUntouched:
    def test_a_path_in_another_project_never_reaches_the_policy(self, repo: Path, tmp_path):
        """The user's own workspace keeps the existing in-root / approval flow."""
        outside = tmp_path / "user-project" / "app.py"
        outside.parent.mkdir(parents=True)
        outside.write_text("print(1)", encoding="utf-8")

        assert classify_self_path(outside, "s1", "write") is None
        assert classify_self_path(outside, "s1", "read") is None


class TestFileToolGate:
    """``resolve_tool_path`` must enforce on the IN-ROOT branch too."""

    @pytest.fixture(autouse=True)
    def _operator_denies(self, monkeypatch: pytest.MonkeyPatch):
        """Stand in for the HITL card, answering 'reject'."""
        from agent.tools.pub_base import path_utils

        monkeypatch.setattr(path_utils, "_session_is_subagent", lambda _sid: False)
        monkeypatch.setattr(
            "langgraph.types.interrupt", lambda _prompt: {"decisions": [{"type": "reject"}]}
        )

    @staticmethod
    def _run(root: Path, session: str, rel: str, mode: str):
        from agent.tools.pub_base import path_utils

        return path_utils.resolve_tool_path(str(root / rel), session, f"{mode} file", mode=mode)

    def test_an_in_root_write_to_the_source_tree_is_refused(self, repo: Path, monkeypatch):
        from agent.tools.pub_base import path_utils

        monkeypatch.setattr(path_utils, "current_project_dir", lambda _sid: repo)
        _root, resolved, error = self._run(repo, "s1", "agent/core.py", "write")

        assert resolved is None
        assert error is not None and "sherry's own source" in error

    def test_an_in_root_read_of_the_config_is_refused(self, repo: Path, monkeypatch):
        from agent.tools.pub_base import path_utils

        monkeypatch.setattr(path_utils, "current_project_dir", lambda _sid: repo)
        _root, resolved, error = self._run(repo, "s1", ".models", "read")

        assert resolved is None
        assert error is not None and "configuration" in error

    def test_the_whitelisted_in_root_write_passes(self, repo: Path, monkeypatch):
        from agent.tools.pub_base import path_utils

        monkeypatch.setattr(path_utils, "current_project_dir", lambda _sid: repo)
        root, resolved, error = self._run(repo, "s1", "temp/out.txt", "write")

        assert error is None, error
        assert resolved == (repo / "temp" / "out.txt").resolve()

    def test_reading_source_in_root_passes(self, repo: Path, monkeypatch):
        from agent.tools.pub_base import path_utils

        monkeypatch.setattr(path_utils, "current_project_dir", lambda _sid: repo)
        _root, resolved, error = self._run(repo, "s1", "agent/core.py", "read")

        assert error is None, error


class TestSubagentIsolation:
    def test_a_subagent_is_refused_outright_not_asked(self, repo: Path, monkeypatch):
        """A child never gets the parent's file unlocked: no HITL, straight refusal."""
        from agent.tools.pub_base import path_utils
        from agent.tools.pub_base.self_path_policy import write_denied_reason

        monkeypatch.setattr(path_utils, "_session_is_subagent", lambda _sid: True)
        reason = write_denied_reason(repo / "agent" / "core.py", "agent:main:subagent:x")
        assert reason is not None

        with pytest.raises(path_utils.PathOutOfBoundsError, match="subagent"):
            path_utils.enforce_self_path_policy(
                repo / "agent" / "core.py", "agent:main:subagent:x", "write file", "write"
            )

    def test_the_main_agent_gets_an_approval_question(self, repo: Path, monkeypatch):
        from agent.tools.pub_base import path_utils

        asked: list[dict] = []

        def _fake_interrupt(prompt):
            asked.append(prompt)
            return {"decisions": [{"type": "approve"}]}

        monkeypatch.setattr(path_utils, "_session_is_subagent", lambda _sid: False)
        monkeypatch.setattr("langgraph.types.interrupt", _fake_interrupt)
        monkeypatch.setattr(path_utils, "current_project_dir", lambda _sid: repo)

        path_utils.enforce_self_path_policy(repo / "agent" / "core.py", "s1", "write file", "write")

        assert asked, "the main agent must be asked"
        prompt = asked[0]
        assert prompt["action_requests"][0]["name"] == "sherry_self_access"
        # One-shot only: no yolo / approve_dir convenience channel.
        assert prompt["review_configs"][0]["allowed_decisions"] == ["approve", "reject"]

    def test_a_rejection_raises(self, repo: Path, monkeypatch):
        from agent.tools.pub_base import path_utils

        monkeypatch.setattr(path_utils, "_session_is_subagent", lambda _sid: False)
        monkeypatch.setattr(
            "langgraph.types.interrupt", lambda _prompt: {"decisions": [{"type": "reject"}]}
        )
        monkeypatch.setattr(path_utils, "current_project_dir", lambda _sid: repo)

        with pytest.raises(path_utils.PathOutOfBoundsError):
            path_utils.enforce_self_path_policy(repo / ".models", "s1", "read file", "read")


class TestShellScreen:
    """PathGuard's command screen: the best-effort gate for terminal commands."""

    @pytest.mark.parametrize(
        "command",
        [
            "cat .models",
            "cat .network",
            "cat .env",
            "cat sherry.jsonc",
            "cat src/data/auth.db",
            "cat workspace/memory/USER.md",
            "cat workspace/AGENTS.md",
            "cat workspace/sessions/s2/plans/p.md",
            "cat logs/output/a.log",
            "cat mcp_servers/tool.json",
            "cat /etc/passwd",
        ],
    )
    def test_reading_sherry_secrets_is_blocked(self, repo: Path, command: str):
        assert _screen_command_paths(command, repo, "s1") is not None

    @pytest.mark.parametrize(
        "command",
        [
            "echo x > agent/core.py",
            "echo x >> agent/core.py",
            "sed -i 's/a/b/' agent/core.py",
            "tee agent/core.py",
            "echo x > .models",
            "shred src/data/auth.db",
        ],
    )
    def test_writing_sherry_files_is_blocked(self, repo: Path, command: str):
        assert _screen_command_paths(command, repo, "s1") is not None

    @pytest.mark.parametrize(
        "command",
        [
            "cat agent/core.py",  # source is readable
            "rg TODO agent/",
            "rg -n 'def ' .",
            "cat temp/x.txt",
            "cat workspace/sessions/s1/plans/p.md",
            "echo x > temp/out.txt",
            "ls",
            "git log --oneline -5",
            "uv run pytest tests/ -q",
            "python -c 'print(1)'",
            "curl https://example.com/x",
        ],
    )
    def test_the_legitimate_commands_pass(self, repo: Path, command: str):
        assert _screen_command_paths(command, repo, "s1") is None

    def test_a_dynamic_write_is_a_known_limitation(self, repo: Path):
        """Static scanning cannot see this — the file-tool gate is the real one."""
        assert _screen_command_paths("python -c \"open('agent/core.py','w')\"", repo, "s1") is None
