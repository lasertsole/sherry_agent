"""Keyword rules: validation, load/hot reload, scope filtering, replacement."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.middlewares.safety_filter import rules as rules_module

pytestmark = [pytest.mark.unit]


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(rules_module, "WORKSPACE_DIR", tmp_path)
    rules_module.clear_rules_cache()
    yield tmp_path
    rules_module.clear_rules_cache()


def _write(workspace: Path, rules: list[dict]) -> None:
    (workspace / "sensitive_words.json").write_text(
        json.dumps({"version": 1, "rules": rules}), encoding="utf-8"
    )
    rules_module.clear_rules_cache()


class TestValidation:
    def test_accepts_a_well_formed_document(self):
        normalized = rules_module.validate_rules(
            {
                "rules": [
                    {
                        "pattern": "secret",
                        "type": "contains",
                        "action": "replace",
                        "replacement": "[x]",
                        "scope": "both",
                    }
                ]
            }
        )
        assert normalized[0]["action"] == "replace"

    @pytest.mark.parametrize(
        "rule, message",
        [
            ({"pattern": "", "type": "contains", "action": "block"}, "non-empty"),
            ({"pattern": "x", "type": "fuzzy", "action": "block"}, "'type'"),
            ({"pattern": "x", "type": "contains", "action": "drop"}, "'action'"),
            ({"pattern": "x", "type": "contains", "action": "block", "scope": "all"}, "'scope'"),
            ({"pattern": "([", "type": "regex", "action": "block"}, "invalid regex"),
        ],
    )
    def test_rejects_bad_fields(self, rule, message):
        with pytest.raises(ValueError, match=message):
            rules_module.validate_rules({"rules": [rule]})

    def test_rejects_non_list_rules(self):
        with pytest.raises(ValueError, match="'rules' must be a list"):
            rules_module.validate_rules({"rules": {}})

    def test_caps_the_rule_count(self):
        with pytest.raises(ValueError, match="too many rules"):
            rules_module.validate_rules(
                {"rules": [{"pattern": f"w{i}"} for i in range(rules_module.MAX_RULES + 1)]}
            )


class TestScanning:
    def test_contains_hits_only_the_configured_scope(self, workspace: Path):
        _write(
            workspace,
            [
                {"pattern": "alpha", "type": "contains", "action": "block", "scope": "input"},
                {"pattern": "beta", "type": "contains", "action": "warn", "scope": "output"},
                {"pattern": "gamma", "type": "contains", "action": "replace", "scope": "both"},
            ],
        )

        assert [hit.pattern for hit in rules_module.scan("alpha beta gamma", scope="input")] == [
            "alpha",
            "gamma",
        ]
        assert [hit.pattern for hit in rules_module.scan("alpha beta gamma", scope="output")] == [
            "beta",
            "gamma",
        ]

    def test_exact_matches_the_whole_text_only(self, workspace: Path):
        _write(workspace, [{"pattern": "boom", "type": "exact", "action": "block"}])

        assert rules_module.scan("boom", scope="output")
        assert rules_module.scan("a boom", scope="output") == []

    def test_regex_uses_search(self, workspace: Path):
        _write(workspace, [{"pattern": r"sec\w+et", "type": "regex", "action": "block"}])

        hits = rules_module.scan("the secret here", scope="output")

        assert hits and hits[0].matched_text == "secret"

    def test_a_broken_file_reads_as_no_rules(self, workspace: Path):
        (workspace / "sensitive_words.json").write_text("{not json", encoding="utf-8")
        rules_module.clear_rules_cache()

        assert rules_module.scan("anything", scope="output") == []

    def test_replacements_are_literal(self, workspace: Path):
        _write(
            workspace,
            [
                {
                    "pattern": "secret",
                    "type": "contains",
                    "action": "replace",
                    "replacement": r"\0 [x]",
                }
            ],
        )
        hits = rules_module.scan("a secret b", scope="output")

        assert rules_module.apply_replacements("a secret b", hits) == r"a \0 [x] b"


class TestHotReload:
    def test_editing_the_file_takes_effect_without_a_restart(self, workspace: Path):
        _write(workspace, [{"pattern": "one", "type": "contains", "action": "block"}])
        assert rules_module.scan("one two", scope="output")

        _write(workspace, [{"pattern": "two", "type": "contains", "action": "block"}])

        assert [hit.pattern for hit in rules_module.scan("one two", scope="output")] == ["two"]

    def test_write_rules_round_trips_and_clears_the_cache(self, workspace: Path):
        written = rules_module.write_rules(
            {"rules": [{"pattern": "x", "type": "contains", "action": "block"}]}
        )

        assert written[0]["pattern"] == "x"
        assert rules_module.scan("x marks", scope="output")[0].pattern == "x"
        snapshot = rules_module.rules_snapshot()
        assert snapshot["count"] == 1 and snapshot["exists"] is True

    def test_the_rules_path_stays_inside_the_workspace(self, workspace: Path):
        assert rules_module.rules_file().parent == workspace
