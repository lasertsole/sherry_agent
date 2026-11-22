"""Hermetic tests for the 8 benchmark adapters.

Each adapter's pure logic — dataset parsing, image derivation, prompt/answer
scoring, evaluator subsets, the tau-bench tool wrapper — runs against SYNTHETIC
fixtures. Nothing here touches Docker, a dataset download or a model; the
skip-loudly contract (missing dataset → exit 3) is pinned per suite, and the
tau-bench wrapper runs against a FAKE package module so the assumed surface is
pinned in code.
"""

from __future__ import annotations

import json
import sys
import types

import pytest

from evals import external_bench
from evals.cybersec import suite as cybersec
from evals.devbench import suite as devbench
from evals.gaia import suite as gaia
from evals.multi_swe_bench import suite as multi_swe
from evals.osworld import suite as osworld
from evals.tau_bench import api_wrapper, suite as tau_suite
from evals.webarena import suite as webarena

pytestmark = [pytest.mark.unit]

_SWE_RECORD = {
    "instance_id": "django__django-16379",
    "repo": "django/django",
    "base_commit": "abc123def456",
    "problem_statement": "ForeignKey.__init__() crashes when limit_choices_to is a set.",
    "test_patch": "--- a/tests/test.py\n+++ b/tests/test.py\n...",
    "patch": "--- a/django/db/models/fields/related.py\n...",
    "FAIL_TO_PASS": ["tests/test.py::test_new_behaviour"],
    "PASS_TO_PASS": ["tests/test.py::test_old_behaviour"],
}


# ── devbench (the SWE-bench core) ────────────────────────────────────────────


class TestDevbench:
    def test_dataset_loads_and_requires_the_core_fields(self, tmp_path):
        path = tmp_path / "swebench_verified.json"
        path.write_text(json.dumps([_SWE_RECORD]), encoding="utf-8")
        records = devbench.load_dataset(path)
        assert records[0]["instance_id"] == "django__django-16379"

        broken = dict(_SWE_RECORD, instance_id="")
        path.write_text(json.dumps([broken]), encoding="utf-8")
        with pytest.raises(ValueError, match="missing required fields"):
            devbench.load_dataset(path)

    def test_image_naming_follows_the_official_form(self):
        assert devbench.image_for(_SWE_RECORD) == "sweb.eval.x86_64.django__django-16379"
        assert devbench.image_for(dict(_SWE_RECORD, image="custom:tag")) == "custom:tag"

    def test_test_list_parsing_accepts_json_and_bare_strings(self):
        record = dict(_SWE_RECORD, FAIL_TO_PASS='["a::t1", "a::t2"]')
        assert devbench.fail_to_pass(record) == ["a::t1", "a::t2"]
        record = dict(_SWE_RECORD, FAIL_TO_PASS="a::t1\na::t2")
        assert devbench.fail_to_pass(record) == ["a::t1", "a::t2"]

    def test_the_judge_rewards_f2p_passes_and_flags_regressions(self):
        # parse_pytest_output keys the LAST :: segment and needs the -rA
        # short-summary header — exactly what the verifier's pytest prints.
        output = (
            "=========== short test summary info ===========\n"
            "PASSED tests/test.py::test_new_behaviour\n"
            "PASSED tests/test.py::test_old_behaviour\n"
        )
        reward, resolved, detail = devbench.judge(_SWE_RECORD, output)
        assert resolved and reward == 1.0 and detail == ""

        regressed = output.replace(
            "PASSED tests/test.py::test_old_behaviour", "FAILED tests/test.py::test_old_behaviour"
        )
        reward, resolved, detail = devbench.judge(_SWE_RECORD, regressed)
        assert not resolved and "regressed" in detail

        missing = output.replace(
            "PASSED tests/test.py::test_new_behaviour", "FAILED tests/test.py::test_new_behaviour"
        )
        reward, resolved, detail = devbench.judge(_SWE_RECORD, missing)
        assert not resolved and "not passing" in detail

        reward, resolved, _ = devbench.judge(_SWE_RECORD, "garbage, no pytest summary")
        assert not resolved

    def test_build_test_command_quotes_every_target(self):
        record = dict(_SWE_RECORD, PASS_TO_PASS=["tests/a.py::t[1]"])
        command = devbench.build_test_command(record)
        assert '"tests/test.py::test_new_behaviour"' in command
        assert '"tests/a.py::t[1]"' in command
        assert "-rA" in command

    def test_skip_contract_without_a_dataset(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("SWEBENCH_DATA_DIR", raising=False)
        monkeypatch.delenv("SHERRY_EVAL_DATA_ROOT", raising=False)
        monkeypatch.setattr(devbench.external_bench, "maybe_reexec", lambda *_: None)
        monkeypatch.setattr(
            devbench.env_setup, "resolve_dataset_dir", lambda *_: tmp_path / "absent"
        )
        monkeypatch.setattr(sys, "argv", ["evals.py", "devbench"])  # not pytest's argv
        with pytest.raises(SystemExit) as exc:
            devbench.main()
        assert exc.value.code == 3
        assert "SKIPPED" in capsys.readouterr().out

    def test_dry_run_validates_the_dataset_without_docker(self, tmp_path, monkeypatch, capsys):
        data = tmp_path / "data"
        data.mkdir()
        (data / "swebench_verified.json").write_text(json.dumps([_SWE_RECORD]), encoding="utf-8")
        monkeypatch.setenv("SWEBENCH_DATA_DIR", str(data))
        monkeypatch.setattr(devbench.external_bench, "maybe_reexec", lambda *_: None)
        monkeypatch.setattr(sys, "argv", ["evals.py", "devbench", "--dry-run"])
        devbench.main()  # a clean return, not a skip
        out = capsys.readouterr().out
        assert "dry-run ok" in out and "django__django-16379" in out

    def test_multi_swe_language_filter(self):
        records = [
            dict(_SWE_RECORD, instance_id="m-1", language="Java"),
            dict(_SWE_RECORD, instance_id="m-2", language="Go"),
        ]
        assert [r["instance_id"] for r in multi_swe.filter_language(records, "java")] == ["m-1"]
        with pytest.raises(SystemExit, match="carries"):
            multi_swe.filter_language(records, "rust")

    def test_multi_swe_records_without_an_image_skip_as_harness(self, tmp_path):
        record = dict(_SWE_RECORD, instance_id="m-1", language="java")  # no image field
        result = __import__("asyncio").run(multi_swe.run_task(record, tmp_path, timeout=60.0))
        assert result["failure_kind"] == "harness"
        assert "no 'image' field" in result["error"]

    def test_multi_swe_dataset_requires_a_language_field(self, tmp_path):
        path = tmp_path / "multi.json"
        path.write_text(json.dumps([_SWE_RECORD]), encoding="utf-8")
        with pytest.raises(ValueError, match="language"):
            multi_swe.load_dataset(path)


# ── gaia ─────────────────────────────────────────────────────────────────────


class TestGaia:
    def test_the_official_normalization(self):
        score = gaia.score_answer
        assert score("The Sun", "sun")
        assert score("$1,000,000", "1000000")
        assert score("2.5 million", "2500000")
        assert score("a Beatles album", "beatles album")
        assert not score("The Moon", "sun")

    def test_extract_final_answer_takes_the_last_marker(self):
        text = "Working...\nFINAL ANSWER: 42\nFinal Answer: really 42"
        assert gaia.extract_final_answer(text) == "really 42"
        assert gaia.extract_final_answer("no marker\nlast line") == "last line"
        assert gaia.extract_final_answer("") == ""

    def test_dataset_loads_both_key_styles(self, tmp_path):
        path = tmp_path / "metadata.jsonl"
        path.write_text(
            json.dumps({"task_id": "g1", "Level": "1", "Question": "q?", "Final answer": "a"})
            + "\n"
            + json.dumps({"task_id": "g2", "level": "2", "question": "q2?", "final_answer": "b"})
            + "\n",
            encoding="utf-8",
        )
        records = gaia.load_dataset(path)
        assert [r["task_id"] for r in records] == ["g1", "g2"]
        assert records[0]["final_answer"] == "a"

    def test_attachments_stage_into_the_workspace(self, tmp_path):
        data = tmp_path / "data"
        data.mkdir()
        (data / "report.xlsx").write_bytes(b"x")
        staged = gaia.stage_attachment({"file_name": "report.xlsx"}, data, tmp_path / "workspace")
        assert staged is not None and staged.is_file()
        assert gaia.stage_attachment({"file_name": "ghost.xlsx"}, data, tmp_path) is None
        assert gaia.stage_attachment({"file_name": ""}, data, tmp_path) is None


# ── cybersec ─────────────────────────────────────────────────────────────────


class TestCybersec:
    RECORD = {
        "task_id": "c1",
        "question": "Which vulnerability class?",
        "choices": ["XSS", "SQLi", "SSRF"],
        "answer": "B",
    }

    def test_prompt_lists_lettered_choices(self):
        prompt = cybersec.format_prompt(self.RECORD)
        assert "A. XSS" in prompt and "B. SQLi" in prompt
        assert "LAST line" in prompt

    def test_choice_extraction_takes_the_last_bare_letter(self):
        text = "It is likely SQLi.\nB\n"
        assert cybersec.extract_choice(text, 3) == "B"
        assert cybersec.extract_choice("The answer is (C).", 3) == "C"
        assert cybersec.extract_choice("no letter here", 3) == ""
        assert cybersec.extract_choice("E is wrong", 3) == ""  # out of range

    def test_dataset_validates_the_answer_range(self, tmp_path):
        path = tmp_path / "cybersec.json"
        path.write_text(json.dumps([dict(self.RECORD, answer="Z")]), encoding="utf-8")
        with pytest.raises(ValueError, match="out of range"):
            cybersec.load_dataset(path)

    def test_score_is_a_letter_match(self):
        assert cybersec.score_answer("B", "B")
        assert cybersec.score_answer("b", "B")
        assert not cybersec.score_answer("", "B")


# ── tau_bench ────────────────────────────────────────────────────────────────


def _install_fake_tau_bench(monkeypatch) -> None:
    """The package surface api_wrapper pins, as a fake module tree."""
    tool_spec = types.SimpleNamespace(
        info={
            "function": {
                "name": "get_flight",
                "description": "Look up a flight",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "flight_number": {"type": "string", "description": "the flight"}
                    },
                    "required": ["flight_number"],
                },
            }
        }
    )
    env_module = types.SimpleNamespace(
        AirlineEnv=lambda *a, **kw: types.SimpleNamespace(tools=[tool_spec]),
        tasks=[
            types.SimpleNamespace(
                task_id="t-1",
                instructions="Book it.",
                expected_actions=[{"name": "get_flight", "kwargs": {"flight_number": "AA1"}}],
                outputs={"confirmation": "AA1 booked"},
            )
        ],
    )
    empty_module = types.ModuleType("tau_bench.envs.retail")
    data_module = types.SimpleNamespace(
        get=lambda scene: env_module if scene == "airline" else empty_module,
        airline=env_module,
    )
    tau_module = types.ModuleType("tau_bench")
    envs_module = types.ModuleType("tau_bench.envs")
    envs_data = data_module  # the registered submodule IS the namespace with .get
    envs_module.data = envs_data
    tau_module.envs = envs_module
    monkeypatch.setitem(sys.modules, "tau_bench", tau_module)
    monkeypatch.setitem(sys.modules, "tau_bench.envs", envs_module)
    monkeypatch.setitem(sys.modules, "tau_bench.envs.data", envs_data)
    monkeypatch.setitem(sys.modules, "tau_bench.envs.airline", env_module)


class TestTauBench:
    def test_the_wrapper_builds_real_tools_from_the_package_surface(self, monkeypatch):
        _install_fake_tau_bench(monkeypatch)
        surface = tau_suite.import_tau_bench()
        task = {"task_id": "t-1", "index": 0, "raw": surface["envs_data"].get("airline").tasks[0]}
        tools, _env = api_wrapper.build_episode_tools(surface, "airline", task)
        assert [tool.name for tool in tools] == ["get_flight"]
        schema = tools[0].args_schema.model_json_schema()
        assert schema["required"] == ["flight_number"]

    def test_the_episode_judge_checks_expected_actions_and_outputs(self, monkeypatch):
        _install_fake_tau_bench(monkeypatch)
        surface = tau_suite.import_tau_bench()
        task = {"task_id": "t-1", "index": 0, "raw": surface["envs_data"].get("airline").tasks[0]}
        tools, _env = api_wrapper.build_episode_tools(surface, "airline", task)
        api_wrapper.install_episode_tools(tools)
        try:
            tools[0].invoke({"flight_number": "AA1"})
        finally:
            api_wrapper.remove_episode_tools()
        resolved, detail = api_wrapper._judge(
            "AA1 booked", task["raw"].expected_actions, task["raw"].outputs
        )
        assert resolved and detail == ""

        # The recorded action satisfies the action check; the OUTPUT check is
        # what fails when the agent never tells the user the result.
        resolved, detail = api_wrapper._judge(
            "done.", task["raw"].expected_actions, task["raw"].outputs
        )
        assert not resolved and "not in the final response" in detail

    def test_tasks_discovery_is_fail_loud(self, monkeypatch):
        _install_fake_tau_bench(monkeypatch)
        surface = tau_suite.import_tau_bench()
        tasks = tau_suite.available_tasks(surface, "airline")
        assert tasks[0]["task_id"] == "t-1"

        with pytest.raises(RuntimeError, match="no tasks list"):
            tau_suite.available_tasks(surface, "retail")  # the fake's retail is empty

    def test_the_suite_skips_without_the_package(self, monkeypatch, capsys):
        monkeypatch.setenv("MAIN_LLM_API_KEY", "sk-fake")
        monkeypatch.setitem(sys.modules, "tau_bench", None)  # import error shape
        monkeypatch.setattr(tau_suite.external_bench, "maybe_reexec", lambda *_: None)
        monkeypatch.setattr(sys, "argv", ["evals.py", "tau_bench"])
        with pytest.raises(SystemExit) as exc:
            tau_suite.main()
        assert exc.value.code == 3
        assert "tau_bench package is not installed" in capsys.readouterr().out


# ── webarena / visualwebarena ────────────────────────────────────────────────


class TestWebarena:
    def test_url_judges(self):
        spec = {"url_last": "http://site.com/cart"}
        assert webarena.evaluate_url(spec, "http://site.com/cart")[0]
        assert webarena.evaluate_url(spec, "http://other.com/cart")[0], "host-agnostic path"
        assert not webarena.evaluate_url(spec, "http://other.com/other")[0]
        regex = {"url_regex": r"page=2"}
        assert webarena.evaluate_url(regex, "http://s.com/list?page=2")[0]
        assert not webarena.evaluate_url({}, "http://s.com/")[0]

    def test_dataset_needs_an_intent(self, tmp_path):
        path = tmp_path / "tasks.json"
        path.write_text(json.dumps([{"task_id": "w1"}]), encoding="utf-8")
        with pytest.raises(ValueError, match="intent"):
            webarena.load_dataset(path)

    def test_a_failing_compose_is_an_honest_skip(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("MAIN_LLM_API_KEY", "sk-fake")
        monkeypatch.setenv("WEBARENA_DATA_DIR", str(tmp_path))
        (tmp_path / "tasks.json").write_text(
            json.dumps([{"task_id": "w1", "intent": "do it"}]), encoding="utf-8"
        )
        monkeypatch.setattr(webarena.external_bench, "maybe_reexec", lambda *_: None)
        monkeypatch.setattr(
            webarena.container_manager,
            "compose_up",
            lambda *_a, **_kw: (False, "compose unavailable"),
        )
        records = webarena.load_dataset(tmp_path / "tasks.json")
        exit_code = __import__("asyncio").run(
            webarena._run(
                records,
                records,
                tmp_path / "compose.yml",
                {
                    "compose_file": "",
                    "base_url": "http://x",
                    "repo_dir": "",
                    "timeout": 60,
                    "update_baseline": False,
                },
            )
        )
        assert exit_code == 3
        assert "could not start" in capsys.readouterr().out


# ── osworld ──────────────────────────────────────────────────────────────────


class TestOsworld:
    def test_vm_file_exists_judge(self, monkeypatch):
        calls: list[list[str]] = []

        def fake_run(argv, timeout):
            calls.append(argv)
            command = argv[-1]
            return external_bench.CommandResult(
                0, "EXISTS" if "exists.txt" in command else "MISSING"
            )

        monkeypatch.setattr(external_bench, "run", fake_run)
        ok, detail = osworld.evaluate(
            {
                "func": "vm_file_exists",
                "postconfig": [{"type": "vm_file", "path": "/root/exists.txt"}],
            },
            "container",
        )
        assert ok and "EXISTS" in detail
        ok, _ = osworld.evaluate(
            {
                "func": "vm_file_exists",
                "postconfig": [{"type": "vm_file", "path": "/root/gone.txt"}],
            },
            "container",
        )
        assert not ok

    def test_command_line_judge_checks_exit_and_output(self, monkeypatch):
        monkeypatch.setattr(
            external_bench,
            "run",
            lambda argv, timeout: external_bench.CommandResult(0, "120 MB free"),
        )
        ok, detail = osworld.evaluate(
            {
                "func": "vm_command_line",
                "postconfig": [{"type": "vm_command_line", "command": "df -h"}],
                "expected": {"exit_code": 0, "stdout_regex": r"\d+ MB"},
            },
            "container",
        )
        assert ok and "exit=0" in detail
        ok, _ = osworld.evaluate(
            {
                "func": "vm_command_line",
                "postconfig": [{"type": "vm_command_line", "command": "df -h"}],
                "expected": {"exit_code": 2},
            },
            "container",
        )
        assert not ok

    def test_unknown_getters_are_unjudged_never_passed(self):
        ok, detail = osworld.evaluate({"func": "accessibility_tree"}, "container")
        assert not ok and "unjudged" in detail

    def test_dataset_requires_an_evaluator(self, tmp_path):
        path = tmp_path / "tasks.json"
        path.write_text(json.dumps([{"id": "o1", "config": {}}]), encoding="utf-8")
        with pytest.raises(ValueError, match="no evaluator"):
            osworld.load_dataset(path)
