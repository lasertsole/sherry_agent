# Post-Fixes Test Baseline (fix5 — end-to-end validation of the 6-fix stream)

- **HEAD:** `4a0d16800932e0bdf824db6535d8c3aa101a3ce0` (`4a0d168` — test(timeout): raise concurrent e2e budget to 600s to match documented envelope)
- **Date:** 2026-08-31
- **Method:** two sequential validation gates, llm_e2e excluded in both (default addopts `-m 'not llm_e2e'`; never invoked with `--with-llm-e2e` / `-m llm_e2e`):
  1. **Split runner (canonical CI-ready gate):** `uv run python scripts/run_tests_split.py` → `split-run.txt`
  2. **Single-process full-tree cross-check (REPORT.md §2-comparable mode):** `uv run pytest tests/ -q` → `single-process-run.txt`
- Both gates captured via Python `subprocess.run(capture_output=True, text=True, encoding='utf-8', errors='replace')`, written with `pathlib.write_text(encoding='utf-8')` (no PowerShell redirection — GBK-console safe).

## Results — ZERO failures in both modes

### Single-process full tree (gate b — comparable to REPORT.md §2 numbers)

| Metric | Value |
|---|---|
| passed | **1411** |
| failed | **0** |
| skipped | 3 |
| deselected | 3 (the `llm_e2e` tests) |
| warnings | 2 (sunset deprecations) |
| duration | **174.19 s** pytest (176.6 s wall, rc=0) |

### Split runner (gate a — two sequential pytest processes)

| Group | Directories | Result | pytest time | runner time (incl. interpreter start) |
|---|---|---|---|---|
| A | `tests/unit` | **1157 passed / 2 skipped** / 2 warnings, rc=0 | 136.64 s | 139.0 s |
| B | `tests/integration + tests/system + tests/module` | **236 passed / 3 deselected** / 6 warnings, rc=0 | 248.06 s | 250.4 s |

Final verdict: **PASS — all groups green**; total **389.5 s (~6.5 min)**, runner exit 0.

### Cross-check vs recorded references — no drift beyond noise

| Reference | Values | This run | Δ |
|---|---|---|---|
| ee827c0 gate (single-process) | 1411 passed / 3 skipped / 3 deselected in 174.47 s | 1411 / 3 / 3 in 174.19 s | counts exact; −0.16 % |
| test-split-runner `default-run.txt` GROUP A | 1157 passed / 2 skipped, 137.1 s | 1157 / 2, 136.64 s | counts exact; −0.3 % |
| test-split-runner `default-run.txt` GROUP B | 236 passed / 3 deselected, 262.5 s | 236 / 3, 248.06 s | counts exact; −5.5 % (machine-load noise) |

The split runner covers 1393 of the 1411 single-process passes (+2 of 3 skips): the ~18 remaining defs live in `tests/full` + `tests/diagnose`, which the runner intentionally excludes from the standard groups (documented in README Testing section; `tests/full/test_main_agent_e2e.py` is live-network and untagged — not CI-wireable).

## Baseline comparison across the fix stream

| Baseline | HEAD | Selection | Result |
|---|---|---|---|
| Pre-plan (recorded) | `14be3bf` | narrow (~1054 tests; no `tests/module`, `tests/system`, most of `tests/unit/subagent`) | **3 failed** / 1049 passed / 2 skipped — pollution mechanism present, selection too narrow to expose the other 8 |
| Full-tree re-baseline (REPORT.md §2) | `23bc2cf` | full tree, single process | **11 failed** / 1399 passed / 3 skipped in 218.14 s — all 11 from one root-cause family: `tests/unit/subagent/conftest.py::_setup_subagent_alias()` mutating process-global `sys.modules` at import |
| **This baseline (fix5)** | `4a0d168` | full tree, BOTH modes | **0 failed** — single-process: 1411 passed / 3 skipped / 3 deselected in 174.19 s; split runner: GROUP A 1157 P / 2 S + GROUP B 236 P / 3 D, rc=0, 389.5 s total |

## What resolved what (per fix)

- **`c730a46`** — `test(conftest): scope subagent unit stubs, fix cross-suite pollution`: made `tests/unit/subagent/conftest.py` restore-safe (conditional `skills.loader` stub install, real `clear_all_register_sessions` binding, delegate binding pins via autouse fixture) → resolved the 8 non-e2e failures (skill_scope ×3, system ×1, delegate ×4).
- **`ac27ec9`** — `test(e2e): tag real-LLM network tests with llm_e2e marker, deselect by default`: the 3 real-LLM e2e tests are marker-gated and out of every default run.
- **`ea5872a`** — `test(e2e): close leaked aiosqlite connections at teardown`: autouse fixture in `tests/integration/conftest.py` closes aiosqlite connections → eliminated the post-PASS interpreter-shutdown hang (non-daemon `_connection_worker_thread`) on solo runs.
- **`ee827c0` + `4a0d168`** — `test(timeout): add pytest-timeout, 300s budget` / `raise concurrent e2e budget to 600s`: per-test pytest-timeout budgets on the llm_e2e tests (300 s simple / 600 s concurrent, matching the documented 2–9 min concurrent envelope) → a future real hang is bounded and distinguishable from a legitimately slow run.
- **`08340f2`** — `test(ci): process isolation runner + docs`: `scripts/run_tests_split.py` runs unit vs integration+system+module in separate sequential pytest processes → import-time `sys.modules` mutations can never leak across suites; README Testing section documents root cause, usage, marker policy and runtimes.

## Notes

1. **llm_e2e tests were NOT executed** — the real-LLM API budget is exhausted (2/2 runs used). They are excluded by the default addopts (`-m 'not llm_e2e'`) in both gates; the 3 deselected tests in each mode are exactly these.
2. **GROUP B carries ~120 s collection overhead (expected, documented known behavior):** importing the integration test modules triggers `build_skills_snapshot` → per-skill SkillSpector CLI subprocess scans (`server/service/skill_scanner.py:345`). pytest-timeout does NOT bound collection by design (no timer runs during collection), so this overhead is inherent to GROUP B and not a hang signal.
3. **D5 probe finding (pytest-timeout thread-method kill verified healthy end-to-end):** armed → wait → clean cancel on a passing test, plus the 0.69 s rc=1 mechanism-proof kill on `tests/unit/test_atomic_replace.py` (`--timeout=0.01 --timeout-method=thread`, stack dump, no pytest summary) — the plugin's kill path is proven without spending real-LLM tokens.

## Evidence files

- `split-run.txt` — full live output of gate (a), `[gate=split exit_code=0 elapsed=389.5s]`
- `single-process-run.txt` — full output of gate (b), `[gate=single exit_code=0 elapsed=176.6s]`
