"""TDD tests for audit 3.1.2 / 3.1.3 / 3.1.6 / 3.1.7 — curator shared helpers
(_compute_diff + _classify_archived in report.py, canonical _skill_dir in
helpers.py, _parse_skill_manage_args in classify.py, generic config getter in
config.py).
"""

import pytest

from context_engine.curator import classify, config, helpers, report, usage

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class TestSkillDirShared313:
    def test_usage_and_orchestrator_alias_the_canonical(self):
        from context_engine.curator import orchestrator

        assert usage._skill_dir is helpers._skill_dir
        assert orchestrator._resolve_skill_dir is helpers._skill_dir

    def test_flat_lookup(self, tmp_path, monkeypatch):
        from context_engine.curator import constants

        auto = tmp_path / "auto"
        skill = auto / "docker"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("x", encoding="utf-8")
        monkeypatch.setattr(constants, "AUTO_SKILLS_DIR", auto)

        assert helpers._skill_dir("docker") == skill

    def test_nested_lookup_by_leaf_name(self, tmp_path, monkeypatch):
        from context_engine.curator import constants

        auto = tmp_path / "auto"
        nested = auto / "ops" / "docker"
        nested.mkdir(parents=True)
        (nested / "SKILL.md").write_text("x", encoding="utf-8")
        monkeypatch.setattr(constants, "AUTO_SKILLS_DIR", auto)

        assert helpers._skill_dir("docker") == nested

    def test_missing_dir_returns_none(self, tmp_path, monkeypatch):
        from context_engine.curator import constants

        monkeypatch.setattr(constants, "AUTO_SKILLS_DIR", tmp_path / "auto")

        assert helpers._skill_dir("nope") is None

    def test_dir_without_skill_md_is_skipped(self, tmp_path, monkeypatch):
        from context_engine.curator import constants

        auto = tmp_path / "auto"
        (auto / "empty").mkdir(parents=True)
        monkeypatch.setattr(constants, "AUTO_SKILLS_DIR", auto)

        assert helpers._skill_dir("empty") is None


class TestParseSkillManageArgs316:
    TC = {"name": "skill_manage", "arguments": None}

    def test_dict_args_pass_through(self):
        tc = {"name": "skill_manage", "arguments": {"action": "delete", "name": "a"}}
        assert classify._parse_skill_manage_args([tc]) == [{"action": "delete", "name": "a"}]

    def test_string_args_parsed(self):
        tc = {"name": "skill_manage", "arguments": '{"action": "delete", "name": "a"}'}
        assert classify._parse_skill_manage_args([tc]) == [{"action": "delete", "name": "a"}]

    def test_other_tools_skipped(self):
        tc = {"name": "other_tool", "arguments": '{"x": 1}'}
        assert classify._parse_skill_manage_args([tc]) == []

    def test_keep_raw_on_error_true(self):
        tc = {"name": "skill_manage", "arguments": "{broken json"}
        assert classify._parse_skill_manage_args([tc], keep_raw_on_error=True) == [
            {"_raw": "{broken json"}
        ]

    def test_keep_raw_on_error_false_skips(self):
        tc = {"name": "skill_manage", "arguments": "{broken json"}
        assert classify._parse_skill_manage_args([tc]) == []

    def test_absorbed_declarations_skip_broken_args(self):
        """The declarations extractor historically skipped unparseable args
        (while the classifier kept them as _raw evidence)."""
        broken = {"name": "skill_manage", "arguments": "{broken"}
        good = {
            "name": "skill_manage",
            "arguments": {"action": "delete", "name": "a", "absorbed_into": "umbrella"},
        }

        out = classify._extract_absorbed_into_declarations([broken, good])

        assert out == {"a": {"into": "umbrella", "declared": True}}

    def test_classifier_finds_evidence_via_content_haystack(self):
        broken = {"name": "skill_manage", "arguments": "{broken json stays inert"}
        good = {
            "name": "skill_manage",
            "arguments": {
                "action": "create",
                "name": "umbrella",
                "content": "merges old-skill here",
            },
        }

        out = classify._classify_removed_skills(
            removed=["old-skill"], added=[], after_names={"umbrella"}, tool_calls=[broken, good]
        )

        assert out["consolidated"][0]["into"] == "umbrella"
        assert "referenced 'old-skill'" in out["consolidated"][0]["evidence"]


class TestConfigGetter317:
    def _with_config(self, monkeypatch, cfg):
        monkeypatch.setattr(config, "_load_config", lambda: cfg)

    def test_bool_default_when_missing(self, monkeypatch):
        self._with_config(monkeypatch, {})
        assert config.is_enabled() is True
        assert config.get_consolidate() is False  # DEFAULT_CONSOLIDATE

    def test_bool_coercion(self, monkeypatch):
        self._with_config(monkeypatch, {"enabled": 0, "consolidate": "yes"})
        assert config.is_enabled() is False
        assert config.get_consolidate() is True

    def test_int_coercion_and_default_on_bad_value(self, monkeypatch):
        self._with_config(monkeypatch, {"interval_hours": "24"})
        assert config.get_interval_hours() == 24

        self._with_config(monkeypatch, {"interval_hours": None})
        from context_engine.curator.constants import DEFAULT_INTERVAL_HOURS

        assert config.get_interval_hours() == DEFAULT_INTERVAL_HOURS

    def test_float_coercion_and_default(self, monkeypatch):
        from context_engine.curator.constants import DEFAULT_MIN_IDLE_HOURS

        self._with_config(monkeypatch, {"min_idle_hours": "0.5"})
        assert config.get_min_idle_hours() == 0.5

        self._with_config(monkeypatch, {"min_idle_hours": "abc"})
        assert config.get_min_idle_hours() == DEFAULT_MIN_IDLE_HOURS

    def test_days_getters(self, monkeypatch):
        from context_engine.curator.constants import DEFAULT_ARCHIVE_AFTER_DAYS

        self._with_config(monkeypatch, {"stale_after_days": 7, "archive_after_days": "bad"})
        assert config.get_stale_after_days() == 7
        assert config.get_archive_after_days() == DEFAULT_ARCHIVE_AFTER_DAYS


class TestComputeDiff312:
    def test_removed_added_sorted(self):
        after_names, removed, added = report._compute_diff(
            before_names={"a", "b", "c"},
            after_report=[{"name": "a"}, {"name": "d"}, "not-a-dict"],
        )

        assert after_names == {"a", "d"}
        assert removed == ["b", "c"]
        assert added == ["d"]

    def test_write_run_report_uses_shared_pipeline(self, monkeypatch, tmp_path):
        """_write_run_report's archived counts come from the same classifier
        the rename summary uses."""
        calls: list[dict] = []
        real = report._classify_archived

        def _spy(**kwargs):
            calls.append(kwargs)
            return real(**kwargs)

        monkeypatch.setattr(report, "_classify_archived", _spy)
        monkeypatch.setattr(report, "CURATOR_LOGS_DIR", tmp_path)

        run_dir = report._write_run_report(
            started_at=__import__("datetime").datetime(2026, 9, 6, 12, 0, 0),
            elapsed_seconds=1.0,
            auto_counts={},
            auto_summary="",
            before_report=[{"name": "old", "state": "active"}],
            before_names={"old"},
            after_report=[{"name": "umbrella", "state": "active"}],
            llm_meta={
                "final": "",
                "tool_calls": [
                    {
                        "name": "skill_manage",
                        "arguments": {
                            "action": "create",
                            "name": "umbrella",
                            "content": "references old-skill",
                        },
                    }
                ],
            },
        )

        assert run_dir is not None
        assert len(calls) == 1
        assert calls[0]["removed"] == ["old"]
        assert calls[0]["added"] == ["umbrella"]

        payload = __import__("json").loads((run_dir / "run.json").read_text(encoding="utf-8"))
        assert payload["counts"]["archived_this_run"] == 1
        assert payload["archived"] == ["old"]
