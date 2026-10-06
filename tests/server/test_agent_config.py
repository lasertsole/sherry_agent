"""Agent catalogue + per-session agent config（预设 工具/中间件/子代理模型 三栏）。

Stub-tolerant: when ``tests/agent/tools/subagent`` is collected in the same
process its conftest installs sys.modules stubs (including a no-op
``agent.tools.build_main_tools``), so the cases that need real tool names load
the REAL ``agent/tools/__init__.py`` under a private name — the pattern
``tests/agent/tools/taskflow/conftest.py`` established.

Covers the three seams: the catalogue the client renders (tools grouped by the
real build output, middlewares flagged required/gateable), the sanitizer that
rejects unknown names and system-required middleware, and the write path that
parks a mid-turn choice until the turn boundary (the model/thinking twin).
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").exists())
_real_tools_module = None


def _real_agent_tools():
    """Load the REAL agent.tools package under a private name (stub-tolerant)."""
    global _real_tools_module
    if _real_tools_module is None:
        spec = importlib.util.spec_from_file_location(
            "_agent_config_real_tools",
            _ROOT / "agent" / "tools" / "__init__.py",
            submodule_search_locations=[str(_ROOT / "agent" / "tools")],
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["_agent_config_real_tools"] = mod
        spec.loader.exec_module(mod)
        _real_tools_module = mod
    return _real_tools_module


def _real_builder_tools():
    """Every tool the process-wide builders produce, minus the MCP builder.

    ``build_mcp_tools`` needs the live plugin configuration, which the subagent
    suite's stubs remove — and MCP tools are dynamic anyway (they land in the
    catalogue's fallback group). Everything else is deterministic.
    """
    module = _real_agent_tools()
    builders = [b for b in module._MAIN_TOOLS_BUILDERS if b is not module.build_mcp_tools]
    return module.tool_flatten(builders)


@pytest.fixture
def real_tool_names():
    """The main-agent tool names the catalogue knows, without building them.

    Derived from the catalogue's own ``TOOL_GROUPS``: deterministic in every
    process (the builders need live config that test stubs remove) and exactly
    the set the service falls back to when ``agent.tools`` is stubbed.
    """
    from agent.tools.catalog import TOOL_GROUPS

    return {name for names_in_group in TOOL_GROUPS.values() for name in names_in_group}


from server.service import agent_config_service as service
from server.trigger.http import agent_config as agent_config_http

pytestmark = [pytest.mark.module, pytest.mark.timeout(60)]


class _FakeRequest:
    def __init__(self, query_params: dict | None = None, body: dict | None = None):
        self.query_params = query_params or {}
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class _FakeRegister:
    """State-register double with the real get/set/delete surface."""

    def __init__(self):
        self.data: dict[tuple[str, str], object] = {}

    def get_state(self, sid, key, default=None):
        return self.data.get((sid, key), default)

    def set_state(self, sid, key, value):
        self.data[(sid, key)] = value
        return True

    def delete_state(self, sid, key):
        return self.data.pop((sid, key), None) is not None


@pytest.fixture
def registers(monkeypatch):
    """Swap both registers and keep the session idle unless a test says otherwise."""
    import server.service.session_settings_service as settings_service

    mem, db = _FakeRegister(), _FakeRegister()
    monkeypatch.setattr(service, "state_register_mem", mem, raising=False)
    monkeypatch.setattr(settings_service, "state_register_mem", mem)
    monkeypatch.setattr(settings_service, "state_register_db", db)
    monkeypatch.setattr(settings_service, "is_session_busy", lambda sid: False)
    return mem, db


# ----------------------------------------------------------------------
# catalogue
# ----------------------------------------------------------------------


def test_every_main_tool_lands_in_a_named_group():
    from agent.tools.catalog import TOOL_GROUPS, tool_catalog

    entries = tool_catalog(tools=_real_builder_tools())
    names = [entry["name"] for entry in entries]
    assert len(names) == len(set(names)), "tool names must be unique"

    known = {name for names_in_group in TOOL_GROUPS.values() for name in names_in_group}
    unlabelled = sorted(set(names) - known)
    assert unlabelled == [], (
        f"tool(s) missing from agent/tools/catalog.py TOOL_GROUPS: {unlabelled}"
    )
    for name in names:
        entry = next(e for e in entries if e["name"] == name)
        assert entry["group"], f"{name} has no group"
        # The hover tooltip's text: the tool's own first description line, and
        # every shipped tool has one.
        assert entry["description"], f"{name} has no description"
        assert "\n" not in entry["description"], f"{name}'s description must be one line"
        assert len(entry["description"]) <= 240, f"{name}'s description is not bounded"


def test_the_middleware_catalog_marks_the_required_set():
    from agent.middlewares.catalog import (
        MIDDLEWARE_ORDER,
        middleware_catalog,
        middleware_gateable,
        middleware_required,
    )

    catalog = {entry["name"]: entry for entry in middleware_catalog()}
    assert list(catalog) == list(MIDDLEWARE_ORDER)

    # Safety baseline / logical necessities are locked, the optional
    # entries are the gateable ones — and the two sets never overlap.
    assert catalog["HumanInTheLoop"]["required"] is True
    assert catalog["Summarization"]["required"] is True
    assert catalog["system_prompt_injection"]["required"] is True
    assert catalog["ThinkingControlMiddleware"]["required"] is True
    assert catalog["TaskIntentMiddleware"]["required"] is False
    assert catalog["TaskIntentMiddleware"]["gateable"] is True

    # Promoted to the safety baseline (project-dir notice + media preprocessing):
    # locked in the UI, rejected by the service, always on in the chain.
    for name in ("ProjectDirNoticeMiddleware", "MultimodalProcessor"):
        assert catalog[name]["required"] is True, name
        assert catalog[name]["gateable"] is False, name

    for name in MIDDLEWARE_ORDER:
        assert not (middleware_required(name) and middleware_gateable(name)), name
        # Everything that is not required must be gateable (the UI shows the
        # rest as locked, never as a dead toggle).
        assert middleware_required(name) or middleware_gateable(name), name


# ----------------------------------------------------------------------
# sanitizer
# ----------------------------------------------------------------------


def test_sanitize_accepts_a_partial_payload_and_dedups_tools(real_tool_names, monkeypatch):
    monkeypatch.setattr(service, "_tool_names", lambda: real_tool_names)
    cleaned = service.sanitize_agent_config({"tools": ["read_file", "terminal", "read_file"]})

    assert cleaned == {"tools": ["read_file", "terminal"]}
    assert service.sanitize_agent_config({}) == {}


def test_sanitize_rejects_unknown_names(real_tool_names, monkeypatch):
    monkeypatch.setattr(service, "_tool_names", lambda: real_tool_names)
    with pytest.raises(service.AgentConfigError) as exc:
        service.sanitize_agent_config({"tools": ["not_a_tool"]})
    assert "not_a_tool" in str(exc.value)

    with pytest.raises(service.AgentConfigError) as exc:
        service.sanitize_agent_config({"middlewares_disabled": ["NopeMiddleware"]})
    assert "NopeMiddleware" in str(exc.value)

    with pytest.raises(service.AgentConfigError) as exc:
        service.sanitize_agent_config({"subagent_models": {"wizard": None}})
    assert "wizard" in str(exc.value)

    with pytest.raises(service.AgentConfigError):
        service.sanitize_agent_config({"unknown_field": 1})


def test_sanitize_refuses_to_disable_a_required_middleware():
    with pytest.raises(service.AgentConfigError) as exc:
        service.sanitize_agent_config({"middlewares_disabled": ["HumanInTheLoop"]})

    message = str(exc.value)
    assert "HumanInTheLoop" in message
    assert "cannot be disabled" in message


def test_sanitize_accepts_a_gateable_middleware_and_a_role_profile():
    cleaned = service.sanitize_agent_config(
        {
            "middlewares_disabled": ["TaskIntentMiddleware"],
            "subagent_models": {
                "researcher": {"model": "deepseek-chat", "provider": "deepseek"},
                "executor": None,
            },
        }
    )

    assert cleaned["middlewares_disabled"] == ["TaskIntentMiddleware"]
    assert cleaned["subagent_models"]["researcher"]["model"] == "deepseek-chat"
    assert cleaned["subagent_models"]["executor"] is None


def test_sanitize_rejects_a_role_profile_without_a_model():
    with pytest.raises(service.AgentConfigError) as exc:
        service.sanitize_agent_config({"subagent_models": {"executor": {"provider": "x"}}})

    assert "subagent_models.executor" in str(exc.value)

    with pytest.raises(service.AgentConfigError):
        service.sanitize_agent_config({"subagent_models": {"executor": {"model": "m", "q": 1}}})


# ----------------------------------------------------------------------
# write path: live vs parked
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_idle_session_writes_live_and_clears_the_parked_key(
    registers, real_tool_names, monkeypatch
):
    monkeypatch.setattr(service, "_tool_names", lambda: real_tool_names)
    cleaned, pending = await service.apply_agent_config_choice("sess-a", {"tools": ["read_file"]})

    assert pending is False
    assert cleaned == {"tools": ["read_file"]}
    state = service.get_agent_config_state("sess-a")
    assert state == {"config": {"tools": ["read_file"]}, "pending": False}


@pytest.mark.asyncio
async def test_a_busy_session_parks_the_choice_until_promotion(registers, monkeypatch):
    import server.service.session_settings_service as settings_service

    monkeypatch.setattr(settings_service, "_session_turn_active", lambda _sid: True)

    _cleaned, pending = await service.apply_agent_config_choice(
        "sess-b", {"middlewares_disabled": ["TaskIntentMiddleware"]}
    )

    assert pending is True
    # Effective state already reports the parked choice for the NEXT turn...
    state = service.get_agent_config_state("sess-b")
    assert state["pending"] is True
    assert state["config"]["middlewares_disabled"] == ["TaskIntentMiddleware"]

    # ...and the turn boundary promotes it into the live key.
    promoted = settings_service.promote_pending_settings_sync("sess-b")
    assert "agent_config" in promoted
    promoted_state = service.get_agent_config_state("sess-b")
    assert promoted_state["pending"] is False
    assert promoted_state["config"]["middlewares_disabled"] == ["TaskIntentMiddleware"]


def test_get_agent_config_state_is_empty_for_an_unset_session(registers):
    assert service.get_agent_config_state("sess-none") == {"config": {}, "pending": False}


# ----------------------------------------------------------------------
# HTTP boundary
# ----------------------------------------------------------------------


def test_catalog_handler_serves_the_three_lists(real_tool_names, monkeypatch):
    import json

    import agent.tools.catalog as catalog_module

    monkeypatch.setattr(
        catalog_module,
        "tool_catalog",
        lambda tools=None: [{"name": name, "group": "files"} for name in sorted(real_tool_names)],
    )

    response = asyncio.run(agent_config_http.get_agent_catalog_handler(_FakeRequest()))

    assert response.status_code == 200
    data = json.loads(response.description)
    assert data["success"] is True
    assert {entry["name"] for entry in data["tools"]} >= {"read_file", "terminal"}
    middleware_names = {entry["name"] for entry in data["middlewares"]}
    assert "HumanInTheLoop" in middleware_names
    assert any(role["role"] == "researcher" for role in data["subagent_roles"])


def test_put_handler_rejects_a_required_middleware_with_400(registers):
    request = _FakeRequest(
        body={"session_id": "sess-h", "config": {"middlewares_disabled": ["PathGuard"]}}
    )

    response = asyncio.run(agent_config_http.put_agent_config_handler(request))

    assert response.status_code == 400
    assert "PathGuard" in response.description


def test_put_handler_round_trips_a_valid_config(registers, real_tool_names, monkeypatch):
    import json

    monkeypatch.setattr(service, "_tool_names", lambda: real_tool_names)

    request = _FakeRequest(
        body={"session_id": "sess-ok", "config": {"tools": ["read_file", "terminal"]}}
    )

    response = asyncio.run(agent_config_http.put_agent_config_handler(request))

    assert response.status_code == 200
    assert json.loads(response.description)["config"] == {"tools": ["read_file", "terminal"]}


def test_get_handler_requires_a_session_id():
    response = asyncio.run(agent_config_http.get_agent_config_handler(_FakeRequest()))

    assert response.status_code == 400
