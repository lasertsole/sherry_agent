"""system.notice push: frame shape, validation and best-effort contract."""

from __future__ import annotations

import json
from typing import Any

import pytest

from runtime.session import system_notice

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []

    async def fake_broadcast(session_id: str, payload: dict[str, Any], **_kwargs: Any) -> int:
        captured.append({"session_id": session_id, "payload": payload})
        return 1

    monkeypatch.setattr(system_notice, "broadcast_to_session", fake_broadcast)
    return captured


async def test_frame_shape_and_defaults(sent: list[dict[str, Any]]):
    content = await system_notice.push_system_notice("sess-1", title="Build done")

    assert sent[0]["session_id"] == "sess-1"
    frame = sent[0]["payload"]
    assert frame["event"] == "system_notice"
    assert frame["session_id"] == "sess-1"
    assert content == frame["content"]
    assert content == {"title": "Build done", "content": "", "level": "info", "source": "external"}


async def test_fields_are_cleaned_and_capped(sent: list[dict[str, Any]]):
    await system_notice.push_system_notice(
        " sess-1 ",
        title="  " + "t" * 200 + "  ",
        content="c" * 5000,
        level="nope",
        source="  ",
    )

    content = sent[0]["payload"]["content"]
    assert len(content["title"]) == 120
    assert content["title"] == "t" * 120
    assert len(content["content"]) == 2000
    assert content["level"] == "info"  # unknown level degrades, not fails
    assert content["source"] == "external"  # blank source keeps the default


async def test_valid_level_and_source_pass_through(sent: list[dict[str, Any]]):
    await system_notice.push_system_notice(
        "s", title="t", content="c", level="progress", source="tool:terminal"
    )

    content = sent[0]["payload"]["content"]
    assert content["level"] == "progress"
    assert content["source"] == "tool:terminal"


async def test_blank_session_is_a_no_op(sent: list[dict[str, Any]]):
    result = await system_notice.push_system_notice("  ", title="t")

    assert result == {}
    assert sent == []


async def test_broadcast_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch):
    async def broken(session_id: str, payload: dict[str, Any], **_kwargs: Any) -> int:
        raise RuntimeError("socket gone")

    monkeypatch.setattr(system_notice, "broadcast_to_session", broken)

    # Must not raise — a notice never breaks its producer.
    content = await system_notice.push_system_notice("s", title="t")
    assert content["title"] == "t"


async def test_frame_serializes_for_the_wire(sent: list[dict[str, Any]]):
    await system_notice.push_system_notice(
        "s", title="标题", content="内容", level="success", source="plugin:demo"
    )

    text = json.dumps(sent[0]["payload"], ensure_ascii=False)
    assert "标题" in text and "plugin:demo" in text
