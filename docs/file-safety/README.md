# 🛡️ File Write Safety

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> How concurrent writers — the main agent, up to 8 subagents, and a second Sherry process on the same project — are stopped from silently destroying each other's edits: an atomic write, a two-layer CAS, an in-process per-path lock, a cross-process `flock`, a read-before-write license for `write_file`, and opt-in isolated workspaces whose changes merge back under a lock.

Source of truth: `agent/tools/pub_base/atomic_write.py`, `agent/tools/pub_base/path_lock.py`, `agent/tools/pub_base/file_lock.py`, `agent/tools/pub_base/read_state.py`, `agent/tools/file_tools/write_file.py`, `agent/tools/file_tools/read_file.py`, `agent/tools/file_tools/patch_file.py`, `agent/tools/subagent/isolation/`, `agent/tools/subagent/announce/workspace_merge.py`. Every constant in this document was verified against that code.

## Table of Contents

- [Overview](#-overview)
- [The Layers](#-the-layers)
  - [1. Atomic write](#1-atomic-write)
  - [2. Two-layer CAS](#2-two-layer-cas)
  - [3. Per-path locks](#3-per-path-locks)
  - [4. Leftover sweep](#4-leftover-sweep)
- [Read-before-write license](#-read-before-write-license)
- [Isolated subagent workspaces](#-isolated-subagent-workspaces)
- [Alternatives, measured](#%EF%B8%8F-alternatives-measured)
- [Resource files & encodings](#%EF%B8%8F-resource-files--encodings)
- [Snapshots & revert](#-snapshots--revert)
- [Conversation rewind](#%EF%B8%8F-conversation-rewind)
- [Boundaries](#-boundaries)
- [Testing](#-testing)
- [File Map](#%EF%B8%8F-file-map)

## 🎯 Overview

Every file tool shares one process with every subagent, and the tools are process-level singletons — so two writers on one path is a normal Tuesday, not a corner case. Without protection the failures are silent: the later writer overwrites the earlier edit and both report success, a reader catches a half-written file, a crash mid-write truncates the target.

Six mechanisms answer that, in the order a write meets them:

1. `file_write_lock` — a per-path `threading.Lock`, then a cross-process `flock`.
2. `atomic_write_text_no_follow` — same-directory temporary file, `fsync`, mode preservation, `os.replace`.
3. `patch_file`'s two-layer CAS — a fingerprint at read time, `expected_revision` re-asserted just before the replace.
4. `write_file`'s read-before-write license — an existing file may only be overwritten by a session that has read it (§ next but one).
5. `sweep_stale_temp_files` — a `kill -9` leftover is swept by the next write into that directory.
6. Isolated subagent workspaces — an opt-in private copy whose changes merge back under a lock, conflicts reported.

## 🧱 The Layers

### 1. Atomic write

`atomic_write_text_no_follow` (and the byte-level `atomic_write_bytes_no_follow` under it) writes into a `.sherry-tmp-*.swp` file in the target's own directory, `fsync`s it, copies the target's mode onto it — a script's execute bit must survive — and then `os.replace`s it into place. A reader therefore sees the old content or the new content, never a torn file, and a crash mid-write cannot truncate the target.

Two deliberate edges:

- A symlinked final component is REFUSED (`ELOOP`, via `_open_no_follow`), so the helper cannot reuse `pub/func/atomic_replace.py`, which follows a link on purpose.
- When the filesystem refuses the rename, the helper degrades to an in-place write — availability over atomicity, logged at WARNING.

### 2. Two-layer CAS

`patch_file` reads the file, matches fuzzily, and then checks twice: a fingerprint taken at read time (`mtime_ns` + `size`, with a content-hash exemption so a no-op `touch` is not a conflict), and the `expected_revision` (`mtime:<int ms>:size:<bytes>`) that the atomic write re-asserts immediately before the `os.replace`. A change by anyone else is refused with a "re-read and retry" error instead of being overwritten.

The check-to-write window is not zero and this document does not claim otherwise; it is narrowed to the smallest interval that does not require a filesystem-level transaction.

### 3. Per-path locks

`path_lock.py` keeps a reference-counted registry of `threading.Lock`s — one per resolved path — around the whole read-modify-write cycle, so two agents in this process serialize instead of racing. `file_lock.py` then adds the cross-process half: an advisory `flock` on a sidecar file under `src/data/locks/`, with a timeout that answers `FileBusyError` (an actionable tool error, not a hang).

The rule that surprises people: **lock files are never deleted.** The lock lives on the inode, so unlinking a sidecar — even a stale-looking one — lets the next writer create a fresh file and take a lock nobody else is excluding against; a probe showed both holders inside the critical section. The 0-byte sidecars are inert, and `flock` is released by the kernel when a holder dies, so there is no dead-owner protocol to implement.

`terminal`, `python_repl` and ast-grep rewrites run in subprocesses and take no lock: the CAS is what catches them, and that boundary is deliberate.

### 4. Leftover sweep

Only a hard kill between the temporary file's creation and the replace can leave a `.sherry-tmp-*.swp` behind; every failure path unlinks it. The next write into the same directory sweeps those older than 1 hour (`sweep_stale_temp_files`): a live writer's temporary file is seconds old, so the age threshold is what keeps the sweep away from it. The sweep never raises — a scan that cannot read the directory must not fail the write it precedes.

## 📖 Read-before-write license

`write_file` is a blind overwrite: it never reads the target, so it had no revision to carry into the CAS and could destroy content this session had never seen. `read_state.py` supplies the missing precondition — a process-local map of `(session, resolved path) → revision`, fed by two events only:

- a COMPLETE `read_file` (first page, nothing truncated; a partial read licenses nothing), whose revision comes from the open descriptor's `fstat`, so a path swapped mid-read cannot license bytes the session never saw;
- the session's own whole-file `write_file`, because the session authored that content.

A session's own `append` or `patch_file` ADVANCES a license it already holds and never invents one — a delta is not knowledge of the whole file. The license rides into the atomic write as `expected_revision`, so:

- an existing file the session never read is refused outright with a "read it first" error and a `read_file`/`patch_file` hint;
- a file that changed after the read is refused by the same assertion that catches a concurrent writer;
- a path that does not exist yet is licensed as `absent`, so a create raced by another writer is refused rather than clobbered;
- a directory or a symlinked target skips the gate — its write fails with its own error, and "read it first" would be nonsense.

The registry is capped (4096 entries, LRU) and is **not persisted**: a restart forgets the licenses, and the next overwrite of an existing file answers "read it first" — one extra read, never a lost edit. Eviction drops protection, never grants it.

## 🌱 Isolated subagent workspaces

`sessions_spawn(isolation=True)` gives a child its own copy of the project directory (`agent/tools/subagent/isolation/`, workspace under `src/data/isolated/`, the child's cwd is `<workspace>/tree`). The child works end to end in the copy — its tools, its `terminal`, its tests — and the announce flow merges the copy back when the run turns terminal, before the completion message is delivered (a silent child's work merges too).

The merge is the same rule as everywhere else, applied in the other direction, under a per-parent-root `flock`:

- the copy carries a manifest (`snapshot.json`) of every regular file's revision at copy time;
- a file the child changed is applied only while the parent still holds the snapshot revision — otherwise it is a CONFLICT, and the parent's file is left untouched;
- a new file is created into empty space only; a deletion requires the parent to still match; symlinks are never merged through (they are skipped and reported);
- the caches (`node_modules`, `.git`, `.venv`, `__pycache__`, `dist`, `build`, …) are neither copied nor merged.

A clean merge removes the workspace; a conflicting one keeps `<workspace>/tree` for inspection and consumes the manifest, so nothing can ever merge the same tree twice. The completion reply the parent reads carries the report (`applied`, `created`, `deleted`, and every conflict by path), and the merged paths are marked stale in the parent's evidence ledger.

## ⚖️ Alternatives, measured

**Row-hash editing (omo's hashline)** is not built: it is finer than a whole-file CAS — only the lines an edit touches are compared — but it requires changing the read tool's own output format, and the four layers above already remove the silent-loss failures it targets.

**Directory `fsync` after the rename** is not enabled. Measured on this device (f2fs): a write costs 1.34 ms with the directory `fsync` and 0.52 ms without — 2.6× the write path, about 0.8 ms absolute per file. What it buys is narrow: the file's data is already `fsync`ed, so a power loss can never leave a torn file — only an edit that silently reverts to its previous content, which is also what every mainstream editor does. A process crash needs nothing at all: the page cache survives. It is one line behind a flag if a deployment ever promises power-loss durability for an acknowledged write.

## 🗂️ Resource files & encodings

The tools edit TEXT, and the license made that load-bearing: a read used to hand
the model a screenful of replacement characters for a PNG and then license an
overwrite, so an image could be replaced by mojibake. `file_utils.sniff_text_encoding`
now decides, from the BOM first and a NUL / UTF-8-decode check second:

- **UTF-8, UTF-8-with-BOM and UTF-16 (LE/BE)** are read and edited normally, and
  the license carries the codec forward — `write_file` and `patch_file` write
  back in the file's OWN encoding, so a UTF-16 config keeps its BOM and byte
  order instead of silently becoming UTF-8, and a big-endian file stays
  big-endian.
- **Anything else is a resource file**: `read_file` answers with the size and a
  `terminal` hint and grants NO license, `patch_file` refuses, and `write_file`
  refuses before the license is even consulted. That is what keeps an image or
  an archive from being replaced by text: a binary never holds a license, so
  nothing can text-write it.
- **Legacy codecs without a BOM (GBK/GB18030)** are refused, not guessed — a
  wrong guess that rewrites a file in the wrong codec is worse than an
  actionable refusal. `terminal` (cp / python) is the escape hatch.
- **`append` is UTF-8 only**: appending into a UTF-16 file would write UTF-8
  bytes into it or plant a second BOM mid-file, so it answers with "read it and
  write the whole file" instead.
- The isolated-workspace merge is byte-level throughout, so resource files merge
  exactly even though the text tools will not edit them.

## 🧷 Snapshots & revert

Every write through the file tools captures the OLD bytes before it lands, so
an agent that breaks a file can be put back — the ZCode model, with two
additions of our own: the freshness check runs on both the plan and the apply,
and a refusal gives the user a way forward instead of just "no".

- **Capture** (`snapshot.py`): a content-addressed blob under
  `SESSIONS_DIR/<session>/file_snapshots/` (writing the same old content twice
  costs one blob) plus one row in `file-snapshots.db` per write: the before and
  after revisions, the after-content hash computed from bytes the call already
  held, `existed_before`, the owning `tool_call_id`, and the capturing process's
  `pid` + start token. Creating a file costs no read at all; a refused write
  captures nothing. Fail-open: a broken index warns and the write proceeds.
- **`file_changes_revert`** plans then applies. One file that moved since its
  snapshot refuses the WHOLE batch — that rule is the point, because restoring
  over an edit the revert did not make is exactly the harm this feature exists
  to prevent. The restored content is the earliest snapshot in scope; the
  freshness check compares the disk against the path's NEWEST row (checking
  against the earliest would make any twice-written file unrevertable). A
  created file is deleted (never left as a 0-byte leftover); a file deleted by
  someone else is refused rather than resurrected; a collected blob answers
  `snapshot_expired`. Reverts are one-shot: a second call finds nothing, and
  there is no redo.
- **A refusal still leaves a way forward**: inside a git work tree the plan
  probes three-way-merge feasibility with `git merge-file -p` on temporary
  copies (`-p` is mandatory — without it git writes the result back into its
  first argument) and reports the command. The probe never touches the work
  tree, and its failure is information, never a second refusal.
- **Retention** (`snapshot_gc.py`) is capped three ways per session (bytes,
  rows, age) and deletes on identity, not age alone: a row's captured `pid` +
  start token are checked with `kill(pid, 0)` — `ESRCH` is proven-dead (and
  deliberately does NOT compare a start time: with no process there is none to
  compare, and demanding one would make the branch dead code), a matching token
  is alive, a mismatched one means the pid was recycled, and `EPERM` counts as
  ALIVE because the process exists. A long turn can therefore still revert its
  own first write at hour six. Orphan blobs from a crashed capture are swept
  too. The daemon thread starts beside the auth cleanup at boot.
- **Client**: `file_changes_updated` is pushed after every write and
  `file_changes_refresh` answers with the same payload; the chat shows a
  revert chip (greyed out, never hidden, when a revert is no longer possible)
  and a two-step dialog — the per-file plan first, the destructive confirm
  second. `GET /sessions/file-changes` and `POST /sessions/file-changes/revert`
  call the very same core the agent-side tool does.

## ⏪ Conversation rewind

A rewind cuts the conversation back to a message — the file-tool twin of the
snapshot revert, for the conversation itself. Nothing is deleted: the store is
append-only, so the cut is recorded as a hidden id RANGE and every reader
filters through it (the chat page, the prompt, the continuity snapshot). A
message sent after the rewind has a larger id and is visible again at once.

- **`POST /sessions/rewind`** (`{session_id, cut_after_message_id}`) applies the
  cut; **`GET /sessions/rewind`** reports the branch state and whether a rewind
  is allowed right now. Messages stay in SQLite — `runtime/session/conversation_branch.py`
  is the record (`hidden_ranges`, `branch_generation`, `rewound_at`), stored in
  the session state register (memory + `state_register.db`).
- **The turn boundary is the only safe place for a cut.** Both endpoints refuse
  while the session has a turn in flight (the same `session_turn_active` check
  the composer uses), because no fence can stop a tool call that is already
  running — and `GET` reports the same verdict so the client can grey its
  control out rather than offer an action that will be refused.
- **Fencing** keeps work created before the cut from landing after it: a
  subagent's spawn stamps the parent's `branch_generation` onto the child, and
  the announce flow drops a run whose stamp is older than the parent's current
  generation (missing stamp = fail open, so a legitimate announcement is never
  lost); a compression nudge scheduled before a rewind is dropped when it would
  run after one; and a rewind clears the HITL pending flag, so an approval that
  arrives for an interrupt the user already cut away is refused instead of
  resuming the abandoned branch.
- **Client**: every rendered message row carries a 回到这里 / "Back to here" control OUTSIDE and BELOW its bubble (the bubble stays pure content), two-state on purpose — the first click arms it, the second cuts — and greyed out (never hidden) while the server refuses.
- **Deliberate deviation**: queued USER messages are not fenced. They are user
  intent, not branch state — dropping them silently would lose something the
  user typed, while delivering them answers a message the user still means.

## 🚧 Boundaries

- The cross-process lock is advisory: an external editor does not take it. The CAS layers are the answer there.
- `terminal`, `python_repl` and ast-grep rewrites bypass locks and licenses entirely (subprocesses), so an agent that writes files through a shell command is outside every guarantee here.
- Network filesystems: `flock` semantics are unreliable on NFS; local disks are the target.
- Hard links are broken by design: `os.replace` swaps the inode, so a hard link to the old content keeps the old content.

## 🧪 Testing

`tests/agent/tools/file_tools/` pins the write path: `test_atomic_write.py` (atomicity, symlink refusal, both CAS layers, the sweep), `test_file_write_concurrency.py` (parallel patches, torn-read freedom), `test_file_lock_cross_process.py` (two real processes, `kill -9` release), `test_read_before_write.py` (the license matrix). `tests/agent/tools/subagent/test_workspace_isolation.py` pins the copy, the merge CAS, conflicts, symlink skipping and the per-root serialization.

## 🗺️ File Map

| Path | Role |
| --- | --- |
| `agent/tools/pub_base/atomic_write.py` | Atomic write, revision ids, the leftover sweep |
| `agent/tools/pub_base/path_lock.py` | In-process per-path lock registry |
| `agent/tools/pub_base/file_lock.py` | `flock` sidecars, `file_write_lock`, `FileBusyError` |
| `agent/tools/pub_base/read_state.py` | The read-before-write license registry |
| `agent/tools/subagent/isolation/tree.py` | Workspace copy, manifest, discard |
| `agent/tools/subagent/isolation/merge.py` | The locked, CAS-checked merge-back |
| `agent/tools/subagent/announce/workspace_merge.py` | Merge hook + report in the completion reply |
