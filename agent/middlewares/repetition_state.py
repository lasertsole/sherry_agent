"""Session-scoped repetition-guard state over ``state_register_mem``.

The per-session keys below are the single source of truth for the
``OutputRepetitionGuard`` middleware and the ``RepetitionGuardWrapper``
stream layer (both import them from the guard module, which re-exports
everything defined here).
"""

from __future__ import annotations

from runtime.state_register import StateRegisterMeM

# ---------------------------------------------------------------------------
# Per-session state keys (stored in ``state_register_mem``).
#
# * ``_HISTORY_KEY``           -- rolling hashes of recent *visible output*
#   contents, used for cross-call identical-output detection.
# * ``_WARN_COUNT_KEY``        -- (reserved) cumulative warning counter; reset
#   at the start of every agent turn by the middleware's before_agent hook.
# * ``_INTERNAL_WARNED_KEY``   -- whether an internal-repetition warning has
#   already been issued for the *visible output* this session, so we warn at
#   most once.
# * ``_HALTED_KEY``            -- whether a hard HALT was already forced this
#   turn; when set, every subsequent model call returns the halt message.
# * ``_REASONING_HISTORY_KEY`` -- like ``_HISTORY_KEY`` but for *reasoning*
#   text (CoT / ``<think>`` chains) so output and reasoning loops are tracked
#   independently.
# * ``_REASONING_WARNED_KEY``  -- like ``_INTERNAL_WARNED_KEY`` but scoped to
#   reasoning text.
# ---------------------------------------------------------------------------
_HISTORY_KEY = "output_repetition_history"
_WARN_COUNT_KEY = "output_repetition_warn_count"
_INTERNAL_WARNED_KEY = "output_repetition_internal_warned"
_HALTED_KEY = "output_repetition_halted"
_REASONING_HISTORY_KEY = "output_repetition_reasoning_history"
_REASONING_WARNED_KEY = "output_repetition_reasoning_warned"

# The 6 per-session state keys owned by this middleware, exposed publicly so
# the subagent teardown path can release exactly these (and only these) keys
# via ``delete_state`` when a subagent-derived agent is destroyed -- without
# clobbering the other middlewares' top-level session state.
SESSION_STATE_KEYS: tuple[str, ...] = (
    _HISTORY_KEY,
    _WARN_COUNT_KEY,
    _INTERNAL_WARNED_KEY,
    _HALTED_KEY,
    _REASONING_HISTORY_KEY,
    _REASONING_WARNED_KEY,
)

# Maximum number of content hashes kept per session before trimming.
_MAX_HISTORY = 30


class RepetitionState:
    """Read/write access to the session-scoped repetition-guard state.

    The register is injected so callers control which ``state_register_mem``
    binding the state goes through (tests patch the guard module's global).
    """

    def __init__(self, register: StateRegisterMeM):
        self._register = register

    def reset(self, session_id: str) -> None:
        """Clear all per-session repetition state at the start of each turn.

        Clears the output/reasoning history windows and resets the warning and
        halt flags so a fresh turn starts with a clean slate.
        """
        self._register.set_state(session_id, _HISTORY_KEY, [])
        self._register.set_state(session_id, _WARN_COUNT_KEY, 0)
        self._register.set_state(session_id, _INTERNAL_WARNED_KEY, False)
        self._register.set_state(session_id, _HALTED_KEY, False)
        self._register.set_state(session_id, _REASONING_HISTORY_KEY, [])
        self._register.set_state(session_id, _REASONING_WARNED_KEY, False)

    def get_history(self, session_id: str, history_key: str) -> list[str]:
        """Rolling window of dual content hashes for one stream kind."""
        return self._register.get_state(session_id, history_key, [])

    def record_hash(self, session_id: str, history_key: str, digest: str) -> None:
        """Append a hash to the rolling window, capped at ``_MAX_HISTORY``."""
        history = self.get_history(session_id, history_key)
        history.append(digest)
        if len(history) > _MAX_HISTORY:
            history = history[-_MAX_HISTORY:]
        self._register.set_state(session_id, history_key, history)

    def is_halted(self, session_id: str) -> bool:
        """Whether a hard HALT was already forced this turn."""
        return self._register.get_state(session_id, _HALTED_KEY, False)

    def mark_halted(self, session_id: str) -> None:
        """Make the halt sticky for the rest of the turn."""
        self._register.set_state(session_id, _HALTED_KEY, True)

    def is_warned(self, session_id: str, warned_key: str) -> bool:
        """Whether the once-per-session internal warning already fired."""
        return self._register.get_state(session_id, warned_key, False)

    def mark_warned(self, session_id: str, warned_key: str) -> None:
        """Consume the once-per-session internal warning slot."""
        self._register.set_state(session_id, warned_key, True)
