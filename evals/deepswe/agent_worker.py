"""One DeepSWE local agent turn in a disposable process.

The parent local suite owns the checkout, the venv and grading. This worker
owns exactly one thing: the real main agent's turn. Running it in its own
process group is what gives the local suite a HARD wall-clock timeout — the
production turn (and its detached subagents) can survive an ``asyncio``
cancellation, but it cannot survive ``SIGKILL`` to its process group.

The worker writes one JSON result file and exits. It never grades and never
touches the parent's result report.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


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


def main() -> int:
    """Run one agent turn and write its result JSON; return a process exit code."""
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

    import config.path  # noqa: F401 — hydrates .env / .models / .network
    import agent.core as agent_core  # — binds the real tool builder at import

    from evals import external_bench
    from evals.coding_capability import suite as harness
    from evals.deepswe import local
    from evals.sandbox import EvalSandbox

    sandbox = EvalSandbox(results_dir)
    sandbox.apply()
    os.environ["SANDBOX_POLICY"] = "auto"
    try:
        harness._seed_persona(sandbox.workspace)
        local._append_agents_note(sandbox.workspace)
        harness.init_agent()
        external_bench.trim_tools()
        missing = sorted(
            {"read_file", "write_file", "patch_file", "terminal"}
            - {getattr(tool, "name", "") for tool in agent_core.get_agent_tools()}
        )
        if missing:
            _write(output, _error_payload(f"the main agent is missing {missing}"))
            return 0

        async def run_turn() -> dict[str, Any]:
            payload = await external_bench.bind_external_session(args.session_id, repo)
            drive = await harness._drive(args.session_id, prompt)
            tokens = await harness._session_tokens(args.session_id)
            return {"drive": drive, "tokens": tokens, "agent_config": payload}

        _write(output, asyncio.run(run_turn()))
        return 0
    except Exception as exc:  # noqa: BLE001 — the parent reports the reason
        _write(output, _error_payload(f"{type(exc).__name__}: {exc}"))
        return 0
    finally:
        try:
            sandbox.close_aiosqlite_connections()
        except Exception:  # noqa: BLE001, S110 — teardown must not change the result
            pass
        # The agent runtime parks non-daemon worker threads; exit hard after
        # the result is on disk so the parent's timeout is the only clock.
        os._exit(0)


if __name__ == "__main__":  # pragma: no cover - exercised by the local suite
    raise SystemExit(main())
