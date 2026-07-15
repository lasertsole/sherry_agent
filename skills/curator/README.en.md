# Curator — Background Skill Maintenance Orchestrator

**English** | [**中文文档**](README.md)

> **Curator** is the background skill maintenance system for the EMA AI Agent, responsible for lifecycle management, archiving, and consolidation of agent-created skills.

---

## Table of Contents

- [Overview](#overview)
- [Core Responsibilities](#core-responsibilities)
- [Architecture](#architecture)
- [Trigger Mechanism](#trigger-mechanism)
- [Lifecycle State Machine](#lifecycle-state-machine)
- [Execution Flow](#execution-flow)
- [Automatic Transition Rules](#automatic-transition-rules)
- [LLM Consolidation](#llm-consolidation)
- [Classification & Reconciliation](#classification--reconciliation)
- [Usage Record System](#usage-record-system)
- [Pin Mechanism](#pin-mechanism)
- [Report System](#report-system)
- [Configuration Reference](#configuration-reference)
- [Invariants](#invariants)
- [File Structure](#file-structure)

---

## Overview

Curator is an **inactivity-triggered** background task. When the Agent is idle and the last Curator run was more than `interval_hours` ago, `maybe_run_curator()` spawns a background review.

It only operates on agent-created skills (under `skills/auto/`), **never touching built-in skills** (`skills/builtin/`). The default behavior is to **archive rather than delete**, ensuring all operations are recoverable.

---

## Core Responsibilities

1. **Automatic Lifecycle Transitions** — advance `active → stale → archived` based on skill activity timestamps
2. **Consolidation** (optional LLM pass) — merge overlapping narrow skills into class-level umbrella skills
3. **Persistent State** — save run history in the `.curator_state` file

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  maybe_run_curator()                                            │
│    │                                                            │
│    ├── should_run_now()? ── No ──► return None                  │
│    │                                                            │
│    └── Yes ──► run_curator_review()                             │
│                  │                                              │
│                  ├── 1. Auto-transitions (apply_automatic_...)  │
│                  │     ├── Iterate agent_created_report()       │
│                  │     ├── Skip pinned / cron-referenced        │
│                  │     └── Mark stale/archived by cutoff times  │
│                  │                                              │
│                  ├── 2. LLM Consolidation (optional)            │
│                  │     ├── _render_candidate_list()             │
│                  │     ├── _run_llm_review(prompt)              │
│                  │     └── Parse structured YAML output         │
│                  │                                              │
│                  └── 3. Report & Persist                        │
│                        ├── _write_run_report() → logs/curator/  │
│                        └── save_state() → .curator_state        │
└─────────────────────────────────────────────────────────────────┘
```

---

## Trigger Mechanism

Curator uses an **inactivity-triggered** pattern rather than a scheduled cron:

```
maybe_run_curator(idle_for_seconds=...)
  │
  ├── should_run_now() checks:
  │     ├── is_enabled() == False  → skip
  │     ├── is_paused() == True    → skip
  │     ├── First run ever         → seed last_run_at, return False (defer one interval)
  │     └── now - last_run_at >= interval_hours → eligible
  │
  └── idle_for_seconds < min_idle_hours * 3600 → skip
```

| Parameter | Default | Description |
|-----------|---------|-------------|
| `interval_hours` | 168 (7 days) | Minimum interval between Curator runs |
| `min_idle_hours` | 2 | Agent must be idle for at least N hours |

On the first call, Curator only seeds the `last_run_at` timestamp and **does not execute**, ensuring a review doesn't run immediately after Agent startup.

---

## Lifecycle State Machine

```
    active ──────(stale_after_days no activity)──────► stale
      ▲                                                 │
      │             (new activity / reactivation)        │
      └─────────────────────────────────────────────────┘
      │                                                 │
      │         (archive_after_days no activity)         │
      └──────────────────► archived ◄────────────────────┘
                               │
                               │ (restore_skill)
                               ▼
                            active
```

| State | Meaning |
|-------|---------|
| `active` | Skill is normally available |
| `stale` | No activity for `stale_after_days`, marked as stale |
| `archived` | No activity for `archive_after_days`, moved to `.archive/` |

**Key constraints**:
- Pinned skills are **never** auto-transitioned
- Cron-referenced skills are **never** auto-transitioned
- Archiving is recoverable (`restore_skill()`); deletion is irreversible

---

## Execution Flow

### run_curator_review()

```
run_curator_review(synchronous=True, dry_run=False, consolidate=None)
  │
  ├── 1. Auto-transition phase
  │     ├── dry_run=True → count only, no mutations
  │     └── dry_run=False → apply_automatic_transitions()
  │           ├── Mark stale
  │           ├── Archive (move to .archive/)
  │           └── Reactivate
  │
  ├── 2. Save intermediate state
  │     └── last_run_at, run_count, last_run_summary
  │
  ├── 3. LLM consolidation (_llm_pass)
  │     ├── consolidate=False → skip, write report
  │     └── consolidate=True:
  │           ├── Snapshot before_report (skill list)
  │           ├── _render_candidate_list() → candidate list
  │           ├── _run_llm_review(prompt) → LLM invocation
  │           ├── Snapshot after_report
  │           ├── _build_rename_summary() → classify changes
  │           └── _write_run_report() → logs/curator/{timestamp}/
  │
  ├── 4. Execution mode
  │     ├── synchronous=True → run on current thread
  │     └── synchronous=False → run on new daemon thread
  │
  └── 5. Return
        └── { started_at, auto_transitions, summary_so_far }
```

### _run_llm_review()

```
_run_llm_review(prompt)
  │
  ├── Build LLM (build_main_llm, temperature=0.3)
  ├── Assemble messages (system prompt + user prompt)
  ├── llm.invoke(messages)
  │
  └── Return { final, summary, model, provider, tool_calls, error }
```

The LLM may invoke `skill_manage` tools to create/modify/delete skills. These tool_calls are recorded and used for classification reconciliation.

---

## Automatic Transition Rules

`apply_automatic_transitions()` evaluates each agent-created skill:

```
For each agent-created skill:
  │
  ├── pinned? → skip
  ├── cron-referenced? → skip
  ├── no usage record? → seed_record_if_missing(), skip
  │
  ├── never used (use_count==0) and created < stale_cutoff?
  │     └── if currently stale → reactivate to active
  │
  ├── last_activity <= archive_cutoff and not archived?
  │     └── _remove_skill() → archive or delete
  │
  ├── last_activity <= stale_cutoff and currently active?
  │     └── mark as stale
  │
  └── last_activity > stale_cutoff and currently stale?
        └── reactivate to active
```

Time cutoffs:
- `stale_cutoff = now - stale_after_days` (default 30 days)
- `archive_cutoff = now - archive_after_days` (default 90 days)

---

## LLM Consolidation

The LLM pass receives `CURATOR_REVIEW_PROMPT`, instructing it to merge narrow skills into class-level umbrella skills:

**Consolidation strategies**:
- **a. Merge into existing umbrella** — add labeled sections, archive siblings
- **b. Create new umbrella** — write class-level skill, archive siblings
- **c. Demote to references** — move narrow content into umbrella's support directories, archive old skill

**LLM output format** (YAML structured summary):
```yaml
consolidations:
  - from: old-skill-name
    into: umbrella-skill-name
    reason: why merged
prunings:
  - name: skill-name
    reason: why archived
```

**Dry-run mode**: The LLM only outputs "actions it would take" without actually modifying the skill library.

---

## Classification & Reconciliation

After the LLM pass executes, some skills may have been removed. `classify.py` determines whether each removed skill was **consolidated** (merged into an umbrella) or **pruned** (simply archived):

### Three-source Reconciliation

```
_reconcile_classification(removed, heuristic, model_block, destinations, absorbed_declarations)
  │
  ├── For each removed skill:
  │
  │   1. absorbed_into declaration (attached at LLM delete time)
  │      ├── target exists in destinations → consolidated
  │      └── declaration is empty → pruned
  │
  │   2. Model structured block (consolidations in YAML output)
  │      ├── target exists → consolidated
  │      └── target missing → fall back to heuristic or mark as pruned
  │
  │   3. Heuristic audit (old skill name referenced in tool_call content)
  │      ├── evidence found → consolidated
  │      └── no evidence → pruned
  │
  │   4. No evidence at all → mark as pruned (no-evidence fallback)
  │
  └── Output: { consolidated: [...], pruned: [...] }
```

**Heuristic audit** (`_classify_removed_skills`) inspects the LLM's `skill_manage` tool_calls:
- Iterates tool_call arguments (file_path, content, new_string, etc.)
- Searches for references to the removed skill name
- If found → evidence that the skill was consolidated into the target umbrella

---

## Usage Record System

Each agent-created skill has a corresponding JSON record file under `skills/auto/.usage/`:

```json
{
  "name": "my-skill",
  "state": "active",
  "pinned": false,
  "use_count": 3,
  "view_count": 5,
  "patch_count": 1,
  "activity_count": 9,
  "created_at": "2026-07-15T10:00:00+00:00",
  "last_activity_at": "2026-07-15T12:30:00+00:00",
  "_persisted": true
}
```

| Field | Description |
|-------|-------------|
| `use_count` | Number of times the skill was invoked |
| `view_count` | Number of times the skill was viewed |
| `patch_count` | Number of times the skill was modified |
| `activity_count` | Sum of all the above counts |
| `last_activity_at` | Timestamp of the last activity |
| `_persisted` | Whether the record has been persisted to disk |

`bump_usage(name, field)` is the external entry point, called each time a skill is used/viewed/modified. It auto-increments the count and updates `last_activity_at`.

---

## Pin Mechanism

Pinned skills enjoy the highest level of protection:

- **Dual determination**: `pinned=True` in usage record **OR** a `.pinned` marker file exists in the skill directory
- **Protection effect**: bypass all automatic transitions (stale/archived are never triggered)
- **Operations**: `pin_skill(name)` / `unpin_skill(name)`, updating both the record and marker file

---

## Report System

Each run generates a detailed report saved under `logs/curator/{timestamp}/`:

| File | Content |
|------|---------|
| `run.json` | Full structured data (transition counts, classification results, tool_calls, LLM output, etc.) |
| `REPORT.md` | Human-readable Markdown report |

**REPORT.md contains**:
- Run metadata (model, duration, skill count changes)
- Auto-transition statistics
- LLM consolidation statistics (consolidated / pruned)
- Specific consolidation and pruning lists
- Recovery instructions

**Recovery**:
```bash
curator restore <skill-name>   # Restore from .archive/
```

---

## Configuration Reference

Config file path: `config/curator.yaml`

| Setting | Default | Description |
|---------|---------|-------------|
| `enabled` | `true` | Whether Curator is enabled |
| `interval_hours` | `168` (7 days) | Run interval |
| `min_idle_hours` | `2` | Minimum idle time |
| `stale_after_days` | `30` | Days before marking as stale |
| `archive_after_days` | `90` | Days before archiving |
| `consolidate` | `false` | Whether to enable LLM consolidation |
| `prune_builtins` | `true` | Whether to prune usage records for built-in skills |

**Environment variables**:

| Variable | Default | Description |
|----------|---------|-------------|
| `CURATOR_PROCESS_USELESS_SKILL` | `archive` | How to handle useless skills: `archive` or `delete` |

---

## Invariants

Curator adheres to the following strict invariants that must never be violated:

1. **Only touch agent-created skills** (`skills/auto/`), never built-ins (`skills/builtin/`)
2. **Never auto-delete** — default is archive only; archiving is recoverable (unless `CURATOR_PROCESS_USELESS_SKILL=delete`)
3. **Pinned skills bypass all automatic transitions**
4. **Cron-referenced skills are never auto-transitioned**

---

## File Structure

```
curator/
├── __init__.py           # Public API exports
├── constants.py          # Constants (paths, state names, defaults)
├── config.py             # Config loading (curator.yaml + env vars)
├── state.py              # Curator run state persistence (.curator_state)
├── usage.py              # Skill usage record CRUD (.usage/{name}.json)
├── transitions.py        # Auto state transitions + should_run_now logic
├── orchestrator.py       # Main orchestrator (run_curator_review / maybe_run_curator)
├── classify.py           # Removed skill classification (consolidated vs pruned) + reconciliation
├── helpers.py            # Utilities (ISO parsing, atomic writes, cron reference reading)
└── report.py             # Run report generation (run.json + REPORT.md)
```

**Runtime files**:
```
skills/
├── .curator_state              # Curator run state
├── .archive/                   # Archived skill directory
└── auto/
    └── .usage/
        └── {skill-name}.json   # Skill usage record

logs/curator/
└── {timestamp}/
    ├── run.json                # Structured run data
    └── REPORT.md               # Human-readable report
```
