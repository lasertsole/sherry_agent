"""Source message links for cards (prototype ``src/app/links.py``)."""

from __future__ import annotations

from typing import Any

from superagent.core.model import ChannelAddress


def message_link(addr: ChannelAddress | None, registry: Any = None) -> dict[str, str]:
    """Resolve a human-facing link and label for a channel address."""
    if addr is None:
        return {}
    if registry is not None:
        return registry.link(addr)
    return {"url": str(addr.address.get("url", "")), "label": addr.thread}
