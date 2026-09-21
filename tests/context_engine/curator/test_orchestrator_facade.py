"""Facade contract for the split curator orchestrator.

The orchestrator was decomposed into focused siblings; the public API and the
test-pinned private surface must stay reachable through
``context_engine.curator.orchestrator`` (patch points included).
"""

import pytest

from context_engine.curator import orchestrator

pytestmark = [pytest.mark.unit]

_PUBLIC_API = (
    "CURATOR_DRY_RUN_BANNER",
    "CURATOR_REVIEW_PROMPT",
    "maybe_run_curator",
    "run_curator_review",
)

_PATCHED_SURFACE = (
    "_apply_consolidation",
    "_archive_consolidated_sources",
    "_archive_pruned_skills",
    "_generate_umbrella_skill",
    "_merge_umbrella_skills",
    "_migrate_source_files",
    "_provider_misses_logged",
    "_resolve_skill_dir",
    "_schedule_system_prompt_refresh",
    "_write_supporting_files",
    "run_curator_review",
)


@pytest.mark.parametrize("name", _PUBLIC_API)
def test_public_api_reexported(name: str) -> None:
    assert hasattr(orchestrator, name)


@pytest.mark.parametrize("name", _PATCHED_SURFACE)
def test_patched_private_surface_exists(name: str) -> None:
    assert hasattr(orchestrator, name), f"orchestrator.{name} is a patch point and must stay bound"


def test_resolve_skill_dir_is_the_canonical_helper() -> None:
    from context_engine.curator import helpers

    assert orchestrator._resolve_skill_dir is helpers._skill_dir


def test_moved_implementations_live_in_siblings() -> None:
    from context_engine.curator import migration, refresh, review, run_state, umbrella

    assert review.CURATOR_REVIEW_PROMPT is orchestrator.CURATOR_REVIEW_PROMPT
    assert run_state._ReviewRun is orchestrator._ReviewRun
    assert umbrella._generate_umbrella_skill is orchestrator._generate_umbrella_skill
    assert migration._archive_pruned_skills is orchestrator._archive_pruned_skills
    assert refresh._schedule_system_prompt_refresh is orchestrator._schedule_system_prompt_refresh
