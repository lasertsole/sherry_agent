"""A model-drafted empty todo must not abort the turn.

Live evidence: two consecutive turns died with
``ValueError: todo content must be a non-empty string`` raised out of the store
validator — langgraph's default tool-error handler only converts its own
``ToolInvocationError``, so a tool's own exception tears the whole run down.
``todowrite`` therefore validates the payload itself and answers TEXT the model
can act on, leaving the store untouched.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent.tools.todolist import service
from agent.tools.todolist.tools.todowrite import todowrite

pytestmark = [pytest.mark.unit]


@pytest.mark.asyncio
async def test_an_empty_content_is_refused_as_text_and_nothing_is_written(isolated_db):
    result = await todowrite.ainvoke(
        {"todos": [{"content": "  ", "status": "pending"}], "session_id": "s1"}
    )

    assert isinstance(result, str) and result.startswith("todowrite refused:")
    assert "item #0" in result
    # The refusal is not a write: the store stays empty.
    assert await service.TodoService.get_todos("s1") == []


@pytest.mark.asyncio
async def test_a_non_dict_item_never_reaches_the_store(isolated_db):
    """A wrong ITEM TYPE is refused by the tool schema.

    That failure is a ``ToolInvocationError`` at the graph boundary — the one
    exception langgraph's default handler DOES convert into an error tool
    message — so it is a validation error here and a readable result there.
    """
    with pytest.raises(ValidationError):
        await todowrite.ainvoke({"todos": ["just a string"], "session_id": "s1"})

    assert await service.TodoService.get_todos("s1") == []


@pytest.mark.asyncio
async def test_a_valid_list_still_writes(isolated_db):
    result = await todowrite.ainvoke(
        {"todos": [{"content": "do the thing", "status": "pending"}], "session_id": "s1"}
    )

    assert "do the thing" in result
    stored = await service.TodoService.get_todos("s1")
    assert [item["content"] for item in stored] == ["do the thing"]


@pytest.mark.asyncio
async def test_clearing_the_list_stays_legal(isolated_db):
    """An empty LIST is a legitimate clear-all — only empty ITEMS are refused."""
    await todowrite.ainvoke({"todos": [{"content": "x", "status": "pending"}], "session_id": "s1"})
    result = await todowrite.ainvoke({"todos": [], "session_id": "s1"})

    assert "refused" not in result
    assert await service.TodoService.get_todos("s1") == []
