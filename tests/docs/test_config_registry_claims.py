"""The four-language long-running-tasks document must not drift on registry size.

The drift class this pins down: the ``config/features`` registry grew a module
at a time, and every language's README still claimed the old totals (45 objects
= 26 agent-side + 19 infra-side, while the registry actually held 54 = 34 + 20).
The docs-parity gate cannot see this — it only checks that the four languages
agree with *each other*, and they did, all four being equally stale.

Checked here:

1. the counts claimed in every language's README equal the live registry counts;
2. the contract test's coverage claim (18 covered, `MODEL_PRICING` and
   `HTTP_CLIENT` omitted) matches ``test_features_infra_side``'s CASES;
3. AGENTS.md's registry counts (the config table's TypedDict totals) match too.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from config.features import agent_side, infra_side

pytestmark = [pytest.mark.unit]

_GROUP = tuple(
    pathlib.Path(f"docs/long-running-tasks/README{suffix}.md")
    for suffix in ("", ".zh", ".ja", ".ko")
)
_AGENTS = pathlib.Path("AGENTS.md")


def _registry_counts() -> tuple[int, int, int]:
    """(agent-side, infra-side, total) feature objects, read off the packages."""
    agent = sorted(name for name in dir(agent_side) if name.isupper())
    infra = sorted(name for name in dir(infra_side) if name.isupper())
    return len(agent), len(infra), len(agent) + len(infra)


def test_the_registry_actually_has_the_counts_the_test_assumes():
    """Guards the guard: the numbers below are re-derived, never hardcoded."""
    agent, infra, total = _registry_counts()
    assert agent >= 30 and infra >= 15, "registry shrank unexpectedly"
    assert total == agent + infra


def test_every_language_states_the_live_registry_counts():
    agent, infra, total = _registry_counts()
    for doc in _GROUP:
        text = doc.read_text(encoding="utf-8")
        assert f"**{agent}**" in text, f"{doc}: agent-side module count is stale"
        assert f"**{infra}**" in text, f"{doc}: infra-side module count is stale"
        assert f"**{total}" in text, f"{doc}: registry total is stale"
        # The prose repeats the split inline ("34 agent-side + 20 infra-side").
        assert re.search(rf"{agent} agent-side \+ {infra} infra-side", text) or re.search(
            rf"Agent 侧 {agent} \+ 基础设施侧 {infra}|エージェント側 {agent} \+ インフラ側 {infra}"
            rf"|에이전트 측 {agent} \+ 인프라 측 {infra}",
            text,
        ), f"{doc}: the inline agent/infra split is stale"
        # The closing pitfalls bullet repeats the totals again ("size is 54").
        assert re.search(rf"size is {total}|规模是 {total}|規模は {total}|규모는 {total}", text), (
            f"{doc}: the closing registry-size bullet is stale"
        )


def test_the_contract_coverage_claim_matches_the_test_file():
    """The 'covers 18 …' claim must name the features the contract test omits."""
    import importlib

    module = importlib.import_module("tests.config.test_features_infra_side")
    cases = getattr(module, "CASES")
    covered = {name for name, *_rest in cases} | {"GATEWAY"}
    omitted = sorted(name for name in dir(infra_side) if name.isupper() and name not in covered)

    expected_covered = 18
    assert len(covered) == expected_covered, "contract coverage changed; update the docs claim"
    for doc in _GROUP:
        text = doc.read_text(encoding="utf-8")
        assert (
            f"covers {expected_covered} of them" in text
            or (f"覆盖其中 {expected_covered} 个" in text)
            or (f"そのうち {expected_covered} 個" in text)
            or (f"그중 {expected_covered}개" in text)
        ), f"{doc}: coverage count is stale"
        for name in omitted:
            assert f"`{name}`" in text, f"{doc}: omitted feature {name} is not named"


def test_agents_md_registry_counts_match_the_code():
    agent, infra, total = _registry_counts()
    text = _AGENTS.read_text(encoding="utf-8")
    assert f"Per-object feature config ({total} TypedDicts)" in text
    assert f"| `config/features/agent_side/` | {agent} per-object TypedDicts" in text
    assert f"| `config/features/infra_side/` | {infra} per-object TypedDicts" in text
    assert f"Aggregator — all {total} TypedDicts + instances re-exported" in text
