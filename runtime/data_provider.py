"""Process-level prompt data provider registry for dependency inversion (leaf module).

The system prompt is assembled by ``workspace`` from data owned by ``agent``
(todos, taskflow registry rows, memory files, the todolist
knowledge block) and by ``context_engine`` (cross-session continuity).
``context_engine`` needs the same taskflow rows, and the
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

The write side of the same seam: the curator (``context_engine``) consolidates
agent-created skills through primitives owned by
``agent.tools.skill_tools.skill_manage``. :class:`SkillWriteProvider` forwards
those calls so ``context_engine`` never imports ``agent``. A missing skill
writer degrades the curator's mutation paths to a logged no-op — never a
partial write, never a partial delete — while a registered writer's exceptions
surface unchanged.

Leaf module by contract: standard library only, no project imports, and it
never calls the provider itself. Registration is last-writer-wins, so repeated
assembly is idempotent.
"""

from __future__ import annotations

from typing import Any, Protocol

__all__ = [
    "PromptDataProvider",
    "SkillWriteProvider",
    "clear_prompt_data_provider",
    "clear_skill_write_provider",
    "get_prompt_data_provider",
    "get_skill_write_provider",
    "set_prompt_data_provider",
    "set_skill_write_provider",
]


class PromptDataProvider(Protocol):
    """Read-side data forwarding interface for prompt assembly and extraction.

    Method contracts (the registered agent-side implementation must provide
    these exact signatures; the workspace/context_engine consumers depend on
    them):

    Todos / taskflow (``agent.tools.todolist`` + ``agent.tools.taskflow``)
        :meth:`get_todos` returns the session's todo rows;
        :meth:`requester_session_key` builds the canonical
        ``agent:main:session:<id>`` key; :meth:`get_active_flows` returns the
        session's non-terminal taskflow rows (SQL-scoped, no post-filtering);
        :meth:`step_status` / :meth:`steps_summary` are the taskflow step
        state helpers used to render progress.

    Memory (``agent.tools.memory``)
        :meth:`format_memory_for_system_prompt` returns the frozen memory
        snapshot block for ``"memory"`` / ``"user"`` (``None`` when empty).

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

    def get_active_flows(self, session_id: str) -> list[dict]:
        """Return the session's non-terminal taskflow rows (``[]`` when none)."""
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


class SkillWriteProvider(Protocol):
    """Write-side forwarding interface for curator skill mutations.

    Method contracts (the registered agent-side implementation must provide
    these exact signatures; the curator depends on them):

    :meth:`create_skill` / :meth:`write_file`
        Thin forwarders onto ``skill_manage._create_skill`` /
        ``skill_manage._write_file``. Both return the same result dict the
        underlying helper returns (the curator inspects ``success`` and logs
        ``error``), and both keep the helper's validation intact.

    :meth:`split_oversized_skill` / :meth:`umbrella_skill_char_target`
        The shared umbrella-content budget seam. The splitter is the pure
        (no-IO) helper that trims a generated SKILL.md body into
        ``references/partNN.md`` files, returning ``(slim, merged_files)``;
        the target is the config-backed character budget it defaults to and
        the prompt hint quotes.
    """

    def create_skill(self, name: str, content: str) -> dict[str, Any]:
        """Create a skill; returns ``skill_manage._create_skill``'s result dict."""
        ...

    def write_file(self, name: str, file_path: str, file_content: str) -> dict[str, Any]:
        """Write one supporting file; returns ``skill_manage._write_file``'s result dict."""
        ...

    def split_oversized_skill(
        self,
        main_content: str,
        target: int,
        supporting_files: dict[str, str] | None = None,
    ) -> tuple[str, dict[str, str]]:
        """Split an oversized SKILL.md body; pure, no IO."""
        ...

    def umbrella_skill_char_target(self) -> int:
        """Return the shared umbrella SKILL.md character budget."""
        ...


_skill_writer: SkillWriteProvider | None = None


def set_skill_write_provider(provider: SkillWriteProvider) -> None:
    """Bind the process-wide skill writer (last registration wins; idempotent)."""
    global _skill_writer
    _skill_writer = provider


def get_skill_write_provider() -> SkillWriteProvider | None:
    """Return the registered skill writer, or ``None`` when nothing was registered."""
    return _skill_writer


def clear_skill_write_provider() -> None:
    """Drop the skill-writer registration (no-op when absent); for test isolation."""
    global _skill_writer
    _skill_writer = None
