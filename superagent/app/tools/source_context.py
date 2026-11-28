"""Source context extraction (prototype ``src/app/source_context.py``)."""

from __future__ import annotations

from superagent.core.model import Trigger


def source_context_of(trigger: Trigger) -> dict[str, object]:
    """Return the minimal Source context carried by a Trigger."""
    return {
        "source": trigger.source,
        "threads": [{"source": ref.source, "id": ref.id} for ref in trigger.threads],
        "channel": trigger.channel.address if trigger.channel else {},
        "url": trigger.url,
    }
