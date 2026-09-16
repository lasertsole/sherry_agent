# Path Traversal Hardening Plan

> Merged from `PATH_TRAVERSAL_HARDENING_TODO.md` + `VIRTUAL_MODE_MIGRATION_PLAN.md`. Identifies gaps in sherry_agent's path traversal defense and specifies fixes ported from deepagents' `virtual_mode`.

**Current defense layers** (see `docs/sandbox/README.md`):

- L0: `pub/func/path.py` — `has_traversal_component()` + `validate_within_dir()`
- L1: `agent/tools/pub_base/path_utils.py` — `resolve_project_path()` + `resolve_external_path()` (5-gate HITL)
- L2: `agent/tools/skill_tools/skill_manage.py` — traversal pre-check + subdir allowlist
- L3: `agent/tools/subagent/spawn/attachments.py` — filename/mount-path sanitization
- L4: OS sandbox (bwrap / seatbelt) — filesystem write containment

**Reference design:** deepagents `FilesystemBackend(virtual_mode=True)` (`backends/filesystem.py:142`) — 7 structural layers making traversal impossible by design. See [Appendix A](#appendix-a--deepagents-virtual_mode-reference) for the full breakdown.

**Principle:** Each item is a standalone PR. No item depends on another. Test independently.

---

## P0-1 — Shell can read any file on the system

**Threat:** bwrap does `--ro-bind / /`; seatbelt does `(allow default)`. Both restrict **writes** only. The model can `cat ~/.ssh/id_rsa`, `cat .env`, or `cp /etc/passwd .` via terminal — no read barrier exists.

**Current code:** `agent/tools/pub_base/sandbox_bwrap.py:86-92`, `agent/tools/pub_base/sandbox_seatbelt.py:42-51`

**Plan:**

- [ ] `sandbox_bwrap.py` — after `--ro-bind / /`, add `--ro-bind /var/empty <sensitive_path>` for each sensitive path (covers with an empty dir):
  ```
  ~/.ssh, ~/.aws, ~/.gnupg, ~/.config/gh, ~/.docker
  ```
- [ ] `sandbox_seatbelt.py` — after `(deny file-write*)`, add deny-read rules:
  ```
  (deny file-read* (subpath "~/.ssh"))
  (deny file-read* (subpath "~/.aws"))
  (deny file-read* (subpath "~/.gnupg"))
  (deny file-read* (regex #"(^|/)\\.env$"))
  (deny file-read* (regex #"(^|/)\\.env\\."))
  ```
- [ ] Add a `_sensitive_read_paths()` helper shared by both backends, expandable via env `SHERRY_DENY_READ_PATHS`.
- [ ] Windows: no OS backend — document that read protection is unavailable (honesty note).

---

## P0-2 — Terminal tool has zero application-level path checks

**Threat:** File tools all call `resolve_project_path()` / `resolve_external_path()`. The terminal tool calls `subprocess.Popen(argv, cwd=ROOT_DIR)` with **no path analysis**. `sandbox=False` (HITL-approved) commands can access any path.

**Current code:** `agent/tools/terminal.py:174-205` — `_execute_sync` spawns directly.

**Plan:**

- [ ] Add `_SENSITIVE_FILE_PATTERNS` regex list to `terminal.py` (checked after `_check_dangerous`):
  ```python
  _SENSITIVE_FILE_PATTERNS = [
      re.compile(r"\b(?:cat|head|tail|less|more)\s+.*?(/etc/(?:passwd|shadow|sudoers))\b"),
      re.compile(r"\b(?:cat|head|tail)\s+.*?\.env\b"),
      re.compile(r"\bcp\s+.*?\.ssh/"),
      re.compile(r"\bcurl\s+.*?-d\s+@.*?\.env\b"),
      re.compile(r"\b(?:cat|head|tail)\s+.*?~/.ssh/"),
      re.compile(r"\b(?:cat|head|tail)\s+.*?~/.aws/"),
  ]
  ```
- [ ] `_check_sensitive_file_access(joined: str) -> None` — raises `ToolException` on match, telling the model to use `read_file` with external_path approval instead.
- [ ] Call it from both `_run` and `_arun` after `_check_dangerous`, before spawn.
- [ ] Wire the same check into `python_repl.py` for `open()` calls targeting sensitive paths (stretch — may require AST inspector or block `open` of deny-listed paths in the wrapper script).

---

## P0-3 — Path resolution lacks three-gate defense + O_NOFOLLOW

**Threat:** `resolve_project_path()` only has Gate 2 (resolve + relative_to). Missing: string-level `..`/`~` rejection (Gate 1), symlink loop detection (Gate 3), and `os.open(O_NOFOLLOW)` to close TOCTOU race between resolve and open.

**Source:** `filesystem.py:203-215` (three-gate), `filesystem.py:449, 508-511, 551, 572-575` (O_NOFOLLOW)

**Current code:** `agent/tools/pub_base/path_utils.py:19-37`

```python
resolved = p.resolve()                    # T0: check
if not resolved.is_relative_to(ROOT_DIR): # T1: validate
    raise PathOutOfBoundsError(...)
return resolved                           # T2: caller opens str(resolved) later
# ↑ TOCTOU: symlink can be swapped between T0 and T2
```

### Gate 1: String-level `..`/`~` rejection (no filesystem touch)

```python
# path_utils.py — add before p.resolve()

vpath = file_path.lstrip("/")
if ".." in vpath or file_path.startswith("~"):
    raise PathOutOfBoundsError(f"Path traversal not allowed: {file_path}")
```

### Gate 3: Symlink loop detection

```python
# path_utils.py — copy verbatim from filesystem.py:1537-1576

_WIN32_ERROR_CANT_RESOLVE_FILENAME = 1921

def _is_eloop_oserror(exc: BaseException | None) -> bool:
    return isinstance(exc, OSError) and (
        exc.errno == errno.ELOOP
        or getattr(exc, "winerror", None) == _WIN32_ERROR_CANT_RESOLVE_FILENAME
    )

def _is_symlink_loop_error(exc: Exception) -> bool:
    if _is_eloop_oserror(exc):
        return True
    return isinstance(exc, RuntimeError) and any(
        _is_eloop_oserror(chained)
        for chained in (exc.__cause__, exc.__context__)
    )

def _raise_if_symlink_loop(path: Path) -> None:
    if not path.is_symlink():
        return
    try:
        path.stat()
    except OSError as exc:
        if _is_eloop_oserror(exc):
            raise
```

### O_NOFOLLOW I/O — close TOCTOU

```python
# path_utils.py — new helper

def _open_no_follow(path: Path, flags: int, mode: int = 0o644) -> int:
    """os.open with O_NOFOLLOW; Windows fallback: is_symlink check."""
    if not hasattr(os, "O_NOFOLLOW"):
        if path.is_symlink():
            raise OSError(errno.ELOOP, "Symbolic link not allowed")
    return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), mode)
```

Apply to file tools:

```python
# read_file.py — _core
fd = _open_no_follow(resolved, os.O_RDONLY)
try:
    with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as f:
        fd = -1
        raw = f.read()
finally:
    if fd >= 0:
        os.close(fd)

# write_file.py — _core (stop calling super()._run())
flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
fd = _open_no_follow(resolved, flags, 0o644)
with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
    f.write(text)

# patch_file.py — _core (read + write, both via _open_no_follow)
```

### Files changed

| File                                   | Change                                                         |
| -------------------------------------- | -------------------------------------------------------------- |
| `agent/tools/pub_base/path_utils.py`   | Add Gate 1 + Gate 3 + `_open_no_follow` + 4 helper functions   |
| `agent/tools/file_tools/read_file.py`  | Replace `resolved.open()` with `_open_no_follow` + `os.fdopen` |
| `agent/tools/file_tools/write_file.py` | Replace `super()._run()` with direct `_open_no_follow` write   |
| `agent/tools/file_tools/patch_file.py` | Read + write both via `_open_no_follow`                        |
| `pub/func/path.py`                     | No change (keep `has_traversal_component` for skill tools)     |

### Tests

- [ ] `test_resolve_rejects_double_dot` — `resolve_project_path("../../etc/passwd")` raises without touching filesystem
- [ ] `test_resolve_rejects_tilde` — `resolve_project_path("~/secret")` raises
- [ ] `test_resolve_detects_symlink_loop` — create `a -> b`, `b -> a` inside ROOT_DIR, verify `OSError(ELOOP)`
- [ ] `test_read_rejects_symlink_last_component` — `link.txt -> /etc/passwd` inside ROOT_DIR, `read_file("link.txt")` fails
- [ ] `test_write_rejects_symlink` — `write_file("link.txt", "data")` fails on symlink
- [ ] `test_patch_rejects_symlink` — same for `patch_file`
- [ ] `test_normal_files_unaffected` — all three tools work on regular files
- [ ] `test_windows_fallback` — mock `O_NOFOLLOW` absent, verify `is_symlink` check fires
- [ ] `test_toctou_race` — symlink swap mid-call, verify rejection

### Checklist

- [ ] Copy `_is_eloop_oserror`, `_is_symlink_loop_error`, `_raise_if_symlink_loop` into `path_utils.py`
- [ ] Add `import errno` to `path_utils.py`
- [ ] Add Gate 1 (string `..`/`~` check) before `p.resolve()`
- [ ] Add Gate 3 (`_raise_if_symlink_loop`) after `relative_to` check
- [ ] Add `_open_no_follow` helper to `path_utils.py`
- [ ] Replace I/O in `read_file.py._core`
- [ ] Replace I/O in `write_file.py._core` — stop calling `super()._run()`
- [ ] Replace I/O in `patch_file.py._core` — read + write both use `_open_no_follow`
- [ ] Run `uv run pytest tests/agent/tools/ -k "resolve or path or file" -q`
- [ ] Run `uv run --with ruff ruff check agent/tools/pub_base/path_utils.py agent/tools/file_tools/`

---

## P1-1 — No centralized path validation middleware

**Threat:** Each tool implements its own `try: resolve_project_path() except: resolve_external_path()` pattern. A new tool that forgets this copy-paste block has **zero path protection**. The pattern is convention, not enforcement.

**Current code:** `read_file.py:67-76`, `write_file.py:55-63`, `patch_file.py:120-128`, `search_files.py:208-216` — identical try/except duplicated 4x.

**Plan:**

- [ ] Create `agent/middlewares/path_guard/__init__.py` — a `before_tool` middleware that:
  - Scans tool call args for keys named `file_path`, `path`, `directory`, `dir`.
  - Calls `resolve_project_path(value)` — on `PathOutOfBoundsError`, tags the call for external-path approval (or rejects if no HITL context).
  - Injects the resolved path back into the args so tools receive a pre-validated `Path` object.
- [ ] Register in `agent/middlewares/__init__.py` after `ToolCallNormalize` and before `ToolGuardrails`.
- [ ] Deprecate the per-tool try/except blocks — keep them as a second line of defense but mark with `# redundant: path_guard middleware handles this`.
- [ ] Test: create a tool with a `file_path` arg that does NOT call `resolve_project_path()`; verify the middleware still rejects `/etc/passwd`.

---

## P1-2 — ROOT_DIR leaks to model via tool output + error messages

**Threat:** File tools return real filesystem paths in success messages, error messages, and search results. `OSError.__str__` embeds the full path (e.g. `"[Errno 13] Permission denied: '/home/user/project/.env'"`). The model learns the project's real location, enabling exfiltration via terminal commands.

**Source:** `filesystem.py:225-264` (`_to_virtual_path` + `_display_path`), `filesystem.py:1201-1214` (`_safe_detail`)

### New functions in `path_utils.py`

```python
def to_virtual_path(real_path: Path) -> str:
    """Convert a real filesystem path to a virtual path anchored at ROOT_DIR.

    /home/user/project/src/main.py -> /src/main.py

    Raises ValueError if the path is outside ROOT_DIR.
    """
    return "/" + real_path.resolve().relative_to(ROOT_DIR.resolve()).as_posix()

def display_path(real_path: Path) -> str:
    """Safely render a path for model-visible output.

    Returns a virtual path in normal cases. If the path cannot be converted
    (outside root, unresolvable symlink), falls back to just the filename
    so ROOT_DIR never leaks.
    """
    try:
        return to_virtual_path(real_path)
    except (ValueError, OSError, RuntimeError):
        return real_path.name or "/"

def safe_error_detail(exc: Exception) -> str:
    """Extract an agent-safe error detail string.

    OSError.__str__ embeds the real file path. This returns only the
    strerror ('Permission denied') so ROOT_DIR never leaks into error
    messages visible to the model.
    """
    if isinstance(exc, OSError):
        detail = exc.strerror
    else:
        detail = getattr(exc, "reason", None)
        if detail is None:
            detail = str(exc)
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
```

### Tool output changes

```python
# read_file.py — error messages + return
return json.dumps({"error": f"File not found: {display_path(resolved)}"}, ...)
return json.dumps({"error": f"Failed to read file: {safe_error_detail(e)}"}, ...)

# write_file.py — return path
return json.dumps({"success": True, "path": display_path(resolved)}, ...)

# patch_file.py — diff header + return + except blocks
diff = _unified_diff(old, new, display_path(resolved))
return json.dumps({"error": f"Failed to read file: {safe_error_detail(e)}"}, ...)

# search_files.py — result paths + error accumulation
virt = display_path(matched_path)
msg = f"child error: cannot stat: {safe_error_detail(e)}"
```

### Files changed

| File                                     | Change                                                                   |
| ---------------------------------------- | ------------------------------------------------------------------------ |
| `agent/tools/pub_base/path_utils.py`     | Add `to_virtual_path` + `display_path` + `safe_error_detail` (~25 lines) |
| `agent/tools/pub_base/__init__.py`       | Export 3 new functions                                                   |
| `agent/tools/file_tools/read_file.py`    | Replace `file_path`/`{e}` in ~6 strings                                  |
| `agent/tools/file_tools/write_file.py`   | Replace return path                                                      |
| `agent/tools/file_tools/patch_file.py`   | Replace diff header + return + except blocks                             |
| `agent/tools/file_tools/search_files.py` | Convert matched paths + error accumulation                               |

### Tests

- [ ] `test_display_path_normal` — `display_path(ROOT_DIR / "src" / "main.py")` returns `/src/main.py`
- [ ] `test_display_path_outside_root` — `display_path(Path("/etc/passwd"))` returns `passwd` (no leak)
- [ ] `test_safe_detail_strips_path` — `FileNotFoundError("[Errno 2] No such file: /home/.env")` → `"FileNotFoundError: No such file or directory"`
- [ ] `test_read_file_error_no_root_leak` — read a file that triggers `PermissionError`, verify ROOT_DIR not in error JSON
- [ ] `test_search_results_use_virtual_paths` — search results contain virtual paths

### Checklist

- [ ] Add 3 functions to `path_utils.py`
- [ ] Export from `pub_base/__init__.py`
- [ ] Update `read_file.py` (~6 string replacements)
- [ ] Update `write_file.py` (return path)
- [ ] Update `patch_file.py` (diff header + return + except blocks)
- [ ] Update `search_files.py` (result paths + errors)
- [ ] Run `uv run pytest tests/agent/tools/file_tools/ -q`

---

## P2-1 — Search results not containment-filtered

**Threat:** `search_files.py` uses `root.rglob("*")` which follows symlinks. A symlink inside ROOT_DIR pointing to `/etc` would surface `/etc/passwd` in search results.

**Source:** `filesystem.py:362-365` (ls), `1097-1105` (grep), `1383-1411` (glob)

**Plan:**

```python
# search_files.py — _search_files, inside the rglob loop:

for matched_path in root.rglob(pattern):
    # NEW: containment check — skip paths that resolve outside ROOT_DIR
    try:
        matched_path.resolve().relative_to(ROOT_DIR.resolve())
    except (ValueError, OSError, RuntimeError):
        continue
    # ... existing match logic ...

# _search_content — same pattern:
for file_path in root.rglob("*"):
    try:
        file_path.resolve().relative_to(ROOT_DIR.resolve())
    except (ValueError, OSError, RuntimeError):
        continue
    # ... existing content search ...
```

### Files changed

| File                                     | Change                                                                      |
| ---------------------------------------- | --------------------------------------------------------------------------- |
| `agent/tools/file_tools/search_files.py` | Add containment check in `_search_files` + `_search_content` (2 × ~4 lines) |

### Tests

- [ ] `test_search_skips_symlink_escape` — create `ROOT_DIR/escape -> /etc`, search `*`, verify no `/etc/passwd` in results
- [ ] `test_search_content_skips_symlink_escape` — same for content search
- [ ] `test_search_normal_files_unaffected` — regular files still appear

---

## P2-2 — `has_traversal_component` does not decode URL-encoded paths

**Threat:** Low (paths come from LLM tool calls, not HTTP). But `%2e%2e`, `..%2f`, or Unicode equivalents can bypass the regex.

**Current code:** `pub/func/path.py:5-12`

```python
def has_traversal_component(path_str: str) -> bool:
    parts = Path(path_str).parts
    return any(re.compile(r"\.{2,}").fullmatch(p) for p in parts)
```

**Plan:**

- [ ] Add URL-decode + backslash normalization before the check:
  ```python
  import urllib.parse
  decoded = urllib.parse.unquote(path_str)
  normalized = decoded.replace("\\", "/")
  parts = Path(normalized).parts
  ```
- [ ] Add test cases: `%2e%2e`, `..%2f`, `..%5c`, `...` (already handled), `....` (already handled).
- [ ] Keep the function pure (no IO, no logging) — it is called from skill tools and attachment validation.

---

## Summary

| ID   | Priority | Gap                                      | Files to change                                                   | Effort |
| ---- | -------- | ---------------------------------------- | ----------------------------------------------------------------- | ------ |
| P0-1 | P0       | Shell read any file                      | `sandbox_bwrap.py`, `sandbox_seatbelt.py`                         | Small  |
| P0-2 | P0       | Terminal zero path check                 | `terminal.py`, `python_repl.py`                                   | Medium |
| P0-3 | P0       | Three-gate resolve + O_NOFOLLOW + TOCTOU | `path_utils.py`, `read_file.py`, `write_file.py`, `patch_file.py` | Medium |
| P1-1 | P1       | No path validation middleware            | New `middlewares/path_guard/`                                     | Medium |
| P1-2 | P1       | ROOT_DIR leak in output + errors         | `path_utils.py`, 4 file tools                                     | Small  |
| P2-1 | P2       | Search results not filtered              | `search_files.py`                                                 | Tiny   |
| P2-2 | P2       | No URL-decode in traversal check         | `pub/func/path.py`                                                | Tiny   |

> **Note:** External path allowlist exact-match issue (originally P2 in the hardening todo) has been moved to `TODO/EXTERNAL_PATH_IMPROVEMENT_PLAN.md` as Improvement 1.

### Recommended implementation order

1. **P0-3** — highest value, smallest change, structural `..` rejection + TOCTOU elimination
2. **P1-2** — prevent ROOT_DIR leak, tiny change
3. **P2-1** — prevent symlink-escape results, tiny change
4. **P0-1** — sandbox read protection, small change
5. **P0-2** — terminal path checks, medium
6. **P1-1** — path validation middleware, medium
7. **P2-2** — URL decode, tiny

### Total impact

| Metric                   | Value                                                                                                                                                  |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| New functions            | 7 (`_is_eloop_oserror`, `_is_symlink_loop_error`, `_raise_if_symlink_loop`, `_open_no_follow`, `to_virtual_path`, `display_path`, `safe_error_detail`) |
| Files modified           | 8 (`path_utils.py`, 4 file tools, `sandbox_bwrap.py`, `sandbox_seatbelt.py`, `pub/func/path.py`)                                                       |
| Files created            | 1 (`middlewares/path_guard/`)                                                                                                                          |
| Estimated lines added    | ~120                                                                                                                                                   |
| Estimated lines modified | ~30                                                                                                                                                    |
| New dependencies         | 0                                                                                                                                                      |
| Architecture changes     | 0                                                                                                                                                      |

---

## What NOT to Migrate

| deepagents feature                    | Reason                                                                                                                                                                                                                                                                                                                              |
| ------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `BackendProtocol` abstract base       | Requires rewriting all 4 tools to delegate to a backend — architecture change, not a migration                                                                                                                                                                                                                                      |
| `CompositeBackend` prefix routing     | sherry has no multi-backend use case (no StoreBackend, no SandboxBackend)                                                                                                                                                                                                                                                           |
| `FilesystemMiddleware` tool ownership | sherry uses LangChain tools directly with its own middleware chain; replacing the tool surface is a rewrite                                                                                                                                                                                                                         |
| `StateBackend`                        | Files in graph state, not on disk — terminal/python_repl/skill tools cannot see them (split-brain). State bloat: every checkpoint serializes all "virtual files". Per-session isolation is simpler via `TEMP_DIR / session_id/` (~10 lines). deepagents' eviction pipeline (`tool_token_limit_before_evict`) not present in sherry. |
| Full virtual path namespace           | sherry's `prompt_builder`, `skill_manage`, `terminal` cwd all use real paths; switching to virtual paths would break model understanding                                                                                                                                                                                            |
| `LocalShellBackend`                   | sherry has its own terminal tool with sandbox/HITL; deepagents' shell backend is unrestricted                                                                                                                                                                                                                                       |

---

## Appendix A — deepagents `virtual_mode` Reference

deepagents' `FilesystemBackend(virtual_mode=True)` makes traversal **structurally impossible** rather than checked-after-the-fact:

1. All paths are treated as virtual absolute paths anchored to `root_dir` — `..` and `~` are rejected with `ValueError` at `_resolve_path` (`filesystem.py:205-207`), before any filesystem I/O.
2. `resolve()` + `relative_to(root)` is a second structural check — even if `..` somehow passes, the resolved path must be inside `root_dir` (`filesystem.py:208-213`).
3. All I/O uses `os.open(path, O_NOFOLLOW)` — symlink-following is blocked at the kernel level (`filesystem.py:449, 511, 551, 575`).
4. `_to_virtual_path()` converts real paths back to virtual form for model-visible output, so `root_dir` never leaks to the model (`filesystem.py:225-241`).
5. `_display_path()` wraps the conversion in a try/except, falling back to `path.name` — even a path that escapes root does not leak the real root path (`filesystem.py:243-264`).
6. In `ls`, `grep`, and `glob`, results outside root are silently skipped (`filesystem.py:362-365, 1097-1105, 1383-1411`).
7. Error messages use `_safe_detail()` which strips real paths from `OSError` strings — only `strerror` is surfaced (`filesystem.py:1201-1214`).

The key design difference: deepagents creates a **virtual path namespace** where traversal is structurally impossible by design. sherry_agent operates on **real filesystem paths** with check-after-resolve — effective but not structurally guaranteed, and vulnerable to TOCTOU, middleware bypass, and information leakage in error messages. This plan ports the structural guarantees without the virtual namespace.
