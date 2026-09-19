"""Unit tests for ``origin`` persistence in the message store (store/core.py).

Covers the full-coverage origin contract:

- A human row without an explicit ``metadata.origin`` (and without the frozen
  completion-carrier contract) persists ``origin = "user"``.
- An explicit ``metadata.origin`` is persisted verbatim (``"user"`` /
  ``"task_intent"`` / ...), so every entry can positively identify its source.
- The frozen subagent-completion contract (``internal is True`` AND
  ``provenance == "subagent_completion"``) persists
  ``origin = "subagent_completion"``.
- AI / tool rows persist ``origin IS NULL`` — origin describes the human
  request source only.
- ``get_session_ids`` title derivation only accepts user-origin human rows:
  carriers and TaskIntent injections are excluded.
- Read paths (``get_history_by_turn_page``, SELECT *) surface the new
  ``origin`` field automatically.

Contract: ``origin`` TEXT — full origin semantics (``"user"`` /
``"task_intent"`` / ``"subagent_completion"`` / ...); NULL is legacy
compatibility for rows written before origin tagging and is read as a user
message. Plain string, no JSON, no backfill of old rows.
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
    """Human rows get an explicit origin; the write path covers all roles."""

    @pytest.mark.asyncio
    async def test_full_contract_carrier_origin_is_subagent_completion(self, store_db):
        """HumanMessage with the full metadata contract → origin tag lands."""
        await store_core.add_messages("s_tag", [_carrier_message()])

        row = store_db.execute("SELECT origin FROM messages WHERE session_id = 's_tag'").fetchone()
        assert row is not None
        assert row["origin"] == "subagent_completion"

    @pytest.mark.asyncio
    async def test_explicit_origin_persists_verbatim(self, store_db):
        """An explicit metadata.origin (TaskIntent) is persisted unchanged."""
        task_intent = HumanMessage(
            content="[SYSTEM DIRECTIVE: ARM]",
            metadata={"origin": "task_intent", "internal": True},
        )
        await store_core.add_messages("s_intent", [task_intent])

        row = store_db.execute(
            "SELECT origin FROM messages WHERE session_id = 's_intent'"
        ).fetchone()
        assert row["origin"] == "task_intent"

    @pytest.mark.asyncio
    async def test_explicit_user_origin_persists(self, store_db):
        """The entry-stamped user origin lands as 'user' (not NULL)."""
        await store_core.add_messages(
            "s_explicit_user", [HumanMessage("hi", metadata={"origin": "user"})]
        )

        row = store_db.execute(
            "SELECT origin FROM messages WHERE session_id = 's_explicit_user'"
        ).fetchone()
        assert row["origin"] == "user"

    @pytest.mark.asyncio
    async def test_unmarked_human_defaults_to_user_ai_tool_null(self, store_db):
        """human without metadata → 'user'; ai/tool rows stay NULL."""
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
        assert by_role["human"] == "user"
        assert by_role["ai"] is None
        assert by_role["tool"] is None


class TestContractMismatchFallsBackToUser:
    """Partial completion contracts never tag 'subagent_completion'."""

    @pytest.mark.asyncio
    async def test_provenance_without_internal_origin_is_user(self, store_db):
        """Right provenance but internal not True → partial contract → user."""
        partial = HumanMessage("hi", metadata={"provenance": "subagent_completion"})
        await store_core.add_messages("s_partial", [partial])

        row = store_db.execute(
            "SELECT origin FROM messages WHERE session_id = 's_partial'"
        ).fetchone()
        assert row["origin"] == "user"

    @pytest.mark.asyncio
    async def test_internal_truthy_string_origin_is_user(self, store_db):
        """``internal`` must be True (bool), not merely truthy — strict check."""
        strict = HumanMessage(
            "hi",
            metadata={"internal": "true", "provenance": "subagent_completion"},
        )
        await store_core.add_messages("s_truthy", [strict])

        row = store_db.execute(
            "SELECT origin FROM messages WHERE session_id = 's_truthy'"
        ).fetchone()
        assert row["origin"] == "user"

    @pytest.mark.asyncio
    async def test_internal_without_provenance_origin_is_user(self, store_db):
        """internal=True but provenance missing/wrong → user, not carrier."""
        wrong = HumanMessage("hi", metadata={"internal": True, "provenance": "something_else"})
        await store_core.add_messages("s_wrong_prov", [wrong])

        row = store_db.execute(
            "SELECT origin FROM messages WHERE session_id = 's_wrong_prov'"
        ).fetchone()
        assert row["origin"] == "user"


class TestSessionTitleExcludesOrigin:
    """The title query only accepts user-origin human rows."""

    @pytest.mark.asyncio
    async def test_title_ignores_carrier_row(self, store_db):
        """[user "hello", later carrier] → title comes from "hello"."""
        await store_core.add_messages("s_title", [HumanMessage("hello"), AIMessage("hi there")])
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

    @pytest.mark.asyncio
    async def test_title_prefers_user_over_later_task_intent(self, store_db):
        """A later TaskIntent injection must not override the user question."""
        await store_core.add_messages("s_intent_title", [HumanMessage("real question")])
        await store_core.add_messages(
            "s_intent_title",
            [
                HumanMessage(
                    content="[SYSTEM DIRECTIVE: ORCHESTRATOR MODE ARMED]",
                    metadata={"origin": "task_intent", "internal": True},
                )
            ],
        )

        sessions = {s["session_id"]: s for s in store_core.get_session_ids()}
        assert sessions["s_intent_title"]["title"] == "real question"

    @pytest.mark.asyncio
    async def test_title_reads_explicit_user_origin_row(self, store_db):
        """An entry-stamped origin='user' row is a valid title source."""
        await store_core.add_messages(
            "s_user_origin", [HumanMessage("stamped question", metadata={"origin": "user"})]
        )

        sessions = {s["session_id"]: s for s in store_core.get_session_ids()}
        assert sessions["s_user_origin"]["title"] == "stamped question"


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
        assert by_content["user question"] == "user"
        assert by_content["[subagent:researcher completed]\nTask result text"] == (
            "subagent_completion"
        )
        assert by_content["reply"] is None
