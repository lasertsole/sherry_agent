"""Panel views, timeline and i18n tests."""

from __future__ import annotations

from superagent.core.model import Report, ReportKind
from superagent.panel.locales import keys, text
from superagent.panel.views import (
    by_stage,
    detail,
    markdown,
    overview,
    owners_of,
    plain,
    preview,
    readable,
    search,
    settings_page,
    thread_row,
    timeline,
    trigger_row,
)
from tests.superagent.conftest import make_ticket, make_trigger


def test_text_views_are_safe() -> None:
    assert plain("a\nb") == "a b"
    assert preview("x" * 200, 10) == "x" * 9 + "…"
    assert markdown("# **bold**") == "bold"
    assert "<script>" not in readable("<script>alert(1)</script>")


def test_ticket_thread_and_timeline_views() -> None:
    ticket = make_ticket()
    ticket.latest_report = Report("assign-1", ReportKind.READY, "done", "t")
    assert "person/review" in by_stage([ticket])
    assert detail(ticket)["authorization"]["policy_version"] == "v1"
    trigger = make_trigger()
    assert trigger_row(trigger)["id"] == trigger.id
    assert owners_of(trigger.id, [ticket]) == [ticket.id]
    thread = type(
        "T",
        (),
        {
            "id": "thread-1",
            "source": "github",
            "is_subscribed": True,
            "has_eval": False,
            "queued_trigger_ids": [],
        },
    )()
    assert thread_row(thread)["subscribed"] is True
    assert timeline(thread, [trigger])[0]["kind"] == "trigger"
    assert search("NOPE", timeline(thread, [trigger])) == []


def test_overview_and_settings_views() -> None:
    ticket = make_ticket()
    data = overview([ticket], {"worker": {"state": "error"}}, {"running": 1}, {"ok": True})
    assert data["problems"] and data["load"]["running"] == 1
    page = settings_page({"policy": {"value": {}, "revision": 1}}, {"revision": 0})
    assert set(page["groups"]) == {"sources", "eval", "executor", "prompts", "deployment"}


def test_locales_zh_en_complete() -> None:
    assert keys("zh") == keys("en")
    assert text("overview", "zh") == "总览"
    assert text("overview", "en") == "Overview"
    assert text("missing", "en") == "missing"
