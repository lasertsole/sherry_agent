"""Registry/resolution tests for tool-result enrichment."""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import ToolMessage

from agent.middlewares.tool_result_enrichment import core as trc
from agent.middlewares.tool_result_enrichment.core import ToolResultInjector


pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(trc, "_INJECTORS", {})
    monkeypatch.setattr(trc, "_CHANGE_STATE", {})


def _result(content: str = "ok") -> ToolMessage:
    return ToolMessage(content=content, tool_call_id="call-1", name="terminal")


def _builder(text: str | None):
    def build(tool_name: str, tool_args: dict[str, Any], result: ToolMessage, session: str):
        del tool_name, tool_args, result, session
        return text

    return build


def test_registered_injectors_is_a_copy():
    injector = ToolResultInjector("a", _builder("A"))
    trc.register(injector)
    copied = trc.registered_injectors()
    copied.clear()

    assert trc.registered_injectors() == {"a": injector}


def test_priority_is_low_first():
    trc.register(ToolResultInjector("high", _builder("H"), priority=90))
    trc.register(ToolResultInjector("low", _builder("L"), priority=10))

    assert trc.resolve_injections("terminal", {}, _result(), "s") == ["L", "H"]


def test_tools_filter_limits_an_injector():
    trc.register(ToolResultInjector("r", _builder("R"), tools_filter=frozenset({"read_file"})))

    assert trc.resolve_injections("read_file", {}, _result(), "s") == ["R"]
    assert trc.resolve_injections("terminal", {}, _result(), "s") == []


def test_only_if_changed_dedupes_per_injector_and_tool():
    trc.register(ToolResultInjector("c", _builder("same"), only_if_changed=True))

    assert trc.resolve_injections("read_file", {}, _result(), "s") == ["same"]
    assert trc.resolve_injections("read_file", {}, _result(), "s") == []
    assert trc.resolve_injections("terminal", {}, _result(), "s") == ["same"]


def test_a_broken_builder_is_skipped(monkeypatch: pytest.MonkeyPatch):
    def boom(*_args: Any, **_kwargs: Any):
        raise RuntimeError("broken")

    trc.register(ToolResultInjector("bad", boom))
    trc.register(ToolResultInjector("good", _builder("G")))

    assert trc.resolve_injections("terminal", {}, _result(), "s") == ["G"]


def test_clear_session_drops_only_that_sessions_change_state():
    trc.register(ToolResultInjector("c", _builder("same"), only_if_changed=True))
    trc.resolve_injections("terminal", {}, _result(), "s1")
    trc.resolve_injections("terminal", {}, _result(), "s2")

    trc.clear_session("s1")

    assert trc.resolve_injections("terminal", {}, _result(), "s1") == ["same"]
    assert trc.resolve_injections("terminal", {}, _result(), "s2") == []
