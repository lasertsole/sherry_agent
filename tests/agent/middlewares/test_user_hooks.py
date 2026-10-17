"""User-configurable lifecycle hooks (Step 1: read-only dispatch).

Contract under test — the hook chain must never be able to break or block a
turn: dispatch is off unless the config enables it, a hook's timeout kills its
process, a non-JSON stdout is a successful run, a nonzero exit is only a log
line (the tool result rides through untouched), the matcher selects the tools a
hook sees, and the payload carries bounded PREVIEWS rather than raw payloads.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from agent.middlewares.user_hooks import UserHooksMiddleware
from agent.middlewares.user_hooks.core import hook_matcher_matches
from config.features import USER_HOOKS

pytestmark = [pytest.mark.unit]

#: A hook script that echoes the payload it received back on stdout (and then
#: whatever the test appends), so a dispatch can be asserted from the outside.
_ECHO = f'{sys.executable} -c "import json,sys;print(json.dumps(json.load(sys.stdin)))"'
#: A hook that writes its stdin payload to a file given as argv[1].
_CAPTURE = f"{sys.executable} -c \"import sys;open(sys.argv[1],'w').write(sys.stdin.read())\""


def _middleware(**section: Any) -> UserHooksMiddleware:
    """Build the middleware from a raw ``hooks`` config section."""
    return UserHooksMiddleware({"enabled": True, **section})


def _request(name: str, args: dict | None = None, session: str = "sess-1") -> Any:
    """A ToolCallRequest stand-in with the fields the middleware reads."""

    class _Tool:
        def __init__(self, tool_name: str) -> None:
            self.name = tool_name

    class _Request:
        def __init__(self) -> None:
            self.tool_call = {"name": name, "id": f"call-{name}", "args": args or {}}
            self.tool = _Tool(name)
            self.state = {"session_id": session}

    return _Request()


async def _ok_handler(_request: Any) -> ToolMessage:
    return ToolMessage(content="done", name="write_file", tool_call_id="call-write_file")


async def _error_handler(_request: Any) -> ToolMessage:
    return ToolMessage(
        content="Error: boom", name="write_file", tool_call_id="call-write_file", status="error"
    )


class TestMatcher:
    def test_star_and_empty_match_everything(self):
        assert hook_matcher_matches("*", "write_file") is True
        assert hook_matcher_matches("", "write_file") is True

    def test_glob_alternation_and_exact_names(self):
        assert hook_matcher_matches("write_*|patch_file", "write_file") is True
        assert hook_matcher_matches("write_*|patch_file", "patch_file") is True
        assert hook_matcher_matches("write_*|patch_file", "read_file") is False
        assert hook_matcher_matches("write_file", "write_file") is True
        assert hook_matcher_matches("write_file", "write_file_x") is False

    def test_slash_wrapped_matcher_is_a_regex(self):
        assert hook_matcher_matches("/^write_/", "write_file") is True
        assert hook_matcher_matches("/^write_/", "read_file") is False
        # An invalid pattern never matches (fail-closed), never raises.
        assert hook_matcher_matches("/[bad/", "write_file") is False


class TestConfigLoading:
    def test_no_section_is_disabled(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        import config.sherry_settings as settings

        config_file = tmp_path / "sherry.jsonc"
        config_file.write_text('{"LOG_LEVEL": "INFO"}', encoding="utf-8")
        monkeypatch.setattr(settings, "SHERRY_CONFIG_PATH", config_file)

        assert settings.load_sherry_hooks() == {"enabled": False}

    def test_entries_are_validated_and_clamped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import config.sherry_settings as settings

        config_file = tmp_path / "sherry.jsonc"
        config_file.write_text(
            """
            {
              // a comment and a trailing comma, like the real file
              "hooks": {
                "enabled": true,
                "PostToolUse": [
                  {"command": "  echo hi  ", "matcher": "write_*", "timeout_ms": 999999},
                  {"command": "   "},
                  {"not": "a hook"},
                  "not a dict",
                ],
              },
            }
            """,
            encoding="utf-8",
        )
        monkeypatch.setattr(settings, "SHERRY_CONFIG_PATH", config_file)

        loaded = settings.load_sherry_hooks()

        assert loaded["enabled"] is True
        specs = loaded["PostToolUse"]
        assert len(specs) == 1, "only the entry with a usable command survives"
        assert specs[0]["command"] == "echo hi"
        assert specs[0]["matcher"] == "write_*"
        assert specs[0]["timeout_ms"] == USER_HOOKS["max_timeout_ms"], "clamped to the ceiling"
        assert specs[0]["blocking"] is False

    def test_a_disabled_section_keeps_its_specs_but_stays_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import config.sherry_settings as settings

        config_file = tmp_path / "sherry.jsonc"
        config_file.write_text(
            '{"hooks": {"PostToolUse": [{"command": "echo hi"}]}}', encoding="utf-8"
        )
        monkeypatch.setattr(settings, "SHERRY_CONFIG_PATH", config_file)

        loaded = settings.load_sherry_hooks()

        assert loaded["enabled"] is False, "no enabled flag -> off"
        assert loaded["PostToolUse"][0]["timeout_ms"] == USER_HOOKS["default_timeout_ms"]


class TestDispatch:
    @pytest.mark.asyncio
    async def test_disabled_or_empty_hooks_pass_through_without_spawning(self, tmp_path: Path):
        marker = tmp_path / "ran"
        spec = {"command": f"touch {marker}", "matcher": "*"}

        for middleware in (
            UserHooksMiddleware({"enabled": False, "PostToolUse": [spec]}),
            UserHooksMiddleware({"enabled": True}),
        ):
            result = await middleware.awrap_tool_call(_request("write_file"), _ok_handler)
            assert result.content == "done"
        assert not marker.exists(), "no hook process may be spawned"

    @pytest.mark.asyncio
    async def test_post_tool_use_receives_a_bounded_preview_and_the_call_identity(
        self, tmp_path: Path
    ):
        captured = tmp_path / "payload.json"
        middleware = _middleware(
            PostToolUse=[{"command": f"{_CAPTURE} {captured}", "matcher": "*"}]
        )

        async def _big_handler(_request: Any) -> ToolMessage:
            return ToolMessage(
                content="x" * (USER_HOOKS["result_preview_chars"] + 500),
                name="write_file",
                tool_call_id="call-write_file",
            )

        result = await middleware.awrap_tool_call(_request("write_file"), _big_handler)

        assert result.content.startswith("xxx"), "the tool result is returned untouched"
        payload = json.loads(captured.read_text(encoding="utf-8"))
        assert payload["event"] == "PostToolUse"
        assert payload["toolName"] == "write_file"
        assert payload["toolCallId"] == "call-write_file"
        assert payload["sessionId"] == "sess-1"
        assert payload["durationMs"] >= 0
        assert "chars truncated" in payload["result"], "the preview is bounded"
        assert len(payload["result"]) < USER_HOOKS["result_preview_chars"] + 100

    @pytest.mark.asyncio
    async def test_an_error_result_dispatches_the_failure_event(self, tmp_path: Path):
        captured = tmp_path / "payload.json"
        middleware = _middleware(
            PostToolUse=[{"command": f"{_CAPTURE} {captured}"}],
            PostToolUseFailure=[{"command": f"{_CAPTURE} {captured}.failure"}],
        )

        await middleware.awrap_tool_call(_request("write_file"), _error_handler)

        assert not captured.exists(), "a failed tool must not fire PostToolUse"
        payload = json.loads(captured.with_suffix(".json.failure").read_text(encoding="utf-8"))
        assert payload["event"] == "PostToolUseFailure"
        assert "boom" in payload["error"]

    @pytest.mark.asyncio
    async def test_the_matcher_decides_which_tools_reach_a_hook(self, tmp_path: Path):
        captured = tmp_path / "only-writes.json"
        middleware = _middleware(
            PostToolUse=[{"command": f"{_CAPTURE} {captured}", "matcher": "write_*|patch_file"}]
        )

        await middleware.awrap_tool_call(_request("read_file"), _ok_handler)
        assert not captured.exists(), "read_file does not match write_*"

        await middleware.awrap_tool_call(_request("write_file"), _ok_handler)
        assert captured.exists()

    @pytest.mark.asyncio
    async def test_a_non_json_stdout_is_a_successful_run(self):
        middleware = _middleware(PostToolUse=[{"command": "echo not-json"}])

        # No raise, and the tool result rides through unchanged.
        result = await middleware.awrap_tool_call(_request("write_file"), _ok_handler)

        assert result.content == "done"

    @pytest.mark.asyncio
    async def test_a_json_outcome_carries_the_step2_fields_without_acting_on_them(self):
        hook = (
            f'{sys.executable} -c "import sys;'
            'print(\'{\\"decision\\": \\"deny\\", \\"reason\\": \\"nope\\", '
            '\\"updatedInput\\": {\\"path\\": \\"/tmp/x\\"}, '
            '\\"additionalContext\\": \\"hi\\"}\')"'
        )
        middleware = _middleware(PostToolUse=[{"command": hook}])

        # Step 1 ignores decision / updatedInput / additionalContext: the tool
        # still ran and its result is what the model sees.
        result = await middleware.awrap_tool_call(_request("write_file"), _ok_handler)

        assert result.content == "done"

    @pytest.mark.asyncio
    async def test_a_failing_hook_never_breaks_the_turn(self):
        middleware = _middleware(
            PostToolUse=[{"command": "exit 3"}, {"command": "definitely-not-a-command"}]
        )

        result = await middleware.awrap_tool_call(_request("write_file"), _ok_handler)

        assert result.content == "done"

    @pytest.mark.asyncio
    async def test_a_timeout_kills_the_hook_and_returns_the_result(self):
        middleware = _middleware(
            PostToolUse=[
                {"command": f'{sys.executable} -c "import time;time.sleep(30)"', "timeout_ms": 300}
            ]
        )

        started = asyncio.get_running_loop().time()
        result = await middleware.awrap_tool_call(_request("write_file"), _ok_handler)
        elapsed = asyncio.get_running_loop().time() - started

        assert result.content == "done"
        assert elapsed < 5, f"the timeout must kill the process (took {elapsed:.1f}s)"

    @pytest.mark.asyncio
    async def test_a_graph_interrupt_is_re_raised_and_dispatches_nothing(self, tmp_path: Path):
        from langgraph.errors import GraphInterrupt

        captured = tmp_path / "must-not-exist.json"
        middleware = _middleware(PostToolUse=[{"command": f"{_CAPTURE} {captured}"}])

        async def _interrupting(_request: Any) -> ToolMessage:
            raise GraphInterrupt(())

        with pytest.raises(GraphInterrupt):
            await middleware.awrap_tool_call(_request("write_file"), _interrupting)
        assert not captured.exists(), "a parked turn is not a failure"

    @pytest.mark.asyncio
    async def test_hooks_run_serially_in_config_order(self, tmp_path: Path):
        order: list[str] = []
        for name in ("first", "second"):
            script = (
                f"{sys.executable} -c \"import sys;open(sys.argv[1],'a').write('{name}\\\\n')\""
            )
            order.append(script)
        log = tmp_path / "order.txt"
        middleware = _middleware(PostToolUse=[{"command": f"{script} {log}"} for script in order])

        await middleware.awrap_tool_call(_request("write_file"), _ok_handler)

        assert log.read_text(encoding="utf-8").splitlines() == ["first", "second"]

    @pytest.mark.asyncio
    async def test_the_session_switch_turns_dispatch_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        captured = tmp_path / "payload.json"
        middleware = _middleware(PostToolUse=[{"command": f"{_CAPTURE} {captured}"}])

        monkeypatch.setattr(
            "agent.middlewares.user_hooks.core.middleware_enabled",
            lambda *_args, **_kwargs: False,
        )

        await middleware.awrap_tool_call(_request("write_file"), _ok_handler)

        assert not captured.exists(), "预设-中间件 turned it off for this session"


class TestStopEvent:
    @pytest.mark.asyncio
    async def test_stop_reports_the_response_preview_and_the_tool_call_count(self, tmp_path: Path):
        captured = tmp_path / "payload.json"
        middleware = _middleware(Stop=[{"command": f"{_CAPTURE} {captured}"}])
        state = {
            "session_id": "sess-1",
            "messages": [
                ToolMessage(content="r1", name="a", tool_call_id="1"),
                AIMessage(content="all done"),
                ToolMessage(content="r2", name="b", tool_call_id="2"),
            ],
        }

        await middleware.aafter_agent(state)

        payload = json.loads(captured.read_text(encoding="utf-8"))
        assert payload["event"] == "Stop"
        assert payload["responsePreview"] == "all done"
        assert payload["toolCallCount"] == 2

    @pytest.mark.asyncio
    async def test_stop_without_a_session_still_dispatches_with_an_empty_id(self, tmp_path: Path):
        # Fail-open, like every other per-session switch in this chain: a turn
        # that ended is a turn that ended, and an audit hook should hear about
        # it. The payload simply carries no session id.
        captured = tmp_path / "payload.json"
        middleware = _middleware(Stop=[{"command": f"{_CAPTURE} {captured}"}])

        await middleware.aafter_agent({"messages": [AIMessage(content="hi")]})

        payload = json.loads(captured.read_text(encoding="utf-8"))
        assert payload["sessionId"] == ""
        assert payload["responsePreview"] == "hi"


class TestUnimplementedEvents:
    def test_a_pre_tool_use_hook_is_loaded_but_not_dispatched(self, capsys):
        middleware = _middleware(PreToolUse=[{"command": "echo audit"}])

        assert middleware._hooks.get("PreToolUse"), "the spec is loaded (vocabulary validates)"
        assert middleware._runners_for("PreToolUse", "write_file") == []
