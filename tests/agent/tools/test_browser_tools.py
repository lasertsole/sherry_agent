"""The browser tool family: the off-by-default gate, main_only metadata, text.

The tools are thin shells over the manager (reached through
``runtime/hooks.BROWSER_MANAGER``), so these tests pin what the model actually
sees: no tools at all while the feature is off, the catalogue group, the
main-agent-only contract, the readable snapshot text, and error strings instead
of raised exceptions.
"""

from __future__ import annotations

import asyncio

import pytest

from agent.tools import _MAIN_TOOLS_BUILDERS
from agent.tools.browser import build_browser_tools
from agent.tools.catalog import TOOL_GROUPS, tool_catalog
from config.features import BROWSER_AGENT
from runtime import hooks

pytestmark = [pytest.mark.unit]

_ALL_NAMES = {
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
        #: Where the fake capture claims to have written the PNG (a test that
        #: wants the native image block points this at a file that exists).
        self.screenshot_path = "/tmp/shot.png"

    async def navigate(self, session_id, url, page=None):
        return {"page": "p1", "url": url, "title": "T", "session_id": session_id, "loaded": True}

    async def snapshot(self, session_id, page=None, *, max_elements=None):
        return {
            "page": "p1",
            "url": "https://fake.test",
            "title": "Fake",
            "session_id": session_id,
            "text": "hello world",
            "elements": [
                {"ref": "e1", "tag": "button", "name": "Go"},
                {"ref": "e2", "tag": "input", "name": "q", "value": "x"},
            ],
            "truncated": True,
        }

    async def click(
        self, session_id, *, ref=None, x=None, y=None, button="left", double=False, page_id=None
    ):
        return {
            "page": "p1",
            "clicked": ref or f"{x},{y}",
            "ok": True,
            "tag": "button",
            "text": "Go",
        }

    async def type_text(self, session_id, text, *, ref=None, submit=False, page_id=None):
        return {"page": "p1", "typed": len(text), "submitted": submit}

    async def press(self, session_id, keys, page_id=None):
        return {"page": "p1", "pressed": keys}

    async def scroll(self, session_id, *, delta_y=0, delta_x=0, page_id=None):
        return {"page": "p1", "scrolled": [delta_x, delta_y]}

    async def screenshot(self, session_id, *, full_page=False, page_id=None):
        return {
            "page": "p1",
            "path": self.screenshot_path,
            "bytes": 10,
            "width": 4,
            "height": 3,
            "full_page": full_page,
        }

    async def evaluate(self, session_id, expression, page_id=None):
        return {"page": "p1", "result": "42", "truncated": False}


class _FailingManager(_FakeManager):
    """Raises the exact error classes the guard must translate."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def click(self, session_id, **kwargs):
        raise self.error


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


# ------------------------------------------------------------------- gating


def test_disabled_returns_no_tools(monkeypatch):
    monkeypatch.setitem(BROWSER_AGENT, "enabled", 0)
    assert build_browser_tools() == []


def test_enabled_builds_seven_tools_and_adds_evaluate_on_opt_in(enabled):
    tools = build_browser_tools()
    assert {tool.name for tool in tools} == _ALL_NAMES - {"browser_evaluate"}

    enabled["allow_evaluate"] = 1
    names = {tool.name for tool in build_browser_tools()}
    assert names == _ALL_NAMES


def test_the_family_rides_the_main_tool_builders(enabled):
    assert build_browser_tools in _MAIN_TOOLS_BUILDERS


def test_every_browser_tool_is_main_agent_only(enabled):
    for tool in build_browser_tools():
        assert tool.metadata.get("scope") == "main_only", tool.name


def test_the_catalogue_groups_them_under_browser(enabled):
    entries = tool_catalog(build_browser_tools())
    assert {entry["name"] for entry in entries} == _ALL_NAMES - {"browser_evaluate"}
    assert {entry["group"] for entry in entries} == {"browser"}
    assert set(TOOL_GROUPS["browser"]) == _ALL_NAMES
    # The group is switchable per tool — never in the bulk-only set.
    from agent.tools.catalog import BULK_ONLY_GROUPS

    assert "browser" not in BULK_ONLY_GROUPS


def test_the_subagent_policy_drops_browser_tools(enabled):
    """The shared ``scope=main_only`` contract is what real spawns read."""
    tools = build_browser_tools()
    try:
        from agent.tools.subagent.spawn.inherited_tool_policy import apply_tool_policy
    except Exception:  # pragma: no cover - stub-polluted process (see AGENTS.md)
        pytest.skip("the subagent package is stubbed in this process")
    assert apply_tool_policy(tools, None, []) == []


# --------------------------------------------------------------------- text


def test_missing_bridge_is_a_clean_error(enabled):
    hooks.unregister(hooks.BROWSER_MANAGER)
    result = asyncio.run(_tool("browser_navigate").ainvoke({"url": "https://x.test"}))
    assert result.startswith("Error: the browser bridge is not assembled")


def test_navigate_reports_the_loaded_page(enabled, bridge):
    result = asyncio.run(_tool("browser_navigate").ainvoke({"url": "https://x.test"}))
    assert "page p1" in result and "https://x.test" in result and "loaded" in result


def test_snapshot_renders_text_elements_and_truncation(enabled, bridge):
    result = asyncio.run(_tool("browser_snapshot").ainvoke({}))
    assert "hello world" in result
    assert "[e1] button «Go»" in result
    assert "[e2] input «q» value=«x»" in result
    assert "truncated" in result


def test_click_and_type_and_press_and_scroll_report_their_effect(enabled, bridge):
    clicked = asyncio.run(_tool("browser_click").ainvoke({"ref": "e1"}))
    assert "clicked [e1]" in clicked and "«Go»" in clicked
    typed = asyncio.run(_tool("browser_type").ainvoke({"text": "abc", "submit": True}))
    assert "typed 3 character(s) and pressed Enter" in typed
    pressed = asyncio.run(_tool("browser_press").ainvoke({"keys": "Enter"}))
    assert "pressed Enter" in pressed
    scrolled = asyncio.run(_tool("browser_scroll").ainvoke({"delta_y": 600}))
    assert "scrolled [0, 600" in scrolled


def test_screenshot_attaches_the_image_for_a_vision_model(enabled, bridge, tmp_path):
    """The model that can SEE should not be sent to the image_to_text skill."""
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"body")
    bridge.screenshot_path = str(shot)

    result = asyncio.run(_tool("browser_screenshot").ainvoke({}))

    assert isinstance(result, list), result
    assert f"screenshot saved: {shot}" in result[0]["text"]
    block = result[1]
    assert block["type"] == "image_url"
    assert block["image_url"]["url"].startswith("data:image/png;base64,")


def test_screenshot_falls_back_to_the_path_when_the_image_is_unusable(enabled, bridge):
    """No readable file (the canned path does not exist) → the text answer."""
    result = asyncio.run(_tool("browser_screenshot").ainvoke({}))

    assert isinstance(result, str)
    assert "/tmp/shot.png" in result and "4×3" in result


def test_evaluate_is_added_only_with_the_opt_in(enabled, bridge):
    enabled["allow_evaluate"] = 1
    result = asyncio.run(_tool("browser_evaluate").ainvoke({"expression": "1 + 1"}))
    assert "42" in result


@pytest.mark.parametrize(
    ("error", "needle"),
    [
        (ValueError("unknown ref 'e9' — take a fresh snapshot"), "unknown ref 'e9'"),
        (
            KeyError("session 's1' has no browser page — call browser_navigate first"),
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
        result = asyncio.run(_tool("browser_click").ainvoke({"ref": "e1"}))
    finally:
        hooks.unregister(hooks.BROWSER_MANAGER)
    assert result.startswith("Error: ") and needle in result
