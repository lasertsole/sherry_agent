"""The knowledge prompt block is retired in favour of skill-quality nudges."""

from __future__ import annotations

import pytest

import workspace.prompt_builder as prompt_builder

pytestmark = [pytest.mark.unit]


def test_the_knowledge_prompt_block_is_gone():
    assert not hasattr(prompt_builder, "_build_knowledge_block")


def test_the_reminder_catalog_has_no_knowledge_source():
    from agent.middlewares.reminder_catalog.core import registered_sources
    from agent.middlewares.reminder_catalog.sources import register_default_sources

    register_default_sources()
    assert "knowledge_index" not in registered_sources()
