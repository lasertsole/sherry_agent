"""Unit tests for ``is_user_message`` — the positive user-content test.

The transcript carries several injected HumanMessages (reminders, notices,
continuation directives, completion carriers, cron results). Only a message the
server stamped with ``metadata.origin == "user"`` is user content; everything
else is fail-closed.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from pub.func.message.classify import is_user_message

pytestmark = [pytest.mark.unit]


def test_a_real_user_message_is_user_content():
    assert is_user_message(HumanMessage(content="hello", metadata={"origin": "user"})) is True


def test_injected_human_messages_are_not_user_content():
    injected = [
        {"origin": "project_dir", "internal": True, "provenance": "workspace_notice"},
        {"origin": "git_head", "internal": True, "provenance": "workspace_notice"},
        {"origin": "todo_continuation", "internal": True, "provenance": "continuation"},
        {"origin": "task_intent", "internal": True},
        {"origin": "subagent_completion", "internal": True},
        {"origin": "cron", "internal": True},
        {"origin": "todo_block", "internal": True, "provenance": "reminder_catalog"},
    ]

    assert all(
        is_user_message(HumanMessage(content="x", metadata=meta)) is False for meta in injected
    )


def test_no_metadata_and_unknown_origins_are_not_user_content():
    assert is_user_message(HumanMessage(content="legacy")) is False
    assert is_user_message(HumanMessage(content="x", metadata={"origin": "someone"})) is False
    assert is_user_message(HumanMessage(content="x", metadata={"internal": True})) is False


def test_non_human_messages_are_not_user_content():
    # Even a stray origin tag cannot make a model message user-authored.
    assert is_user_message(AIMessage(content="hi", metadata={"origin": "user"})) is False
    assert is_user_message(object()) is False
