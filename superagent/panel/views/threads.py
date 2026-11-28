"""Thread views and timeline (prototype ``src/app/views/threads.ts``)."""

from __future__ import annotations

from superagent.core.model import Thread, Trigger


def thread_row(thread: Thread) -> dict[str, object]:
    """Return one Thread row."""
    return {
        "id": thread.id,
        "source": thread.source,
        "subscribed": thread.is_subscribed,
        "evaluating": thread.has_eval,
        "queued": len(thread.queued_trigger_ids),
    }


def timeline(thread: Thread, triggers: list[Trigger]) -> list[dict[str, object]]:
    """Return newest-first Trigger → Decision events for a Thread."""
    rows = [
        {
            "at": trigger.at,
            "kind": "trigger",
            "text": trigger.text[:400],
            "id": trigger.id,
        }
        for trigger in triggers
        if any(ref.id == thread.id for ref in trigger.threads)
    ]
    ordered: list[dict[str, object]] = [dict(item) for item in rows]
    return sorted(ordered, key=lambda item: str(item["at"]), reverse=True)


def search(query: str, rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Filter rows by a case-insensitive substring."""
    low = query.lower()
    return [row for row in rows if low in str(row).lower()]
