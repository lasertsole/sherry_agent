"""Run ToolBench's official ToolEval pass-rate evaluator on converted answers.

This script is executed in an isolated ``openai<1`` environment because the
official evaluator uses the legacy ``openai.ChatCompletion.create`` function
API. It imports the evaluator, its prompt template and its step parser from the
pinned ToolBench checkout; no grading logic is re-implemented here. Only the
evaluator's model id and API-key pool location are parameterised, so the
official prompts and verdict functions stay intact.

Usage (the suite builds this command)::

    python judge_runner.py \
        --repo-tooleval /cache/toolbench/repo/toolbench/tooleval \
        --converted converted.json --test-ids G1_instruction_test_query_ids.json \
        --out verdicts.json --times 1 --model gpt-3.5-turbo-16k
"""

# pyright: reportMissingImports=false
# The official ToolEval modules are imported after sys.path points at the
# pinned ToolBench checkout; they are not installed in this project's venv.

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path


def _prepare_evaluator_config(tooleval: Path, evaluator: str, model: str, pool_path: Path) -> Path:
    """Copy one evaluator config and point it at the chosen model + key pool."""
    import yaml

    source = tooleval / "evaluators" / evaluator
    if not source.is_dir():
        raise SystemExit(f"evaluator config not found: {source}")
    target_root = Path(tempfile.mkdtemp(prefix="sherry-tooleval-"))
    target = target_root / evaluator
    shutil.copytree(source, target)
    config_path = target / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["apis_json"] = str(pool_path)
    config.setdefault("completions_kwargs", {})["model"] = model
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    return target_root


def _key_pool(path: Path | None) -> Path:
    """Resolve the OpenAI key pool the official OpenaiPoolRequest expects."""
    if path is not None and path.is_file():
        return path
    env_path = os.environ.get("SHERRY_TOOLBENCH_JUDGE_KEYS", "").strip()
    if env_path and Path(env_path).is_file():
        return Path(env_path)
    key = (
        os.environ.get("SHERRY_TOOLBENCH_JUDGE_KEY", "").strip()
        or os.environ.get("OPENAI_API_KEY", "").strip()
    )
    if not key:
        raise SystemExit(
            "no judge key: set SHERRY_TOOLBENCH_JUDGE_KEY / OPENAI_API_KEY or "
            "SHERRY_TOOLBENCH_JUDGE_KEYS (the official key-pool JSON)"
        )
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump([{"api_key": key, "organization": None}], handle)
    handle.close()
    return Path(handle.name)


def main() -> None:
    """Load the official evaluator and write one verdict per test query."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-tooleval", required=True)
    parser.add_argument("--converted", required=True)
    parser.add_argument("--test-ids", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--times", type=int, default=1)
    parser.add_argument("--evaluator", default="tooleval_gpt-3.5-turbo_default")
    parser.add_argument("--model", default="gpt-3.5-turbo-16k")
    parser.add_argument("--api-pool", default="")
    args = parser.parse_args()

    tooleval = Path(args.repo_tooleval).resolve()
    sys.path.insert(0, str(tooleval))
    from evaluators import load_registered_automatic_evaluator
    from evaluators.registered_cls.rtl import AnswerPass, AnswerStatus
    from utils import get_steps

    pool_path = _key_pool(Path(args.api_pool) if args.api_pool else None)
    config_root = _prepare_evaluator_config(tooleval, args.evaluator, args.model, pool_path)
    os.environ["API_POOL_FILE"] = str(pool_path)
    evaluator = load_registered_automatic_evaluator(
        evaluator_name=args.evaluator, evaluators_cfg_path=str(config_root)
    )

    converted = json.loads(Path(args.converted).read_text(encoding="utf-8"))
    test_ids = {str(key) for key in json.loads(Path(args.test_ids).read_text(encoding="utf-8"))}
    verdicts: dict[str, dict[str, object]] = {}
    for query_id, example in converted.items():
        if str(query_id) not in test_ids:
            continue
        passed = 0
        failed = 0
        reason = ""
        try:
            not_hallucinate = evaluator.check_has_hallucination(
                example["available_tools"], example["answer"]
            )
            del not_hallucinate  # reported by the official CSV, not used in pass/fail
            _, final_step = get_steps(example)
            if "'name': 'Finish'" not in final_step:
                failed += 1
                reason = "No answer (no Finish step)"
            else:
                for _ in range(max(1, args.times)):
                    is_solved, solved_reason = evaluator.check_is_solved(
                        {
                            "query": example["query"],
                            "available_tools": example["available_tools"],
                        },
                        example["answer"],
                        return_reason=True,
                    )
                    is_solved_flag = is_solved == AnswerStatus.Solved
                    task_solvable, solvable_reason = evaluator.check_task_solvable(
                        {
                            "query": example["query"],
                            "available_tools": example["available_tools"],
                        },
                        has_been_solved=is_solved_flag,
                        return_reason=True,
                    )
                    label = evaluator.is_passed(
                        {
                            "query": example["query"],
                            "available_tools": example["available_tools"],
                        },
                        example["answer"],
                        answer_status=is_solved,
                        task_status=task_solvable,
                    )
                    if label == AnswerPass.Passed:
                        passed += 1
                    elif label == AnswerPass.Failed:
                        failed += 1
                    else:
                        failed += 1
                    reason = (
                        f"Is solved: {solved_reason}\nTask solvable: {solvable_reason}\n"
                        f"Verdict: {label}"
                    )
        except Exception as exc:  # noqa: BLE001 — a judge crash is a harness record
            verdicts[str(query_id)] = {
                "passed": False,
                "harness_error": f"{type(exc).__name__}: {exc}",
                "reason": reason,
            }
            continue
        verdicts[str(query_id)] = {"passed": passed > failed, "reason": reason}

    Path(args.out).write_text(json.dumps(verdicts, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"judged": len(verdicts), "out": args.out}, ensure_ascii=False))


if __name__ == "__main__":
    main()
