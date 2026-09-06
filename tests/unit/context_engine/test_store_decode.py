"""TDD tests for audit 3.1.5 — shared JSON-column decode helper
(context_engine/store/core.py::_decode_json_columns).

Pins the decode block both paginated/turn-scoped readers used to duplicate:
content/tool_calls/images/audios/videos JSON cells decoded, non-string cells
left alone, and the internal ``ts_ms`` ordering column popped.
"""

import json

import pytest

from context_engine.store.core import _decode_json_columns

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


def _row(**overrides):
    row = {
        "id": 1,
        "session_id": "s1",
        "turn_num": 3,
        "role": "ai",
        "content": json.dumps({"parts": ["hello"]}, ensure_ascii=False),
        "tool_calls": json.dumps([{"name": "bash", "args": {"cmd": "ls"}}], ensure_ascii=False),
        "images": json.dumps(["/img/a.png"], ensure_ascii=False),
        "audios": json.dumps(["/aud/a.mp3"], ensure_ascii=False),
        "videos": None,
        "ts_ms": 1757155200123,
    }
    row.update(overrides)
    return row


class TestDecodeJsonColumns:
    def test_decodes_all_json_cells_and_pops_ts_ms(self):
        row = _decode_json_columns(_row())

        assert row["content"] == {"parts": ["hello"]}
        assert row["tool_calls"] == [{"name": "bash", "args": {"cmd": "ls"}}]
        assert row["images"] == ["/img/a.png"]
        assert row["audios"] == ["/aud/a.mp3"]
        assert row["videos"] is None  # non-string stays as-is
        assert "ts_ms" not in row  # internal ordering column dropped

    def test_non_string_cells_left_untouched(self):
        row = _decode_json_columns(
            _row(content=json.dumps("plain text"), audios=None, videos=None)
        )

        assert row["content"] == "plain text"  # JSON-encoded string decodes
        assert row["audios"] is None  # non-string cells stay as-is
        assert row["videos"] is None

    def test_mutates_and_returns_same_dict(self):
        row = _row()
        out = _decode_json_columns(row)

        assert out is row

    def test_end_to_end_through_turn_scope_query(self, tmp_path, monkeypatch):
        """The helper is wired into the real query paths (turn-scoped read)."""
        import asyncio

        from context_engine.store import core as store_core
        from context_engine.store import db as store_db
        from langchain_core.messages import AIMessage, HumanMessage

        monkeypatch.setattr(store_db, "_db_path", tmp_path / "mes_memory.db")
        monkeypatch.setattr(store_db, "_db", None)  # force a fresh connection on tmp
        monkeypatch.setattr(store_core, "_db", store_db.get_db())

        async def _seed():
            await store_core.add_messages(
                "s1", [HumanMessage(content="q"), AIMessage(content="a")]
            )

        asyncio.run(_seed())

        rows = store_core.get_turns_by_turn_num_scope("s1", target_turn_num=1, half_scope=1)

        assert len(rows) == 2
        assert all("ts_ms" not in r for r in rows)
        assert any(r["role"] == "human" and r["content"] == "q" for r in rows)
        assert any(r["role"] == "ai" and r["content"] == "a" for r in rows)
