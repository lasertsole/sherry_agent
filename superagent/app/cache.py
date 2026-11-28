"""Bounded LRU text cache (prototype ``src/app/cache.ts``)."""

from __future__ import annotations

from collections import OrderedDict


class TextCache:
    """LRU cache that silently drops entries beyond its character budget."""

    def __init__(self, max_chars: int) -> None:
        self._max_chars = max_chars
        self._items: OrderedDict[str, str] = OrderedDict()
        self._chars = 0

    def get(self, key: str) -> str | None:
        """Return a value and refresh its LRU position."""
        if key not in self._items:
            return None
        value = self._items.pop(key)
        self._items[key] = value
        return value

    def set(self, key: str, value: str) -> None:
        """Insert a value, evicting LRU entries to stay within budget."""
        if key in self._items:
            old = self._items.pop(key)
            self._chars -= len(old)
        self._items[key] = value
        self._chars += len(value)
        while self._chars > self._max_chars and self._items:
            _, old = self._items.popitem(last=False)
            self._chars -= len(old)

    def __len__(self) -> int:
        """Return the number of cached entries."""
        return len(self._items)
