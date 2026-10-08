"""Async FIFO queue scheduler for swarm group run ordering."""

import asyncio
from collections import defaultdict


class SwarmFifoQueue:
    """Async-safe FIFO queue that orders run_ids within each swarm group."""

    def __init__(self):
        self._queues: dict[str, list[str]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def enqueue(self, group_id: str, run_id: str) -> int:
        """Add a run_id to the tail of the group's queue; returns new queue size."""
        async with self._lock:
            self._queues[group_id].append(run_id)
            return len(self._queues[group_id])

    async def dequeue(self, group_id: str) -> str | None:
        """Remove and return the head run_id, or None if the queue is empty."""
        async with self._lock:
            if self._queues[group_id]:
                return self._queues[group_id].pop(0)
            return None

    def size(self, group_id: str) -> int:
        """Return the number of items in the group's queue."""
        return len(self._queues[group_id])

    def peek(self, group_id: str) -> str | None:
        """Return the head run_id without removing it, or None if empty."""
        q = self._queues.get(group_id)
        if q:
            return q[0]
        return None

    async def remove(self, group_id: str, run_id: str) -> bool:
        """Remove a specific run_id from the queue; returns True if found."""
        async with self._lock:
            if run_id in self._queues[group_id]:
                self._queues[group_id].remove(run_id)
                return True
            return False


_fifo: SwarmFifoQueue | None = None  # Module-level singleton instance


def get_fifo() -> SwarmFifoQueue:
    """Return the global SwarmFifoQueue singleton, creating it on first access."""
    global _fifo
    if _fifo is None:
        _fifo = SwarmFifoQueue()
    return _fifo
