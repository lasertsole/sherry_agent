"""One DeepSWE local agent turn, driven directly on the host (no container).

Eval-only helper for the Docker-free rerun. It uses the current product agent
and the current eval gates; grading is done by the parent driver with
``deepswe_local.py``. The file is intentionally untracked work-in-progress.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_partial(path: Path, drive: dict[str, Any], tokens: dict[str, int]) -> None:
    payload = {
        "drive": dict(drive),
        "tokens": dict(tokens),
        "agent_config": {},
        "partial": True,
    }
    tmp = path.with_name(path.name + ".part")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def _error_payload(error: str) -> dict[str, Any]:
    return {
        "drive": {
            "tool_calls": 0,
            "text": "",
            "resumes": 0,
            "interrupted": 0,
            "timeout": False,
            "error": error,
        },
        "tokens": {},
        "agent_config": {},
    }


def _apply_sandbox_policy() -> None:
    """Apply the driver's sandbox policy AFTER ``EvalSandbox.apply()``.

    ``EvalSandbox`` pins ``SANDBOX_POLICY=required`` on Linux (correct for the
    container path, where bwrap exists). On a bare host without an OS sandbox
    backend that refuses EVERY ``terminal``/``python_repl`` call — the measured
    2026-11-27 rerun could not even run ``git status`` or the project tests, and
    the recon step child could not answer its JSON schema, which blocked the
    whole flow. An explicit ``SHERRY_LOCAL_EVAL_SANDBOX_POLICY`` (set by the
    driver) therefore wins; otherwise the historical ``auto`` stays.
    """
    override = os.environ.get("SHERRY_LOCAL_EVAL_SANDBOX_POLICY")
    if override:
        os.environ["SANDBOX_POLICY"] = override
    else:
        os.environ.setdefault("SANDBOX_POLICY", "auto")


def main() -> int:
    """Drive one DeepSWE task with the production Sherry agent."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--results-dir", required=True)
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    output = Path(args.output)
    results_dir = Path(args.results_dir)
    repo = Path(args.repo)
    prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    os.environ["SHERRY_WORKSPACE_DIR"] = str(results_dir / "sandbox" / "workspace")

    import config.path  # noqa: F401 - hydrate .env / .models / .network
    import agent.core as agent_core  # bind the real tool builder before the sandbox

    from evals import external_bench
    from evals.coding_capability import suite as harness
    from evals.sandbox import EvalSandbox
    from evals.shared.eval_iteration_budget import pin_eval_budget
    from evals.shared.eval_temperature import pin_eval_temperature

    sandbox = EvalSandbox(results_dir)
    sandbox.apply()
    # AFTER apply(): the sandbox pins SANDBOX_POLICY=required on Linux, which a
    # bare host without bwrap cannot honour (every exec is refused). The driver
    # decides via SHERRY_LOCAL_EVAL_SANDBOX_POLICY.
    _apply_sandbox_policy()
    try:
        harness._seed_persona(sandbox.workspace)
        harness.init_agent()
        pin_eval_temperature(0.0)
        pin_eval_budget(int(os.getenv("SHERRY_EVAL_MAX_ITERATIONS", "180") or 180))
        external_bench.trim_tools()
        external_bench.install_orchestration_gate()
        missing = sorted(
            {"read_file", "write_file", "patch_file", "terminal"}
            - {getattr(tool, "name", "") for tool in agent_core.get_agent_tools()}
        )
        if missing:
            _write(output, _error_payload(f"the main agent is missing {missing}"))
            return 0

        async def run_turn() -> dict[str, Any]:
            payload = await external_bench.bind_external_session(
                args.session_id,
                repo,
                append_note="local Docker-free DeepSWE rerun",
            )
            latest: dict[str, Any] = {
                "tool_calls": 0,
                "text": "",
                "resumes": 0,
                "interrupted": 0,
                "timeout": False,
                "error": "",
            }
            tokens: dict[str, int] = {}
            last_write = [0.0]

            def progress(state: dict[str, Any]) -> None:
                latest.update(state)
                now = time.monotonic()
                if now - last_write[0] >= 1.0:
                    last_write[0] = now
                    _write_partial(output, latest, tokens)

            async def token_loop() -> None:
                while True:
                    await asyncio.sleep(10.0)
                    try:
                        tokens.update(await harness._session_tokens(args.session_id))
                    except Exception:  # noqa: BLE001 - telemetry only
                        continue
                    _write_partial(output, latest, tokens)

            token_task = asyncio.create_task(token_loop())
            extraction: dict[str, object] = {}
            final: dict[str, Any] = {}
            try:
                drive = await harness._drive(args.session_id, prompt, progress=progress)
                tokens.update(await harness._session_tokens(args.session_id))
                final = {
                    "drive": drive,
                    "tokens": tokens,
                    "agent_config": payload,
                    "orchestration_gate": external_bench.orchestration_state(args.session_id),
                    "experience_extraction": extraction,
                }
                # Persist immediately: a late cancellation must not lose the turn.
                _write(output, final)
                if os.getenv("SHERRY_FORCE_EXPERIENCE_EXTRACTION") == "1":
                    from evals.shared.eval_session import force_experience_extraction

                    extraction = await force_experience_extraction(args.session_id)
                    final["experience_extraction"] = extraction
                    _write(output, final)
            finally:
                token_task.cancel()
                try:
                    await token_task
                except asyncio.CancelledError:
                    pass
            if final:
                _write(output, final)
            return final

        _write(output, asyncio.run(run_turn()))
        return 0
    except BaseException as exc:  # noqa: BLE001 - a cancelled turn still needs a result
        existing: dict[str, Any] = {}
        try:
            existing = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        if isinstance(existing, dict) and existing.get("drive"):
            drive = existing.setdefault("drive", {})
            drive.setdefault("error", "")
            drive["error"] = (
                drive["error"] + f" late worker error: {type(exc).__name__}: {exc}"
            ).strip()
            _write(output, existing)
        else:
            _write(output, _error_payload(f"{type(exc).__name__}: {exc}"))
        return 0
    finally:
        try:
            sandbox.close_aiosqlite_connections()
        except Exception:  # noqa: BLE001, S110 - teardown must not change the result
            pass
        os._exit(0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
