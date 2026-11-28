"""Read-only panel views."""

from __future__ import annotations

from superagent.panel.views.overview import overview
from superagent.panel.views.records import owners_of, ticket_row, trigger_row
from superagent.panel.views.settings import page as settings_page
from superagent.panel.views.sources import source_state
from superagent.panel.views.text import markdown, plain, preview, readable
from superagent.panel.views.threads import search, thread_row, timeline
from superagent.panel.views.tickets import by_stage, detail

__all__ = [
    "by_stage",
    "detail",
    "markdown",
    "overview",
    "owners_of",
    "plain",
    "preview",
    "readable",
    "search",
    "settings_page",
    "source_state",
    "thread_row",
    "ticket_row",
    "timeline",
    "trigger_row",
]
