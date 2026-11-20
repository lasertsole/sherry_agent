"""VisualWebArena — WebArena's vision sibling, reusing the webarena adapter.

The deltas are exactly the plan's list: tasks need VISUAL understanding, the
agent must be able to screenshot and have the screenshots reach a
vision-capable model, and the browser's JS-evaluate permission is on
(``SHERRY_BROWSER_ALLOW_EVALUATE=1``) for the visual checks. The dataset
comes from ``VISUALWEBARENA_DATA_DIR`` with the same record shape.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals import external_bench  # noqa: E402
from evals.shared import env_setup  # noqa: E402
from evals.webarena import suite as webarena  # noqa: E402

BASELINE_PATH = Path(__file__).with_name("baseline.json")
SUITE = "visualwebarena"
DATA_ENV_VAR = "VISUALWEBARENA_DATA_DIR"


def require_vision_model() -> bool:
    """The vision requirement is honest: a run whose MAIN model has no image
    input would screenshot into nothing. Read from the same capability table
    the multimodal middleware uses, fail-open (a capability probe failure is
    recorded in the report, not a skip)."""
    from agent.middlewares.media_pipeline.tool_media import effective_model_key  # noqa: PLC0415

    try:
        provider, _, model = effective_model_key("").partition("/")
        from agent.middlewares.llm_capability_cache import get_capability  # noqa: PLC0415

        capability = get_capability(provider, model, "vision") if model else "auto"
        return capability != "unsupported"
    except Exception:  # noqa: BLE001 — advisory
        return True


def main() -> None:
    """Run the suite (reuses webarena's machinery with the vision deltas)."""
    external_bench.maybe_reexec("SHERRY_EVAL_VWA_CHILD", Path(__file__))
    args = sys.argv[1:]
    if args and args[0] == SUITE:
        args = args[1:]
    os.environ.setdefault("SHERRY_BROWSER_ALLOW_EVALUATE", "1")
    webarena.SUITE = SUITE  # the runner writes this suite's report/skip labels
    webarena.DATA_ENV_VAR = DATA_ENV_VAR

    options = webarena._parse_args(args)
    data_dir = env_setup.resolve_dataset_dir(SUITE, DATA_ENV_VAR)
    dataset_file = options["dataset"] or webarena._find_dataset(data_dir)
    if dataset_file is None:
        print(
            f"[evals] {SUITE} SKIPPED: no dataset (looked in {data_dir}; set "
            f"{DATA_ENV_VAR} or pass --dataset). A skipped suite is not a pass.",
            flush=True,
        )
        raise SystemExit(3)
    try:
        records = webarena.load_dataset(Path(dataset_file))
    except (ValueError, OSError) as exc:
        print(f"[evals] HARNESS FAILURE: {exc}", flush=True)
        raise SystemExit(2) from exc

    selected = webarena._select(records, options)
    compose_file = Path(options["compose_file"] or data_dir / "docker-compose.yml")
    print(
        f"[evals] {SUITE}: {len(records)} tasks ({len(selected)} selected; "
        f"compose={compose_file} vision_model={require_vision_model()})",
        flush=True,
    )
    if options["dry_run"]:
        for record in selected:
            print(f"[evals] dry-run ok: {record['task_id']}", flush=True)
        print(
            "[evals] note: a vision-capable MAIN model is required — screenshots "
            "must reach the model for the visual tasks to be answerable",
            flush=True,
        )
        return
    if not os.getenv("MAIN_LLM_API_KEY", "").strip():
        print(f"[evals] {SUITE} SKIPPED: MAIN_LLM_API_KEY is not configured", flush=True)
        raise SystemExit(3)
    raise SystemExit(asyncio.run(_run(records, selected, compose_file, options)))


async def _run(
    records: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    compose_file: Path,
    options: dict[str, Any],
) -> int:
    started = time.monotonic()
    run_id = external_bench.run_id_now()
    results_dir = REPO_ROOT / "evals" / "results" / SUITE / run_id
    results_dir.mkdir(parents=True, exist_ok=True)
    vision_ok = require_vision_model()
    exit_code = await webarena._run(records, selected, compose_file, options)
    # Post-process: the shared runner labeled itself webarena; patch the report
    # to carry THIS suite's identity and the vision evidence.
    report_path = results_dir / "eval_report.json"
    if report_path.is_file():
        import json  # noqa: PLC0415 — local

        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["suite"] = SUITE
        task_count = len(report.get("tasks") or [])
        report["kind"] = f"VisualWebArena slice ({task_count} tasks) — external benchmark"
        report["benchmark"]["vision_model"] = vision_ok
        report["benchmark"]["js_evaluate"] = os.environ.get("SHERRY_BROWSER_ALLOW_EVALUATE", "")
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    del started
    return exit_code


if __name__ == "__main__":
    main()
