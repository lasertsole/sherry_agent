"""DevBench — SWE-bench Verified on the sherry harness.

Structure follows the DeepSWE template exactly (the closest benchmark shape):
the agent edits a HOST copy of the task's ``/testbed`` checkout; the same tree
is bind-mounted into the task's pre-built ``sweb.eval.x86_64.<instance>``
image where its ``terminal`` commands run; grading applies the model patch +
the held-out ``test_patch`` inside the same image and judges the official
FAIL_TO_PASS / PASS_TO_PASS test lists from the parsed pytest output.

Differences from DeepSWE, documented on purpose:

- the dataset is a LOCAL file (``SWEBENCH_DATA_DIR`` or ``--dataset``): SWE-bench
  Verified ships as one JSON array of instances; nothing is downloaded by the
  suite (a missing dataset SKIPS with exit 3, per the plan's kill criteria);
- the task images are PRE-BUILT by the operator with the official
  ``swebench`` tooling (``python -m swebench.harness.prepare_images``) — the
  suite pulls them but never builds them (the official build needs the
  swebench package and hours per instance);
- grading is the compact in-image pytest pass, judged against the instance's
  FAIL_TO_PASS / PASS_TO_PASS lists through
  ``external_bench.parse_pytest_output`` — the official log-parsers reduced to
  what one pytest invocation produces.

Usage::

    uv run python evals/evals.py devbench --dry-run
    uv run python evals/evals.py devbench --all --batch 0-49
    uv run python evals/evals.py devbench --resume --all
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals import external_bench  # noqa: E402
from evals.shared import batch_runner, env_setup, score_reporter  # noqa: E402
from evals.shared.container_manager import TaskContainer, work_root  # noqa: E402
from evals.shared.eval_checklist import acceptance_checklist  # noqa: E402
from evals.shared.eval_iteration_budget import (  # noqa: E402
    pin_eval_budget,
    resolve_eval_iteration_budget,
)
from evals.shared.eval_temperature import pin_eval_temperature  # noqa: E402

BASELINE_PATH = Path(__file__).with_name("baseline.json")
SUITE = "devbench"
DATA_ENV_VAR = "SWEBENCH_DATA_DIR"

#: Match deepswe's bounded continuation ladder (3 rounds) so the two coding
#: benchmarks give the agent the same number of auto-resume chances.
_MAX_PLAN_CONTINUATIONS = 3

_WORKDIR = "/testbed"
_PROMPT = """{problem_statement}

---
You are working in `{repo}` at commit `{base}`. The checkout is this session's
project directory; the SAME tree is mounted in the task container at `{workdir}`,
and the task text's container paths were rewritten to that project directory. The
`terminal` tool runs inside the container with the task's official toolchain;
`read_file`, `write_file` and `patch_file` edit the same bytes from the host side.

- There is no internet access inside the task container.
- Fix the issue in the repository's source; do NOT modify test files and do not
  run `git commit`.
- Complete the task; do not ask questions.
- The project directory is `{project_dir}` and is the ONLY valid root for file
  tools. Never use `{workdir}` or any path outside it in file tools — the
  terminal translates project paths for you.

## Orchestration mandate (harness requirement, not advice)

1. Plan with `todowrite` and keep every item updated as you go.
2. Create ONE TaskFlow flow (`taskflow_flow(action="plan")`), drive its steps
   through `taskflow_step`, and close it with `taskflow_flow(action="finish")`
   when your own verification passed.
3. Verify your change inside the container (run the relevant tests) before
   finishing; an item is DONE only when its verification command passed.
"""


def load_dataset(path: Path) -> list[dict[str, Any]]:
    """Parse a SWE-bench-shaped dataset: JSON array / JSONL / HF wrapper."""
    records = env_setup.load_json_records(path)
    missing = [
        str(record.get("instance_id") or index)
        for index, record in enumerate(records)
        if not record.get("instance_id")
        or not record.get("repo")
        or not record.get("base_commit")
        or not record.get("problem_statement")
    ]
    if missing:
        raise ValueError(f"{path}: records missing required fields: {missing[:5]}")
    return records


def image_for(record: dict[str, Any]) -> str:
    """The pre-built task image: the record's own field, else the official
    swebench naming (``sweb.eval.x86_64.<instance_id lowercased>``)."""
    image = str(record.get("image") or "").strip()
    if image:
        return image
    instance = str(record.get("instance_id") or "").strip().lower()
    return f"sweb.eval.x86_64.{instance}"


def fail_to_pass(record: dict[str, Any]) -> list[str]:
    """The tests that MUST flip to passing (JSON or bare-line encoding)."""
    raw = record.get("FAIL_TO_PASS") or record.get("fail_to_pass") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = [line.strip() for line in raw.splitlines() if line.strip()]
    return [str(entry) for entry in raw]


def pass_to_pass(record: dict[str, Any]) -> list[str]:
    """The tests that must keep passing."""
    raw = record.get("PASS_TO_PASS") or record.get("pass_to_pass") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = [line.strip() for line in raw.splitlines() if line.strip()]
    return [str(entry) for entry in raw]


def build_test_command(record: dict[str, Any]) -> str:
    """The one pytest invocation the verifier runs: FAIL_TO_PASS first (the
    tests that MUST flip to passing), then PASS_TO_PASS (the ones that must
    survive). A missing FAIL_TO_PASS list means the dataset cannot be graded —
    the caller refuses the task instead of generating an empty command."""
    f2p = fail_to_pass(record)
    p2p = pass_to_pass(record)
    targets = " ".join(f'"{test}"' for test in dict.fromkeys([*f2p, *p2p]))
    return f"python -m pytest -rA --no-header -p no:cacheprovider {targets}"


def judge(record: dict[str, Any], pytest_output: str) -> tuple[float, bool, str]:
    """reward 1.0 iff every FAIL_TO_PASS test passed AND no PASS_TO_PASS
    regression; the detail names what missed (bounded)."""
    results = external_bench.parse_pytest_output(pytest_output)
    if results is None:
        return 0.0, False, "pytest output not parseable"
    f2p = fail_to_pass(record)
    p2p = pass_to_pass(record)
    misses = [test for test in f2p if str(results.get(_norm(test), "")).lower() != "passed"]
    regressions = [test for test in p2p if str(results.get(_norm(test), "")).lower() == "failed"]
    if misses or regressions:
        detail = "; ".join(
            [
                *[f"fail_to_pass not passing: {t}" for t in misses[:3]],
                *[f"pass_to_pass regressed: {t}" for t in regressions[:3]],
            ],
        )
        return 0.0, False, detail or "not resolved"
    return 1.0, True, ""


def _norm(test_id: str) -> str:
    """SWE-bench ids are full nodeids (``tests/test.py::Class::test``) while
    ``parse_pytest_output`` keys the LAST :: segment — normalize both sides."""
    return (test_id or "").strip().split("::")[-1].strip()


# ── Grading container ───────────────────────────────────────────────────────


def grade_in_container(
    image: str,
    model_patch: str,
    test_patch: str,
    record: dict[str, Any],
    out_dir: Path,
    *,
    timeout_s: float = 1800.0,
) -> tuple[float, bool, str]:
    """Apply both patches in the task image, run the tests, judge the output."""
    artifacts = out_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "model.patch").write_text(model_patch, encoding="utf-8", newline="\n")
    (artifacts / "test.patch").write_text(test_patch, encoding="utf-8", newline="\n")
    command = build_test_command(record)
    script = "\n".join(
        [
            "set -o pipefail",
            "cd /testbed",
            # The tests must be EXACTLY the held-out versions: reset every
            # modified test path the model may have touched, then apply.
            "git checkout -- . 2>/dev/null || true",
            "git apply --whitespace=nowarn /logs/artifacts/test.patch",
            "git apply --whitespace=nowarn /logs/artifacts/model.patch || "
            "echo 'model patch failed to apply'",
            command,
        ]
    )
    args = [
        external_bench.container_bin(),
        "run",
        "--rm",
        "--entrypoint",
        "/bin/bash",
        "-v",
        f"{artifacts}:/logs/artifacts:ro",
        image,
        "-c",
        script,
    ]
    completed = external_bench.run(args, timeout=timeout_s + 120.0)
    (out_dir / "verifier-output.txt").write_text(completed.output, encoding="utf-8")
    reward, resolved, detail = judge(record, completed.output)
    if "model patch failed to apply" in completed.output:
        return 0.0, False, "model patch failed to apply"
    return reward, resolved, detail


# ── One task ────────────────────────────────────────────────────────────────


async def run_task(
    record: dict[str, Any],
    results_dir: Path,
    *,
    timeout: float,
    keep_workdir: bool = False,
) -> dict[str, Any]:
    """One instance end to end: container, agent drive, patch, grade."""
    from evals.coding_capability import suite as harness

    instance_id = str(record["instance_id"])
    base_commit = str(record["base_commit"])
    image = image_for(record)
    session_id = f"evals-{SUITE}-{instance_id}-{uuid.uuid4().hex[:6]}"
    started = time.monotonic()
    task_root = (
        work_root(SUITE, results_dir, os.environ.get("SHERRY_EVAL_WORK_ROOT", "")) / instance_id
    )
    result: dict[str, Any] = {
        "id": instance_id,
        "repo": record.get("repo"),
        "base_commit": base_commit,
        "image": image,
        "session_id": session_id,
        "resolved": False,
        "reward": None,
        "failure_kind": "",
        "invalid_run": False,
        "duration_s": 0.0,
        "tool_calls": 0,
        "tool_names": {},
        "todolist_calls": 0,
        "taskflow_calls": 0,
        "orchestration_compliant": False,
        "timeout": False,
        "error": "",
        "reply": "",
        "tokens": {},
        "patch_path": "",
        "changed_paths": [],
    }
    container = TaskContainer(
        suite=SUITE,
        task_id=instance_id,
        image=image,
        host_workdir=task_root / "workspace",
        container_workdir=_WORKDIR,
        network="none",
    )
    try:
        if not external_bench.image_exists(image):
            pulled = external_bench.pull_image(image)
            if not pulled.ok:
                result["failure_kind"] = "harness"
                result["error"] = f"task image missing: {image} ({pulled.output[-300:]})"
                return result
        container.prepare()
        container.connect()
        await external_bench.bind_external_session(session_id, container.host_workdir)
        # Same reviewer contract as deepswe: extract decidable clauses from the
        # task text so the reviewer answers PASS/FAIL per item instead of
        # agreeing with the plan. No clauses -> checklist left unset (no-op).
        problem = str(record.get("problem_statement") or "")
        clauses = acceptance_checklist(problem)
        if clauses:
            external_bench.set_reviewer_checklist(clauses)
        prompt = _PROMPT.format(
            problem_statement=external_bench.localize_instruction(
                str(record.get("problem_statement") or ""),
                container.host_workdir,
                _WORKDIR,
            ),
            repo=record.get("repo"),
            base=base_commit[:12],
            workdir=_WORKDIR,
            project_dir=container.host_workdir,
        )
        drive = await _drive_with_continuations(session_id, prompt, harness, timeout, results_dir)
        result.update(
            {
                "tool_calls": drive["tool_calls"],
                "tool_names": dict(drive["tool_names"]),
                "todolist_calls": sum(
                    c for n, c in drive["tool_names"].items() if n.startswith("todo")
                ),
                "taskflow_calls": sum(
                    c for n, c in drive["tool_names"].items() if n.startswith("taskflow")
                ),
                "timeout": drive["timeout"],
                "error": drive["error"],
                "reply": drive["text"][:2000],
                "tokens": await harness._session_tokens(session_id),
            }
        )
        result["orchestration_compliant"] = bool(
            result["todolist_calls"] and result["taskflow_calls"]
        )
        model_patch = external_bench.run(
            ["git", "-C", str(container.host_workdir), "diff", base_commit],
            timeout=300.0,
        ).output
        patch_path = results_dir / f"patch-{instance_id}.patch"
        patch_path.write_text(model_patch, encoding="utf-8", newline="\n")
        result["patch_path"] = str(patch_path)
        result["changed_paths"] = [
            line[6:] for line in model_patch.splitlines() if line.startswith("+++ b/")
        ]
        grade_dir = results_dir / "verifier" / instance_id
        reward, resolved, detail = grade_in_container(
            image,
            model_patch,
            str(record.get("test_patch") or ""),
            record,
            grade_dir,
        )
        result["reward"] = reward
        result["resolved"] = resolved
        result["reward_detail"] = detail
        if not resolved and not drive["timeout"] and not drive["error"]:
            kind = harness._failure_kind(False, drive)
            result["failure_kind"] = kind
            result["invalid_run"] = kind == "provider"
        result["duration_s"] = round(time.monotonic() - started, 1)
        result["tokens"] = result["tokens"] or {}
    except Exception as exc:  # noqa: BLE001 — a harness error is reported as such
        result["failure_kind"] = "harness"
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["duration_s"] = round(time.monotonic() - started, 1)
        print(f"[evals] {instance_id}: HARNESS ERROR: {result['error']}", flush=True)
    finally:
        container.cleanup(keep_workdir=keep_workdir)
    return result


async def _drive_with_continuations(
    session_id: str, prompt: str, harness: Any, timeout: float, results_dir: Path
) -> dict[str, Any]:
    """Drive the task like deepswe does: one turn, then bounded continuations
    while the plan registry still has open items."""
    from evals.deepswe.suite import _open_plan_items as open_plan_items

    drive: dict[str, Any] = {
        "tool_calls": 0,
        "tool_names": {},
        "text": "",
        "timeout": False,
        "error": "",
    }
    for attempt in range(1 + _MAX_PLAN_CONTINUATIONS):
        turn = await asyncio.wait_for(harness._drive(session_id, prompt), timeout=timeout)
        drive["tool_calls"] += int(turn.get("tool_calls") or 0)
        for name, count in (turn.get("tool_names") or {}).items():
            drive["tool_names"][name] = drive["tool_names"].get(name, 0) + int(count)
        drive["text"] += str(turn.get("text") or "")
        if turn.get("timeout") or turn.get("error"):
            drive["timeout"] = bool(turn.get("timeout"))
            drive["error"] = str(turn.get("error") or drive["error"])
            break
        open_items = open_plan_items(results_dir)
        if not open_items or attempt >= _MAX_PLAN_CONTINUATIONS:
            break
        prompt = (
            "[SYSTEM DIRECTIVE: PLAN CONTINUATION]\nOpen items:\n"
            + "\n".join(f"- {item}" for item in open_items[:12])
            + "\nFinish them, verify, close the flow, then summarize."
        )
    return drive


# ── CLI ─────────────────────────────────────────────────────────────────────


def main() -> None:
    """Run the suite (the dispatcher passes the suite name as argv[0])."""
    external_bench.maybe_reexec("SHERRY_EVAL_DEVBENCH_CHILD", Path(__file__))
    args = sys.argv[1:]
    if args and args[0] == SUITE:
        args = args[1:]

    options = _parse_args(args)
    data_dir = env_setup.resolve_dataset_dir(SUITE, DATA_ENV_VAR)
    dataset_file = options["dataset"] or _find_dataset(data_dir)
    if dataset_file is None:
        print(
            f"[evals] {SUITE} SKIPPED: no dataset (looked in {data_dir}; set "
            f"{DATA_ENV_VAR} or pass --dataset). A skipped suite is not a pass.",
            flush=True,
        )
        raise SystemExit(3)

    docker_ok, docker_detail = external_bench.docker_available()
    try:
        records = load_dataset(Path(dataset_file))
    except (ValueError, OSError) as exc:
        print(f"[evals] HARNESS FAILURE: {exc}", flush=True)
        raise SystemExit(2) from exc

    ids = [str(record["instance_id"]) for record in records]
    ids = batch_runner.apply_batch(ids, options["batch"])
    if options["all"]:
        selected = [r for r in records if str(r["instance_id"]) in set(ids)]
    elif options["tasks"]:
        known = {str(r["instance_id"]): r for r in records}
        unknown = [t for t in options["tasks"] if t not in known]
        if unknown:
            raise SystemExit(f"unknown task id(s): {unknown}")
        selected = [known[t] for t in options["tasks"]]
    else:
        selected = records[: options["n_tasks"] or 5]
    if options["n_tasks"]:
        selected = selected[: options["n_tasks"]]
    if options["resume"]:
        before = len(selected)
        selected = batch_runner.apply_resume(
            [str(r["instance_id"]) for r in selected],
            REPO_ROOT / "evals" / "results" / SUITE,
        )
        selected = [r for r in records if str(r["instance_id"]) in set(selected)]
        print(
            f"[evals] resume: {before - len(selected)} already graded, {len(selected)} to go",
            flush=True,
        )

    print(
        f"[evals] {SUITE}: {len(records)} instances at {dataset_file} "
        f"({len(selected)} selected; docker: {docker_detail})",
        flush=True,
    )
    if options["dry_run"]:
        for record in selected:
            print(
                f"[evals] dry-run ok: {record['instance_id']} image={image_for(record)} "
                f"f2p={len(fail_to_pass(record))} p2p={len(pass_to_pass(record))}",
                flush=True,
            )
        print(
            "[evals] dry-run only; a real run needs Docker + task images + MAIN_LLM_API_KEY",
            flush=True,
        )
        return

    if not docker_ok:
        print(f"[evals] {SUITE} SKIPPED: Docker is not usable ({docker_detail})", flush=True)
        raise SystemExit(3)
    if not os.getenv("MAIN_LLM_API_KEY", "").strip():
        print(f"[evals] {SUITE} SKIPPED: MAIN_LLM_API_KEY is not configured", flush=True)
        raise SystemExit(3)

    raise SystemExit(asyncio.run(_run(records, selected, options)))


def _find_dataset(data_dir: Path) -> Path | None:
    """The dataset file inside *data_dir*, or None (the suite skips)."""
    if not data_dir.is_dir():
        return None
    try:
        return env_setup.pick_dataset_file(data_dir, "swebench_verified.json")
    except ValueError:
        return None


def _parse_args(args: list[str]) -> dict[str, Any]:
    options: dict[str, Any] = {
        "dataset": None,
        "all": False,
        "batch": None,
        "resume": False,
        "dry_run": False,
        "n_tasks": None,
        "timeout": env_setup.DEFAULT_TIMEOUT_S,
        "update_baseline": False,
        "keep_workdirs": False,
        "tasks": [],
    }
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in ("--all", "--resume", "--dry-run", "--update-baseline", "--keep-workdirs"):
            key = {
                "--all": "all",
                "--resume": "resume",
                "--dry-run": "dry_run",
                "--update-baseline": "update_baseline",
                "--keep-workdirs": "keep_workdirs",
            }[arg]
            options[key] = True
        elif arg in ("--dataset", "--batch", "--n-tasks", "--timeout"):
            if index + 1 >= len(args):
                raise SystemExit(f"{arg} needs a value")
            value: Any = args[index + 1]
            if arg == "--n-tasks":
                value = int(value)
            if arg == "--timeout":
                value = env_setup.resolve_timeout(float(value))
            options[
                {
                    "--dataset": "dataset",
                    "--batch": "batch",
                    "--n-tasks": "n_tasks",
                    "--timeout": "timeout",
                }[arg]
            ] = value
            index += 1
        elif arg.startswith("--"):
            raise SystemExit(f"unknown flag: {arg}")
        else:
            options["tasks"].append(arg)
        index += 1
    return options


async def _run(
    records: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    options: dict[str, Any],
) -> int:
    run_id = external_bench.run_id_now()
    results_dir = REPO_ROOT / "evals" / "results" / SUITE / run_id
    results_dir.mkdir(parents=True, exist_ok=True)
    eval_workspace = results_dir / "sandbox" / "workspace"
    os.environ["SHERRY_WORKSPACE_DIR"] = str(eval_workspace)
    import config.path  # noqa: F401 — hydrates .env / .models / .network
    import agent.core  # noqa: F401 — bind build_main_tools before the sandbox

    # Pin the same deterministic decoding + env-driven iteration budget every
    # coding suite uses (after workspace override / config hydration).
    pin_eval_budget(resolve_eval_iteration_budget())
    pin_eval_temperature()

    from evals.coding_capability import suite as harness
    from evals.sandbox import EvalSandbox

    sandbox = EvalSandbox(results_dir)
    sandbox.apply()
    os.environ["SANDBOX_POLICY"] = "auto"
    started = time.monotonic()
    results: list[dict[str, Any]] = []
    try:
        harness._seed_persona(eval_workspace)
        harness.init_agent()
        external_bench.trim_tools()
        external_bench.install_orchestration_gate()
        for record in selected:
            result = await run_task(
                record,
                results_dir,
                timeout=float(options["timeout"]),
                keep_workdir=bool(options["keep_workdirs"]),
            )
            results.append(result)
            print(
                f"[evals] {result['id']}: resolved={result['resolved']} "
                f"reward={result['reward']} duration={result['duration_s']}s",
                flush=True,
            )
    except Exception:  # noqa: BLE001 — setup bugs are harness failures
        traceback.print_exc()
        return 2
    finally:
        sandbox.restore()
        sandbox.close_aiosqlite_connections()

    report = score_reporter.build_report(
        run_id=run_id,
        suite=SUITE,
        kind=f"SWE-bench Verified slice ({len(results)} tasks) — external benchmark",
        results_dir=results_dir,
        results=results,
        started=started,
        model_info={**harness._model_info(), "thinking_enabled": False},
        benchmark={
            "dataset": str(options.get("dataset") or DATA_ENV_VAR),
            "grading": (
                "model.patch + test_patch applied in the task's own image; judged "
                "against the instance's FAIL_TO_PASS / PASS_TO_PASS via parse_pytest_output"
            ),
            "agent_network": "none",
        },
    )
    external_bench.write_report(results_dir, report)
    score_reporter.write_summary_txt(results_dir, report)
    return external_bench.publish(
        report, results_dir, BASELINE_PATH, update_baseline=bool(options["update_baseline"])
    )


if __name__ == "__main__":
    main()
