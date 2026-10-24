# 📋 Evidence Ledger — Verification Audit Trail

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> A SQLite audit trail that records every verification command the agent runs (tests, linters, builds) and derives whether each result is still fresh at read time — so completion gates can reject a subagent or taskflow that claims "done" without passing, non-stale verification.

The evidence ledger answers one question at every completion boundary: **"Did the agent verify its work, and is that verification still valid?"** It does this without storing a staleness flag — events are immutable rows, and freshness is computed on the read side from the ordering of verification rows and file-edit events.

**Source of truth:** `agent/tools/pub_base/evidence_ledger.py` (storage + derivation), `agent/tools/pub_base/evidence_recorder.py` (producers), `agent/tools/pub_base/regression_gate.py` (finish-time replay), `agent/tools/taskflow/evidence_collector.py` (read/consumer), `config/features/agent_side/evidence_ledger.py` (taxonomy), `config/features/agent_side/evidence_cross_session.py` (cross-session + regression-gate switches).

---

## How It Works

```
terminal / python_repl runs a command
  │
  ├─ classify: matches "pytest" → kind=test, "ruff" → kind=lint, etc.
  │            (taxonomy in EVIDENCE_LEDGER["verify_commands"])
  │            unmatched commands are silently ignored
  │
  ├─ record_verification_evidence()
  │    └─ insert one SQLite row: {kind, command, exit_code, status, output_snippet, sandboxed, modified_files, session_id, timestamp}
  │
  └─ later: write_file / patch_file edits a source file
       └─ mark_evidence_stale(file_path, session_id) + record_file_edit()
            └─ insert one SQLite row: {event:"stale", file_path, session_id, timestamp}
                 │
                 └─ at read time: collect_evidence_summary()
                      ├─ an evidence row is [stale] if a LATER stale event's file_path
                      │   is a substring of the row's command
                      ├─ direction matters: stale BEFORE evidence = fresh (re-verified)
                      └─ returns: "  [pass] test: `pytest foo.py`\n  [FAIL] lint: `ruff .` [stale]"
```

**Changed-path snapshots (B').** `write_file` / `patch_file` also call `EvidenceLedger.record_file_edit()`, accumulating the session's edited paths (deduped, capped at `EVIDENCE_LEDGER["changed_paths_limit"]` = 200; the list is NOT reset per verification, so a lint run and a test run after the same edits share one snapshot). Recorded paths are normalized to the session's project root: `./a.py`, `a/../a.py` and an in-root absolute path all become `a.py`; an out-of-root absolute path stays absolute. When a verification command is recorded, the current list is copied into the evidence row's `modified_files`. A later write to any of those files invalidates the evidence even if the command never names the file.

**Cross-session propagation.** A file write marks the writer's own session first. When `EVIDENCE_CROSS_SESSION_STALENESS["enabled"]` is true, `EvidenceLedger.mark_stale_for_path_all_sessions()` scans the OTHER sessions' evidence rows for the edited path — in a new row's `modified_files`, as a true path token named by the command, or in a legacy row's command — and appends one `scope="cross_session"` stale row under EACH affected session. `source_session` records the writer; `max_sessions_scan` caps the scan and `exclude_self` skips the already-marked writer. The read-side derivation below needs no change because the broadcast row lives in the affected session's own sequence.

**Finish-time replay.** At `taskflow_flow(action="finish")`, Gate C first calls `regression_gate.reverify_flow_evidence()`: every unique passed command in the flow's sessions is replayed once (allowlisted executables, no shell, `max_reverify_commands` cap, per-command timeout) in the flow's project root. A pass appends an `unmark` event for a command whose evidence was stale/broken; a failure appends a `broken` event. This is the only path that catches a cross-file import break, because it re-runs A's test command even when no stale row names the changed source file. A step may declare `file_deps`; when another step's write touches one, `prioritized_sessions()` moves that step's commands to the front before the `max_reverify_commands` cap (`file_deps_mode="prioritize"`, the default). The opt-in `file_deps_mode="filter"` replays ONLY declared sessions — cheaper, but it can miss an undeclared step's break.

---

## Storage

**File:** `src/data/evidence-ledger.db` (resolved by `config.path.resolve_evidence_ledger_path`)

| Property | Value |
|----------|-------|
| Format | SQLite table `evidence_events`; WAL mode + `busy_timeout=5000` |
| Scope | Repo-wide (shared across all sessions) |
| Mutability | Rows are INSERTed and never UPDATEd; retention deletes old rows (age TTL / per-session cap / global cap) |
| Session isolation | `WHERE session_id = ?` on the indexed column; rows carry a SESSION key (the main session's id, or a child's `agent:{agent}:subagent:{uuid}`) — never a plan / todo / flow id |
| File-edit memory | `verification_state(session_id, changed_paths_json, last_edit_at)` in the same DB; capped per session, deleted with the session/orphan sweep |
| Multi-key reads | `read_for_keys(keys)` runs one indexed `IN` query and preserves each key's insertion order (staleness is derived within one session's sequence) |
| Concurrency | WAL lets readers and writers overlap; a writer retries through `busy_timeout` |
| Legacy JSONL | Imported once on first open (per-line hash dedupe) and renamed to `.migrated` |
| Missing file | `read_all()` returns `[]`; the directory is created on first access |

The DB deliberately stays repo-scoped, not session-scoped: it is the shared audit trail across sessions that verify the same plan.

### Retention & purge

`EVIDENCE_LEDGER` carries the policy: `pruning_enabled` (default true), `max_age_days` (30), `max_events_per_session` (100) and `max_total_events` (10 000). Pruning is lazy — every `append` runs three DELETE layers after the insert, and a pruning failure never loses the row that was just recorded. Deleting a session calls `purge_session` (one `DELETE WHERE session_id = ?` for evidence and one for `verification_state`); the boot orphan sweep calls `purge_orphans(known_sessions)` so evidence and changed-path state for sessions that no longer exist are removed even when they were deleted outside the normal cleanup path. `changed_paths_limit` (200) bounds the per-session file list. `purge_on_session_delete` and `purge_on_boot_sweep` gate those explicit deletes; `pruning_enabled=false` disables only the lazy retention.

---

## Producers (What Gets Recorded)

### 1. Terminal & Python REPL — auto-recording (always on)

Every `terminal` and `python_repl` call passes through `record_verification_evidence()` on **every** exit path (success, non-zero exit, timeout, cancellation, exception). The recorder:

1. Classifies the command via `classify_verification_command()` — a lowercase substring match against `EVIDENCE_LEDGER["verify_commands"]`:
   ```python
   "verify_commands": {
       "test": ["pytest", "jest", "vitest", "cargo test", "go test", "npm test"],
       "lint": ["ruff", "eslint", "flake8", "pylint", "clippy"],
       "build": ["cargo build", "npm run build", "make", "cmake"],
       "typecheck": ["basedpyright", "mypy", "tsc", "pyright"],
       "format": ["ruff format", "prettier", "black"],
   }
   ```
   Commands that don't match any pattern are silently ignored (no row written).

2. Derives `exit_code` from the result string (regex on "Exit code N" / failure prefixes).
3. Sets `status` = `"passed"` if exit_code is 0, else `"failed"`.
4. Appends a row with `output_snippet` (first 500 chars) and `sandboxed` flag.

**Fail-open:** every recorder swallows its own errors — a ledger write never breaks the tool.

### 2. File edits — stale events

`write_file` and `patch_file` are decorated with `@invalidates_evidence(path_arg="file_path", session_arg="session_id")`. The decorator runs in a `finally` block, so **every** outcome (success, error string, raised exception) appends a `{"event": "stale", "file_path": "..."}` row to the ledger and calls `EvidenceLedger.record_file_edit()`. When the cross-session switch is on, the same call also appends a stale row to every OTHER session whose evidence covers the path (`modified_files` hit, or a legacy command substring).

### 3. Subagent workspace merge — stale events

When a child's isolated workspace merges back into the parent session, `workspace_merge._mark_merged_paths_stale()` calls `mark_evidence_stale_for_paths()` for every applied/created/deleted path against the **parent** session id. The parent's prior verification of those files is now invalid.

---

## Row Schemas

### Verification evidence row

```json
{
  "kind": "test",
  "command": "pytest tests/test_foo.py",
  "exit_code": 0,
  "status": "passed",
  "output_snippet": "===== 3 passed in 0.42s =====",
  "sandboxed": true,
  "modified_files": ["src/alpha.py"],
  "session_id": "agent:main:session:abc-123",
  "timestamp": "2026-10-15T08:30:00.123456+00:00"
}
```

### Stale event row

```json
{
  "event": "stale",
  "file_path": "src/foo.py",
  "session_id": "agent:main:session:abc-123",
  "timestamp": "2026-10-15T08:31:00.654321+00:00"
}
```

A cross-session row carries two extra optional fields: `"scope": "cross_session"` and `"source_session": "<writer session id>"`. A row without them is the original per-session mark.

`broken` and `unmark` use the same append-only shape: `broken` carries `command`, `exit_code`, `output_snippet` and `scope="regression_gate"`; `unmark` carries `command` and `scope`. Neither row rewrites earlier history.

---

## Staleness Derivation

Staleness is **derived on the read side**, never stored as a flag. The rule:

> An evidence row is stale iff a **later** stale event (higher index in the append order) names a `file_path` that is in the row's `modified_files` set, or that the command names as a path token. A legacy row without `modified_files` falls back to the original command-substring rule.

```python
def derive_evidence_state(records, index):
    row = records[index]
    command = str(row.get("command") or "")
    modified = row.get("modified_files") or []
    broken = stale = False
    for later in records[index + 1:]:
        event = later.get("event")
        if event == "unmark" and later.get("command") == command:
            broken = stale = False
        elif event == "broken" and later.get("command") == command:
            broken = True
        elif event == "stale":
            path = str(later.get("file_path") or "")
            if modified:
                stale = stale or (path in modified) or _command_references_path(command, path)
            else:
                stale = stale or (path in command)
    return broken, stale
```

Key subtleties:

- **Direction matters:** only a *later* stale event invalidates an earlier evidence row. A stale event before the evidence means the file was edited, then re-verified — the verification is fresh.
- **Exact set for new rows:** `file_path in modified_files`, or a true path token in the command. This catches a source file the command never names (`pytest tests/test_beta.py` + a write to `alpha.py`) and a test file A did not author, without the old substring false positive (`feature.py` vs `test_feature.py`).
- **Legacy fallback:** `file_path in command` for rows recorded before B'; intentionally loose and unchanged.
- **Path normalization:** both the accumulated path and the stale event are normalized against the writer's session root (`./`, `..`, in-root absolute → project-relative; outside-root absolute stays absolute). This is string normalization, not symlink resolution; different session roots are not unified.
- **Session-grouped:** `SessionEvidenceLedger.list_records()` filters by `session_id`, and the derivation compares only within one session's sequence. A cross-session invalidation is therefore written INTO the affected session's group (one row per session), never left in the writer's group.
- **Broken and unmark:** a later `broken` event with the same `command` renders `[broken]` (a re-verified command now fails); a later `unmark` for that command clears both `[stale]` and `[broken]`. `derive_evidence_state()` in `evidence_ledger.py` is the single implementation; `[broken]` outranks `[stale]`.

---

## Evidence Summary Format

`collect_evidence_summary(session_key=None, flow_id=None)` returns a multi-line string (or `None` when no evidence rows exist):

```
  [pass] test: `pytest tests/test_foo.py`
  [FAIL] lint: `ruff check .`
  [pass] build: `npm run build` [stale]
  [?] test: `pytest bar.py`
```

Per-line format: `  [{icon}] {kind}: \`{command}\`{stale_tag}`

| Component | Source |
|-----------|--------|
| Icon | `"pass"` if status == passed, `"FAIL"` if failed, `"?"` if unknown |
| Kind | Verification category: test / lint / build / typecheck / format |
| Command | The exact command string, wrapped in backticks |
| Status tag | `" [broken]"` when broken, else `" [stale]"` when stale |

This string is what the gates and judges parse — they search for the literal substrings `"FAIL"`, `"[broken]"`, `"[stale]"`, and `"[pass]"`.

---

## Consumers — Three Gating Levels

### Level 1: Per-step (TaskFlow step judge)

When `taskflow_step(action="resume")` dispatches a step with acceptance criteria, it calls `judge_step_result()` with `evidence_summary=collect_evidence_summary(session_key=step.child_session_key)`. The step judge's prompt includes a `## Verification Evidence` section containing the summary (or `"(none recorded)"`). The judge uses this to decide PASS / RETRY / BLOCK.

### Level 2: Per-subagent (completion judge + drain gate)

- **Completion judge skip:** On the first goal-loop turn, `_first_turn_self_evident(result_text, evidence_summary)` checks: non-empty result + at least one `[pass]` + no `FAIL` + no `[stale]` + no `[broken]` → the judge call is **skipped entirely**, saving an auxiliary LLM call.
- **Completion judge prompt:** If not skipped, `judge_completion()` receives the evidence summary in its prompt.
- **Drain gate:** `SubagentCompletionDrainMiddleware` (parent-side `before_model`) checks evidence when draining subagent-completion messages. No evidence or `FAIL` → injects a `[GATE] Completion blocked: verification evidence missing or failing` blocking message.

### Level 3: Per-flow (taskflow_flow(action="finish") Gate C)

Before a taskflow can transition to DONE, Gate C checks the flow's AGGREGATED evidence — the union of every dispatched step's `child_session_key` plus the flow's `creator_session_key`, because rows are keyed by session and a flow spans sessions (a flow that never dispatched a step has no keys, hence no evidence, and passes):

| Condition | Result |
|-----------|--------|
| Summary contains `"FAIL"` | Rejected: "failing verification evidence" |
| Summary contains `"[broken]"` | Rejected: "confirmed regression (a re-verified command now fails)" |
| Summary contains `"[stale]"` | Rejected: "stale evidence (code changed since last verification), re-run verification commands before finishing" |
| Otherwise (or no evidence / collector error) | Passed |

Gate order: A (DAG completeness) → B (no unresolved steps) → C (evidence) → E (goal judge).

---

## Design Principles

1. **Fail-open everywhere.** Every recorder swallows its own errors. Every gate returns "pass / not-violated" on collector failure. A broken ledger can never block a finish or trap a subagent.
2. **Immutable event rows + read-time derivation.** A recorded row is never UPDATEd. Staleness is computed at read time from the ordering of rows. Retention may DELETE old rows (age/session/global caps), but it never rewrites history; readers always compute freshness from the surviving sequence.
3. **Path references, not content.** The ledger records command strings and output snippets (500 chars), not file contents. It records *what was verified* and *whether it passed*, not *what the file contained*.
4. **Auto-recording is always on; the two derived layers are switchable.** Every terminal/python_repl call that matches the taxonomy gets a row and every file edit marks the writer's own session, with no on/off switch. Cross-session broadcast (`EVIDENCE_CROSS_SESSION_STALENESS["enabled"]`) and finish-time replay (`EVIDENCE_REGRESSION_GATE["enabled"]`) are the gated layers, both default on; disabling them restores the previous behaviour without changing the ledger schema.
