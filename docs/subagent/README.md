# 🧩 Subagent Design: Two-Axis Roles, Privilege Guards & Layered Completion Gates

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> The design-level companion to the [Subagent System README](../../agent/tools/subagent/README.md). That document is the runtime and API reference — spawn pipeline phases, registry state machines, announce delivery, tool schemas, and the full configuration table. This page states the design invariants behind the role model, the spawn-privilege guards, and the four-layer completion gate stack, and names the failure modes each layer accepts.

Source of truth: `agent/tools/subagent/**` (`types/`, `capabilities/`, `roles/`, `spawn/`), `agent/tools/taskflow/step_judge.py` and `agent/tools/taskflow/tools/`, `agent/middlewares/subagent_completion_drain/`, `config/features/agent_side/`. Every statement below was verified against that code.

## Table of Contents

- [Overview](#-overview)
- [Two Role Axes](#-two-role-axes)
- [Functional Roles: Definition, Loading, Behavior](#-functional-roles-definition-loading-behavior)
  - [Definition Files](#-definition-files)
  - [Fail-Open Loader](#-fail-open-loader)
  - [What a Role Drives](#-what-a-role-drives)
- [Spawn Privilege Guards](#-spawn-privilege-guards)
- [Completion Judge & Goal Loop](#-completion-judge--goal-loop)
- [Layered Completion Gates](#-layered-completion-gates)
- [Relationships to Other Subsystems](#-relationships-to-other-subsystems)
  - [Synthesis Aggregation](#-synthesis-aggregation)
- [Configuration](#-configuration)
- [Test Map](#-test-map)
- [Limitations](#-limitations)

## 🎯 Overview

A spawned worker is described by two orthogonal answers to two different questions:

1. **Who may spawn, and over what?** — the **depth role** (`SubagentSessionRole`), derived from nesting depth. It alone decides spawn permission and control scope.
2. **What kind of worker is this?** — the **functional role** (`FunctionalRole`), an explicit specialization. It alone decides the child LLM tier, the tool allow-list, and role-specific prompt sections.

Completion is then verified by four always-on layers, ordered from the single child run outward to the parent turn: the child-run completion judge and goal loop, the TaskFlow step judge, the `taskflow_finish` flow gates, and the parent-turn completion-drain programmatic gate. The two axes and the gate stack are the subject of this page; the API surface lives in the [module README](../../agent/tools/subagent/README.md).

### 🧱 Invariants

1. **The axes never merge.** A functional role cannot grant spawn permission, and the depth role cannot change the tool allow-list or the prompt. Both compose on every spawn.
2. **`general` is the identity role.** With no functional-role hint, spawn behavior is unchanged: no definition is loaded, and the depth role supplies the LLM tier.
3. **Spawn privilege is depth-gated twice** — at assembly time (tool-policy intersection) and at call time (a permission check inside the tool itself).
4. **Completion gates are always on.** There is no enable/disable switch anywhere in the stack; only budgets and criteria are inputs.
5. **Fail-open means model error, unparseable output, or an unavailable lookup — never a negative verdict.** A parsed `RETRY`, `BLOCK`, or failing evidence row still blocks.
6. **Child context is isolated.** A child starts from its own empty message list; the parent transcript is never inherited, and there is no mode that changes this.
7. **Completion carriers are claims, not verified results.** The parent turn receives the child's report as a claim; when the session has no passing evidence, the drain gate appends a mandatory-verification message.

## 🧭 Two Role Axes

| Axis | Resolved from | Drives |
|------|---------------|--------|
| **Depth role** (`SubagentSessionRole`) | nesting depth | spawn permission, control scope |
| **Functional role** (`FunctionalRole`) | explicit hint → `agent_id` match → configured default | child LLM, tool allow-list, prompt sections |

**Depth role.** `resolve_subagent_capabilities(depth, max_depth)` maps depth `0` to `MAIN` with `ControlScope.CHILDREN`, any `depth >= max_depth` to `LEAF` with `ControlScope.NONE`, and everything in between to `ORCHESTRATOR` with `CHILDREN`. The single predicate behind permission decisions is `can_spawn_children(role)`, true for `MAIN` and `ORCHESTRATOR` only. Depth itself resolves from the run record first, then from the `:subagent:` count in the session key (`get_subagent_depth`). `max_spawn_depth` defaults to `2` and is hard-capped at `2`.

**Functional role.** `_resolve_functional_role(hint, agent_id)` tries the explicit hint (an unknown hint logs a warning and falls through), then the `agent_id` name, then the configured `default_functional_role`, and finally `GENERAL`. `GENERAL` short-circuits before any definition is loaded, which is what keeps the depth-based behavior intact for hint-less spawns.

The two axes are orthogonal in both directions: a `researcher` at depth 1 is still an `ORCHESTRATOR` that may spawn; a `general` at the depth limit is still a `LEAF` that may not.

## 🧰 Functional Roles: Definition, Loading, Behavior

### 📄 Definition Files

Built-in definitions ship inside the package, tracked and distributable, at `agent/tools/subagent/roles/definitions/<name>/AGENTS.md`. An optional per-user override may sit (untracked) at `workspace/subagent_roles/<name>/AGENTS.md`; resolution order is **override → package default → none**, and the override directory name is configurable. Each file carries YAML frontmatter (`name`, `description`, `model_tier`, `tools`) plus a markdown body that is appended to the child prompt. `tools: inherit` resolves to “all tools”; an unknown `model_tier` falls back to `inherit`.

| Role | Purpose | Effective LLM tier | Role tool set |
|------|---------|--------------------|---------------|
| `general` | Default worker; identity role (its package definition is never loaded) | depth role | all tools + ast-grep `ast_grep_search`, `ast_grep_rewrite` |
| `researcher` | Read-only codebase and web research | `auxiliary` | `read_file`, `terminal`, `web_search` + code-intel `explore`, `callers`, `callees`, `impact`, `semantic_code_search` + LSP `lsp_goto_definition`, `lsp_find_references`, `lsp_workspace_symbol`, `lsp_call_hierarchy`, `lsp_rename`, `lsp_diagnostics`, `lsp_format`, `lsp_status` + ast-grep `ast_grep_search`, `ast_grep_rewrite` |
| `executor` | Write-capable implementation and command execution | `auxiliary` | `read_file`, `write_file`, `patch_file`, `terminal`, `python_repl` + ast-grep `ast_grep_search`, `ast_grep_rewrite` |
| `reviewer` | Read-only diff and quality audit | `auxiliary` | `read_file`, `terminal` + ast-grep `ast_grep_search`, `ast_grep_rewrite` |

**ast-grep is universal; code-intel is not.** Every functional role (including `general`) additionally receives the ast-grep structural search/rewrite tools `ast_grep_search` and `ast_grep_rewrite`. The tree-sitter code-intel tools `explore` / `callers` / `callees` / `impact` / `semantic_code_search` remain `researcher`-only, because they need the symbol index; `semantic_code_search` is the embedding-backed concept search over that index. The LSP tools `lsp_goto_definition` / `lsp_find_references` / `lsp_workspace_symbol` / `lsp_call_hierarchy` / `lsp_rename` / `lsp_diagnostics` / `lsp_format` / `lsp_status` are `researcher`-only for the same reason: they need a running language server, which the child starts lazily and stops when idle. `lsp_rename` and `lsp_format` preview by default, `lsp_diagnostics` waits for the asynchronous diagnostics notification, and `lsp_status` reports availability without starting anything.

### 🛡️ Fail-Open Loader

`load_role_definition(role)` walks the candidate paths, parses the first existing file, and returns `None` on every failure: no file at all, missing or unterminated frontmatter, non-mapping frontmatter, an invalid `tools` value, or an unreadable file. Each parse failure logs a warning; the caller treats `None` as `general`. The loader is fail-open on *model, file, and parse* problems only — it can never turn a malformed definition into a privileged one. `load_all_role_definitions()` caches results in-process; call `invalidate_role_cache()` after editing a workspace override.

### ⚙️ What a Role Drives

**LLM selection.** An explicit `model_override` wins; otherwise a non-`general` role's `model_tier` applies; otherwise the depth role decides (ORCHESTRATOR → main LLM, LEAF → auxiliary LLM).

```text
explicit model_override
  └─► functional-role model_tier ("main" | "auxiliary")
        └─► depth role (ORCHESTRATOR → main LLM, LEAF → auxiliary LLM)
```

**System prompt.** A non-`general` role (or any non-empty role description/body) adds the `{ROLE}` specialization line to `## Your Role` and appends a `## Role Instructions` section carrying the definition body.

**Tool policy.** A role's `tools` list becomes an allow-list, and the default deny-list is cleared because the allow-list already restricts. Per-spawn `extra_tools` join that allow-list; both remain subject to the unconditional `main_only` metadata drop and to any explicit deny-list.

## 🔒 Spawn Privilege Guards

Spawn privilege is enforced at two independent points, so no single leak can escalate it:

- **Assembly time (primary gate).** After the final allow-list is built from the role whitelist and `extra_tools`, Phase 8.6 intersects it with `can_spawn_children(role)`: for any role that may not spawn, `sessions_spawn` and `sessions_yield` are stripped from the list. In inherit mode the list is empty and the default deny-list already blocks the pair, so the intersection is a no-op there.
- **Call time (defense in depth).** `check_spawn_permission(session_id)` resolves the canonical caller key (child and swarm keys verbatim; any other id gets the `agent:main:session:` prefix), resolves depth from the run record first and the session-key shape second, then resolves the depth role and refuses non-spawning callers. It returns a `(allowed, reason)` tuple and never raises; the tool renders the refusal through its existing string contract.

```text
assembly:  final allow-list ∩ can_spawn_children(role)   → sessions_spawn / sessions_yield stripped
call:      check_spawn_permission(session_id)            → "status=forbidden", never raises
```

The call-time guard backs `sessions_spawn` (the escalation path) in both the tool and the runtime spawn wrapper. `sessions_yield` is protected by assembly-time stripping alone.

## 🧪 Completion Judge & Goal Loop

The completion judge (`spawn/completion_judge.py`) is the subagent-level gate. An auxiliary LLM at temperature `0` receives the task, the child's latest response (capped at `8000` characters), and the child session's verification-evidence summary, and returns `DONE` or `CONTINUE` with a one-sentence reason; a `CONTINUE` verdict also carries a short continuation prompt. Parsing degrades safely — JSON repair first, then a regex scan — and any model error or unusable output finalizes as `DONE` with an explicit fail-open reason.

The goal loop runs on every spawn whose budget exceeds one turn, under `COMPLETION_JUDGE["goal_max_turns"]` (default `5`), which counts the first turn. A per-spawn `goal_max_turns` parameter overrides it. On `CONTINUE` the judge's prompt is injected as the next `HumanMessage` on the **same checkpoint thread**, so the continuation extends the same child conversation instead of starting a new one.

```text
turn 1 ─► judge DONE ─────────────────────────────────────► finalize
      └─► judge CONTINUE (continuation_prompt) ─► turn 2 ─► judge …
               (repeat until DONE, empty continuation prompt, or turns_used == goal_max_turns)
```

When the budget is spent the run still finalizes as `OK`, with `error="goal_loop_budget_exhausted"` recorded — the loop never converts exhaustion into a failure. An empty continuation prompt also ends the loop. A budget of `1` keeps the child single-turn: the judge is not called at all.

## 🚦 Layered Completion Gates

Four gates stack from the child run outward. All four are always on; the only inputs are budgets and criteria.

| # | Layer | Gate | Input it needs | Fail-open behavior |
|---|-------|------|----------------|--------------------|
| 1 | Child run | completion judge + goal loop (`spawn/completion_judge.py`) | task, latest response, evidence summary; budget `goal_max_turns` (default `5`) | model error / unparseable → `done` |
| 2 | Step | TaskFlow step judge (`agent/tools/taskflow/step_judge.py`) | the step's `validation_criteria` (absent → no judge call) | model error / unparseable → `pass` |
| 3 | Flow | `taskflow_finish` gates A–D (`agent/tools/taskflow/tools/taskflow_finish.py`) | DAG state and flow evidence; plus `todo` and `plan_path` for Gate D | unreadable ledger / verifier error → pass |
| 4 | Parent turn | completion-drain programmatic gate (`agent/middlewares/subagent_completion_drain/`) | the session's verification-evidence summary | lookup failure → gate skipped |

**Layer 2 — step judge.** After `taskflow_resume` injects a child result, a step that carries `validation_criteria` is judged `PASS` / `RETRY` / `BLOCK`. A `RETRY` re-dispatches the step with the judge's feedback while the step's retry count is below `STEP_JUDGE["max_retries"]` (default `2`); exhausting the budget blocks the step. Criteria are the judge's input, not a switch — a step without them is never sent to the judge.

**Layer 3 — flow gates.** `taskflow_finish` refuses the `DONE` transition until Gate A sees every step `done` or `blocked`, Gate B sees no blocked steps, Gate C sees no `FAIL` or `[stale]` flow evidence, and — only when the caller supplies both `todo` and `plan_path` — Gate D passes the `SisyphusVerifier`. Gate D's linkage is an input rather than a switch: without it the finish has no independent verdict, and the skipped gate is silent.

**Layer 4 — parent-turn gate.** When the drain injects queued completion carriers, it also checks the parent session's evidence. The carriers are injected verbatim; if the session has no passing evidence, a mandatory-verification message is appended after them. The check is unconditional — no configuration disables it — and it is fail-open only when the evidence lookup itself is unavailable.

A parsed negative verdict always blocks: fail-open covers model errors, unparseable output, and unavailable lookups, never a real `RETRY`, `BLOCK`, or failing evidence row. The gate-internals (breaker thresholds, announce retries, the evidence ledger) are detailed in the [Loop Prevention README](../loop-prevention/README.md) and the [Long-Running Tasks README](../long-running-tasks/README.md).

## 🔗 Relationships to Other Subsystems

| Subsystem | What crosses this boundary | Details |
|-----------|---------------------------|---------|
| TaskFlow engine | step dispatch, `validation_criteria`, `aggregate_deps`, finish gates A–D | [Long-Running Tasks](../long-running-tasks/README.md) |
| Loop prevention | completion gates, judge budgets, drain and announce retry behavior | [Loop Prevention](../loop-prevention/README.md) |
| Context engine | only the completion carrier is persisted to MesMemory; child transcripts stay checkpoint-only | [Context Engine README](../../context_engine/README.md) |
| Runtime lanes | the `SUBAGENT` lane bounds concurrent child runs; over-limit spawns queue as `PENDING` | [Loop Prevention](../loop-prevention/README.md) |
| Memory files | a non-empty drain reconciles `MEMORY.md` and `USER.md` (load → persist, fail-open) | [Subagent System README](../../agent/tools/subagent/README.md) |

### 🧵 Synthesis Aggregation

`taskflow_run_task(aggregate_deps=True)` marks a synthesis step so its dispatched task text carries its dependencies' recorded results. The flag is stored on the step, and `build_task_with_dep_results(step, steps, results)` appends a `## Upstream Results` block with one `### {step_id}` section per dependency in `depends_on` order, matching each dependency's `child_session_key` against the flow's `{child_session_key, result, result_hash}` record; a dependency with no recorded result contributes a `no result recorded` placeholder. Because the aggregation is re-derived on every dispatch or retry from stable records, the stored step task is never rewritten; without the flag the task text is unchanged. The full behavior is documented in the [Long-Running Tasks README](../long-running-tasks/README.md).

## ⚙️ Configuration

| Knob | Location | Default | Effect |
|------|----------|---------|--------|
| `goal_max_turns` | `config/features/agent_side/completion_judge.py` | `5` | goal-loop turn budget, including the first turn |
| per-spawn `goal_max_turns` | `sessions_spawn` parameter | `None` → configured value | overrides the budget for one spawn |
| `max_retries` | `config/features/agent_side/step_judge.py` | `2` | `RETRY` verdicts before the step is blocked |
| `default_functional_role` | `SubagentConfig` | `"general"` | fallback when neither a hint nor an `agent_id` match resolves |
| `roles_override_dir_name` | `SubagentConfig` | `"subagent_roles"` | workspace directory holding optional role overrides |
| `max_spawn_depth` | `SubagentConfig` | `2` | depth at which the role flips to `LEAF` (hard cap `2`) |
| `verify_commands` | `config/features/agent_side/evidence_ledger.py` | test / lint / build / typecheck / format families | commands auto-recorded as verification evidence |

## 🗺️ Test Map

| Area | Tests |
|------|-------|
| Role enum and loader | `tests/agent/tools/subagent/types/test_functional_role.py`, `tests/agent/tools/subagent/roles/test_loader.py` |
| Role-driven spawn behavior | `tests/agent/tools/subagent/spawn/test_functional_role_integration.py`, `tests/agent/tools/subagent/spawn/test_system_prompt_role.py` |
| Spawn privilege guard | `tests/agent/tools/subagent/test_spawn_privilege_guard.py` |
| Completion judge and drain | `tests/agent/tools/subagent/test_completion_judge.py`, `tests/agent/tools/subagent/test_completion_drain.py` |
| Parent-turn gate | `tests/agent/middlewares/test_completion_drain_gate.py` |
| Step judge | `tests/agent/tools/taskflow/test_step_judge.py`, `tests/agent/tools/taskflow/test_resume_with_judge.py` |
| Finish gates | `tests/agent/tools/taskflow/test_finish_gate.py` |
| Synthesis aggregation | `tests/agent/tools/taskflow/test_synthesize.py`, `tests/agent/tools/taskflow/test_synthesize_e2e.py` |

## ⚠️ Limitations

- Every judge is fail-open by design: an unreachable or unparseable judge finalizes `done` / `pass`. Availability wins over strictness — a wedged judge must never trap a run.
- `goal_max_turns` of `1` makes the child single-turn; the completion judge is not called for that spawn.
- The call-time privilege guard backs `sessions_spawn`; `sessions_yield` relies on assembly-time stripping alone.
- Gate D activates only with both `todo` and `plan_path`; without that linkage `taskflow_finish` has no independent completion verdict.
- The parent-turn gate reasons over a text evidence summary and its `FAIL` / `[stale]` markers, not over a structured ledger.
- `aggregate_deps` concatenates raw dependency results — it does not summarize, so very large results grow the dispatched task text.
- Child context isolation is fixed by design: there is no mode that inherits the parent transcript.
