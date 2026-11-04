"""Action-scoped idempotency metadata for grouped read/write tools."""

import pytest

from agent.tools.todolist.knowledge.knowledge_tool import knowledge

pytestmark = [pytest.mark.unit]


def test_knowledge_lists_only_its_read_actions_as_repeatable():
    assert knowledge.metadata["idempotent"] is False
    assert knowledge.metadata["idempotent_actions"] == ("read", "list")
    assert "write" not in knowledge.metadata["idempotent_actions"]
