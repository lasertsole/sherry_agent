"""Multi-SWE-bench — ByteDance's multi-language SWE-bench on the sherry harness.

The record schema matches SWE-bench (``instance_id`` / ``repo`` /
``base_commit`` / ``problem_statement`` / ``test_patch`` plus per-language
test lists), so the ADAPTER is the devbench core; the differences:

- the dataset comes from ``MULTI_SWE_BENCH_DATA_DIR`` (or ``--dataset``);
- ``--lang java|go|rust|...`` narrows the selection (the dataset's ``language``
  or ``lang`` field; the plan's own note says the exact field names were to be
  verified once the network allowed — both spellings are accepted and an
  unknown value lists what the dataset actually carries);
- the task image must come from the record itself (``image`` field) — the
  official swebench naming does not exist here, so a record without one is a
  per-task harness skip, never a silent pass.

Grading mirrors devbench: model patch + test_patch in the task image, judged
against the record's test lists.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals import external_bench  # noqa: E402
from evals.shared.eval_iteration_budget import (  # noqa: E402
    pin_eval_budget,
    resolve_eval_iteration_budget,
)
from evals.shared.eval_temperature import pin_eval_temperature  # noqa: E402
from evals.devbench import suite as devbench  # noqa: E402
from evals.shared import batch_runner, env_setup, score_reporter  # noqa: E402

BASELINE_PATH = Path(__file__).with_name("baseline.json")
SUITE = "multi_swe_bench"
DATA_ENV_VAR = "MULTI_SWE_BENCH_DATA_DIR"


def language_of(record: dict[str, Any]) -> str:
    """The record's language (both `language` and `lang` spellings)."""
    return str(record.get("language") or record.get("lang") or "").strip().lower()


def load_dataset(path: Path) -> list[dict[str, Any]]:
    """The devbench loader + the language-field requirement."""
    records = devbench.load_dataset(path)
    languages = sorted({language_of(record) for record in records})
    if any(not language for language in languages):
        raise ValueError(
            f"{path}: records without a language field ({languages}); Multi-SWE-bench "
            "needs it for the --lang filter and the image mapping"
        )
    return records


def filter_language(records: list[dict[str, Any]], language: str) -> list[dict[str, Any]]:
    """``--lang`` narrowing; an unknown value refuses with what exists."""
    if not language:
        return list(records)
    wanted = language.strip().lower()
    available = sorted({language_of(record) for record in records})
    if wanted not in available:
        raise SystemExit(
            f"unknown language {wanted!r}; the dataset carries: {', '.join(available)}"
        )
    return [record for record in records if language_of(record) == wanted]


async def run_task(record: dict[str, Any], results_dir: Path, **kwargs: Any) -> dict[str, Any]:
    """The devbench task loop with one override: the image MUST be explicit."""
    if not str(record.get("image") or "").strip():
        instance = str(record.get("instance_id"))
        return {
            "id": instance,
            "resolved": False,
            "reward": None,
            "failure_kind": "harness",
            "invalid_run": False,
            "error": (
                "record has no 'image' field — Multi-SWE-bench images are "
                "per-language builds the operator names in the dataset"
            ),
            "duration_s": 0.0,
            "language": language_of(record),
            "tokens": {},
        }
    result = await devbench.run_task(record, results_dir, **kwargs)
    result["language"] = language_of(record)
    return result


def main() -> None:
    """Run the suite (the dispatcher passes the suite name as argv[0])."""
    external_bench.maybe_reexec("SHERRY_EVAL_MSWE_CHILD", Path(__file__))
    args = sys.argv[1:]
    if args and args[0] == SUITE:
        args = args[1:]

    options = devbench._parse_args(args)
    language = ""
    index = 0
    while index < len(args):
        if args[index] == "--lang":
            if index + 1 >= len(args):
                raise SystemExit("--lang needs a value")
            language = args[index + 1]
            index += 2
            continue
        index += 1

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
        records = filter_language(load_dataset(Path(dataset_file)), language)
    except (ValueError, OSError) as exc:
        print(f"[evals] HARNESS FAILURE: {exc}", flush=True)
        raise SystemExit(2) from exc

    ids = [str(record["instance_id"]) for record in records]
    ids = batch_runner.apply_batch(ids, options["batch"])
    by_id = {str(r["instance_id"]): r for r in records}
    if options["tasks"]:
        unknown = [t for t in options["tasks"] if t not in by_id]
        if unknown:
            raise SystemExit(f"unknown task id(s): {unknown}")
        selected = [by_id[t] for t in options["tasks"]]
    else:
        selected = [by_id[task_id] for task_id in ids]
    if options["n_tasks"]:
        selected = selected[: options["n_tasks"]]
    if options["resume"]:
        before = len(selected)
        keep = set(
            batch_runner.apply_resume(
                [str(r["instance_id"]) for r in selected],
                REPO_ROOT / "evals" / "results" / SUITE,
            )
        )
        selected = [r for r in selected if str(r["instance_id"]) in keep]
        print(
            f"[evals] resume: {before - len(selected)} already graded, {len(selected)} to go",
            flush=True,
        )

    print(
        f"[evals] {SUITE}: {len(records)} instances"
        + (f" [lang={language}]" if language else "")
        + f" ({len(selected)} selected; docker: {docker_detail})",
        flush=True,
    )
    if options["dry_run"]:
        for record in selected:
            print(
                f"[evals] dry-run ok: {record['instance_id']} "
                f"lang={language_of(record)} image={devbench.image_for(record)}",
                flush=True,
            )
        print("[evals] dry-run only", flush=True)
        return

    if not docker_ok:
        print(f"[evals] {SUITE} SKIPPED: Docker is not usable ({docker_detail})", flush=True)
        raise SystemExit(3)
    if not os.getenv("MAIN_LLM_API_KEY", "").strip():
        print(f"[evals] {SUITE} SKIPPED: MAIN_LLM_API_KEY is not configured", flush=True)
        raise SystemExit(3)

    raise SystemExit(asyncio.run(_run(selected, options)))


def _find_dataset(data_dir: Path) -> Path | None:
    if not data_dir.is_dir():
        return None
    try:
        return env_setup.pick_dataset_file(data_dir, "multi_swe_bench.json")
    except ValueError:
        return None


async def _run(selected: list[dict[str, Any]], options: dict[str, Any]) -> int:
    run_id = external_bench.run_id_now()
    results_dir = REPO_ROOT / "evals" / "results" / SUITE / run_id
    results_dir.mkdir(parents=True, exist_ok=True)
    eval_workspace = results_dir / "sandbox" / "workspace"
    os.environ["SHERRY_WORKSPACE_DIR"] = str(eval_workspace)
    import config.path  # noqa: F401
    import agent.core  # noqa: F401

    # Same eval controls as the other coding suites (devbench.run_task below
    # inherits them); after workspace override / config hydration.
    pin_eval_budget(resolve_eval_iteration_budget())
    pin_eval_temperature()

    from evals.coding_capability import suite as harness
    from evals.sandbox import EvalSandbox

    sandbox = EvalSandbox(results_dir)
    sandbox.apply()
    os.environ["SANDBOX_POLICY"] = "auto"
    started = __import__("time").monotonic()
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
    except Exception:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        return 2
    finally:
        sandbox.restore()
        sandbox.close_aiosqlite_connections()

    report = score_reporter.build_report(
        run_id=run_id,
        suite=SUITE,
        kind=f"Multi-SWE-bench slice ({len(results)} tasks) — external benchmark",
        results_dir=results_dir,
        results=results,
        started=started,
        model_info={**harness._model_info(), "thinking_enabled": False},
        benchmark={
            "dataset": str(options.get("dataset") or DATA_ENV_VAR),
            "grading": "same as devbench: model.patch + test_patch in the task image",
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
