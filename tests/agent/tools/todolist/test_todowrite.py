"""Behavior tests for the todowrite tool: E6a fan-out reminder + JSON result.

The tool is exercised through ``build_todolist_tools()`` so the assertions run
against the same objects the agent is wired with. ``TodoService.update_todos``
is monkeypatched in every test: the store/barrier layer has its own suite
(``test_service.py``) and is never invoked here.
"""

import json
import sys

import pytest

from agent.tools.todolist.tools import build_todolist_tools

# The real module, not the re-exported StructuredTool: the family package
# re-exports the tool under the same name, so package-attribute traversal
# resolves to the tool. sys.modules keys are exact and immune to shadowing.
todowrite_module = sys.modules["agent.tools.todolist.tools.todowrite"]

pytestmark = [pytest.mark.unit]


def _todowrite():
    return {t.name: t for t in build_todolist_tools()}["todowrite"]


def _fake_update(returned: list[dict]):
    """A ``TodoService.update_todos`` stand-in that records its call."""

    async def _update(session_id: str, todos: list[dict]) -> list[dict]:
        _update.calls.append((session_id, todos))
        return returned

    _update.calls = []
    return _update


@pytest.fixture()
def fresh_reminder_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the module-level once-per-session reminder set between tests."""
    monkeypatch.setattr(todowrite_module, "_reminded_sessions", set())


@pytest.mark.asyncio
async def test_fanout_reminder_appended_once_per_session(
    monkeypatch: pytest.MonkeyPatch, fresh_reminder_state: None
):
    """Given a fresh session, When todowrite is called twice,
    Then the fan-out reminder appears on the first result only."""
    fake = _fake_update([{"content": "step", "status": "pending"}])
    monkeypatch.setattr(todowrite_module.service.TodoService, "update_todos", staticmethod(fake))
    tool = _todowrite()

    first = await tool.coroutine(todos=[{"content": "step"}], session_id="sess-A")
    second = await tool.coroutine(todos=[{"content": "step"}], session_id="sess-A")

    assert todowrite_module._FANOUT_REMINDER in first
    assert todowrite_module._FANOUT_REMINDER not in second
    # A different session is a different "first call".
    third = await tool.coroutine(todos=[{"content": "step"}], session_id="sess-B")
    assert todowrite_module._FANOUT_REMINDER in third
    assert len(fake.calls) == 3
    assert fake.calls[0][0] == "sess-A"
    assert fake.calls[2][0] == "sess-B"


@pytest.mark.asyncio
async def test_todowrite_forwards_todos_and_returns_downgraded_json(
    monkeypatch: pytest.MonkeyPatch, fresh_reminder_state: None
):
    """Given the service downgrades a bogus category, When todowrite runs,
    Then it forwards the caller's list untouched and serializes the result."""
    downgraded = [{"content": "x", "status": "pending", "category": "quick"}]
    fake = _fake_update(downgraded)
    monkeypatch.setattr(todowrite_module.service.TodoService, "update_todos", staticmethod(fake))
    # Suppress the reminder so the whole return value is parseable JSON.
    monkeypatch.setattr(todowrite_module, "_reminded_sessions", {"sess-J"})
    tool = _todowrite()

    raw = [{"content": "x", "category": "bogus"}]
    output = await tool.coroutine(todos=raw, session_id="sess-J")

    assert json.loads(output) == downgraded
    assert fake.calls == [("sess-J", raw)]


@pytest.mark.asyncio
async def test_todowrite_forwards_downgraded_category_in_returned_json(
    monkeypatch: pytest.MonkeyPatch, fresh_reminder_state: None
):
    """Given a service result carrying a downgraded category, When todowrite
    serializes it, Then the downgraded value is visible in the JSON text."""
    fake = _fake_update(
        [
            {
                "content": "task",
                "status": "pending",
                "category": "quick",
                "delegation": "self",
            }
        ]
    )
    monkeypatch.setattr(todowrite_module.service.TodoService, "update_todos", staticmethod(fake))
    monkeypatch.setattr(todowrite_module, "_reminded_sessions", {"sess-K"})
    tool = _todowrite()

    output = await tool.coroutine(
        todos=[{"content": "task", "category": "not-a-category"}], session_id="sess-K"
    )

    payload = json.loads(output)
    assert payload[0]["category"] == "quick"
    assert payload[0]["delegation"] == "self"
    assert fake.calls[0][1][0]["category"] == "not-a-category"
