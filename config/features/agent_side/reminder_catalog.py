"""Reminder-catalog budgets: the injection layer's total and default limits."""

from typing import TypedDict


class ReminderCatalogConfig(TypedDict):
    """Injection budgets for the dynamic ``<system-reminder>`` layer.

    The system prompt holds only STABLE content (persona, memory, skills
    index); live state (todos, TaskFlow board, boulder, continuity, knowledge,
    project directory) is injected as reminders. These are the bounds on that
    layer — per-source caps live in the source registration, the total is what
    a turn may spend on reminders overall.
    """

    enabled: bool
    #: Total characters of reminders ONE model call may carry. Over budget, the
    #: lowest-priority sources are dropped (never truncated mid-thought) — a
    #: turn with many live views degrades to the important ones.
    total_budget_chars: int
    #: Default cooldown (turns) for a source that does not set its own. 0 = the
    #: source may inject on every turn it has something to say.
    default_cooldown_turns: int


REMINDER_CATALOG: ReminderCatalogConfig = {
    "enabled": True,
    # 30 000 chars ≈ 7 500 tokens in the 200K+ window: generous enough for every
    # source at once (measured worst case ~15K), small enough that a runaway
    # source cannot eat the context.
    "total_budget_chars": 30_000,
    "default_cooldown_turns": 0,
}
