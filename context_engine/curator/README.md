# Curator — Background Skill Maintenance Orchestrator

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> **Curator** is the background skill maintenance system for the EMA AI Agent, responsible for lifecycle management, consolidation, and pruning of agent-created skills.

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
- [Umbrella Skill Generation](#umbrella-skill-generation)
- [Classification & Reconciliation](#classification--reconciliation)
- [Usage Record System](#usage-record-system)
- [Orphan Record Cleanup](#orphan-record-cleanup)
- [Pin Mechanism](#pin-mechanism)
- [Report System](#report-system)
- [Configuration Reference](#configuration-reference)
- [Curator State File](#curator-state-file)
- [Invariants](#invariants)
- [File Structure](#file-structure)

---

## Overview

Curator is an **inactivity-triggered** background task. When the Agent is idle and the last Curator run was more than `interval_hours` ago, `maybe_run_curator()` spawns a background review.

It only operates on agent-created skills (under `skills/auto/`), **never touching built-in skills** (`skills/builtin/`). Stale and unused skills are **deleted** (removed from disk), with LLM consolidation optionally merging overlapping skills into umbrella skills before pruning.

---

## Core Responsibilities

1. **Automatic Lifecycle Transitions** — advance `active → stale` based on skill activity timestamps; delete skills that exceed the archive cutoff
2. **Consolidation** (optional LLM pass) — merge overlapping narrow skills into class-level umbrella skills with automated content generation and file migration
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
│                  │     ├── Skip pinned                          │
│                  │     └── Mark stale / delete by cutoff times  │
│                  │                                              │
│                  ├── 2. LLM Consolidation (optional)            │
│                  │     ├── _render_candidate_list()             │
│                  │     ├── _run_llm_review(prompt)              │
│                  │     ├── _apply_consolidation()               │
│                  │     │     ├── _generate_umbrella_skill()     │
│                  │     │     └── Migrate support files          │
│                  │     └── Parse structured YAML output         │
│                  │                                              │
│                  └── 3. Report & Persist                        │
│                        ├── _build_rename_summary()              │
│                        ├── _write_run_report() → logs/curator/  │
│                        └── save_state() → .curator_state        │
└─────────────────────────────────────────────────────────────────┘
```

---

## Trigger Mechanism

Curator uses an **inactivity-triggered** pattern rather than a scheduled cron:

```
maybe_run_curator(idle_for_seconds=..., on_summary=...)
  │
  ├── should_run_now() checks:
  │     ├── is_enabled() == False  → skip
  │     ├── is_paused() == True    → skip
  │     ├── last_run_at is None    → eligible (first run executes immediately)
  │     └── now - last_run_at >= interval_hours → eligible
  │
  └── idle_for_seconds < min_idle_hours * 3600 → skip
```

| Parameter | Default | Description |
|-----------|---------|-------------|
| `interval_hours` | 168 (7 days) | Minimum interval between Curator runs |
| `min_idle_hours` | 2 | Agent must be idle for at least N hours |

If `last_run_at` has never been set, the first call to `should_run_now()` returns `True` and the review proceeds immediately (no deferred-first-run seeding).

---

## Lifecycle State Machine

```
    active ──────(stale_after_days no activity)──────► stale
      ▲                                                 │
      │             (new activity / reactivation)        │
      └─────────────────────────────────────────────────┘
      │                                                 │
      │         (archive_after_days no activity)         │
      └──────────────────► deleted ◄─────────────────────┘
```

| State | Meaning |
|-------|---------|
| `active` | Skill is normally available |
| `stale` | No activity for `stale_after_days`, marked as stale |

When a skill exceeds `archive_after_days` of inactivity, it is **deleted** (directory and usage record removed from disk). There is no intermediate `archived` state — deletion is irreversible.

**Key constraints**:
- Pinned skills are **never** auto-transitioned or deleted
- Skills with `use_count == 0` created after the stale cutoff are **reactivated** if currently stale

---

## Execution Flow

### run_curator_review()

```
run_curator_review(on_summary=None, synchronous=True, dry_run=False, consolidate=None)
  │
  ├── 1. Auto-transition phase
  │     ├── dry_run=True → count only, no mutations
  │     └── dry_run=False → apply_automatic_transitions()
  │           ├── Mark stale
  │           ├── Delete (remove from disk)
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
  │           ├── _apply_consolidation(llm_final):
  │           │     ├── Parse structured YAML (consolidations + prunings)
  │           │     ├── For each umbrella: _generate_umbrella_skill()
  │           │     ├── Migrate support files (references/, templates/, scripts/, assets/)
  │           │     ├── Delete consolidated source skills
  │           │     └── Delete pruned skills
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
  ├── no persisted usage record? → seed_record_if_missing(), skip
  │
  ├── never used (use_count==0) and anchor > stale_cutoff?
  │     └── if currently stale → reactivate to active
  │
  ├── anchor <= archive_cutoff and not archived?
  │     └── _remove_skill() → delete from disk
  │
  ├── anchor <= stale_cutoff and currently active?
  │     └── mark as stale
  │
  └── anchor > stale_cutoff and currently stale?
        └── reactivate to active
```

Where `anchor` = `last_activity_at` (or `created_at` if never active, or `now` as fallback).

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

**Dry-run mode**: The LLM only outputs "actions it would take" without actually modifying the skill library. The `CURATOR_DRY_RUN_BANNER` prefix is added to the prompt.

---

## Umbrella Skill Generation

When consolidation produces new umbrella skills, `_apply_consolidation()` orchestrates the full merge:

### _generate_umbrella_skill()

Creates a consolidated umbrella skill for each new umbrella. It returns **both** the main
`SKILL.md` content and a mapping of optional **supporting files** the LLM split out, so the
main document stays concise:

```
_generate_umbrella_skill(umbrella, reasons, source_content, file_inventory)
  → (main_content: str, supporting_files: dict[path, content])
  │
  ├── Build LLM (build_main_llm, temperature=0.3)
  ├── System prompt: skill librarian creating umbrella skill
  │     - Output one or more file blocks, each starting with a '<<<PATH>>>' header
  │     - FIRST block MUST be '<<<SKILL.md>>>' (YAML frontmatter + markdown body)
  │     - Extra blocks permitted UNDER allowed subdirectories:
  │         references/, templates/, scripts/, assets/, examples/, resources/
  │     - Keep SKILL.md under ~15k characters (it is loaded on every skill use)
  │     - Offload bulky material into supporting blocks instead of inflating SKILL.md
  │       (long API docs → references/, runnable demos → examples/, helper logic → scripts/)
  │     - Reference each supporting file with a relative link + one-line description
  │     - frontmatter: name, description, created_by: curator
  │     - Synthesize & deduplicate overlapping instructions
  │
  ├── User prompt: umbrella name + merge reasons + source skill content + file inventory
  │
  ├── Parse response via _parse_multifile_umbrella():
  │     - Splits blocks on '<<<PATH>>>' headers deterministically
  │     - SKILL.md block → main content; other allowed-subdir blocks → supporting_files
  │     - Invalid paths / empty bodies are dropped with a warning
  │     - No valid headers → whole response treated as SKILL.md (historical fallback)
  │
  ├── On success → return (main_content, supporting_files)
  └── On failure/fallback → return (fallback skeleton SKILL.md, empty dict)
```

**Hard length enforcement** (shared with `skill_manage`):

After the LLM returns, the umbrella `SKILL.md` main content is passed through
`skill_manage.split_oversized_skill(main_content, _UMBRELLA_SKILL_CHAR_TARGET, supporting_files)`.
This is the same deterministic guard used when creating/editing any ordinary skill via
`skill_manage`. When the main content exceeds the 15,000-character target, the function
splits it along `## ` headings into `references/partNN.md` reference files and replaces each
section with a concise stub link — so `SKILL.md` always lands under budget with zero content
loss. Bodies with no `## ` headings, or already-under-budget content, are returned unchanged.

**File persistence** (`_apply_consolidation`):

1. `_create_skill(umbrella, main_content)` writes the main SKILL.md.
2. Each item in `supporting_files` is written via `_write_file(umbrella, path, content)`
   (path is validated against the allowed umbrella subdirectories).
3. Source subdirectory files are then migrated in (files already written by the LLM are
   skipped to avoid clobbering synthesized content).

### File Migration

After creating the umbrella skill, source-skill support files are migrated into the
umbrella's corresponding subdirectory:

```
For each consolidation entry (from → into umbrella):
  │
  ├── For each support subdirectory (references/, templates/, scripts/, assets/,
  │     examples/, resources/):
  │     └── Copy each file into umbrella's corresponding subdirectory
  │         (skip any path already written from supporting_files)
  │
  └── Delete source skill (delete_skill with absorbed_into=into)
```

### Pruning

Skills listed in the `prunings` block that are not already part of a consolidation are simply deleted.

---

## Classification & Reconciliation

After the LLM pass executes, some skills may have been removed. `classify.py` determines whether each removed skill was **consolidated** (merged into an umbrella) or **pruned** (simply deleted):

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
- Searches for references to the removed skill name (including `-`/`_` variants)
- Uses `_needle_in_path_component()` for path-aware matching on `file_path` fields
- Uses word-boundary regex for content fields
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
  "last_activity_at": "2026-07-15T12:30:00+00:00"
}
```

| Field | Description |
|-------|-------------|
| `use_count` | Number of times the skill was invoked |
| `view_count` | Number of times the skill was viewed |
| `patch_count` | Number of times the skill was modified |
| `activity_count` | Sum of all the above counts |
| `last_activity_at` | Timestamp of the last activity (null if never used) |
| `created_at` | Timestamp when the record was created |
| `_persisted` | Internal flag — `True` after `seed_record_if_missing()` writes the record |

`_default_record()` creates a new record with `use_count=0`, `activity_count=0`, and `last_activity_at=None`.

---

## Orphan Record Cleanup

`agent_created_report()` automatically calls `_cleanup_orphan_records()` to remove `.usage/` JSON files that have no corresponding skill directory. This keeps the usage store consistent with the actual skill directories on disk.

---

## Pin Mechanism

Pinned skills enjoy the highest level of protection:

- **Dual determination**: `pinned=True` in usage record **OR** a `.pinned` marker file exists in the skill directory
- **Protection effect**: bypass all automatic transitions (stale/deletion are never triggered); `_pinned_guard()` blocks any delete or state change
- **Guard behavior**: `set_state()`, `delete_skill()`, and `_remove_skill()` all check `_pinned_guard()` before proceeding — if pinned, the operation is rejected with a warning

Public API:

- `pin_skill(name) -> (bool, str)` — sets `pinned: True` in the usage record and persists it. The skill must already exist on disk under `skills/auto/`. Returns `(True, desc)` on success or `(False, error)` if the skill directory is missing.
- `unpin_skill(name) -> (bool, str)` — clears the `pinned` flag in the usage record (if present) and removes any `.pinned` marker file from the skill directory. Nested skills under `skills/auto/<category>/<skill>/` are resolved via `_skill_dir`, mirroring `is_pinned`.

Pinning can be managed through these public functions, through the HTTP API (`POST /skills/pin`), or externally by setting the `pinned` field in the usage record or creating a `.pinned` marker file.

---

## Report System

Each run generates a detailed report saved under `logs/curator/{timestamp}/`:

| File | Content |
|------|---------|
| `run.json` | Full structured data (transition counts, classification results, tool_calls, LLM output, etc.) |
| `REPORT.md` | Human-readable Markdown report |

**REPORT.md contains**:
- Run metadata (model, provider, duration, skill count changes)
- Auto-transition statistics
- LLM consolidation statistics (consolidated / pruned)
- Specific consolidation and pruning lists (up to 50 entries each)
- Tool call counts by name
- Auto summary text
- LLM final summary text
- Recovery notes

**Recovery**:
> **Note**: Since skills are deleted (not archived), recovery is only possible via version control or backup. There is no `restore_skill()` function in the current implementation.

---

## Configuration Reference

Config file path: `curator.yaml` (at project root, alongside `ROOT_DIR`)

| Setting | Default | Description |
|---------|---------|-------------|
| `enabled` | `true` | Whether Curator is enabled |
| `interval_hours` | `168` (7 days) | Run interval |
| `min_idle_hours` | `2` | Minimum idle time |
| `stale_after_days` | `30` | Days before marking as stale |
| `archive_after_days` | `90` | Days before deleting |
| `consolidate` | `false` | Whether to enable LLM consolidation |

Config is loaded via `_load_config()` which reads `curator.yaml` using PyYAML. Each getter function (`is_enabled`, `get_interval_hours`, etc.) falls back to the constant defaults on parse errors.

---

## Curator State File

Path: `skills/.curator_state`

```json
{
  "last_run_at": "2026-07-28T10:00:00+00:00",
  "last_run_duration_seconds": 12.34,
  "last_run_summary": "auto: 2 marked stale; llm: skipped",
  "last_run_summary_shown_at": null,
  "last_report_path": "/path/to/logs/curator/20260728-100000",
  "paused": false,
  "run_count": 5
}
```

| Field | Description |
|-------|-------------|
| `last_run_at` | ISO timestamp of the last run |
| `last_run_duration_seconds` | Duration of the last run in seconds |
| `last_run_summary` | Human-readable summary of the last run |
| `last_run_summary_shown_at` | When the summary was last displayed |
| `last_report_path` | Path to the last run's report directory |
| `paused` | If `True`, Curator will not run |
| `run_count` | Total number of runs completed |

State is loaded via `load_state()` (merges with `_default_state()`, preserving unknown keys starting with `_`) and saved via `save_state()` (atomic JSON write).

---

## Invariants

Curator adheres to the following strict invariants that must never be violated:

1. **Only touch agent-created skills** (`skills/auto/`), never built-ins (`skills/builtin/`)
2. **Pinned skills bypass all automatic transitions** — they are never marked stale or deleted
3. **`_pinned_guard()` is the enforcement layer** — every destructive operation checks it

---

## File Structure

```
curator/
├── __init__.py           # Public API exports
├── constants.py          # Constants (paths, state names, defaults)
├── config.py             # Config loading (curator.yaml + env vars)
├── state.py              # Curator run state persistence (.curator_state)
├── usage.py              # Skill usage record CRUD (.usage/{name}.json) + agent_created_report + orphan cleanup
├── transitions.py        # Auto state transitions + should_run_now logic
├── orchestrator.py       # Main orchestrator (run_curator_review / maybe_run_curator / _apply_consolidation / _generate_umbrella_skill)
├── classify.py           # Removed skill classification (consolidated vs pruned) + reconciliation
├── helpers.py            # Utilities (ISO parsing, atomic writes, skill description reader, path needle matching)
└── report.py             # Run report generation (run.json + REPORT.md + _build_rename_summary)
```

**Runtime files**:
```
skills/
├── .curator_state              # Curator run state
└── auto/
    └── .usage/
        └── {skill-name}.json   # Skill usage record

logs/curator/
└── {timestamp}/
    ├── run.json                # Structured run data
    └── REPORT.md               # Human-readable report
```
