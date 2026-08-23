"""Requester settle-wake batch state machine: coordinates wake-up of parent agents after all children settle.

State machine: IDLE → COMPLETING → SETTLED → DONE, with rearm and retry scheduling.
"""

import asyncio
import time
from enum import Enum
from loguru import logger
from ..types.registry import SubagentRunRecord


class SettleWakeState(str, Enum):
    """States for the settle-wake lifecycle per requester session."""
    IDLE = "idle"
    COMPLETING = "completing"
    SETTLED = "settled"
    DONE = "done"


class RequesterSettleWakeBatch:
    """Manages settle-wake state per requester: tracks pending runs, transitions states, and schedules retries."""

    def __init__(self):
        self._state: dict[str, SettleWakeState] = {}
        self._pending: dict[str, list[str]] = {}
        self._rearms: dict[str, int] = {}
        self._timers: dict[str, asyncio.Task] = {}

    def register_run_for_settle(self, run_id: str, requester_session_key: str) -> None:
        """Add a run to the pending list for a requester and initialize state if needed."""
        if requester_session_key not in self._pending:
            self._pending[requester_session_key] = []
        self._pending[requester_session_key].append(run_id)
        self._state.setdefault(requester_session_key, SettleWakeState.IDLE)

    def transition_batch(self, requester_session_key: str, event: str) -> SettleWakeState:
        """Apply a state transition event for a requester's batch, returning the new state."""
        current = self._state.get(requester_session_key, SettleWakeState.IDLE)

        if current == SettleWakeState.IDLE and event == "child_completed":
            self._state[requester_session_key] = SettleWakeState.COMPLETING
            return SettleWakeState.COMPLETING

        if current == SettleWakeState.COMPLETING and event == "all_settled":
            self._state[requester_session_key] = SettleWakeState.SETTLED
            return SettleWakeState.SETTLED

        if current == SettleWakeState.SETTLED and event == "woke":
            self._state[requester_session_key] = SettleWakeState.DONE
            return SettleWakeState.DONE

        if current == SettleWakeState.DONE and event == "new_child":
            self._state[requester_session_key] = SettleWakeState.COMPLETING
            return SettleWakeState.COMPLETING

        return current

    async def complete_batch(self, requester_session_key: str) -> bool:
        """Check if all descendants are settled; if so, transition and wake the parent. Returns True on success."""
        from ..registry.queries import count_active_descendant_runs
        active = count_active_descendant_runs(requester_session_key)

        if active == 0:
            self.transition_batch(requester_session_key, "all_settled")
            try:
                from ..registry import wake_yield_if_all_children_settled
                woke = await wake_yield_if_all_children_settled(requester_session_key)
                if woke:
                    self.transition_batch(requester_session_key, "woke")
                    return True
            except Exception as e:
                logger.debug("Settle wake failed for {}: {}", requester_session_key, e)
        return False

    def schedule_settle_wake_retry(self, requester_session_key: str, delay: float = 5.0) -> None:
        """Schedule a delayed retry of complete_batch for a requester; skips if one is already pending."""
        existing = self._timers.get(requester_session_key)
        if existing and not existing.done():
            return

        self._rearms[requester_session_key] = self._rearms.get(requester_session_key, 0) + 1

        async def _retry():
            await asyncio.sleep(delay)
            self._timers.pop(requester_session_key, None)
            await self.complete_batch(requester_session_key)

        self._timers[requester_session_key] = asyncio.create_task(_retry())

    def retire_after_settle(self, requester_session_key: str) -> None:
        """Clean up all state for a requester after settle-wake completes, and persist."""
        self._state.pop(requester_session_key, None)
        self._pending.pop(requester_session_key, None)
        self._rearms.pop(requester_session_key, None)
        timer = self._timers.pop(requester_session_key, None)
        if timer and not timer.done():
            timer.cancel()
        self._persist_state()

    def get_pending_state(self) -> dict[str, dict]:
        """Return a serializable snapshot of all non-DONE requester states."""
        result = {}
        for key, state in self._state.items():
            if state != SettleWakeState.DONE:
                result[key] = {
                    "state": state.value,
                    "pending_run_ids": self._pending.get(key, []),
                    "rearms": self._rearms.get(key, 0),
                }
        return result

    def restore_pending_state(self, data: dict[str, dict]) -> None:
        """Restore settle-wake state from a previously serialized snapshot."""
        for key, entry in data.items():
            self._state[key] = SettleWakeState(entry.get("state", "idle"))
            self._pending[key] = entry.get("pending_run_ids", [])
            self._rearms[key] = entry.get("rearms", 0)

    def _persist_state(self) -> None:
        """Persist current pending state to SQLite for crash recovery."""
        try:
            from .store_sqlite import save_settle_wake_state
            save_settle_wake_state(self.get_pending_state())
        except Exception as e:
            logger.debug("Failed to persist settle-wake state: {}", e)

    def load_persisted_state(self) -> None:
        """Load and restore previously persisted settle-wake state from SQLite."""
        try:
            from .store_sqlite import load_settle_wake_state
            data = load_settle_wake_state()
            if data:
                self.restore_pending_state(data)
        except Exception as e:
            logger.debug("Failed to load settle-wake state: {}", e)


_settle_wake_batch = RequesterSettleWakeBatch()


def get_settle_wake_batch() -> RequesterSettleWakeBatch:
    """Return the singleton RequesterSettleWakeBatch instance."""
    return _settle_wake_batch
