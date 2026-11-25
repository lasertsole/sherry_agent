# 🛠️ WorkMode Sub-Mechanisms — Task-Level Reliability & Coordination

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> Part of [Long-Running Tasks](../README.md): five opt-in mechanisms that harden a running TaskFlow against interruption, silent stalls, low-quality steps, repeated planning, and missing task facts. They borrow the *sub-mechanisms* of a WorkMode-style long-task system without copying its three-layer organization; the expert-panel mode answers "who does the work", these answer "how it is done reliably".

**Source of truth:** `agent/middlewares/task_intent_router/`, `agent/tools/taskflow/watchdog.py`, `agent/tools/taskflow/step_judge.py`, `agent/tools/taskflow/template_store.py`, `agent/tools/taskflow/tools/`, `agent/tools/subagent/spawn/system_prompt.py`, `config/features/agent_side/taskflow_infra.py`, `config/path.py`. Every constant and code path below was verified against that source.

## The five mechanisms at a glance

| Mechanism | Problem it solves | Where it lives | Default |
| :--- | :--- | :--- | :--- |
| Intent-classified input routing | A user message mid-flow must not blindly interrupt the task | `agent/middlewares/task_intent_router/` before-agent middleware | Active flows only; fail-open |
| Watchdog + deadlock detection | A flow that stalls or loops without progress goes unnoticed | `agent/tools/taskflow/watchdog.py`, swept by the subagent registry sweeper | Warnings only, never mutates a flow |
| Scored self-evaluation loop | A binary pass/retry judge gives no quality gradient or retry ceiling | `step_judge.py::judge_step_self_eval` + resume consumer | Opt-in per step (`self_evaluate`) |
| Plan template reuse | Re-planning the same decomposition from scratch every time | `agent/tools/taskflow/template_store.py`, `taskflow_flow` actions | Opt-in actions |
| Plan / environment dual-file | Task facts (paths, stack, constraints, credential refs) scatter across the conversation | flow `state["environment"]` → child system prompt | Empty = no prompt change |

All five are **additive**: with no active flow, no `self_evaluate`, no template action and no `environment`, behaviour is byte-identical to before.

---

## 1. Intent-classified input routing

When a TaskFlow is running, a new user message is first classified by `classify_turn_intent()` in `agent/middlewares/task_intent_router/intent.py`. It is **rules-only** — no LLM call — and mirrors the four-language keyword matching of the existing TaskIntent: Chinese, English, Japanese and Korean cancel phrases are all recognized. Ambiguous input resolves to `CONTINUE` (fail-open: never drop or misroute a message).

Priority: `CANCEL` > `SOFT_PREEMPT` > `RESUME` > `CONTINUE` > `NEW`.

| Intent | Trigger | Route |
| :--- | :--- | :--- |
| `CANCEL` | Explicit stop words (`停下`, `cancel`, `やめて`, `중단`) | Cancel every active flow via `taskflow_flow(action="cancel")`, then inject a short notice |
| `SOFT_PREEMPT` | "wait / adjust direction" phrasing | Advisory steering message for the next turn; does not cancel or interrupt the running step |
| `RESUME` / `CONTINUE` | HITL answer or task-related supplement | No-op — the existing turn/HITL path handles it |
| `NEW` | A clearly unrelated new topic | Advisory steering; the existing queued-input mechanism remains the authoritative queue |

`TaskIntentRouterMiddleware` is registered at main position 30 in `agent/middlewares/specs.py` (`ORDER IS A CONTRACT`), just before the optional `DelegationPressureGate`. The middleware is a **no-op when no active flow exists**, so short tasks never enter this path. Because the agent layer cannot import server queues, SoftPreemption and New emit advisory steering into the turn rather than directly manipulating server-side queued input. Internal/system-directive messages are never re-classified (a cancel notice cannot cancel a second time).

---

## 2. Watchdog + deadlock detection

`agent/tools/taskflow/watchdog.py` adds task-level liveness detection to the existing call-level guards (InvalidToolCallRepair, LLMRetry, MaxTokensBoost). The subagent registry sweeper calls `sweep_and_notify()` after its liveness pass. Detection is purely **fail-open**: it writes no flow state, it only pushes a `system.notice` warning (`source="taskflow:watchdog"`) so the agent or user decides to cancel, retry or continue.

`WatchdogAlert(flow_id, session_id, kind, detail)` kinds:

- **stale heartbeat** (`heartbeat_timeout_s`, default 600s): flow non-terminal and `updated_at` older than the threshold.
- **no progress** (`deadlock_no_progress_s`, default 1800s): flow running for too long with no step status change.
- **tool loop** (`deadlock_tool_fail_count`, default 3): the same step/tool failing repeatedly with the same arguments.
- **judge loop** (`deadlock_judge_fail_count`, default 5): a step's judge returning retry again and again.

All thresholds live in `config/features/agent_side/taskflow_infra.py`. Setting a threshold to `0` disables that check. Notification failures are swallowed — a broken notice channel never blocks the sweeper.

---

## 3. Scored self-evaluation loop

`StepJudge`'s default is unchanged: a binary `PASS` / `RETRY` / `BLOCK` / `UNKNOWN` verdict, with `UNKNOWN` (judge outage) failing closed. On top of it, `step_judge.py::judge_step_self_eval` adds an **opt-in scored path** for a step declared with `self_evaluate: true`:

- A strict reviewer LLM scores 1–10 against the step's `judge_criteria` / `validation_criteria`, returning `{"score", "reason", "feedback"}`.
- Score ≥ `self_eval_min_score` (default 7) → step `DONE`.
- Score < minimum and retry budget remains (`self_eval_max_retries`, default 5) → `RETRY` with the score and feedback appended to the step.
- Budget exhausted → `forced_pass: true` and the step is marked `DONE`, so a subjective judge can never loop forever.
- A judge outage returns `score=None` and follows the configured `on_judge_error` policy — it never becomes a silent pass.

The three step fields (`self_evaluate`, `self_eval_min_score`, `self_eval_max_retries`) are in the `run_task` `TaskflowStepInput` schema and the `_OPTIONAL_STEP_FIELDS` whitelist. A step without `self_evaluate` takes the original binary judge path, unchanged.

---

## 4. Plan template reuse

`agent/tools/taskflow/template_store.py` stores the **decomposition structure** of a finished flow — step ids, tasks, dependencies, functional role, judge/validation criteria and response schema — under `APP_HOME/taskflow_templates/<name>.json` (`config.path.TASKFLOW_TEMPLATES_DIR`). Runtime fields (child session keys, results, revisions, timestamps) are stripped by a whitelist, so a template is an empty shell to be re-executed, not a replay of a previous run.

Two `taskflow_flow` actions expose it:

- `save_as_template(flow_id, name, description="")` — write the flow's step structure atomically (`tempfile` + `os.replace` under a file lock).
- `load_from_template(name, flow_id, description="")` — create a new flow from the template's step structure and bind its todo root.

This is deliberately **complementary** to PTC/LDW replay: a template reuses *how the work was decomposed*; PTC/LDW reuses *the exact execution call sequence*. A task may load a template and then let a precompiled script replay the calls.

> Progressive step expansion is **not** adopted. Sherry's nested TaskFlow already covers it: a top-level flow keeps coarse steps and a step's child ORCHESTRATOR creates its own sub-flow; progress reports travel upward via `progress_push` / `system.notice`. Adding an `expandable → expanded` transition to the DAG state machine would buy no compression (steps live in SQLite, not the conversation) and add partial-expansion finish gates for no gain.

---

## 5. Plan / environment dual-file

A flow may be created with `taskflow_flow(action="create", environment={...})`. The dict is stored verbatim in `state["environment"]` (`taskflow_create.py`) — credentials stay references (`keychain://...`), never resolved. `store_sqlite.get_flow_environment_sync(flow_id)` reads it back by globally-unique flow id.

At step dispatch, `_dispatch.dispatch_child()` prefers an explicitly supplied `environment`, otherwise reads the owning flow's environment, and passes it into `spawn_subagent_direct()`. `SpawnRequest.environment` carries it into `SubagentPromptContext`, where `_render_environment_section()` in `agent/tools/subagent/spawn/system_prompt.py` renders a `## Task Environment` block *after* the role instructions and *before* the rules. Non-string values are JSON-rendered; when any value contains `credential_ref`, the block reminds the child these are references to resolve through the approved credential tool, never usable secrets.

Guarantees:

- Empty or absent environment → **no section at all**, so existing child prompts are byte-identical.
- A failed environment read → dispatch still proceeds (fail-open), the child simply has no environment block.
- Explicit `environment` on a dispatch overrides the flow-level value.
- Environment is task-level, orthogonal to session-level `project_dir` / `AGENTS.md` injection.

---

## Configuration reference

| Config (`TASKFLOW_INFRA`) | Default | Purpose |
| :--- | :--- | :--- |
| `heartbeat_timeout_s` | `600.0` | Stale-flow warning threshold |
| `deadlock_tool_fail_count` | `3` | Repeated same-tool failure count |
| `deadlock_judge_fail_count` | `5` | Repeated judge-retry count |
| `deadlock_no_progress_s` | `1800.0` | No-progress warning threshold |

## Testing

- `tests/agent/middlewares/test_task_intent_router.py` — four-language classification, active-flow no-op, cancel routing, internal-message handling.
- `tests/agent/tools/taskflow/test_watchdog.py` — each alert kind, thresholds, terminal filtering, notification fail-open.
- `tests/agent/tools/taskflow/test_self_eval.py` — score parsing, pass/retry/forced-pass, outage policy, default judge untouched.
- `tests/agent/tools/taskflow/test_template_store.py` — structure whitelist, atomic CRUD, merged actions.
- `tests/agent/tools/taskflow/test_environment.py` — persistence, sync read, dispatch injection/precedence/fail-open.
- `tests/agent/tools/subagent/spawn/test_system_prompt_environment.py` — block rendering, credential refs, empty backward compatibility.

Run them with `uv run pytest tests/agent/tools/taskflow -q` (the split process runner `tests/run_tests_split.py` is the CI gate).
