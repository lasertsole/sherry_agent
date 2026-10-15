# 📋 Evidence Ledger — Verification Audit Trail

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> An append-only JSONL audit trail that records every verification command the agent runs (tests, linters, builds) and derives whether each result is still fresh at read time — so completion gates can reject a subagent or taskflow that claims "done" without passing, non-stale verification.

The evidence ledger answers one question at every completion boundary: **"Did the agent verify its work, and is that verification still valid?"** It does this without storing a staleness flag — the trail is append-only, and freshness is computed on the read side from the ordering of verification rows and file-edit events.

**Source of truth:** `agent/tools/todolist/evidence_ledger.py` (storage), `agent/tools/todolist/evidence_recorder.py` (producers), `agent/tools/taskflow/evidence_collector.py` (read/consumer), `config/features/agent_side/evidence_ledger.py` (taxonomy).

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
  │    └─ append one JSONL row: {kind, command, exit_code, status, output_snippet, sandboxed, session_id, timestamp}
  │
  └─ later: write_file / patch_file edits a source file
       └─ mark_evidence_stale(file_path, session_id)
            └─ append one JSONL row: {event:"stale", file_path, session_id, timestamp}
                 │
                 └─ at read time: collect_evidence_summary()
                      ├─ an evidence row is [stale] if a LATER stale event's file_path
                      │   is a substring of the row's command
                      ├─ direction matters: stale BEFORE evidence = fresh (re-verified)
                      └─ returns: "  [pass] test: `pytest foo.py`\n  [FAIL] lint: `ruff .` [stale]"
```

---

## Storage

**File:** `src/data/evidence-ledger.jsonl` (resolved by `config.path.resolve_evidence_ledger_path`)

| Property | Value |
|----------|-------|
| Format | JSON Lines (one JSON object per line) |
| Scope | Repo-wide (shared across all sessions) |
| Mutability | Append-only — existing lines are never rewritten or deleted |
| Session isolation | Achieved by filtering on the `session_id` field at read time; rows carry a SESSION key (the main session's id, or a child's `agent:{agent}:subagent:{uuid}`) — never a plan / todo / flow id |
| Multi-key reads | `read_for_keys(keys)` groups one pass over the file by key, preserving each key's own order (staleness is derived within one session's sequence) |
| Broken lines | A line that does not parse is skipped with one warning — a single torn line must never make the whole ledger unreadable |
| Write lock | Appends take the shared per-path write lock (`file_write_lock`: in-process lock + `flock`) |
| Missing file | `read_all()` returns `[]` — the `data/` directory is created on first append |

The file deliberately stays repo-scoped, not session-scoped: it is the append-only audit trail shared across sessions that verify the same plan.

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

`write_file` and `patch_file` are decorated with `@invalidates_evidence(path_arg="file_path", session_arg="session_id")`. The decorator runs in a `finally` block, so **every** outcome (success, error string, raised exception) appends a `{"event": "stale", "file_path": "..."}` row to the ledger.

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

---

## Staleness Derivation

Staleness is **derived on the read side**, never stored as a flag. The rule:

> An evidence row is stale iff a **later** stale event (higher index in the append order) has a `file_path` that appears as a **substring** in the row's `command` string.

```python
def _is_stale(index, row, stale_events):
    command = str(row.get("command") or "")
    return any(
        stale_index > index
        and str(event.get("file_path") or "") in command
        for stale_index, event in stale_events
    )
```

Key subtleties:

- **Direction matters:** only a *later* stale event invalidates an earlier evidence row. A stale event before the evidence means the file was edited, then re-verified — the verification is fresh.
- **Substring match:** `file_path in command`. Intentionally loose — a stale event for `src/foo.py` invalidates `pytest src/foo.py tests/`.
- **Session-scoped:** `SessionEvidenceLedger.list_records()` filters by `session_id` first, so stale events from other sessions don't cross-contaminate.

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
| Stale tag | `" [stale]"` appended when `_is_stale` returns True |

This string is what the gates and judges parse — they search for the literal substrings `"FAIL"`, `"[stale]"`, and `"[pass]"`.

---

## Consumers — Three Gating Levels

### Level 1: Per-step (TaskFlow step judge)

When `taskflow_resume` dispatches a step with acceptance criteria, it calls `judge_step_result()` with `evidence_summary=collect_evidence_summary(session_key=step.child_session_key)`. The step judge's prompt includes a `## Verification Evidence` section containing the summary (or `"(none recorded)"`). The judge uses this to decide PASS / RETRY / BLOCK.

### Level 2: Per-subagent (completion judge + drain gate)

- **Completion judge skip:** On the first goal-loop turn, `_first_turn_self_evident(result_text, evidence_summary)` checks: non-empty result + at least one `[pass]` + no `FAIL` + no `[stale]` → the judge call is **skipped entirely**, saving an auxiliary LLM call.
- **Completion judge prompt:** If not skipped, `judge_completion()` receives the evidence summary in its prompt.
- **Drain gate:** `SubagentCompletionDrainMiddleware` (parent-side `before_model`) checks evidence when draining subagent-completion messages. No evidence or `FAIL` → injects a `[GATE] Completion blocked: verification evidence missing or failing` blocking message.

### Level 3: Per-flow (taskflow_finish Gate C)

Before a taskflow can transition to DONE, Gate C checks the flow's AGGREGATED evidence — the union of every dispatched step's `child_session_key` plus the flow's `creator_session_key`, because rows are keyed by session and a flow spans sessions (a flow that never dispatched a step has no keys, hence no evidence, and passes):

| Condition | Result |
|-----------|--------|
| Summary contains `"FAIL"` | Rejected: "failing verification evidence" |
| Summary contains `"[stale]"` | Rejected: "stale evidence (code changed since last verification), re-run verification commands before finishing" |
| Otherwise (or no evidence / collector error) | Passed |

Gate order: A (DAG completeness) → B (no unresolved steps) → C (evidence) → E (goal judge).

---

## Design Principles

1. **Fail-open everywhere.** Every recorder swallows its own errors. Every gate returns "pass / not-violated" on collector failure. A broken ledger can never block a finish or trap a subagent.
2. **Append-only + read-time derivation.** No historical line is ever rewritten. Staleness is computed at read time from the ordering of rows. The audit trail is never corrupted by edits — it just grows, and readers compute freshness.
3. **Path references, not content.** The ledger records command strings and output snippets (500 chars), not file contents. It records *what was verified* and *whether it passed*, not *what the file contained*.
4. **Always on, no toggle.** The auto-recording path has no on/off switch. Every terminal/python_repl call that matches the taxonomy gets a row. Every file edit gets a stale event. The system cannot be bypassed by the model.
