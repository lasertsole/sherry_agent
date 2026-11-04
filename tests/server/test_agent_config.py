"""Agent catalogue + per-session agent config (预设 的子代理模型 / 技能 两栏).

The built-in tool set and the middleware chain are build-time contracts: every
main tool is always available and every registered middleware always runs, so
the session payload carries only ``subagent_models`` and ``skills``.

Covers the three seams: the catalogue the client renders (roles + skills), the
sanitizer that rejects unknown names and dropped required skills, and the write
path that parks a mid-turn choice until the turn boundary (the model/thinking
twin).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

# The subagent conftest installs a minimal ``skills.loader`` stub when the real
# module has not been imported yet. This suite needs the real catalogue, so
# replace a stub left behind by an earlier-collected test.
if "skills.loader" in sys.modules and not hasattr(sys.modules["skills.loader"], "skills_catalog"):
    sys.modules.pop("skills.loader", None)
import skills.loader  # noqa: E402,F401

_ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").exists())

from runtime.session.state_keys import StateKey
from server.service import agent_config_service as service
from server.trigger.http import agent_config as agent_config_http

pytestmark = [pytest.mark.module, pytest.mark.timeout(60)]


@pytest.fixture
def real_skill_names(monkeypatch):
    """Real skill names, minus the per-process snapshot cache.

    ``scan_skills`` reads ``skills_snapshot.json`` (a process-level cache of the
    live tree); the sanitizer tests only need A stable name set, so the fixture
    resolves the catalogue once and the tests patch ``_skill_names`` with it.
    """
    from skills.loader import skills_catalog

    names = {entry["name"] for entry in skills_catalog()}
    if not names:  # pragma: no cover - a tree without skills is a broken checkout
        names = {"clawhub"}
    return names


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
    # Both tiers: the service's system-prompt invalidation walks them (a real-db
    # write from a test would be exactly the isolation failure the fixture exists
    # to prevent).
    monkeypatch.setattr(service, "state_register_mem", mem, raising=False)
    monkeypatch.setattr(service, "state_register_db", db, raising=False)
    monkeypatch.setattr(settings_service, "state_register_mem", mem)
    monkeypatch.setattr(settings_service, "state_register_db", db)
    monkeypatch.setattr(settings_service, "is_session_busy", lambda sid: False)
    return mem, db


# ----------------------------------------------------------------------
# catalogue
# ----------------------------------------------------------------------


def test_the_catalog_serves_roles_and_skills_only():
    """The tool / middleware lists were removed with the configurable presets."""
    import json

    response = asyncio.run(agent_config_http.get_agent_catalog_handler(_FakeRequest()))

    assert response.status_code == 200
    data = json.loads(response.description)
    assert data["success"] is True
    assert "tools" not in data, "every built-in tool is always available"
    assert "middlewares" not in data, "the middleware chain is not configurable"
    assert any(role["role"] == "researcher" for role in data["subagent_roles"])
    # The Skills tab renders from the same response: name + description + the
    # Built-in / Third-party split, never a client-side hardcoded name.
    assert data["skills"], "the catalogue must carry the skill list"
    assert all({"name", "description", "builtin"} <= set(entry) for entry in data["skills"])


# ----------------------------------------------------------------------
# sanitizer
# ----------------------------------------------------------------------


def _skill_selection(*optional: str) -> list[str]:
    """A VALID ``skills`` payload: the required media chain plus the given names."""
    from skills.loader import REQUIRED_SKILLS

    return [*sorted(REQUIRED_SKILLS), *optional]


def test_sanitize_accepts_an_empty_payload_and_rejects_unknown_fields():
    assert service.sanitize_agent_config({}) == {}

    with pytest.raises(service.AgentConfigError):
        service.sanitize_agent_config({"unknown_field": 1})

    # The removed knobs are DROPPED so a preset saved by an older client still
    # re-applies instead of 400ing (no migration needed).
    assert service.sanitize_agent_config({"tools": ["read_file"]}) == {}
    assert service.sanitize_agent_config({"middlewares_disabled": ["TaskIntentMiddleware"]}) == {}
    assert service.sanitize_agent_config({"middleware_options": {}}) == {}


def test_sanitize_accepts_a_role_profile():
    cleaned = service.sanitize_agent_config(
        {
            "subagent_models": {
                "researcher": {"model": "deepseek-chat", "provider": "deepseek"},
                "executor": None,
            },
        }
    )

    assert cleaned["subagent_models"]["researcher"]["model"] == "deepseek-chat"
    assert cleaned["subagent_models"]["researcher"]["max_output_tokens"] == "8192"
    assert cleaned["subagent_models"]["executor"] is None


def test_sanitize_normalizes_a_role_profile_output_cap():
    cleaned = service.sanitize_agent_config(
        {
            "subagent_models": {
                "researcher": {"model": "m", "max_output_tokens": " 16384 "},
                "executor": {"model": "m", "max_output_tokens": 32768},
            }
        }
    )

    assert cleaned["subagent_models"]["researcher"]["max_output_tokens"] == "16384"
    assert cleaned["subagent_models"]["executor"]["max_output_tokens"] == "32768"


@pytest.mark.parametrize("bad", ["8191", "0", "-1", "abc", "1.5", True, []])
def test_sanitize_rejects_a_bad_role_profile_output_cap(bad):
    with pytest.raises(service.AgentConfigError) as exc:
        service.sanitize_agent_config(
            {"subagent_models": {"researcher": {"model": "m", "max_output_tokens": bad}}}
        )

    assert "max_output_tokens" in str(exc.value)


def test_sanitize_rejects_a_role_profile_without_a_model():
    with pytest.raises(service.AgentConfigError) as exc:
        service.sanitize_agent_config({"subagent_models": {"executor": {"provider": "x"}}})

    assert "subagent_models.executor" in str(exc.value)

    with pytest.raises(service.AgentConfigError):
        service.sanitize_agent_config({"subagent_models": {"executor": {"model": "m", "q": 1}}})


def test_sanitize_rejects_an_unknown_role():
    with pytest.raises(service.AgentConfigError) as exc:
        service.sanitize_agent_config({"subagent_models": {"wizard": None}})
    assert "wizard" in str(exc.value)


def test_sanitize_accepts_a_skill_selection_and_an_empty_one(real_skill_names, monkeypatch):
    from skills.loader import REQUIRED_SKILLS

    monkeypatch.setattr(service, "_skill_names", lambda: real_skill_names)
    picked = _skill_selection("clawhub")

    cleaned = service.sanitize_agent_config({"skills": [*picked, *sorted(REQUIRED_SKILLS)]})

    # Deduplicated, caller order preserved…
    assert cleaned["skills"] == picked
    # …and "only the required chain" is the legal minimum (nothing else).
    assert service.sanitize_agent_config({"skills": sorted(REQUIRED_SKILLS)}) == {
        "skills": sorted(REQUIRED_SKILLS)
    }
    assert service.sanitize_agent_config({"skills": None}) == {"skills": None}


def _required_skill_names() -> set[str]:
    from skills.loader import REQUIRED_SKILLS

    return set(REQUIRED_SKILLS)


def test_sanitize_requires_the_multimedia_skills(real_skill_names, monkeypatch):
    from skills.loader import REQUIRED_SKILLS

    monkeypatch.setattr(service, "_skill_names", lambda: real_skill_names)
    with pytest.raises(service.AgentConfigError) as exc:
        service.sanitize_agent_config({"skills": ["clawhub"]})

    message = str(exc.value)
    assert "required skill(s) cannot be dropped" in message
    for name in sorted(REQUIRED_SKILLS):
        assert name in message, name

    # Including them is accepted verbatim (order preserved).
    payload = [*sorted(REQUIRED_SKILLS), "clawhub"]
    assert service.sanitize_agent_config({"skills": payload}) == {"skills": payload}


def test_sanitize_rejects_an_unknown_skill_name(real_skill_names, monkeypatch):
    monkeypatch.setattr(service, "_skill_names", lambda: real_skill_names)
    with pytest.raises(service.AgentConfigError) as exc:
        service.sanitize_agent_config({"skills": ["not_a_skill"]})

    assert "not_a_skill" in str(exc.value)
    with pytest.raises(service.AgentConfigError):
        service.sanitize_agent_config({"skills": "alpha"})


# ----------------------------------------------------------------------
# write path: live vs parked
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_idle_session_writes_live_and_clears_the_parked_key(registers):
    payload = {"skills": _skill_selection("clawhub")}
    cleaned, pending = await service.apply_agent_config_choice("sess-a", payload)

    assert pending is False
    assert cleaned == payload
    state = service.get_agent_config_state("sess-a")
    assert state == {"config": payload, "pending": False}


@pytest.mark.asyncio
async def test_a_busy_session_parks_the_choice_until_promotion(registers, monkeypatch):
    import server.service.session_settings_service as settings_service

    monkeypatch.setattr(settings_service, "_session_turn_active", lambda _sid: True)
    payload = {"subagent_models": {"researcher": {"model": "deepseek-chat"}}}

    _cleaned, pending = await service.apply_agent_config_choice("sess-b", payload)

    assert pending is True
    # Effective state already reports the parked choice for the NEXT turn...
    state = service.get_agent_config_state("sess-b")
    assert state["pending"] is True
    assert state["config"]["subagent_models"]["researcher"]["model"] == "deepseek-chat"

    # ...and the turn boundary promotes it into the live key.
    promoted = settings_service.promote_pending_settings_sync("sess-b")
    assert "agent_config" in promoted
    promoted_state = service.get_agent_config_state("sess-b")
    assert promoted_state["pending"] is False
    assert promoted_state["config"]["subagent_models"]["researcher"]["model"] == "deepseek-chat"


def test_get_agent_config_state_is_empty_for_an_unset_session(registers):
    assert service.get_agent_config_state("sess-none") == {"config": {}, "pending": False}


def test_get_agent_config_state_drops_the_removed_tool_and_middleware_keys(registers):
    """A preset saved by an older build must not leak dead switches to a reader."""
    mem, _db = registers
    mem.set_state(
        "sess-legacy",
        StateKey.AGENT_CONFIG,
        {
            "tools": ["taskflow_create", "web_search"],
            "middlewares_disabled": ["TaskIntentMiddleware"],
            "middleware_options": {"Summarization": {"nudge": False}},
            "skills": ["clawhub"],
        },
    )

    state = service.get_agent_config_state("sess-legacy")

    assert state["config"] == {"skills": ["clawhub"]}


@pytest.mark.asyncio
async def test_a_live_write_clears_the_cached_system_prompt(registers):
    """The skills selection lives inside the cached prompt: a live write drops it."""
    mem, db = registers
    mem.set_state("sess-p", str(StateKey.SYSTEM_PROMPT), "CACHED-PROMPT")
    db.set_state("sess-p", str(StateKey.SYSTEM_PROMPT), "CACHED-PROMPT")

    _cleaned, pending = await service.apply_agent_config_choice(
        "sess-p", {"skills": sorted(_required_skill_names())}
    )

    assert pending is False
    assert mem.get_state("sess-p", str(StateKey.SYSTEM_PROMPT), None) is None
    assert db.get_state("sess-p", str(StateKey.SYSTEM_PROMPT), None) is None


@pytest.mark.asyncio
async def test_a_parked_write_clears_it_at_promotion(registers, monkeypatch):
    """Mid-turn the cache must survive (the live key is still the old value)."""
    import server.service.session_settings_service as settings_service

    mem, db = registers
    monkeypatch.setattr(settings_service, "_session_turn_active", lambda _sid: True)
    mem.set_state("sess-q", str(StateKey.SYSTEM_PROMPT), "CACHED-PROMPT")

    _cleaned, pending = await service.apply_agent_config_choice(
        "sess-q", {"skills": _skill_selection("clawhub")}
    )

    assert pending is True
    assert mem.get_state("sess-q", str(StateKey.SYSTEM_PROMPT), None) == "CACHED-PROMPT"

    promoted = settings_service.promote_pending_settings_sync("sess-q")

    assert "agent_config" in promoted
    assert mem.get_state("sess-q", str(StateKey.SYSTEM_PROMPT), None) is None


# ----------------------------------------------------------------------
# HTTP boundary
# ----------------------------------------------------------------------


def test_put_handler_rejects_an_unknown_field_with_400(registers):
    request = _FakeRequest(body={"session_id": "sess-h", "config": {"nope": 1}})

    response = asyncio.run(agent_config_http.put_agent_config_handler(request))

    assert response.status_code == 400
    assert "nope" in response.description


def test_put_handler_round_trips_a_valid_config(registers):
    import json

    payload = {"subagent_models": {"researcher": {"model": "deepseek-chat"}}}

    request = _FakeRequest(body={"session_id": "sess-ok", "config": payload})

    response = asyncio.run(agent_config_http.put_agent_config_handler(request))

    assert response.status_code == 200
    stored = json.loads(response.description)["config"]
    assert stored["subagent_models"]["researcher"]["model"] == "deepseek-chat"
    assert stored["subagent_models"]["researcher"]["max_output_tokens"] == "8192"


def test_get_handler_requires_a_session_id():
    response = asyncio.run(agent_config_http.get_agent_config_handler(_FakeRequest()))

    assert response.status_code == 400
