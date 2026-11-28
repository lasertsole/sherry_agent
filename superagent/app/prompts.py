"""Prompt configuration (prototype ``src/app/prompts.ts``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from superagent.app.versions import Versions
from superagent.limits import MAX_TRIGGER_TEXT


@dataclass
class PromptConfig:
    """Editable prompt configuration."""

    team_sections: list[str] = field(default_factory=list)
    builtin_parts: dict[str, str] = field(default_factory=dict)
    skills: list[str] = field(default_factory=list)
    skill_prefix: str = "skill:"
    variables: dict[str, str] = field(default_factory=dict)

    def assemble(self) -> str:
        """Assemble the final prompt in stable order."""
        parts = [*self.team_sections, *self.builtin_parts.values()]
        parts.extend(f"{self.skill_prefix}{skill}" for skill in self.skills)
        text = "\n\n".join(part for part in parts if part)
        return text[:MAX_TRIGGER_TEXT]


class Prompts:
    """Versioned prompt config with default-change detection."""

    BUILT_IN = ("admission", "delivery", "worker")

    def __init__(self, versions: Versions | None = None) -> None:
        self.versions = versions or Versions()
        self.defaults: dict[str, str] = {name: self._default_text(name) for name in self.BUILT_IN}

    def current(self) -> dict[str, Any]:
        """Return the current config, revision and default diffs."""
        saved = self.versions.get("prompts")
        config = PromptConfig(**(saved.value or {}))
        return {
            "revision": saved.revision,
            "config": config.__dict__,
            "defaults": self.defaults,
            "changed": [
                name
                for name, text in self.defaults.items()
                if name in config.builtin_parts and config.builtin_parts[name] != text
            ],
            "sections": self.BUILT_IN,
        }

    def save(self, value: dict[str, Any], revision: int, base_revision: int | None = None) -> int:
        """Save prompt config with stale-revision guards."""
        if base_revision is not None and base_revision != self.versions.get("prompts").revision:
            from superagent.core.errors import UserError

            raise UserError("stale_revision", {"block": "prompts"})
        return self.versions.save("prompts", value).revision

    @staticmethod
    def _default_text(name: str) -> str:
        from superagent.app.tools.context import builtin_context

        return builtin_context(name)


__all__ = ["PromptConfig", "Prompts"]
