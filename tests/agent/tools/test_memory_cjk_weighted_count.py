"""Memory char limits count CJK chars by their token cost, not raw length.

ASCII stays at 1 weighted char; a CJK char costs ``chars_per_token /
chars_per_token_cjk`` (default 2) weighted chars. Every limit check and the
user-facing usage block use the weighted count, so CJK content gets the same
effective token budget as ASCII under the same numeric cap.
"""

from __future__ import annotations

import pytest

import agent.tools.memory as memory_module
from agent.tools.memory import MemoryStore
from context_engine.store.memory_entry_repository import MemoryEntryRepository

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


@pytest.fixture
def memory_repo(tmp_path, monkeypatch):
    repo = MemoryEntryRepository(db_path=tmp_path / "memory.db")
    monkeypatch.setattr(memory_module, "memory_entry_repo", repo)
    yield repo
    repo.close()


# -- unit: _weighted_char_count ---------------------------------------------


def test_weighted_char_count_ascii():
    assert MemoryStore._weighted_char_count("a" * 100) == 100


def test_weighted_char_count_cjk():
    # Default weight is chars_per_token / chars_per_token_cjk = 4 / 2 = 2.
    weight = memory_module._CJK_WEIGHT
    assert MemoryStore._weighted_char_count("中" * 100) == int(100 * weight)


def test_weighted_char_count_mixed():
    weight = memory_module._CJK_WEIGHT
    assert MemoryStore._weighted_char_count("中" * 50 + "a" * 50) == int(50 * weight + 50)


def test_weighted_char_count_empty():
    assert MemoryStore._weighted_char_count("") == 0
    assert MemoryStore._weighted_char_count(None) == 0


def test_weighted_char_count_weight_derived_from_config():
    # The module constant must track the shared token-estimation config, not a
    # hard-coded 2, so changing those constants keeps memory limits consistent.
    from config.features import TOKEN_ESTIMATION

    assert memory_module._CJK_WEIGHT == (
        TOKEN_ESTIMATION["chars_per_token"] / TOKEN_ESTIMATION["chars_per_token_cjk"]
    )


# -- integration: add / evict / display use the weighted count ---------------


def test_add_cjk_respects_weighted_limit(memory_repo):
    # A 100-char limit fits 50 CJK chars (weighted 100), not 100 CJK chars.
    store = MemoryStore(memory_char_limit=100)
    assert store.add("memory", "中" * 50)["success"] is True

    rejected = store.add("memory", "中" * 51)

    assert rejected["success"] is False
    assert store.memory_entries == ["中" * 50]


def test_add_ascii_capacity_unchanged(memory_repo):
    store = MemoryStore(memory_char_limit=100)
    assert store.add("memory", "a" * 100)["success"] is True

    rejected = store.add("memory", "a")

    assert rejected["success"] is False
    assert store.memory_entries == ["a" * 100]


def test_render_block_shows_weighted_count(memory_repo):
    store = MemoryStore(memory_char_limit=1000)
    store.add("memory", "中" * 100)
    store.load_from_store()  # refresh the frozen prompt snapshot

    block = store.format_for_system_prompt("memory") or ""

    # 100 CJK chars -> 200 weighted chars, shown in the header indicator.
    assert "200/1,000 chars" in block


def test_success_response_usage_is_weighted(memory_repo):
    store = MemoryStore(memory_char_limit=1000)
    result = store.add("memory", "中" * 100)

    assert "200/1,000 chars" in result["usage"]


def test_evict_cjk_frees_more_budget_than_ascii(memory_repo):
    store = MemoryStore(facts_char_limit=100)
    store.add("facts", "中" * 40)  # weighted 80
    store.add("facts", "a" * 40)  # weighted 40 -> total 120 > 100, evicts oldest (CJK)

    # The CJK entry was evicted; the ASCII entry (cheaper per char) survives.
    assert "中" * 40 not in store.facts_entries
    assert "a" * 40 in store.facts_entries


def test_ascii_add_evict_replace_behaviour_unchanged(memory_repo):
    """Regression: pure-ASCII add/reject/replace is byte-for-byte the old behaviour."""
    store = MemoryStore(memory_char_limit=100)
    store.add("memory", "a" * 40)
    store.add("memory", "b" * 40)
    # memory target rejects overflow (only facts evicts); ASCII capacity is unchanged.
    assert store.add("memory", "c" * 40)["success"] is False
    assert store.memory_entries == ["a" * 40, "b" * 40]

    replaced = store.replace("memory", "b" * 10, "d" * 40)
    assert replaced["success"] is True
    assert "d" * 40 in store.memory_entries
