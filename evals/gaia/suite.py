"""GAIA — the general-assistant benchmark, scored by the official answer match.

Host mode by design (GAIA's tasks are question answering with optional file
attachments — no task container): the agent gets the FULL tool surface
(`terminal` / `python_repl` / file tools / `web_search` / `web_fetch`), the
task's attachment is staged into the sandbox workspace, and the answer is
extracted from the conversation's final ``FINAL ANSWER: …`` line. Scoring
reimplements the official ``gaia_scorer.py`` normalization exactly (case,
articles, punctuation, number separators) so a correct answer scores 1 the
same way the leaderboard's grader would.

Dataset: ``GAIA_DATA_DIR`` (or ``--dataset``) carrying ``metadata.json``-style
records — ``task_id`` / ``level`` / ``Question`` / ``Final answer`` /
``file_name`` — the official test metadata shape; both the official
capitalized keys and lowercase aliases are accepted. A missing dataset SKIPS
(exit 3); note the official files ship as HF-gated zips, which is exactly the
plan's "dataset needs auth → skip" case.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import string
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
from evals.shared import env_setup, score_reporter  # noqa: E402

BASELINE_PATH = Path(__file__).with_name("baseline.json")
SUITE = "gaia"
DATA_ENV_VAR = "GAIA_DATA_DIR"

_PROMPT = """{question}

---
Solve this task. The workspace is `{project_dir}`; any task attachments are
already placed there (file name: `{file_name}`). You have the full tool
surface: `terminal` / `python_repl` run on THIS host (there is no task
container), `read_file` / `write_file` / `patch_file` edit the workspace,
`web_search` / `web_fetch` / `browser` reach the internet.

Finish with your answer on a line of the EXACT form:

FINAL ANSWER: <answer>

That line is what gets graded. Do not ask questions; make the most reasonable
assumption when something is ambiguous.
"""


# ── The official scorer (gaia_scorer.py's normalization, faithfully) ────────


def _normalize_number(text: str) -> str:
    """The official number split: billion/million shorthand, comma separators,
    trailing %, and US-format long numbers become plain digits."""
    text = text.strip().replace("%", "")
    split = text.split(" ")[0].replace(",", "").replace("$", "")
    try:
        number = float(split)
    except ValueError:
        return text.lower()
    if number.is_integer():
        return str(int(number))
    return str(number)


def normalize_answer(text: str) -> str:
    """The official ``normalize_final_answer``: lowercase, drop articles and
    punctuation, normalize numbers — order of operations matches the grader."""
    text = (text or "").strip()
    for article in ("a ", "an ", "the "):
        text = re.sub(rf"\b{article}", "", text, flags=re.IGNORECASE)
    counters = [
        (
            re.compile(r"\b([0-9]+(?:\.[0-9]*)?)\s*billion\b", re.IGNORECASE),
            lambda m: f"{float(m.group(1)) * 1_000_000_000:.0f}",
        ),
        (
            re.compile(r"\b([0-9]+(?:\.[0-9]*)?)\s*million\b", re.IGNORECASE),
            lambda m: f"{float(m.group(1)) * 1_000_000:.0f}",
        ),
    ]
    for pattern, repl in counters:
        text = pattern.sub(repl, text)
    if _looks_numeric(text):
        return _normalize_number(text)
    text = text.strip().lower()
    text = text.translate(str.maketrans("", "", string.punctuation))
    return re.sub(r"\s+", " ", text).strip()


def _looks_numeric(text: str) -> bool:
    stripped = text.strip().replace("$", "").replace("%", "").replace(",", "")
    return bool(
        re.fullmatch(
            r"-?[0-9]+(\.[0-9]+)?(\s*(billion|million))?", stripped.strip(), flags=re.IGNORECASE
        )
    )


def score_answer(final_answer: str, expected: str) -> bool:
    """Exact match AFTER normalization — the official grader's equality."""
    return normalize_answer(final_answer) == normalize_answer(expected)


def extract_final_answer(text: str) -> str:
    """The conversation's LAST ``FINAL ANSWER: …`` line (case-insensitive),
    else the last non-empty line (the model was told the exact form)."""
    matches = re.findall(r"final answer\s*[:：]\s*(.+)", text or "", flags=re.IGNORECASE)
    if matches:
        return matches[-1].strip().strip("*`\"'")
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""


# ── Dataset ─────────────────────────────────────────────────────────────────


def load_dataset(path: Path) -> list[dict[str, Any]]:
    """The official metadata shape (capitalized or lowercase keys)."""
    records = env_setup.load_json_records(path)
    cleaned: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        task_id = str(
            record.get("task_id") or record.get("task_id ".strip()) or f"gaia-{index:04d}"
        )
        question = str(record.get("Question") or record.get("question") or "").strip()
        expected = str(
            record.get("Final answer") or record.get("final_answer") or record.get("answer") or ""
        ).strip()
        if not question or not expected:
            raise ValueError(f"{path}: record {task_id} missing Question/Final answer")
        cleaned.append(
            {
                "task_id": task_id,
                "level": str(record.get("Level") or record.get("level") or ""),
                "question": question,
                "final_answer": expected,
                "file_name": str(record.get("file_name") or record.get("file_name ") or "").strip(),
                "annotator_metadata": str(
                    record.get("Annotator metadata") or record.get("annotator_metadata") or ""
                ),
            }
        )
    return cleaned


def stage_attachment(record: dict[str, Any], data_dir: Path, workspace: Path) -> Path | None:
    """Copy the task's attachment into the sandbox workspace (fail-open: a
    missing attachment is recorded, never fatal — the agent answers from text)."""
    name = record.get("file_name") or ""
    if not name:
        return None
    source = data_dir / str(name)
    if not source.is_file():
        return None
    workspace.mkdir(parents=True, exist_ok=True)
    target = workspace / str(name)
    shutil.copyfile(source, target)
    return target


# ── CLI ─────────────────────────────────────────────────────────────────────


def main() -> None:
    """Run the suite (host mode: no Docker needed, only keys and the dataset)."""
    external_bench.maybe_reexec("SHERRY_EVAL_GAIA_CHILD", Path(__file__))
    args = sys.argv[1:]
    if args and args[0] == SUITE:
        args = args[1:]

    options = devbench_options(args)
    data_dir = env_setup.resolve_dataset_dir(SUITE, DATA_ENV_VAR)
    dataset_file = options["dataset"] or _find_dataset(data_dir)
    if dataset_file is None:
        print(
            f"[evals] {SUITE} SKIPPED: no dataset (looked in {data_dir}; set "
            f"{DATA_ENV_VAR} or pass --dataset). The official files are HF-gated — "
            "download them manually. A skipped suite is not a pass.",
            flush=True,
        )
        raise SystemExit(3)
    try:
        records = load_dataset(Path(dataset_file))
    except (ValueError, OSError) as exc:
        print(f"[evals] HARNESS FAILURE: {exc}", flush=True)
        raise SystemExit(2) from exc

    selected = _select(records, options)
    print(
        f"[evals] {SUITE}: {len(records)} tasks at {dataset_file} ({len(selected)} selected)",
        flush=True,
    )
    if options["dry_run"]:
        for record in selected:
            print(
                f"[evals] dry-run ok: {record['task_id']} level={record['level']} "
                f"file={'yes' if record['file_name'] else 'no'}",
                flush=True,
            )
        return

    if not os.getenv("MAIN_LLM_API_KEY", "").strip():
        print(f"[evals] {SUITE} SKIPPED: MAIN_LLM_API_KEY is not configured", flush=True)
        raise SystemExit(3)
    raise SystemExit(asyncio.run(_run(records, selected, data_dir, options)))


def _find_dataset(data_dir: Path) -> Path | None:
    """The dataset file inside *data_dir*, or None (the suite skips)."""
    if not data_dir.is_dir():
        return None
    try:
        return env_setup.pick_dataset_file(data_dir, "metadata.json", "metadata.jsonl")
    except ValueError:
        return None


def devbench_options(args: list[str]) -> dict[str, Any]:
    """The devbench CLI parser (same flags, no Docker checks)."""
    from evals.devbench.suite import _parse_args

    return _parse_args(args)


def _select(records: list[dict[str, Any]], options: dict[str, Any]) -> list[dict[str, Any]]:
    from evals.shared import batch_runner

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
    data_dir: Path,
    options: dict[str, Any],
) -> int:
    from agent.state_port import read_messages

    import agent.core  # noqa: F401 — bind build_main_tools before the sandbox
    from evals.coding_capability import suite as harness
    from evals.sandbox import EvalSandbox
    from evals.shared.context_analyzer import analyze_transcript  # noqa: PLC0415

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
        # The full surface stays: GAIA needs terminal, browser, web tools and
        # the file tools together (the plan's own tooling table).
        external_bench.install_orchestration_gate()
        for record in selected:
            session_id = f"evals-{SUITE}-{record['task_id']}-{uuid.uuid4().hex[:6]}"
            started_task = time.monotonic()
            workspace = eval_workspace / session_id
            workspace.mkdir(parents=True, exist_ok=True)
            attachment = stage_attachment(record, data_dir, workspace)
            from runtime.session.project_dir import write_project_dir

            write_project_dir(session_id, workspace)
            prompt = _PROMPT.format(
                question=record["question"],
                project_dir=workspace,
                file_name=record.get("file_name") or "(none)",
            )
            result: dict[str, Any] = {
                "id": record["task_id"],
                "level": record["level"],
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
                "expected": record["final_answer"],
                "attachment": str(attachment) if attachment else "",
                "tokens": {},
            }
            try:
                drive = await asyncio.wait_for(
                    harness._drive(session_id, prompt), timeout=float(options["timeout"])
                )
                result.update(
                    {
                        "tool_calls": int(drive.get("tool_calls") or 0),
                        "tool_names": dict(drive.get("tool_names") or {}),
                        "timeout": bool(drive.get("timeout")),
                        "error": str(drive.get("error") or ""),
                        "reply": str(drive.get("text") or "")[:2000],
                        "tokens": await harness._session_tokens(session_id),
                    }
                )
                messages = await read_messages(session_id)
                result["context"] = analyze_transcript(messages, record["question"])
                result["code_intel_usage"] = __import__(
                    "evals.shared.code_intel_tracker", fromlist=["analyze_code_intel"]
                ).analyze_code_intel(record["task_id"], result["tool_names"], messages)
                result["answer"] = extract_final_answer(str(drive.get("text") or ""))
                result["resolved"] = score_answer(result["answer"], record["final_answer"])
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
                f"[evals] {result['id']}: resolved={result['resolved']} "
                f"answer={result['answer'][:80]!r}",
                flush=True,
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
        kind=f"GAIA slice ({len(results)} tasks) — external benchmark",
        results_dir=results_dir,
        results=results,
        started=started,
        model_info={**harness._model_info()},
        benchmark={
            "dataset": str(options.get("dataset") or DATA_ENV_VAR),
            "grading": "official normalization + exact match (see shared/scorer)",
            "tools": "full surface (terminal + browser + web + file tools)",
        },
        extra={"code_intel_summary": score_reporter.code_intel_summary(results)},
    )
    external_bench.write_report(results_dir, report)
    score_reporter.write_summary_txt(results_dir, report)
    return external_bench.publish(
        report, results_dir, BASELINE_PATH, update_baseline=bool(options["update_baseline"])
    )


if __name__ == "__main__":
    main()
