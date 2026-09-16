"""Agent-side ``PromptDataProvider`` implementation (assembly seam).

Owns the concrete :class:`runtime.data_provider.PromptDataProvider` that the
workspace prompt builder and the context-engine consumers resolve at call time.
Every method is a thin, call-time import of the real implementation, so:

* the registry stays a pure interface + assembly seam (no logic duplication —
  the real stores/helpers stay in their original modules), and
* test monkeypatches of the underlying module attributes keep intercepting the
  calls exactly as they did when ``workspace.prompt_builder`` imported them
  lazily.

Registered by ``agent.core.init()`` (called once by ``server/__main__.py``) so
the workspace/context_engine layers never need to import ``agent``.
"""

from __future__ import annotations

from runtime import data_provider


class AgentPromptDataProvider:
    """Forwarding implementation owned by the agent layer."""

    def get_todos(self, session_id: str) -> list[dict]:
        """Return the session's todo rows from the todolist registry."""
        from agent.tools.todolist.registry.store_sqlite import get_todos_sync

        return get_todos_sync(session_id)

    def requester_session_key(self, session_id: str) -> str:
        """Build the canonical requester session key."""
        from agent.tools.taskflow.tools._shared import requester_session_key

        return requester_session_key(session_id)

    def get_active_flows(self) -> list[dict]:
        """Return every non-terminal taskflow row."""
        from agent.tools.taskflow.registry import store_sqlite

        return store_sqlite.get_active_flows_sync()

    def step_status(self, step: dict) -> str:
        """Return a taskflow step's status."""
        from agent.tools.taskflow.tools._shared import step_status

        return step_status(step)

    def steps_summary(self, steps: list[dict]) -> dict[str, int]:
        """Count taskflow steps per status."""
        from agent.tools.taskflow.tools._shared import steps_summary

        return steps_summary(steps)

    def format_memory_for_system_prompt(self, target: str) -> str | None:
        """Return the frozen memory snapshot block for ``memory`` / ``user``."""
        from agent.tools.memory import memory_store

        return memory_store.format_for_system_prompt(target)

    def get_facts_listing(self) -> str:
        """Summarise the non-empty tiered-facts files."""
        from agent.tools.memory_tiered import get_tiered_store

        return get_tiered_store().get_facts_listing()

    def add_fact(self, category: str, fact: str) -> dict:
        """Append one extracted fact to the tiered store."""
        from agent.tools.memory_tiered import get_tiered_store

        return get_tiered_store().add_fact(category, fact)

    def build_todolist_knowledge_block(self, session_id: str) -> str:
        """Render the current plan's knowledge summary."""
        from agent.tools.todolist.knowledge.prompt_block import build_knowledge_block

        return build_knowledge_block(session_id)

    def build_continuity_prompt(self, session_id: str) -> str:
        """Render the previous session's continuity block."""
        from context_engine.session_continuity import build_continuity_prompt

        return build_continuity_prompt(session_id)

    def build_system_prompt(self, session_id: str) -> str:
        """Rebuild the full system prompt for ``session_id``."""
        from workspace.prompt_builder import build_system_prompt

        return build_system_prompt(session_id=session_id)


def register_prompt_data_provider() -> None:
    """Install the agent-side provider (last registration wins; idempotent)."""
    provider: data_provider.PromptDataProvider = AgentPromptDataProvider()
    data_provider.set_prompt_data_provider(provider)
