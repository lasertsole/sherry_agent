# 🛡️ High-Risk Operation Guard

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> How an irreversible operation is recognized, approved once, refused on replay, and bubbled from a subagent to the human at the main agent.

Source of truth: `agent/tools/pub_base/high_risk_guard_store.py`, `agent/tools/pub_base/high_risk_policy.py`, `agent/tools/high_risk_guard.py`, `agent/middlewares/tool_guardrails/core.py`, `agent/middlewares/humanInTheLoop/strategies.py`, `config/features/agent_side/high_risk_guard.py`, `server/trigger/http/high_risk_guard.py`, `client/app/pages/home/components/HighRiskGuardPanel.vue`.

## 🎯 Threat Model

A verification pass, a retried tool call or a re-spawned TaskFlow step can run the same **irreversible** operation twice: a production deploy, a payment charge, a force-push. Re-running a source edit is normal work, so "non-idempotent" alone cannot be the trigger — the guard keys on **high risk**.

| Layer | Answers | Mechanism |
| :---- | :------ | :-------- |
| L1 metadata | Which tool can cause an irreversible effect? | `metadata.high_risk` (MCP defaults to true) |
| L2 keyword registry | Which operations must a human see? | built-in + session/global keywords, substring scan |
| L3 executed-operation ledger | Has this exact call already run? | `(root session, tool, args_hash)` row with the first result |
| L4 approval | Who approves the first run? | main-agent HITL card; subagent `[NEEDS_APPROVAL]` bubble |

The layers are independent: the ledger refuses a replay even after the user approved the first run, and a keyword hit asks before the ledger has anything to say.

## ⚙️ Keyword Registry

One SQLite table (`src/data/high_risk_guard.db`, `high_risk_keywords`) holds built-in overrides and custom keywords. A keyword is matched **case-insensitively as a substring** (no word boundary: `password` must match `user_password=…`), and every keyword must be **at least 3 Unicode characters** — a two-character entry would match almost every Chinese sentence.

| Field | Meaning |
| :---- | :------ |
| `session_id` | a session id, `global`, or the built-in default |
| `keyword` | the scanned text |
| `policy` | `block` (scan/ask) or `allow` (never scan) |
| `source` | `builtin`, `user` (panel) or `agent` (tool) |
| `executed` | 1 = the operation already ran in this session |
| `result_summary` | the first result, shown when a replay is refused |

Resolution is session → global → built-int default. An `allow` row removes the keyword from the active list even when another scope says `block`. The built-in list covers credentials, financial identifiers, destructive shell operations and destructive SQL (`rm -rf`, `mkfs`, `git push --force`, `DROP TABLE`, `password`, `private_key`, …).

## 📒 Executed-Operation Ledger

Only tools that are **high risk AND not idempotent** are ledgered: PTC `execute_code` and every MCP tool by default. `terminal`, `python_repl`, `write_file` and `patch_file` are not ledgered — a shell command or an edit is ordinary work; the keyword scan is what makes a specific command ask.

A ledger row is keyed by the **root main session** (a subagent resolves `APPROVAL_SCOPE_PARENT`), the tool name and a stable argument digest (sorted JSON, never the raw arguments). On a repeat the tool call is refused before execution and the first `result_summary` is returned. Rows expire after the configured retention (24 h) and are removed when the session is deleted; the panel's delete is the explicit "run once more" action.

A keyword that has **executed** gets the same one-shot treatment: after an approved operation succeeds, its keyword is marked executed and a later call whose arguments contain it is refused with the first result until the panel (or `high_risk_guard action=reset_keyword`) clears the marker.

## 🔁 Main Agent vs Subagent

| | Main agent | Subagent |
| :--- | :--- | :--- |
| Keyword hit | `HighRiskKeywordApprovalHandler` interrupts with an approve/reject card | `ToolGuardrails` returns `[NEEDS_APPROVAL]`, the child finishes |
| Ledger hit | refused with the first result (no card) | same |
| After human approval | approval recorded in `KEYWORD_APPROVALS` for this session + args digest | parent resumes the step with `approval_context`; the new child receives it |
| YOLO | reversible tools skip the card; PTC/MCP still ask | a `full_access` child skips the keyword scan, the ledger still applies |
| confirm_all | the card asks as usual | the child refuses (`[BLOCKED: confirm_all mode]`) |

The HITL card freezes the session's heartbeat (`HEARTBEAT_SKIP`) before `interrupt()` and clears it when the resumed pass returns, so a slow operator never gets the turn killed for stalling.

### Bubbling path

1. the child's guardrails return `[NEEDS_APPROVAL: keyword=…, tool=…]`;
2. the child completes; the completion message builder appends a `[SYSTEM: …]` guide telling the parent to use `question` and how to resume;
3. the parent asks the user through `question` (the normal HITL card);
4. on approve the parent calls `taskflow_step(action="resume", step_outcome="approved", step_id=…, approval_context={keyword, tool})` — or `sessions_spawn(approval_context=…)` for a direct spawn;
5. the approval is written into the **new child's own** register (`KEYWORD_APPROVALS`), so only that spawn passes the keyword; the next unrelated spawn asks again;
6. on reject the step is resumed as `blocked`, its dependents stay blocked.

Only TaskFlow steps carry `approval_context` through `_OPTIONAL_STEP_FIELDS` → `step_spawn_kwargs` → `dispatch_child` → `SpawnRequest.approval_context`; an ordinary retry of an approved step keeps the approval (schema retries are the same operation), a fresh step does not inherit it.

## 🧰 Tool, API and Panel

`high_risk_guard` (main + subagents) manages the registry and the ledger: `add_keyword`, `list_keywords`, `remove_keyword`, `set_policy`, `reset_keyword`, `register_op`, `list_ops`, `remove_op`. A child's writes resolve to the root session.

| Endpoint | Purpose |
| :------- | :------ |
| `GET /sessions/:sid/high-risk-keywords` | keywords + ledger (one panel load) |
| `POST /sessions/:sid/high-risk-keywords` | add `{keyword, scope}` |
| `PUT /sessions/:sid/high-risk-keywords` | set `{keyword, policy}` |
| `DELETE /sessions/:sid/high-risk-keywords/:id` | delete a row |
| `POST /sessions/:sid/high-risk-keywords/reset` | clear the executed marker |
| `DELETE /sessions/:sid/high-risk-ops/:id` | delete a ledger row |
| `GET /high-risk-keywords/builtin` | read-only built-in list |

The shield button in the toolbar opens `HighRiskGuardPanel` for the current session: add/remove keywords, switch block↔allow, reset a marker, and delete ledger rows (the explicit re-run).

## 🧪 Testing

| Suite | Covers |
| :---- | :----- |
| `tests/agent/tools/pub_base/test_high_risk_guard_store.py` | validation, scanning, scopes, policy, executed markers, ledger round-trip, purge, retention |
| `tests/agent/middlewares/test_high_risk_guard.py` | ledger block/replay, keyword NEEDS_APPROVAL, approvals, YOLO, confirm_all, root scope |
| `tests/agent/middlewares/humanInTheLoop/test_high_risk_keyword_handler.py` | handler matching, executed refusal, approve/reject, heartbeat freeze, headless deny |
| `tests/agent/tools/test_high_risk_guard_tool.py` | the agent-facing tool's every action + child→root scope |
| `tests/agent/tools/taskflow/test_approval_resume.py` | approved re-dispatch, approval inheritance on retry, validation refusals |
| `tests/agent/tools/subagent/test_child_access_inheritance.py` | access-mode inheritance, approval scope to the token root, approval stamping |
| `tests/server/trigger/http/test_high_risk_guard_api.py` | keyword CRUD, policy, reset, ledger deletion |
| `client/app/pages/home/components/__tests__/HighRiskGuardPanel.integration.test.ts` | panel render, add/policy/reset/delete, backend refusal |

## ⚠️ Honesty & Limitations

- **The scan is a tripwire, not a parser.** A keyword inside an unrelated string (`echo "password"` in a doc-writing command) still asks; an operation phrased without any registered word passes. The registry is operator/agent-maintained.
- **A keyword's executed marker is session-wide.** The plan's "one approval asks again per step" is intentionally stricter here: the first approved operation marks the keyword, and every later call containing it is refused until the marker is reset. That is safe against replays but needs the panel for a legitimate second run.
- **Unannotated tools default to low risk.** Explicit `metadata.high_risk` wins; an `mcp__` tool without the key is high-risk, every other unannotated tool is not. A plugin that hides a dangerous operation behind a plain name must annotate itself.
- **MCP is ledgered by prefix and non-idempotent by default.** A genuinely safe MCP read can declare `high_risk: False` / `idempotent: True`.
- **The ledger stores an args preview (≤300 chars).** It stays in the local `high_risk_guard.db` (never the tracked workspace); a preview may still contain a value the call included.
- **The bubble depends on the parent taking a turn.** The completion message now carries the guide automatically, but the actual approval is still a `question` call the main agent must make; a parent that never reads its completion leaves the step blocked.
- **Retention is lazy.** Expired rows are swept at most once an hour on a store access; a row can outlive the 24 h window until the next sweep.
