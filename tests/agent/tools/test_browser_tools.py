"""The merged browser tool: action dispatch, gating, metadata and security.

The seven everyday verbs are one ``browser`` tool; ``browser_evaluate`` stays a
separate opt-in tool. The handlers remain thin shells over the manager, so these
tests pin what the model sees and that every action still reaches the same
manager call with the same text.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, TypedDict

import pytest
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from agent.security.untrusted_wrapper import is_untrusted_tool
from agent.tools import _MAIN_TOOLS_BUILDERS
from agent.tools.browser import build_browser_tools, build_legacy_browser_tools
from config.features import BROWSER_AGENT
from config.features.agent_side.redaction import REDACTION
from runtime import hooks

pytestmark = [pytest.mark.unit]

_SESSION = "sess-browser-tools"
_LEGACY_NAMES = {
    "browser_navigate",
    "browser_snapshot",
    "browser_click",
    "browser_type",
    "browser_press",
    "browser_scroll",
    "browser_screenshot",
    "browser_evaluate",
}


class _FakeManager:
    """The manager surface the tools use, returning canned outcomes."""

    def __init__(self) -> None:
        self.screenshot_path = "/tmp/shot.png"
        self.last_page: str | None = None
        self.navigations: list[tuple[str, str | None]] = []
        self.snapshot_url = "https://fake.test"
        self.screenshot_url: str | None = None
        self.evaluate_url = "https://fake.test"
        self.navigate_url: str | None = None

    async def navigate(self, session_id, url, page=None):
        self.last_page = page
        self.navigations.append((url, page))
        final = self.navigate_url or url
        return {"page": "p1", "url": final, "title": "T", "session_id": session_id, "loaded": True}

    async def snapshot(self, session_id, page=None, *, max_elements=None):
        self.last_page = page
        return {
            "page": "p1",
            "url": self.snapshot_url,
            "title": "Fake",
            "session_id": session_id,
            "text": "hello world",
            "elements": [
                {"ref": "e1", "tag": "button", "name": "Go"},
                {"ref": "e2", "tag": "input", "name": "q", "value": "x"},
            ],
            "truncated": True,
            "max_elements": max_elements,
        }

    async def click(
        self, session_id, *, ref=None, x=None, y=None, button="left", double=False, page_id=None
    ):
        self.last_page = page_id
        return {
            "page": "p1",
            "clicked": ref or f"{x},{y}",
            "ok": True,
            "tag": "button",
            "text": "Go",
        }

    async def type_text(self, session_id, text, *, ref=None, submit=False, page_id=None):
        self.last_page = page_id
        return {"page": "p1", "typed": len(text), "submitted": submit}

    async def press(self, session_id, keys, page_id=None):
        self.last_page = page_id
        return {"page": "p1", "pressed": keys}

    async def scroll(self, session_id, *, delta_y=0, delta_x=0, page_id=None):
        self.last_page = page_id
        return {"page": "p1", "scrolled": [delta_x, delta_y]}

    async def screenshot(self, session_id, *, full_page=False, page_id=None):
        self.last_page = page_id
        return {
            "page": "p1",
            "url": self.screenshot_url,
            "path": self.screenshot_path,
            "bytes": 10,
            "width": 4,
            "height": 3,
            "full_page": full_page,
        }

    async def evaluate(self, session_id, expression, page_id=None):
        self.last_page = page_id
        return {"page": "p1", "url": self.evaluate_url, "result": "42", "truncated": False}


class _FailingManager(_FakeManager):
    """Raises the exact error classes the guard must translate."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def click(self, session_id, **kwargs):
        raise self.error


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """The SSRF guard resolves DNS per call; the fakes use *.test hosts.

    H5 behavior itself is covered by ``TestNavigationSsrfGuard`` below, which
    overrides this stub where the real verdict matters.
    """
    monkeypatch.setattr("pub.func.validator.public_url.is_public_url", lambda url: True)


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setitem(BROWSER_AGENT, "enabled", 1)
    monkeypatch.setitem(BROWSER_AGENT, "allow_evaluate", 0)
    return BROWSER_AGENT


@pytest.fixture
def bridge():
    fake = _FakeManager()
    hooks.register(hooks.BROWSER_MANAGER, lambda: fake)
    yield fake
    hooks.unregister(hooks.BROWSER_MANAGER)


def _tool(name: str):
    return next(tool for tool in build_browser_tools() if tool.name == name)


async def _invoke(tool, **args):
    return await tool.ainvoke({**args, "session_id": _SESSION})


# ------------------------------------------------------------------- gating


def test_disabled_returns_no_tools(monkeypatch):
    monkeypatch.setitem(BROWSER_AGENT, "enabled", 0)
    assert build_browser_tools() == []
    assert build_legacy_browser_tools() == []


def test_enabled_builds_one_tool_and_adds_evaluate_on_opt_in(enabled):
    assert {tool.name for tool in build_browser_tools()} == {"browser"}

    enabled["allow_evaluate"] = 1
    assert {tool.name for tool in build_browser_tools()} == {"browser", "browser_evaluate"}


def test_the_family_rides_the_main_tool_builders(enabled):
    assert build_browser_tools in _MAIN_TOOLS_BUILDERS


def test_every_browser_tool_is_main_agent_only(enabled):
    enabled["allow_evaluate"] = 1
    for tool in build_browser_tools():
        assert tool.metadata.get("scope") == "main_only", tool.name
        assert tool.metadata.get("idempotent") is False, tool.name


def test_the_merged_surface_keeps_the_approved_names(enabled):
    assert {tool.name for tool in build_browser_tools()} == {"browser"}

    enabled["allow_evaluate"] = 1
    assert {tool.name for tool in build_browser_tools()} == {"browser", "browser_evaluate"}


def test_the_legacy_builder_keeps_the_verb_tools_for_tests(enabled):
    enabled["allow_evaluate"] = 1
    tools = {tool.name: tool for tool in build_legacy_browser_tools()}

    assert set(tools) == _LEGACY_NAMES
    assert tools["browser_navigate"].metadata["scope"] == "main_only"


def test_the_subagent_policy_drops_browser_tools(enabled):
    """The shared ``scope=main_only`` contract is what real spawns read."""
    tools = build_browser_tools()
    try:
        from agent.tools.subagent.spawn.inherited_tool_policy import apply_tool_policy
    except Exception:  # pragma: no cover - stub-polluted process (see AGENTS.md)
        pytest.skip("the subagent package is stubbed in this process")
    assert apply_tool_policy(tools, None, []) == []


# -------------------------------------------------------------------- security


def test_the_merged_browser_output_is_fenced_and_redacted(enabled):
    assert is_untrusted_tool("browser") is True
    assert is_untrusted_tool("browser_snapshot") is True
    assert is_untrusted_tool("browser_evaluate") is True
    assert "browser" in REDACTION["tool_output_tools"]
    # The legacy prefix still covers old transcripts/tool names.
    assert any("browser" in prefix for prefix in REDACTION["tool_output_prefixes"])


# ------------------------------------------------------------------- actions


def test_missing_bridge_is_a_clean_error(enabled):
    hooks.unregister(hooks.BROWSER_MANAGER)
    result = asyncio.run(_invoke(_tool("browser"), action="navigate", url="https://x.test"))
    assert result.startswith("Error: the browser bridge is not assembled")


def test_navigate_reports_the_loaded_page(enabled, bridge):
    result = asyncio.run(_invoke(_tool("browser"), action="navigate", url="https://x.test"))
    assert "page p1" in result and "https://x.test" in result and "loaded" in result
    assert bridge.last_page is None


def test_snapshot_renders_text_elements_and_truncation(enabled, bridge):
    result = asyncio.run(_invoke(_tool("browser"), action="snapshot", max_elements=50))
    assert "hello world" in result
    assert "[e1] button «Go»" in result
    assert "[e2] input «q» value=«x»" in result
    assert "truncated" in result


def test_click_and_type_and_press_and_scroll_report_their_effect(enabled, bridge):
    clicked = asyncio.run(_invoke(_tool("browser"), action="click", ref="e1"))
    assert "clicked [e1]" in clicked and "«Go»" in clicked
    typed = asyncio.run(_invoke(_tool("browser"), action="type", text="abc", submit=True))
    assert "typed 3 character(s) and pressed Enter" in typed
    pressed = asyncio.run(_invoke(_tool("browser"), action="press", keys="Enter"))
    assert "pressed Enter" in pressed
    scrolled = asyncio.run(_invoke(_tool("browser"), action="scroll", delta_y=600))
    assert "scrolled [0, 600" in scrolled


def test_click_by_coordinates_uses_the_same_handler(enabled, bridge):
    result = asyncio.run(_invoke(_tool("browser"), action="click", x=12, y=34))
    assert "clicked [12.0,34.0]" in result


def test_page_is_forwarded_to_the_selected_page(enabled, bridge):
    asyncio.run(_invoke(_tool("browser"), action="click", ref="e1", page="p2"))
    assert bridge.last_page == "p2"


def test_screenshot_attaches_the_image_for_a_vision_model(enabled, bridge, tmp_path):
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"body")
    bridge.screenshot_path = str(shot)

    result = asyncio.run(_invoke(_tool("browser"), action="screenshot"))

    assert isinstance(result, list), result
    assert f"screenshot saved: {shot}" in result[0]["text"]
    assert result[1]["type"] == "image_url"
    assert result[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_screenshot_falls_back_to_the_path_when_the_image_is_unusable(enabled, bridge):
    result = asyncio.run(_invoke(_tool("browser"), action="screenshot"))

    assert isinstance(result, str)
    assert "/tmp/shot.png" in result and "4×3" in result


def test_screenshot_full_page_flag_reaches_the_manager(enabled, bridge):
    result = asyncio.run(_invoke(_tool("browser"), action="screenshot", full_page=True))
    assert isinstance(result, str)
    assert "4×3" in result


def test_evaluate_is_a_separate_opt_in_tool(enabled, bridge):
    enabled["allow_evaluate"] = 1
    result = asyncio.run(_invoke(_tool("browser_evaluate"), expression="1 + 1"))
    assert "42" in result


def test_the_merged_tool_never_executes_evaluate(enabled, bridge):
    result = asyncio.run(_invoke(_tool("browser"), action="evaluate", expression="1 + 1"))
    assert "browser_evaluate" in result and result.startswith("Error:")


# ------------------------------------------------------------------ validation


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        ({"action": "navigate"}, "url"),
        ({"action": "type"}, "text"),
        ({"action": "press"}, "keys"),
        ({"action": "click"}, "ref or (x and y)"),
        ({"action": "click", "x": 10}, "ref or (x and y)"),
        ({"action": "jump"}, "unknown action"),
    ],
)
def test_action_specific_validation_returns_a_correction_hint(enabled, bridge, args, needle):
    result = asyncio.run(_invoke(_tool("browser"), **args))
    assert result.startswith("Error:")
    assert needle in result


def test_click_accepts_ref_or_both_coordinates(enabled, bridge):
    by_ref = asyncio.run(_invoke(_tool("browser"), action="click", ref="e1"))
    by_xy = asyncio.run(_invoke(_tool("browser"), action="click", x=1, y=2))
    assert "clicked [e1]" in by_ref
    assert "clicked [1.0,2.0]" in by_xy


@pytest.mark.parametrize(
    ("error", "needle"),
    [
        (ValueError("unknown ref 'e9' — take a fresh snapshot"), "unknown ref 'e9'"),
        (
            KeyError("session 's1' has no browser page — call browser(action='navigate') first"),
            "no browser page",
        ),
        (PermissionError("browser_evaluate is disabled"), "browser_evaluate is disabled"),
        (RuntimeError("Page.navigate: the CDP connection closed"), "CDP connection closed"),
    ],
)
def test_expected_failures_become_error_strings(monkeypatch, error, needle):
    monkeypatch.setitem(BROWSER_AGENT, "enabled", 1)
    hooks.register(hooks.BROWSER_MANAGER, lambda: _FailingManager(error))
    try:
        result = asyncio.run(_invoke(_tool("browser"), action="click", ref="e1"))
    finally:
        hooks.unregister(hooks.BROWSER_MANAGER)
    assert result.startswith("Error: ") and needle in result


# --------------------------------------------------------------- graph injection


def test_toolnode_injects_the_session_into_the_merged_browser(enabled, bridge):
    """Production reaches the merged tool through ToolNode, not a manual ainvoke."""

    class _State(TypedDict):
        messages: Annotated[list, add_messages]
        session_id: str

    graph = StateGraph(_State)
    graph.add_node("tools", ToolNode([_tool("browser")]))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    app = graph.compile()
    call = {
        "name": "browser",
        "args": {"action": "navigate", "url": "https://injected.test"},
        "id": "call-1",
        "type": "tool_call",
    }

    result = asyncio.run(
        app.ainvoke(
            {
                "messages": [AIMessage(content="", tool_calls=[call])],
                "session_id": _SESSION,
            }
        )
    )

    assert "page p1" in result["messages"][-1].content
    assert "https://injected.test" in result["messages"][-1].content


# ------------------------------------------------- H5: SSRF guard (2026-11-09)


class TestNavigationSsrfGuard:
    """``browser_navigate`` mirrors the ``web_fetch`` SSRF contract."""

    def test_localhost_is_refused_without_optin(self, enabled, bridge, monkeypatch):
        monkeypatch.delenv("SHERRY_BROWSER_ALLOW_LOCAL", raising=False)

        result = asyncio.run(
            _invoke(_tool("browser"), action="navigate", url="http://127.0.0.1:8080/")
        )

        assert result.startswith("Error:")
        assert "opt-in" in result
        assert bridge.navigations == [], "a refused URL must never reach the manager"

    def test_cloud_metadata_address_is_refused(self, enabled, bridge, monkeypatch):
        monkeypatch.delenv("SHERRY_BROWSER_ALLOW_LOCAL", raising=False)

        result = asyncio.run(
            _invoke(
                _tool("browser"),
                action="navigate",
                url="http://169.254.169.254/latest/meta-data/",
            )
        )

        assert result.startswith("Error:")
        assert "local/private" in result
        assert bridge.navigations == []

    def test_local_optin_allows_the_configured_target(self, enabled, bridge, monkeypatch):
        monkeypatch.setenv("SHERRY_BROWSER_ALLOW_LOCAL", "1")

        result = asyncio.run(
            _invoke(_tool("browser"), action="navigate", url="http://127.0.0.1:8080/app")
        )

        assert "page p1" in result
        assert bridge.navigations == [("http://127.0.0.1:8080/app", None)]

    def test_non_http_scheme_is_refused(self, enabled, bridge):
        result = asyncio.run(_invoke(_tool("browser"), action="navigate", url="file:///etc/passwd"))

        assert result.startswith("Error:")
        assert "http" in result
        assert bridge.navigations == []

    def test_unresolvable_or_private_hostname_is_refused(self, enabled, bridge, monkeypatch):
        monkeypatch.setattr("pub.func.validator.public_url.is_public_url", lambda url: False)

        result = asyncio.run(
            _invoke(_tool("browser"), action="navigate", url="https://rebind.test/")
        )

        assert result.startswith("Error:")
        assert "not a public URL" in result
        assert bridge.navigations == []

    def test_redirect_to_a_blocked_target_parks_the_page(self, enabled, bridge):
        bridge.navigate_url = "http://127.0.0.1:8080/admin"

        result = asyncio.run(
            _invoke(_tool("browser"), action="navigate", url="https://public.test/")
        )

        assert "redirected to a blocked URL" in result
        assert ("about:blank", None) in bridge.navigations

    def test_redirect_to_a_failed_load_interstitial_is_not_blocked(self, enabled, bridge):
        # Chrome's own error page carries no fetched content; it must not be
        # reported as an SSRF block.
        bridge.navigate_url = "chrome-error://chromewebdata/"

        result = asyncio.run(
            _invoke(_tool("browser"), action="navigate", url="https://public.test/")
        )

        assert "page p1" in result
        assert "blocked" not in result

    def test_redirect_to_a_file_scheme_parks_the_page(self, enabled, bridge):
        bridge.navigate_url = "file:///etc/passwd"

        result = asyncio.run(
            _invoke(_tool("browser"), action="navigate", url="https://public.test/")
        )

        assert "redirected to a blocked URL" in result
        assert ("about:blank", None) in bridge.navigations


class TestReadPathSsrfGuard:
    """Snapshot / screenshot / evaluate are the exfiltration points."""

    def test_snapshot_withholds_content_from_a_blocked_page(self, enabled, bridge):
        bridge.snapshot_url = "http://127.0.0.1:8080/internal"

        result = asyncio.run(_invoke(_tool("browser"), action="snapshot"))

        assert "content withheld" in result
        assert "hello world" not in result
        assert ("about:blank", None) in bridge.navigations

    def test_snapshot_of_about_blank_still_works(self, enabled, bridge):
        bridge.snapshot_url = "about:blank"

        result = asyncio.run(_invoke(_tool("browser"), action="snapshot"))

        assert "hello world" in result

    def test_screenshot_withholds_pixels_from_a_blocked_page(self, enabled, bridge, tmp_path):
        capture = tmp_path / "secret.png"
        capture.write_bytes(b"\x89PNG\r\n\x1a\nbody")
        bridge.screenshot_path = str(capture)
        bridge.screenshot_url = "file:///etc/secrets.png"

        result = asyncio.run(_invoke(_tool("browser"), action="screenshot"))

        assert isinstance(result, str)
        assert "content withheld" in result
        assert str(capture) not in result
        assert not capture.exists(), "a withheld capture must be deleted from disk"
        assert ("about:blank", None) in bridge.navigations

    def test_evaluate_withholds_results_from_a_blocked_page(self, monkeypatch, bridge):
        monkeypatch.setitem(BROWSER_AGENT, "enabled", 1)
        monkeypatch.setitem(BROWSER_AGENT, "allow_evaluate", 1)
        bridge.evaluate_url = "http://127.0.0.1:8080/private"

        result = asyncio.run(
            _invoke(_tool("browser_evaluate"), expression="document.body.innerText")
        )

        assert "content withheld" in result
        assert "42" not in result
        assert ("about:blank", None) in bridge.navigations
