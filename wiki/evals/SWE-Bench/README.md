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
- HARD slice: `django__django-17051`, `sympy__sympy-20639`.
- Instance cache: `--instance-cache-dir /tmp/swebench-instance-cache`.
- Arms: neutral prompt, `--persona coding`, delegate-only
  (`SHERRY_EVAL_DELEGATE_ONLY=1`), and delegation pressure
  (`SHERRY_EVAL_DELEGATION_PRESSURE=1`).
- The report is `evals/results/swebench/<run_id>/eval_report.json`; the
  per-instance diffs are next to it.

## The failure history

| Run | Arm | Result | What happened |
| :--- | :--- | :--- | :--- |
| 2026-10-26 neutral | neutral rules, 90 iterations | 1/2 | Django resolved; Sympy produced no diff and exhausted the budget |
| 2026-10-30 coding | coding template, read-only children | 0/2 | The template demanded delegation while children could not write; neither side edited |
| 2026-10-31 225133 | softened prompt, full child tools | 1/2 | Sympy resolved; Django made 28 network searches and produced no diff |
| 2026-10-31 234435 | long-research routing | 0/2 | Django's F2P failed; Sympy made 45 terminal calls, 32 identical |
| 2026-11-01 000025 | prompt + no-edit gate | 1/2 dirty | Django added a test file; Sympy's 726-byte patch broke P2P |
| 2026-11-01 002305 | delegate-only | 2/2 clean | Main agent had no direct tools; children implemented and verified |
| 2026-11-01 143952 | first soft gate (10/20) | 1/2 | Gate forced a spawn, then the main agent patched repeatedly and broke P2P |
| 2026-11-01 174750 | soft gate (8/16, cumulative) | 2/2 clean | Main was read/write-blocked after 16 reads and delegated the implementation |

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
   could pass the target tests and still be a dirty success. This gap is still
   open; the current successful run happened not to edit tests.
5. **Writing is not verification.** The first no-edit arm produced a patch but
   did not run the existing test file; the P2P regression was only discovered at
   judging time.
6. **Child results and child tokens were hard to observe.** A timed-out child’s
   checkpoint may be gone; the main agent sometimes re-spawned to learn status,
   and reports counted only the main session’s tokens.

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
`write_file` overwrite, so the license never fired. The new gate blocks
`patch_file` too once the turn is over the read threshold.

## The successful design: DelegationPressureGate

`agent/middlewares/delegation_pressure/core.py`:

- Counts **read/exploration** calls cumulatively within one turn.
- At **8** reads, injects one directive to delegate.
- At **16** reads, blocks every read/write tool for the rest of the turn. Each
  blocked call returns an error naming `sessions_spawn` (researcher for
  read-only work, executor for implementation).
- Browser debugging tools are excluded from both counting and blocking.
- The counter resets only in `after_agent`; an accepted spawn does not unlock
  the turn, and a write does not reset the count. This is what stops the
  post-spawn patch loop that the first soft gate allowed.

## Results

| Arm | Resolved | Duration | Main-session tokens | Estimated cost |
| :--- | :--- | :--- | :--- | :--- |
| `DelegationPressureGate` 8/16 (run 20261101_174750) | 2/2 | 1693.4s | 1,962,778 / 12,673 | $1.000 |
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

SHERRY_EVAL_DELEGATE_ONLY=1 uv run --no-sync python evals/swebench/suite.py \
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
