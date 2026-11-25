"""Reviewer acceptance-checklist extraction and dispatch injection."""

from __future__ import annotations

import pytest

from agent.tools.taskflow.checklist_extractor import (
    ACCEPTANCE_CHECKLIST_LABEL,
    augment_task_with_checklist,
    extract_acceptance_checklist,
)
from agent.tools.taskflow.tools import _dispatch
from config.features import TASKFLOW_INFRA

pytestmark = [pytest.mark.unit]


# -- extraction --------------------------------------------------------------


def test_extract_checklist_basic():
    task = (
        "Fix the JSON parser. It must handle UTF-8 BOM. "
        "Reject empty charset. Raise ValueError on invalid encoding."
    )
    clauses = extract_acceptance_checklist(task)
    assert len(clauses) == 3
    assert any("UTF-8 BOM" in c for c in clauses)
    assert any("Reject empty charset" in c for c in clauses)
    assert any("Raise ValueError" in c for c in clauses)


def test_extract_checklist_no_keywords():
    assert extract_acceptance_checklist("Refactor the module for readability") == []


def test_extract_checklist_empty_task():
    assert extract_acceptance_checklist("") == []
    assert extract_acceptance_checklist("   ") == []


def test_extract_checklist_max_20():
    task = "\n".join(f"You must handle case {i} correctly" for i in range(25))
    assert len(extract_acceptance_checklist(task)) == 20


def test_extract_checklist_case_insensitive():
    assert len(extract_acceptance_checklist("MUST work")) == 1
    assert len(extract_acceptance_checklist("must work")) == 1
    assert len(extract_acceptance_checklist("Must work")) == 1


def test_extract_checklist_dedupes():
    assert extract_acceptance_checklist("Must work.\nMust work.") == ["Must work"]


def test_augment_is_idempotent():
    once = augment_task_with_checklist("Must work", ["Must work"])
    assert ACCEPTANCE_CHECKLIST_LABEL in once
    assert augment_task_with_checklist(once, ["Must work"]) == once


def test_augment_empty_clauses_leaves_text():
    assert augment_task_with_checklist("Must work", []) == "Must work"


def test_checklist_config_enabled():
    assert TASKFLOW_INFRA["reviewer_checklist_enabled"] is True


# -- dispatch integration ----------------------------------------------------


class _FakeResult:
    status = "accepted"
    child_session_key = "agent:main:subagent:child-1"
    error = None


async def _fake_spawn(**kwargs):
    _fake_spawn.calls.append(kwargs)  # type: ignore[attr-defined]
    return _FakeResult()


_fake_spawn.calls = []  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def _reset_calls():
    _fake_spawn.calls = []  # type: ignore[attr-defined]
    yield


@pytest.fixture(autouse=True)
def _stub_spawn(monkeypatch: pytest.MonkeyPatch):
    import agent.tools.subagent as subagent_mod

    monkeypatch.setattr(subagent_mod, "spawn_subagent_direct", _fake_spawn)


@pytest.mark.asyncio
async def test_dispatch_appends_checklist_for_reviewer(monkeypatch, real_child_dispatch):
    task = "Fix parser. It must handle BOM. Reject empty charset."
    key = await _dispatch.dispatch_child(task, "agent:main:session:s1", functional_role="reviewer")

    assert key == "agent:main:subagent:child-1"
    spawned_task = _fake_spawn.calls[0]["task"]  # type: ignore[attr-defined]
    assert spawned_task.startswith(task)
    assert ACCEPTANCE_CHECKLIST_LABEL in spawned_task
    assert "1. It must handle BOM" in spawned_task
    assert "2. Reject empty charset" in spawned_task


@pytest.mark.asyncio
async def test_dispatch_no_checklist_for_executor(real_child_dispatch):
    task = "Fix parser. It must handle BOM."
    await _dispatch.dispatch_child(task, "agent:main:session:s1", functional_role="executor")

    assert _fake_spawn.calls[0]["task"] == task  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_dispatch_no_checklist_when_no_normative_clauses(real_child_dispatch):
    task = "Refactor the module for readability"
    await _dispatch.dispatch_child(task, "agent:main:session:s1", functional_role="reviewer")

    assert _fake_spawn.calls[0]["task"] == task  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_dispatch_checklist_disabled(monkeypatch, real_child_dispatch):
    monkeypatch.setitem(TASKFLOW_INFRA, "reviewer_checklist_enabled", False)
    task = "Fix parser. It must handle BOM."
    await _dispatch.dispatch_child(task, "agent:main:session:s1", functional_role="reviewer")

    assert _fake_spawn.calls[0]["task"] == task  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_dispatch_no_duplicate_when_task_already_has_checklist(real_child_dispatch):
    task = "Fix parser. It must handle BOM.\n\nACCEPTANCE CHECKLIST — existing:\n1. foo"
    await _dispatch.dispatch_child(task, "agent:main:session:s1", functional_role="reviewer")

    assert _fake_spawn.calls[0]["task"] == task  # type: ignore[attr-defined]
