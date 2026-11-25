# 🧪 Eval-Proven Practices in the Product

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> Six mechanisms that started as eval-harness hardening (DeepSWE / external-bench) and are now first-class product behavior. Each is post-task or fail-open: none of them can break a turn, and each is derived from a measured eval failure mode. Source of truth: the files listed under each section.

## 1. Leaf timeout 1800 s (`agent/tools/subagent/config.py`)

`run_timeout_seconds` defaults to **1800 s** (was 600 s). DeepSWE measured 9/20 resolved at 600 s vs 20/20 at 7200 s — the bottleneck was the child being killed mid read-implement-test, not model capability. The depth ladder triples it per level above the leaf (depth-1 = 5400 s). `evals/deepswe/suite.py` sets `_EVAL_CHILD_RUN_TIMEOUT_MULTIPLIER = 1.0` (the product default already covers the eval budget; no extra ×3). Min 600 / max 7200 still clamp explicit overrides.

## 2. CJK-weighted memory char limit (`agent/tools/memory.py`)

Memory's char caps (2200 / 1375 / 1375) now count a CJK char as `chars_per_token / chars_per_token_cjk` (4/2 = 2) weighted chars via `_weighted_char_count`, sharing `pub/func/cjk.py::count_cjk` and the token-estimation config. ASCII capacity is unchanged; CJK content now fits the same token budget instead of costing 2×. Every limit check (add / replace / evict / `_append_to_target`) and the user-facing `[n% — X/Y chars]` indicator use the weighted count. An empty/failed count stays fail-open.

## 3. Cross-turn diff-hash stagnation (`agent/tools/todolist/stagnation_tracker.py`, `agent/middlewares/todo_continuation/core.py`)

The plan-text stagnation check cannot see a model that keeps editing todos while the code patch stays identical ("closure-flow churn"). At each continuation turn end the enforcer hashes the staged + unstaged binary git diff (`_working_tree_diff_hash`, off the event loop). An unchanged hash for `TODOLIST_INFRA["diff_stagnation_threshold"]` (default 2) consecutive continuation turns — with **no step child in flight** — injects `[SYSTEM DIRECTIVE: NO PROGRESS DETECTED]` and latches the stop. A real user turn or in-flight children re-baseline the streak; a non-git directory returns an empty hash (fail-open, never flagged). Runs in parallel with plan-text stagnation; either one stops the gate.

## 4. Verify-completion settle wait (`config/features/agent_side/todolist_infra.py`, `agent/middlewares/todo_continuation/core.py`)

A step child can report `done` while its file edits are still flushing. Completing verify in that window yields a false finish. When the reviewer is finished and step children are in flight, the gate waits up to `verify_settle_wait_s` (default 30 s; eval used 180 s) for them to settle. On timeout it appends `[WARNING: SUB-TASKS STILL RUNNING]` to the verify directive — completion is never blocked, but the model is told the patch may be incomplete. A broken flow read degrades to "settled" (fail-open).

## 5. Reviewer acceptance checklist (`agent/tools/taskflow/checklist_extractor.py`, `agent/tools/taskflow/tools/_dispatch.py`)

A reviewer spawned for a TaskFlow step with `functional_role="reviewer"` now gets a numbered PASS/FAIL **ACCEPTANCE CHECKLIST** appended to its task, extracted from normative sentences (must / should / shall / raise / reject / only / never / charset / return / error …), capped at 20 clauses, de-duplicated, case-insensitive. Gated by `TASKFLOW_INFRA["reviewer_checklist_enabled"]` (default on). Only the task text changes — reviewer model, tools and isolated context are untouched. Idempotent: a task already carrying the checklist label is never appended twice (the eval-side `set_reviewer_checklist` stays authoritative). Extraction errors fail open.

## 6. Session run report (`pub/func/session_report/**`, `server/service/session_report_service.py`, `server/trigger/http/session_report.py`)

Three post-task analyzers moved from eval to a shared product layer — no hot-path hooks:

- **token tracker** — folds `usage_metadata` into a main + per-role child breakdown; USD cost only when `SHERRY_SESSION_REPORT_TOKEN_PRICES` (or the legacy eval var) points at a valid price file;
- **context analyzer** — per-call HIT/WASTED scoring of tool calls against task keywords; `hit_rate = effective / (effective + wasted)`; planning/orchestration tools unscored; undecidable calls count as HIT (conservative);
- **code-intel tracker** — precise (LSP / explore / callers / semantic) vs greedy (search_files / read_file) call ratio, plus degraded-call detection from fail-open ToolMessage text.

The report is generated on first API read and cached in-process (no background turn-end task and no state-register write); `GET /sessions/:session_id/report` (or `?session_id=`, `?refresh=1`) serves it live on cache miss. The client renders it as a collapsible panel in the context viewer (`client/app/components/chat/SessionReport.vue`). A process restart simply rebuilds the cache on the next read.

## Safety invariants

- All six are **additive**: no existing prompt contract, tool schema, state literal or turn-lifecycle hook changed incompatibly (the report cache is process-local; no state-register key was added).
- All read paths are **fail-open**: a missing git, broken store, bad extraction or unavailable transcript degrades to "no signal", never a hard failure.
- Thresholds live in `TODOLIST_INFRA` / `TASKFLOW_INFRA`; no magic numbers in the hot path.
