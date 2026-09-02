"""Unit tests for ``origin`` persistence in the message store (store/core.py).

Plan: subagent-origin-tagging, Task 3 — TDD RED->GREEN.

Covers:
- ``add_messages`` tags a human row ``origin = "subagent_completion"`` ONLY
  when the message carries the FULL frozen completion-metadata contract
  (``internal is True`` AND ``provenance == "subagent_completion"``) — the
  same contract built by ``agent/tools/subagent/announce/completion_message.py``
  and judged by ``_is_internal_completion``
  (``agent/middlewares/subagent_completion_drain.py``).
- Every other row (plain human, partial-contract human, ai, tool) persists
  ``origin IS NULL`` — never an empty string.
- ``get_session_ids`` title derivation excludes origin-tagged rows:
  ``[user "hello", carrier]`` → title "hello"; carrier-only session →
  title "" (client renders an i18n placeholder).
- Read paths (``get_history_by_turn_page``, SELECT *) surface the new
  ``origin`` field automatically.

Contract (decisions.md): ``origin`` TEXT NULL — NULL = real user message;
``"subagent_completion"`` = background subagent-completion injection. Plain
string, no JSON, no backfill of old rows.
"""

from __future__ import annotations

import sqlite3

import pytest

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from context_engine.store import core as store_core
from context_engine.store.db import _migrate

pytestmark = pytest.mark.unit


def _carrier_message(
    content: str = "[subagent:researcher completed]\nTask result text",
    run_id: str = "run-123",
) -> HumanMessage:
    """A carrier HumanMessage with the FROZEN completion metadata contract.

    Exact mirror of the metadata dict built by
    ``completion_message.build_completion_message`` (READ-ONLY reference).
    """
    return HumanMessage(
        content=content,
        metadata={
            "internal": True,
            "provenance": "subagent_completion",
            "run_id": run_id,
            "status": "completed",
        },
    )


@pytest.fixture()
def store_db(monkeypatch, tmp_path) -> sqlite3.Connection:
    """An isolated, fully migrated DB wired into store.core._db.

    Mirrors ``get_db()``'s key settings (row_factory, autocommit) against a
    throwaway file so the production DB is never touched. ``store/core.py``
    resolves ``_db`` at call time from its module globals, so patching the
    attribute redirects every store function in the module.
    """
    conn = sqlite3.connect(
        tmp_path / "mes_memory_test.db",
        check_same_thread=False,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    _migrate(conn)
    monkeypatch.setattr(store_core, "_db", conn)
    yield conn
    conn.close()


class TestOriginTagging:
    """Full-contract human rows get tagged; the write path covers all roles."""

    @pytest.mark.asyncio
    async def test_full_contract_carrier_origin_is_subagent_completion(self, store_db):
        """HumanMessage with the full metadata contract → origin tag lands."""
        await store_core.add_messages("s_tag", [_carrier_message()])

        row = store_db.execute(
            "SELECT origin FROM messages WHERE session_id = 's_tag'"
        ).fetchone()
        assert row is not None
        assert row["origin"] == "subagent_completion"

    @pytest.mark.asyncio
    async def test_ai_and_tool_rows_origin_is_null(self, store_db):
        """ai/tool row dicts must carry ``origin`` too (INSERT column list)."""
        await store_core.add_messages(
            "s_roles",
            [
                HumanMessage("question"),
                AIMessage("answer"),
                ToolMessage(content="ok", name="read", tool_call_id="call_1"),
            ],
        )

        rows = store_db.execute(
            "SELECT role, origin FROM messages WHERE session_id = 's_roles' ORDER BY id"
        ).fetchall()
        by_role = {r["role"]: r["origin"] for r in rows}
        assert set(by_role) == {"human", "ai", "tool"}
        # NULL (never empty string) for every non-carrier role.
        assert by_role["ai"] is None
        assert by_role["tool"] is None
        assert by_role["human"] is None


class TestNullOrigin:
    """Partial / missing contracts must NOT tag — partial match stays NULL."""

    @pytest.mark.asyncio
    async def test_plain_human_origin_is_null(self, store_db):
        """A plain HumanMessage (no metadata) → origin IS NULL."""
        await store_core.add_messages("s_plain", [HumanMessage("hello")])

        row = store_db.execute(
            "SELECT origin FROM messages WHERE session_id = 's_plain'"
        ).fetchone()
        assert row["origin"] is None

    @pytest.mark.asyncio
    async def test_provenance_without_internal_origin_is_null(self, store_db):
        """Right provenance but internal not True → partial contract, NULL."""
        partial = HumanMessage(
            "hi",
            metadata={"provenance": "subagent_completion"},
        )
        await store_core.add_messages("s_partial", [partial])

        row = store_db.execute(
            "SELECT origin FROM messages WHERE session_id = 's_partial'"
        ).fetchone()
        assert row["origin"] is None

    @pytest.mark.asyncio
    async def test_internal_truthy_string_origin_is_null(self, store_db):
        """``internal`` must be True (bool), not merely truthy — strict check."""
        strict = HumanMessage(
            "hi",
            metadata={"internal": "true", "provenance": "subagent_completion"},
        )
        await store_core.add_messages("s_truthy", [strict])

        row = store_db.execute(
            "SELECT origin FROM messages WHERE session_id = 's_truthy'"
        ).fetchone()
        assert row["origin"] is None

    @pytest.mark.asyncio
    async def test_internal_without_provenance_origin_is_null(self, store_db):
        """internal=True but provenance missing/wrong → NULL."""
        wrong = HumanMessage(
            "hi",
            metadata={"internal": True, "provenance": "something_else"},
        )
        await store_core.add_messages("s_wrong_prov", [wrong])

        row = store_db.execute(
            "SELECT origin FROM messages WHERE session_id = 's_wrong_prov'"
        ).fetchone()
        assert row["origin"] is None


class TestSessionTitleExcludesOrigin:
    """The title query must ignore origin-tagged human rows."""

    @pytest.mark.asyncio
    async def test_title_ignores_carrier_row(self, store_db):
        """[user "hello", later carrier] → title comes from "hello"."""
        await store_core.add_messages(
            "s_title", [HumanMessage("hello"), AIMessage("hi there")]
        )
        # Carrier lands in a LATER turn (drain middleware injects per turn).
        await store_core.add_messages("s_title", [_carrier_message()])

        sessions = {s["session_id"]: s for s in store_core.get_session_ids()}
        assert sessions["s_title"]["title"] == "hello"

    @pytest.mark.asyncio
    async def test_carrier_only_session_title_empty(self, store_db):
        """A session whose only human row is a carrier → title "" (placeholder)."""
        await store_core.add_messages("s_carrier_only", [_carrier_message()])

        sessions = {s["session_id"]: s for s in store_core.get_session_ids()}
        assert sessions["s_carrier_only"]["title"] == ""


class TestHistoryRowsExposeOrigin:
    """Read paths use SELECT * — the origin field rides along automatically."""

    @pytest.mark.asyncio
    async def test_history_rows_include_origin_field(self, store_db):
        await store_core.add_messages(
            "s_hist",
            [HumanMessage("user question"), _carrier_message(), AIMessage("reply")],
        )

        rows = store_core.get_history_by_turn_page(
            "s_hist", min_turn_num=1, turn_page_size=10, turn_page_num=1
        )
        assert rows, "expected history rows"
        for row in rows:
            assert "origin" in row, "SELECT * must surface the origin column"

        by_content = {row["content"]: row["origin"] for row in rows}
        assert by_content["user question"] is None
        assert by_content["[subagent:researcher completed]\nTask result text"] == (
            "subagent_completion"
        )
        assert by_content["reply"] is None
