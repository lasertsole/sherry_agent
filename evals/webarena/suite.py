"""WebArena — the web-agent benchmark, driven through sherry's browser tools.

Environment: the official docker-compose stack (GitLab / Reddit / shopping /
CMS) runs RESIDENTLY across tasks — started once per run via
``evals.shared.container_manager.compose_up`` and torn down at the end.
Per-task containers do not exist in WebArena; the agent's terminal stays
host-side (and is trimmed: a web task needs no shell).

The agent's browser tooling is what ``agent/tools/browser/`` already ships
(CDP-driven Chromium): the harness's ``trim_tools`` is SKIPPED for this suite
(the plan's explicit requirement) and ``BROWSER_AGENT["enabled"]`` is forced
so the browser tool registers.

Tasks come from ``WEBARENA_DATA_DIR`` (or ``--dataset``): the benchmark's
``config files/*.json`` — ``{task_id, intent, start_url, evaluator}``. The
evaluator subset this adapter judges natively is URL-shaped
(``url_last`` / ``url_regex`` — where the navigation ENDED); anything else
(element checks, function calls) needs the official evaluator repo: point
``WEBARENA_REPO_DIR`` at a clone and the adapter shells out to it
(``python scripts/get_evaluation_data.py``-style) — fail-open with an honest
"unjudged" per task, never a fabricated pass.
"""

from __future__ import annotations

import asyncio
import os
import re
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
from evals.shared import batch_runner, container_manager, env_setup, score_reporter  # noqa: E402

BASELINE_PATH = Path(__file__).with_name("baseline.json")
SUITE = "webarena"
DATA_ENV_VAR = "WEBARENA_DATA_DIR"


def load_dataset(path: Path) -> list[dict[str, Any]]:
    """Parse + validate the task-config records (intent required)."""
    records = env_setup.load_json_records(path)
    cleaned: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        task_id = str(record.get("task_id") or f"webarena-{index:04d}")
        intent = str(record.get("intent") or record.get("question") or "").strip()
        if not intent:
            raise ValueError(f"{path}: record {task_id} has no intent")
        cleaned.append(
            {
                "task_id": task_id,
                "intent": intent,
                "start_url": str(record.get("start_url") or ""),
                "evaluator": record.get("evaluator") or {},
            }
        )
    return cleaned


def evaluate_url(eval_spec: dict[str, Any], final_url: str) -> tuple[bool, str]:
    """The native URL judge: the spec's ``url_last`` (exact) or ``url_regex``
    (search) matched against the page the agent ended on."""
    expected = str(eval_spec.get("url_last") or "").strip()
    if expected:
        return _urls_match(final_url, expected), f"url_last {expected!r}"
    pattern = str(eval_spec.get("url_regex") or "").strip()
    if pattern:
        return bool(re.search(pattern, final_url)), f"url_regex {pattern!r}"
    return False, "no url judge in the eval spec"


def _urls_match(final_url: str, expected: str) -> bool:
    """Same page ignoring trailing slashes and the fixed host prefix, when the
    task's start_url host equals the expected one (ports differ per compose)."""
    normalize = lambda url: re.sub(r"/+$", "", url.strip())  # noqa: E731
    if normalize(final_url) == normalize(expected):
        return True
    final_path = normalize(final_url).split("//", 1)[-1].split("/", 1)
    expected_path = normalize(expected).split("//", 1)[-1].split("/", 1)
    return len(final_path) == 2 and len(expected_path) == 2 and final_path[1] == expected_path[1]


def final_page_url(messages: list[Any]) -> str:
    """Where the agent's browser ended: the LAST navigation result's URL from
    the transcript (each browser_navigate result renders ``· <url>``). No
    manager coupling, and an absent navigation is an empty string — an honest
    miss, never a fabricated one."""
    for message in reversed(messages):
        name = str(getattr(message, "name", "") or "")
        if name and name not in ("browser_navigate", "browser_merged", "browser"):
            continue
        content = message.content if isinstance(message.content, str) else str(message.content)
        matches = re.findall(r"· (https?://\S+)", content)
        if matches:
            return matches[-1]
    return ""


def main() -> None:
    """Run the suite."""
    external_bench.maybe_reexec("SHERRY_EVAL_WEBARENA_CHILD", Path(__file__))
    args = sys.argv[1:]
    if args and args[0] == SUITE:
        args = args[1:]

    options = _parse_args(args)
    data_dir = env_setup.resolve_dataset_dir(SUITE, DATA_ENV_VAR)
    dataset_file = options["dataset"] or _find_dataset(data_dir)
    if dataset_file is None:
        print(
            f"[evals] {SUITE} SKIPPED: no dataset (looked in {data_dir}; set "
            f"{DATA_ENV_VAR} or pass --dataset — the benchmark's config_files "
            "JSONs). A skipped suite is not a pass.",
            flush=True,
        )
        raise SystemExit(3)
    try:
        records = load_dataset(Path(dataset_file))
    except (ValueError, OSError) as exc:
        print(f"[evals] HARNESS FAILURE: {exc}", flush=True)
        raise SystemExit(2) from exc

    selected = _select(records, options)
    compose_file = Path(options["compose_file"] or data_dir / "docker-compose.yml")
    print(
        f"[evals] {SUITE}: {len(records)} tasks ({len(selected)} selected; "
        f"compose={compose_file} base={options['base_url']})",
        flush=True,
    )
    if options["dry_run"]:
        for record in selected:
            print(
                f"[evals] dry-run ok: {record['task_id']} start={record['start_url']}", flush=True
            )
        return

    if not os.getenv("MAIN_LLM_API_KEY", "").strip():
        print(f"[evals] {SUITE} SKIPPED: MAIN_LLM_API_KEY is not configured", flush=True)
        raise SystemExit(3)

    raise SystemExit(asyncio.run(_run(records, selected, compose_file, options)))


def _find_dataset(data_dir: Path) -> Path | None:
    if not data_dir.is_dir():
        return None
    try:
        return env_setup.pick_dataset_file(data_dir, "tasks.json", "config_files.json")
    except ValueError:
        return None


def _parse_args(args: list[str]) -> dict[str, Any]:
    from evals.devbench.suite import _parse_args as base_parse

    options = base_parse(args)
    index = 0
    while index < len(args):
        if args[index] in ("--compose-file", "--base-url", "--repo-dir"):
            if index + 1 >= len(args):
                raise SystemExit(f"{args[index]} needs a value")
            key = {
                "--compose-file": "compose_file",
                "--base-url": "base_url",
                "--repo-dir": "repo_dir",
            }[args[index]]
            options[key] = args[index + 1]
            index += 2
            continue
        index += 1
    options.setdefault("base_url", os.environ.get("WEBARENA_BASE_URL", "http://localhost:8080"))
    options.setdefault("compose_file", os.environ.get("WEBARENA_COMPOSE_FILE", ""))
    options.setdefault("repo_dir", os.environ.get("WEBARENA_REPO_DIR", ""))
    return options


def _select(records: list[dict[str, Any]], options: dict[str, Any]) -> list[dict[str, Any]]:
    ids = [str(record["task_id"]) for record in records]
    ids = batch_runner.apply_batch(ids, options["batch"])
    by_id = {str(record["task_id"]): record for record in records}
    if options["tasks"]:
        unknown = [t for t in options["tasks"] if t not in by_id]
        if unknown:
            raise SystemExit(f"unknown task id(s): {unknown}")
        selected = [by_id[task_id] for task_id in options["tasks"]]
    else:
        selected = [by_id[task_id] for task_id in ids]
    if options["n_tasks"]:
        selected = selected[: options["n_tasks"]]
    if options["resume"]:
        keep = set(
            batch_runner.apply_resume(
                [str(record["task_id"]) for record in selected],
                REPO_ROOT / "evals" / "results" / SUITE,
            )
        )
        selected = [record for record in selected if str(record["task_id"]) in keep]
    return selected


async def _run(
    records: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    compose_file: Path,
    options: dict[str, Any],
) -> int:
    import agent.core  # noqa: F401
    from config.features import BROWSER_AGENT  # noqa: PLC0415 — forced below
    from evals.coding_capability import suite as harness
    from evals.sandbox import EvalSandbox

    run_id = external_bench.run_id_now()
    results_dir = REPO_ROOT / "evals" / "results" / SUITE / run_id
    results_dir.mkdir(parents=True, exist_ok=True)
    os.environ["SHERRY_WORKSPACE_DIR"] = str(results_dir / "sandbox" / "workspace")
    import config.path  # noqa: F401

    compose_ok, compose_detail = container_manager.compose_up(compose_file, f"sherry-{SUITE}")
    if not compose_ok:
        print(
            f"[evals] {SUITE} SKIPPED: the web applications could not start "
            f"({compose_detail}). A skipped suite is not a pass.",
            flush=True,
        )
        return 3

    sandbox = EvalSandbox(results_dir)
    sandbox.apply()
    os.environ["SANDBOX_POLICY"] = "auto"
    started = time.monotonic()
    results: list[dict[str, Any]] = []
    try:
        harness._seed_persona(results_dir / "sandbox" / "workspace")
        harness.init_agent()
        # The plan's requirement: the browser surface STAYS. trim_tools is
        # deliberately not called; the browser tool's enable flag is forced.
        BROWSER_AGENT["enabled"] = 1
        external_bench.install_orchestration_gate()
        for record in selected:
            session_id = f"evals-{SUITE}-{record['task_id']}-{uuid.uuid4().hex[:6]}"
            started_task = time.monotonic()
            result: dict[str, Any] = {
                "id": record["task_id"],
                "session_id": session_id,
                "resolved": False,
                "failure_kind": "",
                "invalid_run": False,
                "duration_s": 0.0,
                "tool_calls": 0,
                "tool_names": {},
                "timeout": False,
                "error": "",
                "final_url": "",
                "tokens": {},
            }
            try:
                prompt = (
                    f"{record['intent']}\n\n---\nThe web application is at "
                    f"{options['base_url']}. Use the `browser` tools to navigate"
                    + (f" (start at {record['start_url']})" if record["start_url"] else "")
                    + " and complete the task. Do not ask questions."
                )
                drive = await asyncio.wait_for(
                    harness._drive(session_id, prompt), timeout=float(options["timeout"])
                )
                result.update(
                    {
                        "tool_calls": int(drive.get("tool_calls") or 0),
                        "tool_names": dict(drive.get("tool_names") or {}),
                        "timeout": bool(drive.get("timeout")),
                        "error": str(drive.get("error") or ""),
                        "reply": str(drive.get("text") or "")[:1500],
                        "tokens": await harness._session_tokens(session_id),
                    }
                )
                from agent.state_port import read_messages  # noqa: PLC0415 — lazy

                messages = await read_messages(session_id)
                result["final_url"] = final_page_url(messages)
                resolved, detail = evaluate_url(record["evaluator"], result["final_url"])
                result["resolved"] = resolved
                result["reward_detail"] = detail
                if not resolved:
                    result["failure_kind"] = harness._failure_kind(False, drive)
                result["duration_s"] = round(time.monotonic() - started_task, 1)
            except Exception as exc:  # noqa: BLE001
                result["failure_kind"] = "harness"
                result["error"] = f"{type(exc).__name__}: {exc}"
                result["duration_s"] = round(time.monotonic() - started_task, 1)
            results.append(result)
            print(
                f"[evals] {result['id']}: resolved={result['resolved']} url={result['final_url']}",
                flush=True,
            )
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 2
    finally:
        sandbox.restore()
        sandbox.close_aiosqlite_connections()
        container_manager.compose_down(compose_file, f"sherry-{SUITE}")

    report = score_reporter.build_report(
        run_id=run_id,
        suite=SUITE,
        kind=f"WebArena slice ({len(results)} tasks) — external benchmark",
        results_dir=results_dir,
        results=results,
        started=started,
        model_info={**harness._model_info()},
        benchmark={
            "dataset": str(options.get("dataset") or DATA_ENV_VAR),
            "environment": f"docker-compose stack at {options['base_url']} ({compose_detail})",
            "grading": (
                "native: url_last/url_regex on the final page; other evaluators "
                "need WEBARENA_REPO_DIR (the official evaluator) and are unjudged"
            ),
            "tools": "browser surface kept (trim_tools skipped)",
        },
    )
    external_bench.write_report(results_dir, report)
    score_reporter.write_summary_txt(results_dir, report)
    return external_bench.publish(
        report, results_dir, BASELINE_PATH, update_baseline=bool(options["update_baseline"])
    )


if __name__ == "__main__":
    main()
