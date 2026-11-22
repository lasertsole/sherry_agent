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
    "skill_review": "evals.skill_review.suite",
    "coding_capability": "evals.coding_capability.suite",
    # External benchmarks: each runs in its own process (they repoint
    # SHERRY_WORKSPACE_DIR); the container suites need Docker, the tool/agent
    # benchmarks need their own official keys — without them they skip loudly.
    "deepswe": "evals.deepswe.suite",
    "terminal_bench": "evals.terminal_bench.suite",
    "agentbench": "evals.agentbench.suite",
    "toolbench": "evals.toolbench.suite",
    # The 7-benchmark round (plan 2026-11-17): every suite is dataset-optional
    # — a missing dataset / container runtime / key SKIPS loudly (exit 3).
    "devbench": "evals.devbench.suite",
    "multi_swe_bench": "evals.multi_swe_bench.suite",
    "tau_bench": "evals.tau_bench.suite",
    "gaia": "evals.gaia.suite",
    "cybersec": "evals.cybersec.suite",
    "webarena": "evals.webarena.suite",
    "visualwebarena": "evals.visualwebarena.suite",
    "osworld": "evals.osworld.suite",
}


def main() -> None:
    """Dispatch to the selected eval suites (all of them when none is named).

    One named suite may carry FLAGS — everything after the suite name is
    forwarded verbatim as that suite's argv (``evals.py devbench --dry-run``
    is the documented form); several names must stay flagless (each suite
    owns its own flag parsing and they disagree on nothing by design).
    """
    argv = sys.argv[1:]
    names: list[str] = []
    flags: list[str] = []
    for index, arg in enumerate(argv):
        if arg.startswith("-"):
            flags = argv[index:]
            break
        names.append(arg)
    unknown = [n for n in names if n not in SUITES]
    if unknown:
        raise SystemExit(f"unknown suite(s): {unknown}; registered: {sorted(SUITES)}")
    if flags and len(names) != 1:
        raise SystemExit("flags need exactly one named suite (flags: " + ", ".join(flags) + ")")

    selected = names or list(SUITES)
    for name in selected:
        module = __import__(SUITES[name], fromlist=["main"])
        print(f"[evals] === suite: {name} ===")
        if flags and name == names[0]:
            sys.argv = [sys.argv[0], name, *flags]
        module.main()


if __name__ == "__main__":
    main()
