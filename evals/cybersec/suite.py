"""CyberSecEval-style benchmark — security MCQ / code analysis, host mode.

The plan's own assessment: CyberSecEval is mostly MULTIPLE-CHOICE and code
analysis — no container interaction. One dataset file
(``CYBERSEC_DATA_DIR`` / ``--dataset``): JSONL/JSON records of
``{task_id, question, choices: [...], answer: "A", category?}``. The agent
answers from its security knowledge (no tools beyond the question surface:
the answer must be reasoning, not retrieval — `web_search` stays available
because part of the benchmark is open-book).

Scoring: the chosen LETTER must match. The model is instructed to answer with
exactly one letter on the last line; extraction takes the last bare letter.
A missing dataset SKIPS (exit 3). This is also the natural slot for SecBench
records with the same shape.
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
from evals.gaia import suite as gaia_suite  # noqa: E402  (the host-mode runner shape)
from evals.shared import env_setup, score_reporter  # noqa: E402

BASELINE_PATH = Path(__file__).with_name("baseline.json")
SUITE = "cybersec"
DATA_ENV_VAR = "CYBERSEC_DATA_DIR"
LETTERS = "ABCDEFGH"


def load_dataset(path: Path) -> list[dict[str, Any]]:
    """Parse + validate the MCQ dataset (question/choices/answer required)."""
    records = env_setup.load_json_records(path)
    cleaned: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        task_id = str(record.get("task_id") or f"cybersec-{index:04d}")
        question = str(record.get("question") or record.get("prompt") or "").strip()
        choices = record.get("choices") or record.get("options") or []
        answer = str(record.get("answer") or "").strip().upper()
        if not question or not choices or not answer:
            raise ValueError(f"{path}: record {task_id} needs question/choices/answer")
        if answer not in LETTERS[: len(choices)]:
            raise ValueError(f"{path}: record {task_id} answer {answer!r} out of range")
        cleaned.append(
            {
                "task_id": task_id,
                "question": question,
                "choices": [str(choice) for choice in choices],
                "answer": answer,
                "category": str(record.get("category") or ""),
            }
        )
    return cleaned


def format_prompt(record: dict[str, Any]) -> str:
    """The question + lettered choices + the answer-format instruction."""
    lines = [record["question"], ""]
    for index, choice in enumerate(record["choices"]):
        lines.append(f"{LETTERS[index]}. {choice}")
    lines += [
        "",
        "Answer with reasoning if useful, but your LAST line must be exactly the",
        "letter of the correct choice (e.g. `C`).",
    ]
    return "\n".join(lines)


def extract_choice(text: str, choice_count: int) -> str:
    """The last bare-letter line wins; a standalone letter anywhere in the
    last line is the fallback (`The answer is (C).` → C)."""
    valid = set(LETTERS[:choice_count])
    lines = [line.strip() for line in (text or "").strip().splitlines() if line.strip()]
    for line in reversed(lines):
        candidate = line.strip().strip("*`\"'.() ").upper()
        if len(candidate) == 1 and candidate in valid:
            return candidate
    if lines:
        inline = re.findall(r"\b([A-H])\b", lines[-1].upper())
        for letter in reversed(inline):
            if letter in valid:
                return letter
    return ""


def score_answer(answer: str, expected: str) -> bool:
    """Case-insensitive letter match."""
    return bool(answer) and answer.strip().upper() == expected.strip().upper()


def main() -> None:
    """Run the suite (host mode)."""
    external_bench.maybe_reexec("SHERRY_EVAL_CYBERSEC_CHILD", Path(__file__))
    args = sys.argv[1:]
    if args and args[0] == SUITE:
        args = args[1:]

    options = gaia_suite.devbench_options(args)
    data_dir = env_setup.resolve_dataset_dir(SUITE, DATA_ENV_VAR)
    dataset_file = options["dataset"] or _find_dataset(data_dir)
    if dataset_file is None:
        print(
            f"[evals] {SUITE} SKIPPED: no dataset (looked in {data_dir}; set "
            f"{DATA_ENV_VAR} or pass --dataset). A skipped suite is not a pass.",
            flush=True,
        )
        raise SystemExit(3)
    try:
        records = load_dataset(Path(dataset_file))
    except (ValueError, OSError) as exc:
        print(f"[evals] HARNESS FAILURE: {exc}", flush=True)
        raise SystemExit(2) from exc

    selected = gaia_suite._select(records, options)
    print(
        f"[evals] {SUITE}: {len(records)} tasks at {dataset_file} ({len(selected)} selected)",
        flush=True,
    )
    if options["dry_run"]:
        for record in selected:
            print(f"[evals] dry-run ok: {record['task_id']} answer={record['answer']}", flush=True)
        return
    if not os.getenv("MAIN_LLM_API_KEY", "").strip():
        print(f"[evals] {SUITE} SKIPPED: MAIN_LLM_API_KEY is not configured", flush=True)
        raise SystemExit(3)
    raise SystemExit(asyncio.run(_run(selected, options)))


def _find_dataset(data_dir: Path) -> Path | None:
    """The dataset file inside *data_dir*, or None (the suite skips)."""
    if not data_dir.is_dir():
        return None
    try:
        return env_setup.pick_dataset_file(data_dir, "cybersec.json", "cybersec.jsonl")
    except ValueError:
        return None


async def _run(selected: list[dict[str, Any]], options: dict[str, Any]) -> int:
    import agent.core  # noqa: F401
    from evals.coding_capability import suite as harness
    from evals.sandbox import EvalSandbox

    run_id = external_bench.run_id_now()
    results_dir = REPO_ROOT / "evals" / "results" / SUITE / run_id
    results_dir.mkdir(parents=True, exist_ok=True)
    eval_workspace = results_dir / "sandbox" / "workspace"
    os.environ["SHERRY_WORKSPACE_DIR"] = str(eval_workspace)
    import config.path  # noqa: F401

    sandbox = EvalSandbox(results_dir)
    sandbox.apply()
    os.environ["SANDBOX_POLICY"] = "auto"
    started = time.monotonic()
    results: list[dict[str, Any]] = []
    try:
        harness._seed_persona(eval_workspace)
        harness.init_agent()
        external_bench.trim_tools()  # file edits are meaningless here
        for record in selected:
            session_id = f"evals-{SUITE}-{record['task_id']}-{uuid.uuid4().hex[:6]}"
            started_task = time.monotonic()
            result: dict[str, Any] = {
                "id": record["task_id"],
                "category": record["category"],
                "session_id": session_id,
                "resolved": False,
                "failure_kind": "",
                "invalid_run": False,
                "duration_s": 0.0,
                "tool_calls": 0,
                "tool_names": {},
                "timeout": False,
                "error": "",
                "answer": "",
                "expected": record["answer"],
                "tokens": {},
            }
            try:
                drive = await asyncio.wait_for(
                    harness._drive(session_id, format_prompt(record)),
                    timeout=float(options["timeout"]),
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
                result["answer"] = extract_choice(
                    str(drive.get("text") or ""), len(record["choices"])
                )
                result["resolved"] = score_answer(result["answer"], record["answer"])
                if not result["resolved"]:
                    kind = harness._failure_kind(False, drive)
                    result["failure_kind"] = kind
                    result["invalid_run"] = kind == "provider"
                result["duration_s"] = round(time.monotonic() - started_task, 1)
            except Exception as exc:  # noqa: BLE001
                result["failure_kind"] = "harness"
                result["error"] = f"{type(exc).__name__}: {exc}"
                result["duration_s"] = round(time.monotonic() - started_task, 1)
            results.append(result)
            print(
                f"[evals] {result['id']}: {result['answer']!r} vs {record['answer']!r}", flush=True
            )
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 2
    finally:
        sandbox.restore()
        sandbox.close_aiosqlite_connections()

    report = score_reporter.build_report(
        run_id=run_id,
        suite=SUITE,
        kind=f"CyberSecEval-style slice ({len(results)} tasks) — external benchmark",
        results_dir=results_dir,
        results=results,
        started=started,
        model_info={**harness._model_info()},
        benchmark={
            "dataset": str(options.get("dataset") or DATA_ENV_VAR),
            "grading": "choice-letter match",
        },
    )
    external_bench.write_report(results_dir, report)
    score_reporter.write_summary_txt(results_dir, report)
    return external_bench.publish(
        report, results_dir, BASELINE_PATH, update_baseline=bool(options["update_baseline"])
    )


if __name__ == "__main__":
    main()
