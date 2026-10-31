# Eval findings — what the measurement runs actually turned up

Every problem the eval runs surfaced, with its evidence and the improvement it
implies. This file is the improvement backlog the measurement exists for: a
number without a finding is a number nobody can act on.

Rules for entries:

- an entry cites the RUN that produced it (a report directory under
  `evals/results/…`, or a log line), never a recollection;
- `status` ∈ `TODO` / `IN-PROGRESS` / `FIXED` / `WONTFIX-ENV` — `WONTFIX-ENV`
  means the finding is about the measurement HOST, not about Sherry, and must
  still travel with any number we quote;
- a HARNESS problem is fixed in the suite, never filed against the model: the
  split between "the model failed" and "the harness failed" is the whole point
  of the external suites (`coding_capability` = self-designed; `swebench`,
  `deepswe`, `terminal_bench`, `agentbench`, `toolbench` = external).

| # | Finding | Found by | Status |
| --- | --- | --- | --- |
| F1 | Provider 400 on the thinking-mode replay path | `coding_capability` run `20261014_010112` | TODO (product) |
| F2 | The orchestrator steering fights a coding task | every `coding_capability` / `swebench` run | TODO (config) |
| F3 | Near-miss fixes are not verified against the reported behaviour | `swebench` `sympy__sympy-24909` (two runs) | TODO (skill) |
| F4 | Harness: the local pytest gate is load-sensitive | gate runs 2026-10-14 | WONTFIX-ENV |
| F5 | The measurement host has no OS sandbox backend | every eval run | WONTFIX-ENV |
| F6 | The auxiliary model's provider returned 429 "balance exhausted" | unit run 2026-10-14 (review factory fix) | RESOLVED (aux → DeepSeek) |
| F7 | External instances whose own suite fights a hand-built venv | hard-slice gold checks 2026-10-14 | WONTFIX-ENV (documented) |
| F8 | Eval leftovers inside the repo slow a repo-wide search test | gate run 2026-10-14 (`test_search_engines`) | FIXED (suite cleans up) |

## F1 — Provider 400 on the thinking-mode replay path (product)

**Symptom.** In the first coding-capability run two of four tasks died with
`400 … The reasoning_content in the thinking mode must be passed back to the
API`; the agent had made no changes yet.

**Evidence.** `evals/results/coding_capability/20261014_010112` — the task
records carry the provider error, and the run log shows the sequence: the model
stream stalled mid tool-call (`stream stalled mid tool-call (terminal)`), the
retry middleware replayed the partial assistant message, and the provider
refused the replay. The thinking switch was NOT explicitly pinned in that run.

**Root cause (verified for the trace, inferred for the mechanism).** The replay
of an incomplete tool-call turn does not carry the turn's `reasoning_content`,
which the provider requires once the request is in thinking mode.

**Improvement.** Investigate the retry path: a stalled-stream retry should either
resend the complete assistant turn *with* its reasoning or drop the incomplete
tool calls and re-ask — never replay a tool-call turn stripped of its reasoning.
Until then the suites pin `LLM_THINKING_ENABLED=false` per session and record it
in the report (`model.thinking_enabled`).

**Status.** TODO (product). The harness side is already handled: provider
failures are classified `invalid_run`, retried once in a fresh session and
excluded from the pass rate (the invalid-run rule).

## F2 — The orchestrator steering fights a coding task (config) — resolved 2026-10-31

**Symptom (historical).** In every eval task the log showed
`TaskIntentMiddleware: first-arm steering for session evals-…`: the middleware
injected its orchestrator directive — *"YOU ARE AN ORCHESTRATOR. NEVER THE
IMPLEMENTER. You DO NOT write code."* — into a session whose task is "fix this
repository". The coding harness also gave spawned children only read-only tools,
so obeying the steering *could not* solve the task; it also named a
non-existent `task` tool instead of `sessions_spawn`.

**Evidence.** Any suite log, e.g. `/tmp` capture of the coding-capability runs;
the middleware's arming line appears once per session, on the first model call.

**Fix (2026-10-31).** The shipped coding template and the TaskIntent directive
now default to direct implementation for a small, single-file change and use
`sessions_spawn` for delegation when a task is large or parallelizable; the
"NEVER THE IMPLEMENTER" rule is gone. The eval sandbox also gives spawned
children the full tool surface by default (`EvalSandbox(restrict_child_tools=False)`),
so both the direct-edit and delegated paths can produce a patch.

**Status.** Resolved (2026-10-31). The remaining variable is model behaviour,
not a prompt/tool-name dead-end.

## F3 — Near-miss fixes are not verified against the reported behaviour (skill)

**Symptom.** On `sympy__sympy-24909` the agent produced a structurally correct
change (`__mul__` restructured, `__rmul__` added) but kept `return 1` where the
issue's reproduction expects the SymPy object (`S.One`) — the reported symptom
survives the fix, and `FAIL_TO_PASS` stays red while `PASS_TO_PASS` stays green.

**Evidence.** `evals/results/swebench/20261014_082728` (and the earlier
single-instance run): `f2p=False, p2p=True`, diff recorded under
`diff-sympy__sympy-24909.patch`. The HARD slice repeats the shape twice more
(2026-10-14, `evals/results/swebench/20261014_211853`): both instances came back
`f2p=False, p2p=True` with a small, plausible, single-file change — on
`sympy__sympy-20639` the agent even reproduced the bug first ("I've reproduced
the bug") and still shipped a fix whose pretty-printed output is wrong.

**Improvement.** This is the SWE-bench-faithful case (the new tests are hidden
from the agent), so the only available verification is the issue's own
reproduction: a playbook line in the coding skills (`code-wiki` /
`ulw-execute`) on "reproduce the symptom, fix, re-run the reproduction" is the
lever. Worth measuring again on the same instance after the change.

**Status.** TODO (skill/prompt).

## F4 — Harness: the local pytest gate is load-sensitive (WONTFIX-ENV)

**Symptom.** Two full local gate runs failed with pytest-timeout dumps whose
stacks sat in `inspect`/`importlib`/`_path_stat` — no assertion ever ran.
Measured on the same host: `import transformers` (normally ~12 s, dominated by
`torch`) spikes to 50–94 s while the interactive container is busy, which
trips the per-test 60 s watchdogs.

**Evidence.** Gate logs 2026-10-14 (two runs); the timing measurements.

**Improvement / how to triage.** Not a Sherry bug. A red local gate whose
failures are `Timeout` with import/inspect stacks should be re-run
(`… run_tests_split.py -- -p no:timeout`) or checked against
`python -c "import time; t=time.time(); import transformers; print(time.time()-t)"`
before being treated as a regression; CI (a quiet runner) is the authority.

**Status.** WONTFIX-ENV.

## Hard slice — first measured run (2026-10-14)

**0 / 2 resolved** on the hard slice (`--hard`: `django__django-17051`,
`sympy__sympy-20639`), no invalid runs, no timeouts:

| instance | verdict | change | cost |
| --- | --- | --- | --- |
| `django__django-17051` | f2p 0/5, p2p green | one file, `django/db/models/query.py`, 25-line diff | 113 s, 49 tool calls, 1.46M in / 14.9K out tokens |
| `sympy__sympy-20639` | f2p 0/2, p2p green | one file, `sympy/printing/pretty/pretty.py`, 14-line diff | 92 s, 28 tool calls, 545K in / 5.9K out tokens |

What this does and does not say: with 2 instances it is not a capability number,
and the easy spike's 2/3 is not a trend line — but the slice DOES discriminate
(both hard instances fail where the small ones passed), and the failure shape is
informative: the agent does not break anything (p2p green) and does not run out
of budget (92–113 s inside a 40-minute cap); it produces a well-formed change
that does not actually implement the reported behaviour. That is the same
finding as F3, now twice more, and it points at the same lever (verification
against the reported reproduction, not more turns).

**Cost note.** The hard instances are token-heavy (1.46M input tokens for
django): the agent re-reads large files repeatedly, which is where a cheaper
retrieval habit would pay. Worth measuring again after the F3 lever lands.

## F7 — External instances whose own suite fights a hand-built venv (WONTFIX-ENV)

**Symptom.** Two intended hard-slice instances fail the gold-patch self-check —
i.e. the benchmark's OWN fix does not resolve them here — for environment
reasons, so they are dropped from the slice rather than scored:

- `pytest-dev__pytest-11148`: pytest's suite owns the `pytest` distribution, and
  even with the editable checkout two PASS_TO_PASS tests fail in a hand-built
  venv (`testing/acceptance_test.py::test_usage_error_code`, plus a
  conftest-load-failure case).
- `pylint-dev__pylint-7080`: `tests/test_self.py` imports `py._path.local`, and
  the `py` module a modern pytest leaves behind is a stub without `py.io`.

**Evidence.** `evals/swebench` run logs, 2026-10-14 (HARNESS FAILURE blocks with
the failing ids). Both instances' ids are kept in
`evals/swebench/instances.py`'s comment, with the reason.

**Improvement.** These are per-repo environment problems the official images
pin away (older pytest through the repo's own requirements). A future
enhancement is a per-repo install spec (the benchmark ships one per repo); until
then the slice stays at the repos whose gold patch resolves here — which is the
self-check doing exactly its job.

**Status.** WONTFIX-ENV (documented; the instances are excluded, not scored).

## F8 — Eval leftovers inside the repo slow a repo-wide search test (FIXED)

**Symptom.** `tests/agent/tools/file_tools/test_search_engines.py::test_a_repository_wide_search_completes_inside_the_budget`
failed after the SWE-bench runs: each instance leaves a checkout + venv
(hundreds of MB, tens of thousands of files) under `evals/results/**` — gitignored,
but still on disk inside the repository — and the test scans the LIVE repository
with a 60 s ripgrep budget, which the extra files push over.

**Evidence.** Gate run 2026-10-14 (GROUP A, group A's only failure), and the
test passing again after the leftovers were removed.

**Improvement.** `evals/swebench/suite.py` now deletes its `instances/` tree at
the end of every run (`--keep-instances` opts out for debugging), so a
measurement run cannot perturb the unit suite it sits next to.

**Status.** FIXED (in the same commit as this entry).

## F6 — The auxiliary model's provider returned 429 "balance exhausted" (RESOLVED: aux → DeepSeek)

**Symptom.** While adapting the memory-review tests, a test that used to run on
the (faked) main model now builds the REAL auxiliary model — and the provider
answered `429 {'code': '1113', 'message': '余额不足或无可用资源包'}`. Every
auxiliary-backed pass (memory review, plan extraction, step/completion judges,
the summarization call itself) degrades under that: the two nudge forks are
fail-open, so reviews simply stop happening; the summarizer's call is not.
(Update 2026-10-14, later the same day: the memory review no longer runs on the
auxiliary model — it was moved to a cache-warm pass on the session's own main
model, so this finding now covers plan extraction, the judges and the
summarizer.)

**Evidence.** `tests/agent/middlewares/test_memory_review_facts.py` failure
output, 2026-10-14.

**Improvement.** Top up the auxiliary provider (or point `AUXILIARY_LLM_*` at a
local model) before the next measurement run; the eval reports must keep
classifying provider failures as `invalid_run` when they surface there. The
nudge forks' fail-open behaviour is correct (a broken provider must not break a
turn) but means the degradation is SILENT — worth a log at warning level.

**Resolved 2026-10-14.** The auxiliary family was pointed at DeepSeek
(`.models`: `AUXILIARY_LLM_PROVIDER=deepseek`, `AUXILIARY_LLM_API_NAME=deepseek-flash`,
`AUXILIARY_LLM_API_BASE=https://api.deepseek.com/v1`), verified with a live call;
the live nudge-cadence e2e then passes end-to-end (real compaction → real
summary → real memory review, 21k prefix tokens served from cache).

**Status.** RESOLVED (account). The silent-degradation log stays TODO.

## F5 — The measurement host has no OS sandbox backend (WONTFIX-ENV)

**Symptom.** Every eval run executes under `SANDBOX_POLICY=auto` with no
bubblewrap available (PRoot container), so the agent's `terminal` / `python_repl`
commands run **unsandboxed**; the evidence ledger marks those runs
`sandboxed: false`.

**Evidence.** The `isolation` block of every eval report ("commands run
UNSANDBOXED here…"), and the measurement protocol's one-shot-container requirement.

**Improvement.** Run the suites in a container/VM when one is available. Until
then the limitation travels with the numbers: task repositories are throwaway
directories and the prompts are ours, but that mitigation is absent.

**Status.** WONTFIX-ENV (documented, not silent).
