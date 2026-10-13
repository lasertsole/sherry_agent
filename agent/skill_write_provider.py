"""Agent-side ``SkillWriteProvider`` implementation (assembly seam).

Owns the concrete :class:`runtime.data_provider.SkillWriteProvider` that the
curator (``context_engine``) resolves at call time when it consolidates
agent-created skills. Every method is a thin, call-time import of the real
``skill_manage`` primitive, so:

* the registry stays a pure interface + assembly seam (no logic duplication —
  validation, atomic writes and size guards stay in ``skill_manage``), and
* test monkeypatches of ``skill_manage`` attributes keep intercepting the
  calls exactly as they did when the curator imported them lazily.

Registered by ``agent.core.init()`` (called once by ``server/__main__.py``) so
``context_engine`` never needs to import ``agent``.
"""

from __future__ import annotations

from typing import Any

from runtime import data_provider


class AgentSkillWriteProvider:
    """Forwarding implementation owned by the agent layer."""

    def create_skill(self, name: str, content: str) -> dict[str, Any]:
        """Create a skill through ``skill_manage._create_skill``."""
        from agent.tools.skill_tools.skill_manage import _create_skill

        return _create_skill(name, content)

    def write_file(self, name: str, file_path: str, file_content: str) -> dict[str, Any]:
        """Write a supporting file through ``skill_manage._write_file``."""
        from agent.tools.skill_tools.skill_manage import _write_file

        return _write_file(name, file_path, file_content)

    def split_oversized_skill(
        self,
        main_content: str,
        target: int,
        supporting_files: dict[str, str] | None = None,
    ) -> tuple[str, dict[str, str]]:
        """Split an oversized SKILL.md body through the shared helper."""
        from agent.tools.skill_tools.skill_manage import split_oversized_skill

        return split_oversized_skill(main_content, target, supporting_files)

    def umbrella_skill_char_target(self) -> int:
        """Return the ``skill_manage`` umbrella SKILL.md character budget."""
        from agent.tools.skill_tools.skill_manage import _UMBRELLA_SKILL_CHAR_TARGET

        return _UMBRELLA_SKILL_CHAR_TARGET


def register_skill_write_provider() -> None:
    """Install the agent-side skill writer (last registration wins; idempotent)."""
    provider: data_provider.SkillWriteProvider = AgentSkillWriteProvider()
    data_provider.set_skill_write_provider(provider)
