# 🚦 Judgement Gates — Who Decides a Step or a Flow Is Done

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> Part of [Long-Running Tasks](../README.md): every gate between "a result came back" and "this step / flow is done" — the caller's declared outcome, the Tier 1 schema gate, the failure classifier, the auxiliary-LLM StepJudge, and `taskflow_finish`'s A–E — plus the precedence between them.

A claim is not a fact. A child subagent says "done"; the engine has to decide whether that means anything. Two levels of decision, one per boundary:

| Level | Question | Gates |
| :--- | :--- | :--- |
| **Step** (`taskflow_resume`) | Did this result satisfy what the caller asked for? | declared `step_outcome` → Tier 1 schema → failure classifier → Tier 2 StepJudge |
| **Flow** (`taskflow_finish`) | Is this DAG actually finished? | A DAG completeness → B unresolved steps → C evidence → E goal judge |

**Source of truth:** `agent/tools/taskflow/tools/taskflow_resume.py`, `agent/tools/taskflow/tools/taskflow_finish.py`, `agent/tools/taskflow/tools/_retry.py`, `agent/tools/taskflow/step_judge.py`, `agent/tools/taskflow/evidence_collector.py`, `config/features/agent_side/step_judge.py`, `config/features/agent_side/goal_gate.py`. Every constant, verdict and rejection text below was verified against that code.

## Precedence (the one rule to remember)

**A caller-declared outcome outranks every gate; nothing else short-circuits anything.** `step_outcome` is how the caller states how the run ended — `failure` marks the step `failed` and skips the judge entirely, `skipped` marks it `skipped` (its dependents cascade to `skipped`), and `success` / `partial` are recorded on the result while the gates still run. An unknown value is rejected before any state change. Everything below applies when the caller declared nothing.

## Step level (`taskflow_resume`)

### 0. Failure detection — is this a failure at all?

Tier 1 schema validation runs **first** (see below) and produces `schema_validated`. That result is one of two signals the failure classifier consumes — the other is the result text:

- **The structure.** `schema_validated=false` (Tier 1's output) is a failure text classification cannot see, and it re-dispatches here — **before** any judge call, so a broken shape never spends judge tokens.
- **The result text.** `classify_failure` (`_retry.py`) is a lowercased substring scan over a pattern table, with the negated phrasings pre-stripped (`cannot connect` must not read as a connection failure). A failure phrased outside the table is not caught — the classifier is a heuristic, not a verdict.

A step carrying a `retry_policy` re-dispatches a classified failure onto a replacement child instead of settling it, while the policy's budget lasts. `taskflow_wait_all` cannot classify a dead child that returned no text, so it always consumes retry budget while one remains. No policy configured = the failure is recorded and the step settles (`failed` unless the caller said otherwise).

### Tier 1 — structure (free)

When the step declares `response_schema`, the result is parsed (`structured_result` when the caller passed one, else the result text) and validated against that schema (`validate_structured_output`):

- **A miss** is recorded as `schema_validated=false` with its error and is treated as a failure with the retry type `schema_error` — it never spends a judge call, and an unretried miss marks the step `failed`.
- **The miss teaches the retry.** A schema failure short-circuits BEFORE the judge, so `judge_feedback` is empty; the replacement child's task therefore carries the validator's own message (`Your previous output failed schema validation: <field path, expected/got>.`) as its `## Previous Attempt Feedback`, and the child inherits the step's full spawn contract (`response_schema` → its structured-output requirement, functional role, model override, run timeout) — a retry that could not see the schema or the reason could only guess.
- **The shape is also enforced at generation time.** When a step declares `response_schema`, the child agent is built with LangChain's `response_format` (a raw schema resolves to `AutoStrategy`: the provider's native structured output where supported, else a structured-output tool call), so obvious misses are stopped at the model instead of at this validator. The schema text stays in the child's system prompt as the prompt-level fallback, and this Tier-1 check stays as the second net — a provider edge case or a model that never calls the structured-output tool still gets caught here. `sessions_spawn` exposes the same `output_schema` field, so a tool-driven child can be constrained the same way.
- **A pass** stores `structured_result` + `schema_validated=true` and continues to Tier 2. Structure alone never proves the content is right — that is the whole reason the second tier exists.

### Tier 2 — semantics (TaskFlow decision LLM)

With `judge_criteria` (or the legacy `validation_criteria`) on the step, `judge_step_result` (`step_judge.py`, temperature 0) reviews the result — the validated `structured_result` when there is one, else the result text — and returns one of three verdicts:

| Verdict | Effect |
| :--- | :--- |
| **`pass`** | the step is marked `done` and its dependents unlock |
| **`retry`** | the step is re-dispatched through the shared `_retry` seam, reusing the step's own `retry_count` budget (`STEP_JUDGE["max_retries"]`, default 2), with the judge's guidance appended to the replacement task as `## Previous Attempt Feedback` (`with_judge_feedback`). An exhausted budget marks the step `blocked` |
| **`block`** | the step is marked `blocked` with the judge's reason (a decision for the caller: fix, cancel that branch, or replan) |

Details that change behaviour:

- **The evidence summary rides along.** With `STEP_JUDGE["evidence_aware"]` (default on) the prompt carries `collect_evidence_summary` for the flow — the child's recorded verification rows and their derived staleness (see [Evidence Ledger](../gates/evidence-ledger/README.md)).
- **Fail-open, always.** A model error or an unparseable response degrades to `pass`: an unreachable judge never blocks progress. The trade is explicit — the verdict is best-effort, so criteria compliance is never a hard guarantee.
- **Settled steps only.** The judge runs when the step is actually settled (`redispatched_key is None`); a step re-dispatched under the retry policy keeps its criteria stored and is judged on the eventual resume.
- **Passing `validation_criteria` to `taskflow_resume` overrides** the stored value for that call — e.g. to tighten or correct it based on what the child actually did.
- **Result-text budget.** `STEP_JUDGE["max_result_chars"]` (8000) bounds what the judge prompt sees.

### Then: `done` means "the configured gates passed"

A step's `done` never means "the work is right" by itself. With **no** expectation configured (`response_schema`, `judge_criteria` / `validation_criteria`, `retry_policy`) it degrades to the older meaning — "a result was injected" — so a child that reported an error can still land `done` and unlock successors. Declare an expectation (a `response_schema` is validated for free, before any judge call) or report `step_outcome="failure"` and the closure catches it: `failed` / `skipped` / `cancelled` are real step statuses, a failure never unlocks its dependents, and `taskflow_finish` refuses to complete over an unresolved step.

## Flow level (`taskflow_finish`)

`taskflow_finish` gates the DONE transition on four checks, in order:

| Gate | Checks | Reject reason |
| :--- | :--- | :--- |
| **A — DAG completeness** | every step is `done` or `blocked` | `step(s) not done/blocked` |
| **B — no unresolved steps** | no step is `failed`, `skipped`, `cancelled` or `blocked` | the offending steps by id, each with its recorded reason |
| **C — evidence** | the flow's evidence carries no `FAIL` and no `[stale]` row (see [Evidence Ledger](../gates/evidence-ledger/README.md)) | `failing verification evidence` / `stale evidence` |
| **E — goal judge** | an auxiliary model judges whether the collected results satisfy the flow's `description` | a rejection that names `taskflow_replan` |

Every gate is **fail-open**: an unreadable collector, a model error or an unparseable verdict passes rather than blocking a finish. Gate E runs only for a flow with a `description` and at least `GOAL_GATE["min_steps"]` (3) steps — a one- or two-step flow is already covered by the child-level [completion judge](../../subagent/README.md), and a second verdict on the same work is that overlap; `GOAL_GATE["enabled"]` is its master switch and `GOAL_GATE["max_result_chars"]` (4000) bounds each result handed to it.

## Neighbouring gates (other pages, same idea)

This page owns the TaskFlow gates. The same "a claim is not a fact" rule recurs elsewhere, documented where it lives:

- **[Evidence Ledger](../gates/evidence-ledger/README.md)** — the recorded verification trail; its summary is Gate C's input *and* the StepJudge prompt's evidence section.
- **[Subagent completion gates](../../subagent/README.md)** — the per-child layers: the CompletionJudge goal loop (DONE / CONTINUE, bounded by `COMPLETION_JUDGE["goal_max_turns"]`, with the first call skipped when the child's ledger already shows a passing non-stale row) and the completion-drain programmatic gate (`SubagentCompletionDrainMiddleware`, parent-side).
- **[TodoList Enforcement](../todolist/enforcement/README.md)** — the plan side: the E1–E7 enforcement layers, including the turn-end continuation gate and its budget.
- **Loop prevention** — [wiki/loop-prevention](../../loop-prevention/README.md) collects the guards' altitude matrix (turn / step / subagent / pipeline) with the fail-open policy per layer.

## Configuration

| Knob | Default | Effect |
| :--- | :--- | :--- |
| `STEP_JUDGE["max_retries"]` | 2 | `retry` verdicts the judge may spend (the step's own `retry_count` budget) |
| `STEP_JUDGE["max_result_chars"]` | 8000 | result-text window in the judge prompt |
| `STEP_JUDGE["evidence_aware"]` | True | include the evidence summary in the judge prompt |
| `GOAL_GATE["enabled"]` | True | master switch for Gate E |
| `GOAL_GATE["min_steps"]` | 3 | smallest flow Gate E runs for |
| `GOAL_GATE["max_result_chars"]` | 4000 | per-result window in the goal judge's prompt |

## Testing

`tests/agent/tools/taskflow/` pins the layer: `test_validation.py` (criteria storage, verdicts, override, fail-open), `test_step_judge.py` (parsing, verdict coercion, prompt assembly), `test_resume_with_judge.py` (resume + judge: pass / retry / block, retry-feedback injection, budget exhaustion), `test_finish_gate.py` (gates A–C), `test_retry_policy.py` (classification + re-dispatch), `test_expectation_closure.py` (the expectation→actual closure rules) and `test_chain_smoke.py` (judge → retry feedback → goal loop → finish gate against a stub auxiliary LLM).
