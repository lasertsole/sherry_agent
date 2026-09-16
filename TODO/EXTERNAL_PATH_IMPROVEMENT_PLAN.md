# External Path Access Improvement Plan

> Three improvements to sherry_agent's external file access mechanism: directory-level allowlist, YOLO with exclusion list, and pre-mounted external directories.

**Current code:** `agent/tools/pub_base/path_utils.py` — `resolve_external_path()` with 5-level check (ROOT_DIR → YOLO → allowlist exact-match → subagent deny → HITL interrupt)

**Existing partial plan:** `path-traversal-hardening-todo.md` P2 (line 114-143) covers directory-level allowlist. This document supersedes and expands it.

---

## Current State

```
resolve_external_path(file_path, session_id)
  │
  ├─ 1. Inside ROOT_DIR → return (safe)
  ├─ 2. YOLO flag → return (全开，无限制)
  ├─ 3. Session allowlist → return (exact match only: str(resolved) == entry)
  ├─ 4. Subagent without prior auth → deny
  └─ 5. Main agent → HITL interrupt (approve / yolo / reject)
```

**Three problems:**

| #   | Problem                       | Impact                                                                                 |
| --- | ----------------------------- | -------------------------------------------------------------------------------------- |
| 1   | Allowlist is exact-match only | 访问 `/mnt/data/` 下的 100 个文件 → 100 次 HITL 审批。用户烦了直接开 YOLO，安全更差    |
| 2   | YOLO is all-or-nothing        | 开了 YOLO = 全部放行，包括 `~/.ssh/id_rsa`、`.env`、`credentials.json`。没有排除列表   |
| 3   | 无预挂载机制                  | 已知需要访问的外部目录（如 `/mnt/shared-assets/`）只能运行时逐个审批，没有构建时预配置 |

---

## Improvement 1 — Directory-Level Allowlist

**Goal:** Let `approve_dir` decision add an entire directory to the allowlist. Subsequent access to any file under that directory skips HITL.

**Priority:** P2 (existing in `path-traversal-hardening-todo.md`, expanded here)

### 1.1 Modify `_check_allowlist()`

**File:** `agent/tools/pub_base/path_utils.py:77-92`

```python
# Current — exact match only
def _check_allowlist(resolved: Path, session_id: str) -> bool:
    for sid in _candidate_session_ids(session_id):
        entries = state_register_mem.get_state(sid, _ALLOWLIST_KEY, [])
        if not entries:
            continue
        for entry in entries:
            if str(resolved) == entry:   # ← exact match only
                return True
    return False
```

```python
# New — exact match + directory prefix
def _check_allowlist(resolved: Path, session_id: str) -> bool:
    resolved_str = str(resolved)
    for sid in _candidate_session_ids(session_id):
        entries = state_register_mem.get_state(sid, _ALLOWLIST_KEY, [])
        if not entries:
            continue
        for entry in entries:
            # Exact file match
            if resolved_str == entry:
                return True
            # Directory prefix match (entry must end with separator)
            if entry.endswith("/") or entry.endswith("\\"):
                if resolved.is_relative_to(Path(entry)):
                    return True
    return False
```

### 1.2 Modify `_add_to_allowlist()`

**File:** `agent/tools/pub_base/path_utils.py:110-118`

```python
# Current — adds exact path
def _add_to_allowlist(resolved: Path, session_id: str) -> None:
    entries = state_register_mem.get_state(session_id, _ALLOWLIST_KEY, [])
    path_str = str(resolved)
    if path_str not in entries:
        entries.append(path_str)
        state_register_mem.set_state(session_id, _ALLOWLIST_KEY, entries)
```

```python
# New — accept mode parameter
def _add_to_allowlist(
    resolved: Path, session_id: str, *, mode: str = "file"
) -> None:
    entries = state_register_mem.get_state(session_id, _ALLOWLIST_KEY, [])
    if mode == "dir":
        # Ensure trailing separator for prefix matching
        path_str = str(resolved).rstrip("/\\") + "/"
    else:
        path_str = str(resolved)
    if path_str not in entries:
        entries.append(path_str)
        state_register_mem.set_state(session_id, _ALLOWLIST_KEY, entries)
```

### 1.3 Add `approve_dir` decision to HITL interrupt

**File:** `agent/tools/pub_base/path_utils.py:186-210`

```python
action_request = ActionRequest(
    name="external_file_access",
    args={"path": str(resolved)},
    description=(
        f"External file access approval\n"
        f"  Path: {resolved}\n"
        f"  Project root: {ROOT_DIR}\n"
        f"  Intent: {action_desc or 'unspecified'}\n\n"
        f"Options:\n"
        f"  approve      — allow this file only (session-scoped, inherited by subagents)\n"
        f"  approve_dir  — allow entire directory {resolved.parent}/ (session-scoped, inherited by subagents)\n"
        f"  yolo         — permanently allow all external paths (no more prompts)\n"
        f"  reject       — deny"
    ),
)
review_config = ReviewConfig(
    action_name="external_file_access",
    allowed_decisions=["approve", "approve_dir", "yolo", "reject"],
)
```

### 1.4 Handle `approve_dir` in decision processing

**File:** `agent/tools/pub_base/path_utils.py:216-232`

```python
if decision_type == "approve":
    _add_to_allowlist(resolved, session_id, mode="file")
    return resolved

if decision_type == "approve_dir":
    _add_to_allowlist(resolved.parent, session_id, mode="dir")
    return resolved

if decision_type == "yolo":
    state_register_db.set_state(_GLOBAL_SESSION, _YOLO_KEY, True)
    return resolved

# Reject
msg = decisions[0].get("message", "Rejected by user")
raise PathOutOfBoundsError(f"External file access denied: {resolved} ({msg})")
```

### Deliverables

- [ ] Modify `_check_allowlist()` — add directory-prefix matching
- [ ] Modify `_add_to_allowlist()` — accept `mode="file"|"dir"` parameter
- [ ] Add `approve_dir` to HITL `allowed_decisions` and prompt text
- [ ] Handle `approve_dir` in decision processing — add parent dir to allowlist
- [ ] Test: `approve_dir` on `/mnt/data/report.csv` → `/mnt/data/` added to allowlist → subsequent `/mnt/data/other.csv` auto-approved
- [ ] Test: directory entry with trailing `/` matches children but not sibling dirs
- [ ] Test: subagent inherits `approve_dir` entry from parent session
- [ ] Test: `approve` (file) still works as before — backward compatible

---

## Improvement 2 — YOLO with Exclusion List

**Goal:** Even when YOLO is active, deny access to a configurable list of sensitive paths (`.ssh`, `.env`, credentials, etc.).

**Priority:** P1 (security gap — YOLO currently opens everything including secrets)

### 2.1 The problem

```
Current YOLO flow:
  _is_yolo() → True → return resolved  # 任何路径都放行

  write_file("~/.ssh/authorized_keys") → YOLO → 直接写
  read_file(".env")                     → YOLO → 直接读
  read_file("~/.aws/credentials")        → YOLO → 直接读
```

YOLO 的本意是"我不在乎外部路径审批了"，不是"我允许 agent 读我的私钥"。

### 2.2 Design

Add a **deny list** that is checked **before** YOLO. The deny list is:

- Configurable via `sherry.jsonc` (`yolo_deny_paths`)
- Has sensible defaults (sensitive paths)
- Independent of YOLO mode — always enforced, even when YOLO is off and allowlist matches

```
New flow:
  1. Inside ROOT_DIR → return
  2. YOLO deny list → deny (always enforced, even in YOLO)
  3. YOLO flag → return (but deny list already checked)
  4. Session allowlist → return (but deny list already checked)
  5. Subagent without prior auth → deny
  6. Main agent → HITL interrupt
```

### 2.3 Config: `config/features/agent_side/hitl_defaults.py`

```python
class HitlDefaultsConfig(TypedDict):
    # ... existing fields ...
    yolo_deny_paths: list[str]


HITL_DEFAULTS: HitlDefaultsConfig = {
    # ... existing values ...
    "yolo_deny_paths": [
        "~/.ssh/",
        "~/.aws/",
        "~/.gnupg/",
        "~/.config/gcloud/",
        "~/.env",
        "~/.gitconfig",       # can contain tokens
        "~/.npmrc",            # can contain tokens
        "~/.pypirc",           # can contain tokens
    ],
}
```

**Note:** `~` is expanded at check time via `os.path.expanduser()`. Paths with trailing `/` are prefix-matched; without trailing `/` are exact-matched.

### 2.4 Implementation

**File:** `agent/tools/pub_base/path_utils.py`

```python
_YOLO_DENY_KEY = "yolo_deny_paths"


def _get_yolo_deny_paths() -> list[str]:
    """Get the YOLO deny list from config + session overrides."""
    from config.features import HITL_DEFAULTS
    return HITL_DEFAULTS.get(_YOLO_DENY_KEY, [])


def _is_yolo_denied(resolved: Path) -> bool:
    """Check if resolved path is in the YOLO deny list.

    Always enforced — even when YOLO is active or allowlist matches.
    """
    deny_paths = _get_yolo_deny_paths()
    resolved_str = str(resolved)
    for pattern in deny_paths:
        expanded = Path(os.path.expanduser(pattern))
        expanded_str = str(expanded)
        if expanded_str == resolved_str:
            return True
        # Directory prefix match
        if expanded_str.endswith(("/", "\\")):
            try:
                if resolved.is_relative_to(expanded):
                    return True
            except TypeError:
                pass
    return False
```

### 2.5 Integrate into `resolve_external_path()`

```python
def resolve_external_path(file_path: str, *, session_id: str, action_desc: str = "") -> Path:
    p = Path(os.path.expanduser(file_path))
    if not p.is_absolute():
        p = ROOT_DIR / p
    resolved = p.resolve()

    # 1. Inside ROOT_DIR — safe path
    if resolved == ROOT_DIR or resolved.is_relative_to(ROOT_DIR):
        return resolved

    # 2. YOLO deny list — always enforced, even in YOLO mode
    if _is_yolo_denied(resolved):
        raise PathOutOfBoundsError(
            f"Path is in the YOLO deny list and cannot be accessed: {resolved}"
        )

    # 3. YOLO — persistent global allow-all
    if _is_yolo():
        return resolved

    # 4. Session allowlist — exact + directory-prefix match
    if _check_allowlist(resolved, session_id):
        return resolved

    # 5. Subagent without prior authorization — cannot self-approve
    if _is_subagent(session_id):
        raise PathOutOfBoundsError(
            f"External path not authorized for subagent: {resolved}. "
            f"Approve this path from the main session first."
        )

    # 6. Main session — trigger HITL interrupt
    # ... (existing HITL code, with approve_dir from Improvement 1)
```

**Key design:** The deny list check is at position **2**, before YOLO (position 3) and allowlist (position 4). This means:

- YOLO on + deny list match → denied
- Allowlist match + deny list match → denied
- The deny list is the **floor** of security — nothing goes below it

### 2.6 User-facing: sherry.jsonc

```jsonc
{
  "yolo_deny_paths": [
    "~/.ssh/",
    "~/.aws/",
    "~/.gnupg/",
    "~/.env",
    "~/.gitconfig",
    // Add custom deny paths:
    "~/Documents/Secrets/",
    "D:\\\\Workspaces\\\\ci-credentials\\\\",
  ],
}
```

Users can extend the deny list without touching code. The defaults cover common sensitive paths; users add their own.

### Deliverables

- [ ] Add `yolo_deny_paths` field to `HitlDefaultsConfig` TypedDict + defaults
- [ ] Implement `_is_yolo_denied()` in `path_utils.py`
- [ ] Insert deny-list check at position 2 in `resolve_external_path()` (before YOLO)
- [ ] Support `sherry.jsonc` override for `yolo_deny_paths`
- [ ] Support both Unix `~/` and Windows `~\\` path expansion
- [ ] Test: YOLO on + path in deny list → denied
- [ ] Test: YOLO on + path NOT in deny list → allowed
- [ ] Test: YOLO off + allowlist match + path in deny list → denied
- [ ] Test: custom deny paths from `sherry.jsonc` override merge with defaults
- [ ] Test: `~/.ssh/id_rsa` always denied regardless of YOLO/allowlist state

---

## Improvement 3 — Pre-Mounted External Directories

**Goal:** Allow pre-configuring known external directories at build time. These bypass HITL entirely — they're trusted at config time, not discovered at runtime.

**Priority:** P2 (convenience — reduces HITL friction for known-needed external dirs)

**Depends on:** `backend-abstraction-plan.md` Phase 2 (CompositeBackend). This improvement adds external routes to the composite.

### 3.1 The problem

```
Current:
  Agent 需要访问 /mnt/shared-assets/ → 每个文件都要 HITL 审批
  Agent 需要访问 D:\Repos\other-project\ → 每个文件都要 HITL 审批
  Agent 需要访问 ~/Documents/templates/ → 每个文件都要 HITL 审批

用户知道这些目录是安全的，但每次都要手动 approve。
```

### 3.2 Design

Two-layer approach:

1. **Pre-backend (now):** Config-driven external path allowlist that bypasses HITL, implemented as a simple check in `resolve_external_path()`. No backend abstraction needed.

2. **Post-backend (after Phase 2):** External directories mounted as `CompositeBackend` routes, giving the model virtual path access (`/shared/assets/x.txt` → `/mnt/shared-assets/x.txt`).

This document covers layer 1 (immediate, no dependencies). Layer 2 is noted as future work.

### 3.3 Config: `config/features/agent_side/`

New file: `config/features/agent_side/external_mounts.py`

```python
"""Pre-configured external directory mounts."""

from typing import TypedDict


class ExternalMountsConfig(TypedDict):
    """Pre-configured external directories that bypass HITL approval.

    Paths listed here are trusted at config time. The agent can read/write
    them without HITL interrupts. Still subject to the YOLO deny list.
    """
    external_allow_paths: list[str]


EXTERNAL_MOUNTS: ExternalMountsConfig = {
    "external_allow_paths": [
        # Add paths via sherry.jsonc override:
        # "/mnt/shared-assets/",
        # "D:\\Repos\\other-project\\",
        # "~/Documents/templates/",
    ],
}
```

Register in `config/features/agent_side/__init__.py` (follows existing pattern).

### 3.4 Implementation in `resolve_external_path()`

```python
def _is_pre_mounted(resolved: Path) -> bool:
    """Check if resolved path is in the pre-configured external allow list."""
    from config.features import EXTERNAL_MOUNTS
    paths = EXTERNAL_MOUNTS.get("external_allow_paths", [])
    resolved_str = str(resolved)
    for pattern in paths:
        expanded = Path(os.path.expanduser(pattern))
        expanded_str = str(expanded)
        if expanded_str == resolved_str:
            return True
        # Directory prefix match
        if expanded_str.endswith(("/", "\\")):
            try:
                if resolved.is_relative_to(expanded):
                    return True
            except TypeError:
                pass
    return False
```

Insert at position **2.5** (after YOLO deny list, before YOLO flag):

```python
    # 2. YOLO deny list — always enforced
    if _is_yolo_denied(resolved):
        raise PathOutOfBoundsError(...)

    # 2.5. Pre-mounted external directories — trusted at config time
    if _is_pre_mounted(resolved):
        return resolved

    # 3. YOLO — persistent global allow-all
    if _is_yolo():
        return resolved
```

**Key:** Pre-mounted paths bypass HITL but NOT the YOLO deny list. If a path is in both lists, the deny list wins (checked first at position 2).

### 3.5 Future: CompositeBackend virtual mounts (after backend-abstraction Phase 2)

Once the backend layer is built, pre-mounted external directories become virtual path routes:

```python
# agent/backends/__init__.py — build_default_backend()

def build_default_backend() -> CompositeBackend:
    from config.features import EXTERNAL_MOUNTS

    routes: dict[str, BackendProtocol] = {
        "/memories/": FilesystemBackend(root_dir=MEMORY_DIR, virtual_mode=True),
        "/facts/": FilesystemBackend(root_dir=FACTS_DIR, virtual_mode=True),
        "/sessions/": SessionBackend(),
    }

    # Pre-mounted external directories → virtual path routes
    for i, path in enumerate(EXTERNAL_MOUNTS.get("external_allow_paths", [])):
        expanded = Path(os.path.expanduser(path)).resolve()
        name = expanded.name
        routes[f"/external/{name}/"] = FilesystemBackend(
            root_dir=expanded, virtual_mode=True
        )

    return CompositeBackend(
        default=FilesystemBackend(root_dir=ROOT_DIR, virtual_mode=True),
        routes=routes,
    )
```

Model sees:

```
/external/shared-assets/report.csv  → /mnt/shared-assets/report.csv
/external/other-project/src/main.py → D:\Repos\other-project\src\main.py
```

### 3.6 sherry.jsonc

```jsonc
{
  "external_allow_paths": [
    "/mnt/shared-assets/",
    "D:\\Repos\\other-project\\",
    "~/Documents/templates/",
  ],
}
```

### Deliverables

- [ ] `config/features/agent_side/external_mounts.py` — TypedDict + defaults
- [ ] Register in `config/features/agent_side/__init__.py`
- [ ] Implement `_is_pre_mounted()` in `path_utils.py`
- [ ] Insert pre-mount check at position 2.5 in `resolve_external_path()`
- [ ] Support `sherry.jsonc` override for `external_allow_paths`
- [ ] Test: pre-mounted path → no HITL, direct access
- [ ] Test: pre-mounted path + in YOLO deny list → denied (deny list wins)
- [ ] Test: non-pre-mounted external path → HITL as before
- [ ] (Future) CompositeBackend route for pre-mounted dirs — after Phase 2

---

## Complete New Flow

After all 3 improvements:

```
resolve_external_path(file_path, session_id)
  │
  ├─ 1. Inside ROOT_DIR → return (safe)
  ├─ 2. YOLO deny list → deny (always enforced, regardless of YOLO/allowlist)
  ├─ 2.5. Pre-mounted external dirs → return (trusted at config time)
  ├─ 3. YOLO flag → return (but deny list already checked)
  ├─ 4. Session allowlist → return (exact + directory-prefix match ← Improvement 1)
  ├─ 5. Subagent without prior auth → deny
  └─ 6. Main agent → HITL interrupt (approve / approve_dir / yolo / reject ← Improvement 1)
```

```
信任层级 (从低到高):

  ROOT_DIR 内          — 无条件信任
  YOLO deny list       — 无条件拒绝（安全地板）
  预挂载外部目录        — 配置时信任（sherry.jsonc）
  YOLO                 — 用户主动全开（但 deny list 仍然生效）
  Session allowlist    — 逐路径/逐目录信任（HITL approve / approve_dir）
  HITL interrupt       — 逐次审批
  Subagent             — 不能自审，只能继承
```

---

## Interaction Matrix

| Path location              | YOLO deny list  | Pre-mounted    | YOLO on | Allowlist match | Result                             |
| -------------------------- | --------------- | -------------- | ------- | --------------- | ---------------------------------- |
| Inside ROOT_DIR            | —               | —              | —       | —               | ✅ Direct access                   |
| `~/.ssh/id_rsa`            | ✅ in deny list | —              | —       | —               | ❌ Denied                          |
| `~/.ssh/id_rsa`            | ✅ in deny list | ✅             | ✅ on   | ✅              | ❌ Denied (deny list wins)         |
| `/mnt/data/x.csv`          | —               | ✅ pre-mounted | —       | —               | ✅ Direct (no HITL)                |
| `/mnt/data/x.csv`          | —               | —              | ✅ on   | —               | ✅ YOLO bypass                     |
| `/mnt/data/x.csv`          | —               | —              | —       | ✅ (dir)        | ✅ Allowlist match                 |
| `/mnt/data/x.csv`          | —               | —              | —       | —               | 🔔 HITL interrupt                  |
| `/mnt/data/x.csv`          | —               | —              | —       | — (approve_dir) | ✅ `/mnt/data/` added to allowlist |
| Subagent `/mnt/data/x.csv` | —               | —              | —       | —               | ❌ Denied (no self-approve)        |
| Subagent `/mnt/data/x.csv` | —               | —              | —       | ✅ (inherited)  | ✅ Parent's allowlist              |

---

## Implementation Order

```
Phase A (immediate, no dependencies):
  Improvement 2 — YOLO deny list       (P1, security gap)
  Improvement 1 — Directory allowlist   (P2, friction reduction)

Phase B (after backend-abstraction Phase 2):
  Improvement 3 — Pre-mounted dirs     (P2, convenience)
  3.4 _is_pre_mounted() can be done now
  3.5 CompositeBackend routes          (future, needs backend layer)
```

Improvement 2 first (security gap), then Improvement 1 (friction), then Improvement 3 (convenience).

---

## Files to Modify

| File                                            | Changes                                                                                                                                                         |
| ----------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `agent/tools/pub_base/path_utils.py`            | `_check_allowlist()` + dir prefix, `_add_to_allowlist()` + mode param, `_is_yolo_denied()` new, `_is_pre_mounted()` new, `resolve_external_path()` flow reorder |
| `config/features/agent_side/hitl_defaults.py`   | Add `yolo_deny_paths` field + defaults                                                                                                                          |
| `config/features/agent_side/external_mounts.py` | New file — `ExternalMountsConfig` + `EXTERNAL_MOUNTS`                                                                                                           |
| `config/features/agent_side/__init__.py`        | Re-export `EXTERNAL_MOUNTS`                                                                                                                                     |
| `config/sherry_settings.py`                     | Load `yolo_deny_paths` + `external_allow_paths` from `sherry.jsonc`                                                                                             |

No changes to file tools (`read_file.py`, `write_file.py`, etc.) — they call `resolve_external_path()` which handles everything internally.
