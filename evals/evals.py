"""Project-wide eval runner.

Each eval suite lives in its own package under evals/ and registers itself in
SUITES below. Results are written under evals/results/<suite>/<run_id>/.

Usage:
    uv run python evals/evals.py             # run every registered suite
    uv run python evals/evals.py graph_rag   # run a single suite by name
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
# Running as a script puts evals/ itself at sys.path[0], where evals.py shadows
# the evals package; point path[0] at the repo root so evals.graph_rag resolves.
sys.path[0] = str(REPO_ROOT)

SUITES: dict[str, str] = {
    "graph_rag": "evals.graph_rag.suite",
    "subagent": "evals.subagent.suite",
    "long_running_task": "evals.long_running_task.suite",
    "session_memory": "evals.session_memory.suite",
}


def main() -> None:
    """Dispatch to the selected eval suites (all of them when none is named)."""
    names = sys.argv[1:]
    unknown = [n for n in names if n not in SUITES]
    if unknown:
        raise SystemExit(f"unknown suite(s): {unknown}; registered: {sorted(SUITES)}")

    for name in names or list(SUITES):
        module = __import__(SUITES[name], fromlist=["main"])
        print(f"[evals] === suite: {name} ===")
        module.main()


if __name__ == "__main__":
    main()
