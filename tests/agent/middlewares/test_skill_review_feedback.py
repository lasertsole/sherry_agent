"""Bounded feedback collection for the skill-review review_mod."""

from __future__ import annotations

import pytest

import agent.middlewares.summarization.background_reviews as reviews

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


@pytest.fixture(autouse=True)
def _no_real_usage_sidecar(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("agent.tools.pub_base.skill_usage.load_usage", lambda: {})


def _row(role: str, content: str = "", *, origin: str | None = None, tool_calls=None):
    return {
        "role": role,
        "content": content,
        "origin": origin,
        "tool_calls": tool_calls,
    }


def test_collects_user_corrections_and_loaded_skills(monkeypatch):
    rows = [
        _row("human", "use passlib, not bcrypt", origin="user"),
        _row("human", "subagent completion carrier", origin="subagent_completion"),
        _row("human", "<system-reminder source='todo_block'>x</system-reminder>"),
        _row("human", "[SYSTEM DIRECTIVE: continue]"),
        _row(
            "ai",
            tool_calls=[
                {"name": "skill_view", "args": {"name": "alpha"}},
                {"name": "read_file", "args": {"path": "x"}},
            ],
        ),
        _row(
            "ai",
            tool_calls=[
                {
                    "function": {
                        "name": "skill_view",
                        "arguments": '{"name": "beta"}',
                    }
                }
            ],
        ),
    ]
    monkeypatch.setattr(
        "context_engine.store.core.get_messages_by_lastest_n_turns",
        lambda *_args, **_kwargs: rows,
    )
    monkeypatch.setattr(
        "skills.loader.scan_skills",
        lambda: [
            {"name": "alpha", "description": "Alpha skill", "when_to_use": "alpha work"},
            {"name": "beta", "description": "Beta skill", "when_to_use": "beta work"},
        ],
    )

    feedback = reviews._collect_skill_review_feedback("s1")

    assert feedback["user_corrections"] == ["use passlib, not bcrypt"]
    assert {item["name"] for item in feedback["loaded_skills"]} == {"alpha", "beta"}
    assert (
        next(item for item in feedback["loaded_skills"] if item["name"] == "alpha")["description"]
        == "Alpha skill"
    )


def test_feedback_caps_items_and_chars(monkeypatch):
    monkeypatch.setitem(reviews.BACKGROUND_REVIEW_CONFIG, "skill_review_feedback_max_items", 2)
    monkeypatch.setitem(reviews.BACKGROUND_REVIEW_CONFIG, "skill_review_feedback_chars", 20)
    rows = [_row("human", f"correction-{index}-" + "x" * 30, origin="user") for index in range(5)]
    monkeypatch.setattr(
        "context_engine.store.core.get_messages_by_lastest_n_turns",
        lambda *_args, **_kwargs: rows,
    )

    feedback = reviews._collect_skill_review_feedback("s1")

    assert len(feedback["user_corrections"]) <= 2
    assert sum(len(item) for item in feedback["user_corrections"]) <= 20
    assert feedback["loaded_skills"] == []


def test_feedback_read_failure_is_fail_open(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise RuntimeError("store down")

    monkeypatch.setattr("context_engine.store.core.get_messages_by_lastest_n_turns", _boom)

    assert reviews._collect_skill_review_feedback("s1") == {
        "loaded_skills": [],
        "user_corrections": [],
    }
