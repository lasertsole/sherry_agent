"""TDD tests for terminal.py sandbox-hardening (Task 6 of .omo/plans/sandbox-hardening.md).

Covers (plan lines 557-568):
- schema propagation: ``SafeShellInput`` subclass exposes ``sandbox`` through
  ``tool_call_schema`` (class-level access needs the ClassVar descriptor, cf.
  langchain_core 1.4.7 where ``tool_call_schema`` is a bare instance property),
- env scrub wiring: ``scrub_env()`` result reaches BOTH sync and async spawns,
- sandbox wrap structure: backend present -> list-exec via ``backend.wrap``;
  backend None -> BYTE-IDENTICAL Windows fallback (str join + shell=True),
- Windows fallback byte-identity (SANDBOX_PLAN.md:546): args[0] is a plain str,
  shell=True, only ``env=`` added,
- dangerous-command regex: old 4 blacklist entries still rejected, the joined
  malicious list ``["echo ok", "rm -rf /"]`` (Task 5 defect) now BLOCKED,
- guards: subagent scope + sandbox=False denied; SANDBOX_POLICY=required +
  sandbox=False denied; REQUIRED + unavailable backend -> ToolException.

Spawn-point discipline (learnings.md Task 5): every BLOCKED-assertion test
monkeypatches the spawn point (``subprocess.Popen`` /
``asyncio.create_subprocess_*``) to a sentinel so a broken guard can NEVER
reach a real subprocess.
"""

from __future__ import annotations

import asyncio
import subprocess
from typing import Any

import pytest
from loguru import logger
from pydantic import BaseModel

import agent.tools.terminal as terminal
from agent.tools.pub_base.sandbox import SandboxPolicy
from agent.tools.terminal import SafeShellTool, build_terminal_tool
from config import ROOT_DIR

pytestmark = [
    # The upstream ShellInput validator warns on every validation; keep output clean.
    pytest.mark.filterwarnings("ignore:The shell tool has no safeguards"),
]

_SCRUBBED_ENV = {"SCRUBBED_PATH": "/usr/bin", "SCRUBBED_HOME": "/home/x"}


def _scrub_stub(base_env: dict[str, str] | None = None) -> dict[str, str]:
    return dict(_SCRUBBED_ENV)


# ─────────────────────────────────────────────────────────────────────────────
# Spawn-point fakes (never touch a real subprocess)
# ─────────────────────────────────────────────────────────────────────────────
class _FakeProc:
    """Sync Popen stand-in: communicate() -> (b"ok", b""), returncode 0."""

    def __init__(self, out: bytes = b"ok", returncode: int = 0):
        self._out = out
        self.returncode = returncode

    def communicate(self, timeout=None):
        return (self._out, b"")


class _FakeAsyncProc:
    """Async subprocess stand-in: communicate() coroutine, returncode 0."""

    def __init__(self, out: bytes = b"ok", returncode: int = 0):
        self._out = out
        self.returncode = returncode

    async def communicate(self):
        return (self._out, None)

    def kill(self):
        pass


def _no_spawn_popen(record: list[dict[str, Any]]):
    """Sentinel Popen: records the call, never spawns. Unknown calls fail loudly."""

    def _popen(*args: Any, **kwargs: Any) -> _FakeProc:
        record.append({"args": args, "kwargs": kwargs})
        return _FakeProc()

    return _popen


def _sentinel_popen(record: list[dict[str, Any]]):
    """Popen that MUST NOT be reached in blocked tests."""
    def _popen(*args: Any, **kwargs: Any) -> _FakeProc:
        record.append({"args": args, "kwargs": kwargs})
        raise AssertionError(
            f"blocked input must never spawn a subprocess; got {args!r} {kwargs!r}"
        )

    return _popen


class _FakeBackend:
    """SandboxBackend stand-in recording wrap() calls."""

    def __init__(self, wrapper: str = "sandbox-exec"):
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self._wrapper = wrapper

    def probe(self) -> bool:
        return True

    def wrap(
        self, cmd: list[str], env: dict[str, str]
    ) -> tuple[list[str], dict[str, str]]:
        self.calls.append((list(cmd), dict(env)))
        return [self._wrapper, *cmd], {**env, "WRAPPED": "1"}


@pytest.fixture
def log_capture():
    """Capture loguru warning lines (loguru does not propagate to stdlib caplog)."""
    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    yield messages
    logger.remove(sink_id)


@pytest.fixture(autouse=True)
def _no_real_backend(monkeypatch):
    """Default the policy knobs so tests never probe real backends.

    read_policy -> AUTO is NOT applied globally (each test picks); but the
    real get_backend on this Windows machine returns None anyway. Tests that
    need deterministic backends patch ``terminal.get_backend`` explicitly.
    """


def _tool(caller_scope: str | None = None) -> SafeShellTool:
    tool = build_terminal_tool()
    if caller_scope is not None:
        assert tool.metadata is not None
        tool.metadata["caller_scope"] = caller_scope
    return tool


# ─────────────────────────────────────────────────────────────────────────────
# 1. Schema propagation (plan line 559: SafeShellInput subclass; line 605:
#    class-level tool_call_schema access)
# ─────────────────────────────────────────────────────────────────────────────
class TestSchemaPropagation:
    def test_class_level_tool_call_schema_contains_sandbox(self):
        # Plan line 605 acceptance one-liner: CLASS-level access must work
        # (langchain_core 1.4.7 tool_call_schema is a bare instance @property
        # -> needs the ClassVar descriptor, mirrored from python_repl.py).
        props = SafeShellTool.tool_call_schema.model_json_schema()["properties"]
        assert "sandbox" in props, "sandbox field must reach the tool-call schema"
        assert props["sandbox"].get("default") is True
        assert props["sandbox"].get("description"), "description must be non-empty"

    def test_args_schema_is_safe_shell_input_subclass(self):
        # ShellTool has EXPLICIT args_schema=ShellInput -> signature-only changes
        # never propagate; the subclass must be wired as args_schema.
        # NOTE: pydantic v2 has no class-level field attribute access, so read
        # the schema from an instance.
        from langchain_community.tools.shell.tool import ShellInput

        tool = _tool()
        schema = tool.args_schema
        assert isinstance(schema, type) and issubclass(schema, BaseModel)
        assert schema.__name__ == "SafeShellInput"
        assert issubclass(schema, ShellInput), "must subclass the upstream ShellInput"
        # inherited field intact (HITL core.py:277-279 reads args.get("commands"))
        assert "commands" in schema.model_fields
        assert "sandbox" in schema.model_fields
        assert schema.model_fields["sandbox"].default is True

    def test_instance_tool_call_schema_matches_class(self):
        tool = _tool()
        props = tool.tool_call_schema.model_json_schema()["properties"]
        assert "sandbox" in props
        assert props["sandbox"].get("default") is True

    def test_sandbox_flag_flows_to_run_via_invoke(self, monkeypatch):
        captured: list[tuple[Any, dict[str, Any]]] = []

        def fake_run(self: Any, commands: Any, **kwargs: Any) -> str:
            captured.append((commands, kwargs))
            return "ok"

        monkeypatch.setattr(SafeShellTool, "_run", fake_run)
        tool = _tool()
        tool.invoke({"commands": "ls -la", "sandbox": False})
        assert captured, "_run must be called"
        commands, kwargs = captured[0]
        assert commands == ["ls -la"]
        assert kwargs.get("sandbox") is False, (
            "sandbox=False must flow from the schema into _run by field name"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 2. Env scrub wiring (plan line 564/566: scrub_env() -> BOTH spawns)
# ─────────────────────────────────────────────────────────────────────────────
class TestEnvScrubWiring:
    def test_sync_fallback_spawns_with_scrubbed_env(self, monkeypatch):
        record: list[dict[str, Any]] = []
        monkeypatch.setattr(subprocess, "Popen", _no_spawn_popen(record))
        monkeypatch.setattr(terminal, "scrub_env", _scrub_stub)
        monkeypatch.setattr(terminal, "read_policy", lambda: SandboxPolicy.OFF)
        tool = _tool()

        tool._run(["echo ok"])

        assert len(record) == 1
        assert record[0]["kwargs"]["env"] == _SCRUBBED_ENV, (
            "sync fallback spawn must receive the scrubbed env"
        )

    def test_async_fallback_spawns_with_scrubbed_env(self, monkeypatch):
        record: list[dict[str, Any]] = []

        async def fake_shell(*args: Any, **kwargs: Any) -> _FakeAsyncProc:
            record.append({"args": args, "kwargs": kwargs})
            return _FakeAsyncProc()

        monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_shell)
        monkeypatch.setattr(terminal, "scrub_env", _scrub_stub)
        monkeypatch.setattr(terminal, "read_policy", lambda: SandboxPolicy.OFF)
        tool = _tool()

        asyncio.run(tool._arun(["echo ok"]))

        assert len(record) == 1
        assert record[0]["kwargs"]["env"] == _SCRUBBED_ENV, (
            "async fallback spawn must receive the scrubbed env"
        )

    def test_scrub_applied_even_when_sandbox_false(self, monkeypatch):
        record: list[dict[str, Any]] = []
        monkeypatch.setattr(subprocess, "Popen", _no_spawn_popen(record))
        monkeypatch.setattr(terminal, "scrub_env", _scrub_stub)
        monkeypatch.setattr(terminal, "read_policy", lambda: SandboxPolicy.AUTO)
        tool = _tool()  # main scope -> sandbox=False allowed today

        tool._run(["echo ok"], sandbox=False)

        assert len(record) == 1
        assert record[0]["kwargs"]["env"] == _SCRUBBED_ENV, (
            "env scrub is unconditional, even for sandbox=False calls"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Guards: scope + policy (plan lines 561-562)
# ─────────────────────────────────────────────────────────────────────────────
class TestSandboxBypassGuards:
    def test_subagent_scope_sandbox_false_denied(self, monkeypatch):
        record: list[dict[str, Any]] = []
        monkeypatch.setattr(subprocess, "Popen", _sentinel_popen(record))
        monkeypatch.setattr(terminal, "scrub_env", _scrub_stub)
        monkeypatch.setattr(terminal, "read_policy", lambda: SandboxPolicy.AUTO)
        tool = _tool(caller_scope="subagent")

        from langchain_core.tools import ToolException

        with pytest.raises(ToolException) as excinfo:
            tool._run(["echo ok"], sandbox=False)
        msg = str(excinfo.value)
        assert "沙箱绕过仅限主会话人工审批" in msg
        assert "subagent" in msg, "message must include the current scope"
        assert record == [], "denied call must not spawn"

    def test_required_policy_sandbox_false_denied(self, monkeypatch):
        record: list[dict[str, Any]] = []
        monkeypatch.setattr(subprocess, "Popen", _sentinel_popen(record))
        monkeypatch.setattr(terminal, "scrub_env", _scrub_stub)
        monkeypatch.setattr(terminal, "read_policy", lambda: SandboxPolicy.REQUIRED)
        tool = _tool()  # main scope: scope guard passes, policy guard must deny

        from langchain_core.tools import ToolException

        with pytest.raises(ToolException) as excinfo:
            tool._run(["echo ok"], sandbox=False)
        assert "required" in str(excinfo.value)
        assert record == [], "denied call must not spawn"

    def test_main_scope_sandbox_false_allowed_today(self, monkeypatch):
        # Matrix: main + sandbox=False is NOT denied at the tool layer (the
        # human-approval interrupt is Task 8's job — no interrupt() here).
        record: list[dict[str, Any]] = []
        monkeypatch.setattr(subprocess, "Popen", _no_spawn_popen(record))
        monkeypatch.setattr(terminal, "scrub_env", _scrub_stub)
        monkeypatch.setattr(terminal, "read_policy", lambda: SandboxPolicy.AUTO)
        tool = _tool()

        out = tool._run(["echo ok"], sandbox=False)
        assert out == "ok"
        assert len(record) == 1

    def test_sandbox_true_never_gated_by_scope(self, monkeypatch):
        # sandbox=True is never gated (guards only fire on sandbox=False).
        record: list[dict[str, Any]] = []
        monkeypatch.setattr(subprocess, "Popen", _no_spawn_popen(record))
        monkeypatch.setattr(terminal, "scrub_env", _scrub_stub)
        monkeypatch.setattr(terminal, "read_policy", lambda: SandboxPolicy.OFF)
        tool = _tool(caller_scope="subagent")

        out = tool._run(["echo ok"], sandbox=True)
        assert out == "ok"
        assert len(record) == 1

    def test_async_subagent_scope_sandbox_false_denied(self, monkeypatch):
        record: list[dict[str, Any]] = []

        async def fake_shell(*args: Any, **kwargs: Any) -> _FakeAsyncProc:
            record.append({"args": args, "kwargs": kwargs})
            return _FakeAsyncProc()

        monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_shell)
        monkeypatch.setattr(terminal, "scrub_env", _scrub_stub)
        monkeypatch.setattr(terminal, "read_policy", lambda: SandboxPolicy.AUTO)
        tool = _tool(caller_scope="background")

        from langchain_core.tools import ToolException

        with pytest.raises(ToolException) as excinfo:
            asyncio.run(tool._arun(["echo ok"], sandbox=False))
        assert "沙箱绕过仅限主会话人工审批" in str(excinfo.value)
        assert "background" in str(excinfo.value)
        assert record == [], "denied async call must not spawn"


# ─────────────────────────────────────────────────────────────────────────────
# 4. Dangerous-command regex (plan line 563; old blacklist entries + joined
#    malicious list that was the Task 5 defect)
# ─────────────────────────────────────────────────────────────────────────────
_BLOCKED_COMMANDS = [
    "rm -rf /",
    ["rm -rf /"],
    "mkfs.ext4 /dev/sda",
    "shutdown -h now",
    "reboot",
    ["echo ok", "rm -rf /"],      # joined malicious — Task 5 defect, now blocked
    "echo hi && rm -rf /",        # raw str chain
    "ls; rm -rf /",               # semicolon chain
    ["ls -la", "shutdown -h now"],
    "RM -RF /",                   # case-insensitive
    "rm -r /tmp/x",
]

_ALLOWED_COMMANDS = ["ls -la", "git status", "echo hello", ["echo ok", "git status"]]


class TestDangerousCommandRegex:
    @pytest.mark.parametrize("commands", _BLOCKED_COMMANDS, ids=lambda c: repr(c))
    def test_blocked_commands_raise_tool_exception(self, commands, monkeypatch):
        record: list[dict[str, Any]] = []
        monkeypatch.setattr(subprocess, "Popen", _sentinel_popen(record))
        monkeypatch.setattr(terminal, "scrub_env", _scrub_stub)
        monkeypatch.setattr(terminal, "read_policy", lambda: SandboxPolicy.OFF)
        tool = _tool()

        from langchain_core.tools import ToolException

        with pytest.raises(ToolException, match="Blocked: unsafe command."):
            tool._run(commands)  # type: ignore[arg-type]
        assert record == [], "blocked input must never reach a spawn point"

    @pytest.mark.parametrize("commands", _ALLOWED_COMMANDS, ids=lambda c: repr(c))
    def test_benign_commands_pass_regex(self, commands, monkeypatch):
        record: list[dict[str, Any]] = []
        monkeypatch.setattr(subprocess, "Popen", _no_spawn_popen(record))
        monkeypatch.setattr(terminal, "scrub_env", _scrub_stub)
        monkeypatch.setattr(terminal, "read_policy", lambda: SandboxPolicy.OFF)
        tool = _tool()

        tool._run(commands)  # type: ignore[arg-type]
        assert len(record) == 1, "benign input must reach the spawn point exactly once"

    def test_joined_malicious_list_blocked_the_task5_defect(self, monkeypatch):
        # The specific defect from Task 5 characterization: element-exact
        # blacklist let ["echo ok", "rm -rf /"] EXECUTE. The regex over the
        # joined string must block it.
        record: list[dict[str, Any]] = []
        monkeypatch.setattr(subprocess, "Popen", _sentinel_popen(record))
        monkeypatch.setattr(terminal, "scrub_env", _scrub_stub)
        monkeypatch.setattr(terminal, "read_policy", lambda: SandboxPolicy.OFF)
        tool = _tool()

        from langchain_core.tools import ToolException

        with pytest.raises(ToolException, match="Blocked: unsafe command."):
            tool._run(["echo ok", "rm -rf /"])
        assert record == []

    def test_regex_pattern_is_ignorecase_and_joined(self):
        pattern = terminal.DANGEROUS_COMMAND_REGEX
        assert pattern.flags & 2, "re.IGNORECASE expected"  # re.IGNORECASE == 2
        assert pattern.search("echo ok && rm -rf /")
        assert not pattern.search("git status")


# ─────────────────────────────────────────────────────────────────────────────
# 5. Windows fallback byte-identity (plan line 565: 逐字保持现有路径, only env=
#    added; SANDBOX_PLAN.md:546)
# ─────────────────────────────────────────────────────────────────────────────
class TestWindowsFallback:
    def test_sync_fallback_string_shell_true_and_env(self, monkeypatch):
        # get_backend -> None (Windows reality): the existing path must be
        # byte-identical — args[0] is a plain str " && " join, shell=True —
        # with ONLY env= added on top.
        record: list[dict[str, Any]] = []
        monkeypatch.setattr(subprocess, "Popen", _no_spawn_popen(record))
        monkeypatch.setattr(terminal, "scrub_env", _scrub_stub)
        monkeypatch.setattr(terminal, "read_policy", lambda: SandboxPolicy.AUTO)
        monkeypatch.setattr(terminal, "get_backend", lambda policy: None)
        tool = _tool()

        out = tool._run(["echo ok", "git status"])

        assert out == "ok"
        assert len(record) == 1
        popen_args = record[0]["args"]
        popen_kwargs = record[0]["kwargs"]
        assert isinstance(popen_args[0], str), "fallback must pass a str, not a list"
        assert popen_args[0] == "echo ok && git status", (
            "fallback command string must stay byte-identical to pre-Task-6"
        )
        assert popen_kwargs.get("shell") is True, "fallback must keep shell=True"
        assert popen_kwargs.get("env") == _SCRUBBED_ENV
        assert popen_kwargs.get("cwd") == str(ROOT_DIR)

    def test_async_fallback_shell_string_and_env(self, monkeypatch):
        record: list[dict[str, Any]] = []

        async def fake_shell(*args: Any, **kwargs: Any) -> _FakeAsyncProc:
            record.append({"args": args, "kwargs": kwargs})
            return _FakeAsyncProc()

        monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_shell)
        monkeypatch.setattr(terminal, "scrub_env", _scrub_stub)
        monkeypatch.setattr(terminal, "read_policy", lambda: SandboxPolicy.AUTO)
        monkeypatch.setattr(terminal, "get_backend", lambda policy: None)
        tool = _tool()

        asyncio.run(tool._arun(["echo ok", "git status"]))

        assert len(record) == 1
        args, kwargs = record[0]["args"], record[0]["kwargs"]
        assert args[0] == "echo ok && git status", "async fallback string byte-identical"
        assert isinstance(args[0], str)
        assert kwargs.get("env") == _SCRUBBED_ENV

    def test_policy_off_skips_get_backend_entirely(self, monkeypatch):
        record: list[dict[str, Any]] = []
        monkeypatch.setattr(subprocess, "Popen", _no_spawn_popen(record))
        monkeypatch.setattr(terminal, "scrub_env", _scrub_stub)

        def _boom(policy):
            raise AssertionError("get_backend must not be called when policy is OFF")

        monkeypatch.setattr(terminal, "read_policy", lambda: SandboxPolicy.OFF)
        monkeypatch.setattr(terminal, "get_backend", _boom)
        tool = _tool()

        tool._run(["echo ok"])
        assert len(record) == 1

    def test_auto_degrade_emits_exactly_one_warning(self, monkeypatch, log_capture):
        record: list[dict[str, Any]] = []
        monkeypatch.setattr(subprocess, "Popen", _no_spawn_popen(record))
        monkeypatch.setattr(terminal, "scrub_env", _scrub_stub)
        monkeypatch.setattr(terminal, "read_policy", lambda: SandboxPolicy.AUTO)
        monkeypatch.setattr(terminal, "get_backend", lambda policy: None)
        tool = _tool()

        tool._run(["echo ok"])

        warnings = [m for m in log_capture if "degrad" in m.lower()]
        assert len(warnings) == 1, (
            f"exactly ONE degrade warning expected, got {warnings!r}"
        )
        assert len(record) == 1, "degrade still executes via the fallback path"


# ─────────────────────────────────────────────────────────────────────────────
# 6. Sandbox wrap structure (plan line 565: backend present -> list exec)
# ─────────────────────────────────────────────────────────────────────────────
class TestSandboxWrap:
    def test_sync_backend_uses_list_exec_via_wrap(self, monkeypatch):
        record: list[dict[str, Any]] = []
        monkeypatch.setattr(subprocess, "Popen", _no_spawn_popen(record))
        monkeypatch.setattr(terminal, "scrub_env", _scrub_stub)
        monkeypatch.setattr(terminal, "read_policy", lambda: SandboxPolicy.AUTO)
        backend = _FakeBackend()
        monkeypatch.setattr(terminal, "get_backend", lambda policy: backend)
        tool = _tool()

        out = tool._run(["echo ok", "git status"])

        assert out == "ok"
        # wrap received the command in LIST form (exec, no shell string)
        assert len(backend.calls) == 1
        wrapped_input, wrapped_input_env = backend.calls[0]
        assert isinstance(wrapped_input, list)
        assert all(isinstance(part, str) for part in wrapped_input)
        assert "echo ok && git status" in wrapped_input
        assert wrapped_input_env == _SCRUBBED_ENV
        # Popen got the WRAPPED argv (list exec) — never a str + shell=True
        assert len(record) == 1
        popen_args = record[0]["args"]
        popen_kwargs = record[0]["kwargs"]
        assert isinstance(popen_args[0], list), "sandboxed spawn must be list-exec"
        assert "sandbox-exec" in popen_args[0]
        assert "shell" not in popen_kwargs, "list-exec must not pass shell=True"
        assert popen_kwargs.get("env") == {**_SCRUBBED_ENV, "WRAPPED": "1"}, (
            "Popen must receive the env returned by backend.wrap"
        )

    def test_async_backend_uses_create_subprocess_exec(self, monkeypatch):
        record: list[dict[str, Any]] = []

        async def fake_exec(*args: Any, **kwargs: Any) -> _FakeAsyncProc:
            record.append({"args": args, "kwargs": kwargs})
            return _FakeAsyncProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(terminal, "scrub_env", _scrub_stub)
        monkeypatch.setattr(terminal, "read_policy", lambda: SandboxPolicy.AUTO)
        backend = _FakeBackend()
        monkeypatch.setattr(terminal, "get_backend", lambda policy: backend)
        tool = _tool()

        asyncio.run(tool._arun(["echo ok"]))

        assert len(record) == 1
        args, kwargs = record[0]["args"], record[0]["kwargs"]
        # *wrapped -> varargs: the argv elements arrive as positional args
        assert "sandbox-exec" in args
        assert any("echo ok" in part for part in args if isinstance(part, str))
        assert kwargs.get("env") == {**_SCRUBBED_ENV, "WRAPPED": "1"}

    def test_required_unavailable_raises_tool_exception(self, monkeypatch):
        # REQUIRED + sandbox=True + backend unavailable: get_backend raises
        # RuntimeError; the tool must surface it as ToolException so
        # handle_tool_error=True converts it into a tool error message.
        monkeypatch.setattr(terminal, "scrub_env", _scrub_stub)

        def _raise(policy):
            raise RuntimeError("Required sandbox unavailable on Windows")

        monkeypatch.setattr(terminal, "read_policy", lambda: SandboxPolicy.REQUIRED)
        monkeypatch.setattr(terminal, "get_backend", _raise)
        tool = _tool()

        from langchain_core.tools import ToolException

        with pytest.raises(ToolException, match="Required sandbox unavailable"):
            tool._run(["echo ok"])

    def test_required_unavailable_invoke_becomes_error_string(self, monkeypatch):
        # invoke level (handle_tool_error=True): ToolException -> returned as a
        # string (raw message; the "Error:" prefix is a ToolNode-layer detail —
        # do NOT assert it here, cf. langchain_core 1.4.7 base.py:1208).
        monkeypatch.setattr(terminal, "scrub_env", _scrub_stub)

        def _raise(policy):
            raise RuntimeError("Required sandbox unavailable on Windows")

        monkeypatch.setattr(terminal, "read_policy", lambda: SandboxPolicy.REQUIRED)
        monkeypatch.setattr(terminal, "get_backend", _raise)
        tool = _tool()

        out = tool.invoke({"commands": ["echo ok"]})
        assert isinstance(out, str)
        assert "Required sandbox unavailable" in out
        assert "echo" not in out.split("Required")[0], "no command output leaked"


# ─────────────────────────────────────────────────────────────────────────────
# 7. Public invoke semantics: blocked -> string via handle_tool_error
# ─────────────────────────────────────────────────────────────────────────────
class TestPublicInvokeSemantics:
    def test_invoke_blocked_regex_returns_error_string_not_raise(self):
        tool = _tool()
        out = tool.invoke({"commands": ["echo ok", "rm -rf /"]})
        assert isinstance(out, str)
        assert "Blocked: unsafe command." in out

    def test_invoke_subagent_sandbox_false_returns_deny_string(self):
        tool = _tool(caller_scope="subagent")
        out = tool.invoke({"commands": ["echo ok"], "sandbox": False})
        assert isinstance(out, str)
        assert "沙箱绕过仅限主会话人工审批" in out
