# SWE-Bench (host harness)

SWE-Bench is run through the repository's own host harness, not the official
Docker evaluation. The harness pins each instance's base commit, creates its
environment and judges the result with the instance's own FAIL_TO_PASS and
PASS_TO_PASS sets. It answers “did this agent, on this machine, fix these two
HARD instances?” — not “what is the leaderboard position?”.

## What it measures

- The **main agent** through `server.service.messages.async_generate`, with the
  real middleware chain and the selected tool face.
- The **subagents** it spawns, including their file writes and their settlement
  before the harness reads the diff.
- The **harness**: instance setup, the official test patch, and the exact test
  verdict. A harness error is recorded as invalid, never as a failed fix.

## Harness and slices

- Dataset: `princeton-nlp/SWE-bench_Lite`, test split.
- HARD slice: `django__django-17051` and `django__django-16820`. `sympy__sympy-20639` was removed from the runnable slice on 2026-11-08: its test patch changes a long-standing NumberSymbol-vs-Symbol semantic distinction that is not derivable from the issue and the pre-fix test. Historical Sympy runs remain in `evals/swebench/RESULTS.md`.
- Instance cache: `--instance-cache-dir /tmp/swebench-instance-cache`.
- Arms: neutral prompt, `--persona coding`, and delegation pressure
  (`SHERRY_EVAL_DELEGATION_PRESSURE=1`). The delegate-only arm was removed
  with its middleware on 2026-11-02; older result rows are kept as history.
- The report is `evals/results/swebench/<run_id>/eval_report.json`; the
  per-instance diffs are next to it.

- Since 2026-11-04 the task prompt includes the instance's test patch and the
  FAIL_TO_PASS + PASS_TO_PASS names; the test patch is also pre-applied to the
  checkout so the agent can run the target tests. The prompt now also carries
  the exact runner command for each set (`python tests/runtests.py <labels>` for
  Django, `python -m pytest <node ids>` otherwise). Judging resets those files
  and re-applies the published patch, and the report records
  `target_tests_exposed` and `tests_preapplied`.
- Deterministic decoding: every eval turn builds through a pinned `built_agent`
  at temperature 0. `SHERRY_EVAL_MAX_ITERATIONS` optionally raises the main
  model-call ceiling (unset/0 keeps the shipped 180); the report states
  `model.temperature` and `budget.main_max_iterations`.

## The failure history (2026-11-01)

| Run | Arm | Result | What happened |
| :--- | :--- | :--- | :--- |
| 2026-11-01 000025 | prompt + no-edit gate | 1/2 dirty | Django added a test file; Sympy's 726-byte patch broke P2P |
| 2026-11-01 002305 | delegate-only | 2/2 clean | Main agent had no direct tools; children implemented and verified |
| 2026-11-01 143952 | first soft gate (10/20) | 1/2 | Gate forced a spawn, then the main agent patched repeatedly and broke P2P |
| 2026-11-01 174750 | soft gate (8/16, cumulative) | 2/2 clean | Main was read/write-blocked after 16 reads and delegated the implementation |
| 2026-11-03 010944 | 7/14 advisory (cumulative) | 0/2 | Django's condition-only change still failed the unique constraint; Sympy produced no diff and exhausted the 90-call budget |
| 2026-11-03 194320 | network bridge + signature streak (Sympy only) | 0/1 | Non-empty diff, but `test_pretty_sqrt` P2P regressed; streak stayed WARN-only and the 90-call budget was exhausted |
| 2026-11-03 203547 | + exact-signature block (Sympy only) | 0/1 | The repeat blocked at ~20 calls, but the agent switched to `probe1.py`…`probe19.py` (same output, different args) and still exhausted 90 calls |
| 2026-11-03 205218 | + result repetition (Sympy only) | 0/1 | terminal/python_repl blocked at 6 identical results; the agent patched at 72/90 and could no longer run verification |
| 2026-11-03 210023 | + blocks released by a successful `patch_file` (Sympy only) | 0/1 | Clean 28-call run, no guardrail trip; P2P passed, F2P missed the hidden NumberSymbol-vs-Symbol semantics (the repo's existing `test_issue_6285` asserts the OLD behavior) |
| 2026-11-04 003521 | + target tests in the prompt (HARD 2) | 0/2 | Tests were visible but not runnable; both instances exhausted 90 calls |
| 2026-11-04 004714 | + target tests pre-applied to the checkout (HARD 2) | 1/2 | Django resolved; Sympy used `e.is_Symbol` (wrong for `1/x_17`) and looped on a repro script |
| 2026-11-04 005721 | stronger post-failure prompt (Sympy only) | 0/1 | Same wrong condition; still 0 F2P |
| 2026-11-08 111407 | HARD Django only; F2P+P2P in prompt | 1/1 | Django resolved; first run 0/1 on a P2P `NoneType` regression, second resolved |
| 2026-11-08 133112 | temperature=0 + F2P/P2P in prompt | invalid | isolation failed: the temperature pin imported `config.path` before the workspace override, so the run used the live workspace; its 1/1 is not counted |
| 2026-11-08 133743 | temperature=0 after the pin-order fix | 1/1 | Django resolved in 17 calls / 60.6 s; clean F2P+P2P, no scratch files |
| 2026-11-08 135604 | temperature=0, four valid repeats total | 4/4 | All resolved; calls vary 16–82 (60.6–286.3 s). The longest used 82 of the 90-call budget |
| 2026-11-08 145011 | new HARD instance `django__django-16820`, temperature=0 | 0/1 | Gold check passes (5 F2P + 91 P2P); agent hit the 90-call cap at 92 calls, F2P passed, P2P did not |
| 2026-11-08 184144 | same instance, `SHERRY_EVAL_MAX_ITERATIONS=180` | 1/1 | Resolved in 24 calls / 96.5 s; the larger cap removed the truncation ceiling but did not cause the fix (same temperature, very different trajectory) |
| 2026-11-08 222819 | final code: exact command + default 180 budget (16820) | 0/1 | F2P 5/5, five real P2P assertion failures; the agent stopped after a guardrail blocked more test-file reads |
| 2026-11-09 204854 | final code with the verify-todo template (16820) | 1/1 | 31 calls / 117.3 s; the agent used no todo plan, so the verify branch was covered by the deterministic tests |

## Problems exposed

1. **The old iteration budget charged tool pairs twice.** A tool call cost one
   unit for its `AIMessage` and another for its `ToolMessage`, so 90 looked like
   ~45 tool loops. The counter now charges model calls only.
2. **`terminal` was a loop blind spot.** `ToolGuardrails` only hashes idempotent
   tools; `terminal` is non-idempotent, so 32 identical commands passed without a
   no-progress verdict.
3. **“Enough information” had no convergence point.** The model kept validating
   after locating the change, or went looking for an upstream PR; nothing
   required a move to edit and verification.
4. **The eval harness did not reject test-file edits.** A clean diff is part of
   the product contract, but `changed_paths` was only recorded. A test file
   could pass the target tests and still be a dirty success. The gap is closed:
   `dirty_test_edits` flags a test file the agent touched outside the harness's
   own patch and forces `resolved=False`, whatever the tests say.
5. **Writing is not verification.** The first no-edit arm produced a patch but
   did not run the existing test file; the P2P regression was only discovered at
   judging time.
6. **Child results and child tokens were hard to observe.** A timed-out child’s
   checkpoint may be gone; the main agent sometimes re-spawned to learn status,
   and reports counted only the main session’s tokens.
8. **Django's parallel runner can lose the failure it crashed on.** A test
   whose exception cannot be pickled aborts the pool with `TypeError: cannot
   pickle 'traceback' object`, and the report then names no failing test. The
   Django judge now runs with `--parallel 1`; the same diff then yields 91
   parsed outcomes and 5 named failures.

7. **A measurement pin can defeat the sandbox.** Importing the eval seam before
   `SHERRY_WORKSPACE_DIR` froze `WORKSPACE_DIR` onto the live persona directory.
   The run still reported 1/1, but `eval_workspace_has_sessions=false` and
   `live_sessions_unchanged=false` exposed it; the report’s isolation block is
   the gate that keeps such a run out of the capability count.

### Current resolutions

1. **Fixed.** `IterationBudget` now charges one unit per model call; tool results are free.
2. **Fixed.** `ToolGuardrails` now carries a status-blind signature streak (tool + args hash; 3 identical calls, terminal 5 in its own metadata) that warns up to 3 times per turn and then blocks that exact signature, plus a result-repetition detector for non-idempotent tools (3 warnings, 6 consecutive identical results block the tool). A successful `patch_file` (metadata `clears_guardrail_blocks`) clears the blocks, streaks and failure counters so verification can resume. No read-only command classifier is needed.
3. **Mitigated by the tool guards, not a hard finish gate.** Blocked signatures force a different approach, the 20261103_210023 Sympy run converged in 28 model calls and ran the neighboring test file, and the prompt now requires it. A separate "tests must pass before finish" gate does not exist.
4. **Still open.** The harness records `changed_paths` but does not invalidate a `tests/` edit. The soft gate reduces the chance but is not the official guard.
5. **Partially mitigated.** The escalated reminder pushes the main agent to stop its post-spawn patch loop and delegate; there is no hard "tests must pass before finish" gate yet.
6. **Partially fixed.** The per-turn token budget now attributes the main agent + all descendant levels to the root turn; eval reports still count only the main session, and child checkpoint/result readback remains open.

## The no-edit gate and why it failed

The old eval-only gate warned after 15 tool calls and blocked research after
70% of the iteration budget; any write reset it. That made three failures
predictable:

- It detected writes by **tool name** (`patch_file` / `write_file`), so a
  `terminal cp` or heredoc edit was invisible. Django created its test file with
  `cp`.
- It treated **any** write as success, including a write under `tests/` or a
  patch that was immediately reverted.
- The first write disabled the gate for the rest of the turn, so Sympy’s
  first, wrong patch left ~30 terminal probes and a P2P regression unguarded.

It was removed; it cannot be the convergence mechanism.

## Why the read-before-write license did not stop the first soft-gate loop

The license in `agent/tools/pub_base/read_state.py` protects **`write_file`
overwrites**: an existing file may only be replaced by a session that has read
it, and the read revision is the CAS precondition. It does **not** apply to
`patch_file`, which performs its own old-string/new-string edit with fuzzy
matching and a stale-write CAS; it does not require a prior read, and neither
tool knows whether the patch is correct or whether the tests pass.

The first soft gate (10/20) ended its block as soon as a product write landed
and did not target `patch_file`. The main agent spawned a researcher, the gate
reset, and the main agent then made roughly 70 `patch_file` attempts on
`compiler.py`. That loop was a **patch-retry pathology**, not an unlicensed
`write_file` overwrite, so the license never fired. The earlier 8/16 gate
blocked `patch_file` too, which is how the 2026-11-01 run ended; the current
7/12 advisory design deliberately removed blocking.

## The current design: DelegationPressureGate (7/12 advisory)

The 2026-11-01 run measured the earlier 8/16 hard-block variant; the 7/12
behavior below is the current design (the 2026-11-03 rerun measured the earlier 7/14 setting); it did not resolve the HARD slice — see `evals/swebench/RESULTS.md`.

`agent/middlewares/delegation_pressure/core.py`:

- Counts **read/exploration** calls cumulatively within one turn.
- At **7** reads, injects one `<system-reminder source="delegation_pressure">` warning through the gate's own mid-turn `abefore_model` path.
- At **12** reads, injects the escalated system-reminder with stronger language. Tools remain available; the gate advises, never blocks.
- Browser debugging tools are excluded from counting.
- An accepted/pending successful spawn restarts the count, so the main agent
  can inspect/verify and the gate re-arms on another 7/12 reads. A failed spawn
  and writes alone do not reset it; `after_agent` also resets at the turn
  boundary.

It is installed on the main agent only; the child middleware builder never adds
it, and `_active` also rejects `:subagent:`/`:swarm:` sessions.

## Network status bridge

Terminal commands whose failure is network-shaped are tool errors now: a non-zero
exit from `curl` / `wget` / `http` / `https` / `ssh` / `scp` / `sftp` / `ping` /
`dig` / `nslookup`, or from `git clone|fetch|pull|push|ls-remote|remote|submodule`,
raises `ToolException`. `handle_tool_error` turns that into
`ToolMessage(status="error")`, so the exact-failure counter sees it. Local
commands (pytest, compilers, scripts) keep their non-zero exit as normal text
(`status="success"`) so the agent can read the failure and iterate. `web_fetch`
HTTP 4xx/5xx likewise raises.

The eval harness also probes GitHub before a run; when the probe cannot reach
the network it appends a "do not keep probing the network" directive to the
seeded `AGENTS.md`. This removed the Sympy curl/GitHub probe loop. The remaining
Sympy F2P failure is the hidden NumberSymbol-vs-Symbol test semantics, not
network probing.

## Results

| Arm | Resolved | Duration | Main-session tokens | Estimated cost |
| :--- | :--- | :--- | :--- | :--- |
| `DelegationPressureGate` 8/16 (run 20261101_174750) | 2/2 | 1693.4s | 1,962,778 / 12,673 | $1.000 |
| `DelegationPressureGate` 7/14 advisory (run 20261103_010944) | 0/2 | 669.7s | 8,370,656 / 31,119 | $4.232 |
| Delegate-only (run 20261101_145717) | 2/2 | 3469.0s | 2,561,580 / 20,925 | $1.312 |

Django changed product code and release docs only; Sympy changed one library
file. Neither successful diff contains a test file. The token and cost columns
cover the main session only; child usage is not aggregated, so the two arms are
compared as a timing and main-context signal, not as a full bill.

## Reproduce

```bash
SHERRY_EVAL_DELEGATION_PRESSURE=1 uv run --no-sync python evals/swebench/suite.py \
  --hard --persona coding --keep-instances \
  --instance-cache-dir /tmp/swebench-instance-cache
```

## Caveats

- Two instances are a signal, not a benchmark ranking; repeat the slice before
  claiming a general improvement.
- The host harness is not the official Docker harness; Python and dependency
  versions differ from the benchmark images.
- The clean result does not close the test-edit guard: the harness should still
  mark a `changed_paths` entry under `tests/` as invalid.
- Browser tools were excluded by design; this run does not prove anything about
  browser-driven research.
