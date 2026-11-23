"""Hermetic tests for the shared benchmark infrastructure.

No Docker, no datasets, no keys: the shared layer is plain library code, and
the tests pin its contracts exactly — dataset resolution precedence, JSON/JSONL
loading, batch slicing, resume semantics, the report/token/context summaries,
and the container manager's compose parsing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.shared import (
    batch_runner,
    code_intel_tracker,
    container_manager,
    context_analyzer,
    env_setup,
    score_reporter,
)

pytestmark = [pytest.mark.unit]


# ── env_setup ────────────────────────────────────────────────────────────────


class TestEnvSetup:
    def test_env_var_wins_over_the_data_root(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GAIA_DATA_DIR", str(tmp_path / "custom"))
        monkeypatch.setenv("SHERRY_EVAL_DATA_ROOT", str(tmp_path / "root"))
        resolved = env_setup.resolve_dataset_dir("gaia", "GAIA_DATA_DIR")
        assert resolved == (tmp_path / "custom").resolve()

    def test_data_root_fallback_names_the_suite(self, monkeypatch, tmp_path):
        monkeypatch.delenv("GAIA_DATA_DIR", raising=False)
        monkeypatch.setenv("SHERRY_EVAL_DATA_ROOT", str(tmp_path / "root"))
        resolved = env_setup.resolve_dataset_dir("gaia", "GAIA_DATA_DIR")
        assert resolved == (tmp_path / "root" / "gaia").resolve()

    def test_nothing_set_falls_to_the_results_data_dir(self, monkeypatch):
        monkeypatch.delenv("GAIA_DATA_DIR", raising=False)
        monkeypatch.delenv("SHERRY_EVAL_DATA_ROOT", raising=False)
        resolved = env_setup.resolve_dataset_dir("gaia", "GAIA_DATA_DIR")
        assert resolved == env_setup.REPO_ROOT / "evals" / "results" / "gaia" / "data"

    def test_dataset_ready_checks_required_files(self, tmp_path):
        (tmp_path / "tasks.json").write_text("[]", encoding="utf-8")
        ok, _ = env_setup.dataset_ready(tmp_path, "tasks.json")
        assert ok
        ready, detail = env_setup.dataset_ready(tmp_path, "missing.json")
        assert not ready and "missing.json" in detail
        assert not env_setup.dataset_ready(tmp_path / "nope")[0]

    def test_load_json_records_accepts_all_three_shapes(self, tmp_path):
        array = tmp_path / "array.json"
        array.write_text(json.dumps([{"a": 1}]), encoding="utf-8")
        wrapped = tmp_path / "wrapped.json"
        wrapped.write_text(json.dumps({"data": [{"a": 2}]}), encoding="utf-8")
        lines = tmp_path / "lines.jsonl"
        lines.write_text('{"a": 3}\n{"a": 4}\n', encoding="utf-8")
        assert [r["a"] for r in env_setup.load_json_records(array)] == [1]
        assert [r["a"] for r in env_setup.load_json_records(wrapped)] == [2]
        assert [r["a"] for r in env_setup.load_json_records(lines)] == [3, 4]

    def test_load_json_records_refuses_garbage(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("not json at all\n", encoding="utf-8")
        with pytest.raises(ValueError, match="not a JSON array"):
            env_setup.load_json_records(bad)

    def test_pick_dataset_file_prefers_the_known_name(self, tmp_path):
        (tmp_path / "metadata.json").write_text("[]", encoding="utf-8")
        (tmp_path / "other.json").write_text("[]", encoding="utf-8")
        assert env_setup.pick_dataset_file(tmp_path, "metadata.json").name == "metadata.json"

    def test_pick_dataset_file_is_honest_about_ambiguity(self, tmp_path):
        (tmp_path / "one.json").write_text("[]", encoding="utf-8")
        (tmp_path / "two.json").write_text("[]", encoding="utf-8")
        with pytest.raises(ValueError, match="2 dataset files"):
            env_setup.pick_dataset_file(tmp_path)

    def test_timeout_clamps_like_deepswe(self):
        assert env_setup.resolve_timeout(None) == env_setup.DEFAULT_TIMEOUT_S
        assert env_setup.resolve_timeout(1.0) == 60.0
        assert env_setup.resolve_timeout(999_999) == env_setup.MAX_TIMEOUT_S


# ── batch_runner ─────────────────────────────────────────────────────────────


class TestBatchRunner:
    IDS = [f"task-{i}" for i in range(100)]

    def test_batch_spec_is_inclusive_and_zero_based(self):
        assert batch_runner.apply_batch(self.IDS, "0-1") == ["task-0", "task-1"]
        assert batch_runner.apply_batch(self.IDS, "5") == ["task-5"]
        assert batch_runner.apply_batch(self.IDS, "98-200") == ["task-98", "task-99"]
        assert batch_runner.apply_batch(self.IDS, None) == self.IDS

    def test_bad_batch_specs_refuse(self):
        for spec in ("5-2", "abc", "-1", "0--3"):
            with pytest.raises(ValueError):
                batch_runner.parse_batch(spec)

    def _write_report(self, root: Path, tasks: list[dict]) -> None:
        run = root / "20260101_000000"
        run.mkdir(parents=True)
        (run / "eval_report.json").write_text(json.dumps({"tasks": tasks}), encoding="utf-8")

    def test_resume_skips_graded_tasks_and_retries_timeouts(self, tmp_path):
        self._write_report(
            tmp_path,
            [
                {"id": "task-0", "resolved": True},
                {"id": "task-1", "resolved": False, "failure_kind": ""},
                {"id": "task-2", "resolved": False, "failure_kind": "harness"},
                {"id": "task-3", "resolved": False, "failure_kind": "timeout"},
            ],
        )
        completed = batch_runner.completed_task_ids(tmp_path)
        assert completed == {"task-0", "task-1", "task-2"}, "a timeout is retried"
        remaining = batch_runner.apply_resume(["task-0", "task-3"], tmp_path)
        assert remaining == ["task-3"]

    def test_resume_with_no_history_keeps_everything(self, tmp_path):
        assert batch_runner.apply_resume(self.IDS[:2], tmp_path) == self.IDS[:2]

    def test_a_corrupt_report_is_ignored(self, tmp_path):
        run = tmp_path / "20260101_000000"
        run.mkdir(parents=True)
        (run / "eval_report.json").write_text("{broken", encoding="utf-8")
        assert batch_runner.completed_task_ids(tmp_path) == set()


# ── score_reporter ───────────────────────────────────────────────────────────


class TestScoreReporter:
    def _results(self) -> list[dict]:
        return [
            {
                "id": "a",
                "resolved": True,
                "failure_kind": "",
                "duration_s": 1.0,
                "tokens": {"input": 100, "output": 50, "cost_usd": 0.01},
                "context": {"hit_rate": 0.9, "effective_tokens": 9, "wasted_tokens": 1},
            },
            {
                "id": "b",
                "resolved": False,
                "failure_kind": "provider",
                "invalid_run": True,
                "duration_s": 2.0,
                "tokens": {"input": 10, "output": 5},
                "context": {"hit_rate": 0.5, "effective_tokens": 5, "wasted_tokens": 5},
            },
        ]

    def test_report_aggregate_excludes_invalid_runs_from_the_rate(self):
        report = score_reporter.build_report(
            run_id="r1",
            suite="s",
            kind="k",
            results_dir=Path("x"),
            results=self._results(),
            started=__import__("time").monotonic(),
            model_info={},
            benchmark={},
        )
        aggregate = report["aggregate"]
        assert aggregate["resolved"] == 1
        assert aggregate["invalid_runs"] == ["b"]
        assert aggregate["resolve_rate"] == 1.0  # 1 valid run, resolved
        assert report["token_summary"]["total_tokens"] == 165
        assert report["token_summary"]["total_cost_usd"] == 0.01
        assert report["context_summary"]["avg_hit_rate"] == 0.7  # (0.9+0.5)/2

    def test_code_intel_summary_aggregates_by_tool(self):
        results = [
            {
                "id": "a",
                "resolved": True,
                "failure_kind": "",
                "tokens": {},
                "code_intel_usage": {
                    "tool_calls": {"explore": 3, "lsp_hover": 1},
                    "degraded_calls": 1,
                },
            },
            {
                "id": "b",
                "resolved": False,
                "failure_kind": "harness",
                "tokens": {},
                "code_intel_usage": {"tool_calls": {"explore": 2}, "degraded_calls": 0},
            },
        ]
        report = score_reporter.build_report(
            run_id="r1",
            suite="s",
            kind="k",
            results_dir=Path("x"),
            results=results,
            started=__import__("time").monotonic(),
            model_info={},
            benchmark={},
            extra={"code_intel_summary": score_reporter.code_intel_summary(results)},
        )
        summary = report["code_intel_summary"]
        assert summary["total_code_intel_calls"] == 6
        assert summary["by_tool"]["explore"] == 5
        assert summary["degraded_rate"] == pytest.approx(1 / 6, abs=1e-3)

    def test_summary_txt_lands_next_to_the_report(self, tmp_path):
        report = score_reporter.build_report(
            run_id="r1",
            suite="s",
            kind="k",
            results_dir=tmp_path,
            results=[
                {"id": "a", "resolved": True, "failure_kind": "", "duration_s": 1.0, "tokens": {}}
            ],
            started=__import__("time").monotonic(),
            model_info={},
            benchmark={},
        )
        path = score_reporter.write_summary_txt(tmp_path, report)
        assert path.read_text(encoding="utf-8").startswith("suite: s")


# ── context_analyzer (thin alias over the product analyzer) ──────────────────


class TestContextAnalyzer:
    def test_conservative_default_and_terminal_heuristic(self):
        analyzer = context_analyzer.ContextAnalyzer("fix the login crash in app.py")
        analyzer.score_tool_call("terminal", {"commands": "pytest tests/login"}, "ok")
        analyzer.score_tool_call("terminal", {"commands": "curl example.com"}, "done")
        analyzer.score_tool_call("mystery_tool", None, "")
        analyzer.score_tool_call("todowrite", {"todos": []}, "")  # unscored
        payload = analyzer.payload()
        # terminal test command: hit; curl: wasted (no keyword); mystery: conservative hit
        assert payload["effective_tokens"] == 2
        assert payload["wasted_tokens"] == 1
        assert payload["hit_rate"] == pytest.approx(2 / 3, abs=1e-3)
        assert payload["wasted_breakdown"] == {"terminal": 1}
        assert "todowrite" not in payload["by_tool"]

    def test_transcript_walk_pairs_tool_calls_with_results(self):
        from langchain_core.messages import AIMessage, ToolMessage

        messages = [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "terminal", "args": {"commands": "pytest -q"}, "id": "c1"},
                    {"name": "terminal", "args": {"commands": "curl unrelated.dev"}, "id": "c2"},
                ],
            ),
            ToolMessage(content="1 passed", tool_call_id="c1", name="terminal"),
            ToolMessage(content="done", tool_call_id="c2", name="terminal"),
        ]
        payload = context_analyzer.analyze_transcript(messages, "run the login tests")
        assert payload["effective_tokens"] == 1
        assert payload["wasted_tokens"] == 1

    def test_an_empty_transcript_reports_a_full_rate(self):
        assert context_analyzer.analyze_transcript([], "task")["hit_rate"] == 1.0


# ── code_intel_tracker ───────────────────────────────────────────────────────


class TestCodeIntelTracker:
    def test_counters_from_tool_names(self):
        tracker = code_intel_tracker.CodeIntelTracker("t")
        tracker.record_names(
            {"explore": 3, "lsp_goto_definition": 2, "search_files": 4, "terminal": 9}
        )
        payload = tracker.payload()
        assert payload["tool_calls"] == {"explore": 3, "lsp_goto_definition": 2}
        assert payload["total_code_intel_calls"] == 5
        # precise/(precise+greedy): explore+lsp = 5 precise, search_files 4 greedy
        assert payload["lsp_vs_grep_ratio"] == pytest.approx(5 / 9, abs=1e-3)

    def test_degraded_calls_detected_from_the_transcript(self):
        from langchain_core.messages import ToolMessage

        messages = [
            ToolMessage(
                content="lsp_goto_definition failed: basedpyright not found",
                tool_call_id="c1",
                name="lsp_goto_definition",
            ),
            ToolMessage(content="found 3 refs", tool_call_id="c2", name="explore"),
        ]
        payload = code_intel_tracker.analyze_code_intel("t", {"lsp_goto_definition": 1}, messages)
        assert payload["degraded_calls"] == 1
        assert "not found" in payload["degraded_reasons"][0]
        assert payload["code_intel_available"] is True


# ── container_manager ────────────────────────────────────────────────────────


class TestContainerManager:
    def test_work_root_lands_outside_the_checkout(self, tmp_path, monkeypatch):
        import tempfile

        monkeypatch.delenv("SHERRY_EVAL_WORK_ROOT", raising=False)
        root = container_manager.work_root("s", tmp_path)
        assert root == Path(tempfile.gettempdir()) / "sherry_evals" / "bench-work" / tmp_path.name
        assert not root.is_relative_to(env_setup.REPO_ROOT)

    def test_work_root_honors_the_override(self, tmp_path, monkeypatch):
        root = container_manager.work_root("s", tmp_path, override=str(tmp_path / "w"))
        assert root == tmp_path / "w" / tmp_path.name

    def test_compose_up_without_a_plugin_is_an_honest_failure(self, monkeypatch):
        monkeypatch.setattr(
            container_manager.external_bench,
            "container_bin",
            lambda: "docker",
        )
        monkeypatch.setattr(
            container_manager.external_bench,
            "run",
            lambda argv, timeout: container_manager.external_bench.CommandResult(1, "no plugin"),
        )
        ok, detail = container_manager.compose_up(Path("compose.yml"), "proj")
        assert not ok and "no compose plugin" in detail
