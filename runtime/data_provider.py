"""Process-level prompt data provider registry for dependency inversion (leaf module).

The system prompt is assembled by ``workspace`` from data owned by ``agent``
(todos, taskflow registry rows, memory files, tiered facts, the todolist
knowledge block) and by ``context_engine`` (cross-session continuity).
``context_engine`` needs the same taskflow rows and tiered-facts store, and the
curator needs the workspace system-prompt builder. Importing across those
boundaries is the ``agent <-> context_engine`` / ``agent <-> workspace`` /
``context_engine <-> workspace`` cycle this registry exists to break.

The owner (``agent``) registers one :class:`PromptDataProvider` at assembly
time (``agent.core.init()``, called by the server entry point); consumers
resolve it at call time and degrade to empty data when nothing is registered
(evals, unit tests, any process that never assembled the agent). A registered
provider's exceptions are never swallowed by this module: the consumers call it
directly, so a broken store still surfaces exactly as it did before the
migration. Each consumer logs its first miss once (mirroring
``runtime.hooks``), so a missing prompt block stays diagnosable.

Leaf module by contract: standard library only, no project imports, and it
never calls the provider itself. Registration is last-writer-wins, so repeated
assembly is idempotent.
"""

from __future__ import annotations

from typing import Protocol

__all__ = [
    "PromptDataProvider",
    "clear_prompt_data_provider",
    "get_prompt_data_provider",
    "set_prompt_data_provider",
]


class PromptDataProvider(Protocol):
    """Read-side data forwarding interface for prompt assembly and extraction.

    Method contracts (the registered agent-side implementation must provide
    these exact signatures; the workspace/context_engine consumers depend on
    them):

    Todos / taskflow (``agent.tools.todolist`` + ``agent.tools.taskflow``)
        :meth:`get_todos` returns the session's todo rows;
        :meth:`requester_session_key` builds the canonical
        ``agent:main:session:<id>`` key; :meth:`get_active_flows` returns all
        non-terminal taskflow rows (callers filter by creator key);
        :meth:`step_status` / :meth:`steps_summary` are the taskflow step
        state helpers used to render progress.

    Memory / facts (``agent.tools.memory`` + ``agent.tools.memory_tiered``)
        :meth:`format_memory_for_system_prompt` returns the frozen memory
        snapshot block for ``"memory"`` / ``"user"`` (``None`` when empty);
        :meth:`get_facts_listing` returns the one-line non-empty facts
        listing; :meth:`add_fact` appends one extracted fact.

    Prompt blocks
        :meth:`build_todolist_knowledge_block` renders the plan knowledge
        summary; :meth:`build_continuity_prompt` renders the previous
        session's end state; :meth:`build_system_prompt` rebuilds the full
        system prompt (used by the curator's cache refresh).
    """

    def get_todos(self, session_id: str) -> list[dict]:
        """Return the session's todo rows (``[]`` when none)."""
        ...

    def requester_session_key(self, session_id: str) -> str:
        """Build the canonical requester session key for a raw session id."""
        ...

    def get_active_flows(self) -> list[dict]:
        """Return every non-terminal taskflow row (caller filters by session)."""
        ...

    def step_status(self, step: dict) -> str:
        """Return a taskflow step's status (derived for legacy steps)."""
        ...

    def steps_summary(self, steps: list[dict]) -> dict[str, int]:
        """Count taskflow steps per status (all statuses zero-filled)."""
        ...

    def format_memory_for_system_prompt(self, target: str) -> str | None:
        """Return the frozen memory block for ``memory`` / ``user``, else ``None``."""
        ...

    def get_facts_listing(self) -> str:
        """Summarise non-empty tiered-facts files (``""`` when none)."""
        ...

    def add_fact(self, category: str, fact: str) -> dict:
        """Append one fact to the tiered store; returns the store's result dict."""
        ...

    def build_todolist_knowledge_block(self, session_id: str) -> str:
        """Render the current plan's knowledge summary (``""`` when none)."""
        ...

    def build_continuity_prompt(self, session_id: str) -> str:
        """Render the previous session's continuity block (``""`` when none)."""
        ...

    def build_system_prompt(self, session_id: str) -> str:
        """Rebuild the full system prompt for ``session_id``."""
        ...


_provider: PromptDataProvider | None = None


def set_prompt_data_provider(provider: PromptDataProvider) -> None:
    """Bind the process-wide provider (last registration wins; idempotent)."""
    global _provider
    _provider = provider


def get_prompt_data_provider() -> PromptDataProvider | None:
    """Return the registered provider, or ``None`` when nothing was registered."""
    return _provider


def clear_prompt_data_provider() -> None:
    """Drop the registration (no-op when absent); for test/teardown isolation."""
    global _provider
    _provider = None
