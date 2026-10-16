# 计划：写操作前确保 git 仓库就绪

- 建档日期：2026-10-15
- 状态：**计划（未开工）**
- 关联代码：`agent/tools/file_tools/write_file.py`、`agent/tools/file_tools/patch_file.py`、
  `agent/tools/terminal.py`、`agent/tools/python_repl.py`、
  `agent/tools/pub_base/path_utils.py::session_workspace_root`、
  `runtime/session/project_dir.py::read_project_dir`

---

## 0. 需求

当 agent 指定了工作目录（project directory）后：

1. **任何写操作前，先确认工作目录的 git 是否建立**——没建立则 `git init`，
   已有的复用。
2. **如果 `git init` 失败则拒绝写并通知用户**——返回错误文本，不执行写操作。
3. **如果没指定工作目录，允许写操作**——不检查、不 init。

---

## 1. 现状

### 1.1 工作目录解析

`session_workspace_root(session_id)`（`path_utils.py:129-141`）从 state register
读 session 的 project directory。返回 `Path` 或 `None`（无绑定 = 用进程默认
`ROOT_DIR`）。每个 session-aware tool 都调它。

### 1.2 写操作入口

| 工具 | 写入口 | 位置 |
|------|--------|------|
| `write_file` | `_core()` | `write_file.py:153` — `resolve_tool_path` 之后、`file_write_lock` 之前 |
| `patch_file` | `_core()` | `patch_file.py:136` — 同上 |
| `terminal` | `_run()` / `_arun()` | `terminal.py:480` — `_resolve_cwd` + `_session_writable_roots` 之后、`backend.wrap` 之前 |
| `python_repl` | `_run()` / `_arun()` | `python_repl.py:118` — `session_workspace_root` 之后、执行前 |

### 1.3 无 git 检查

当前写操作**不检查 git**——agent 可以在一个非 git 目录里写文件，没有版本
管理、没有回退能力。revert 功能（append `<revert>` AIMessage）依赖 git，
非 git 目录的 revert 会失败。

---

## 2. 设计方案

### 2.1 新增函数

新文件 `agent/tools/pub_base/git_ensure.py`：

```python
"""Ensure a git repo exists in the session's project directory before writes.

Called by every write-capable tool (write_file, patch_file, terminal,
python_repl) when the session has a bound project directory. A session
WITHOUT a bound directory is left alone — writes proceed without git.
"""

import subprocess
from pathlib import Path
from loguru import logger

from .path_utils import session_workspace_root

# Per-session cache: session_id -> True (git is ready). Cleared on project
# dir switch (the park/promote pair that changes the binding fires a
# clear). A failed init is NOT cached — the next write retries.
_git_ready: dict[str, bool] = {}


def clear_git_ready(session_id: str) -> None:
    """Clear the cached git-ready state for a session (on dir switch)."""
    _git_ready.pop(session_id, None)


def ensure_git_for_writes(session_id: str) -> tuple[bool, str]:
    """Ensure the session's project directory is a git repo.

    Returns (True, "") when:
    - The session has no bound project directory (writes proceed without git)
    - The directory is already a git repo (or inside one)
    - git init succeeds

    Returns (False, error_message) when:
    - git init fails (permission denied, disk full, read-only fs, etc.)

    Cached per session: the check runs once, subsequent writes in the same
    session + directory skip the filesystem probe. Cleared on project dir
    switch via clear_git_ready().
    """
    if not session_id:
        return True, ""  # No session = no project dir = no check

    if _git_ready.get(session_id):
        return True, ""  # Cached: already checked for this session

    root = session_workspace_root(session_id)
    if root is None:
        return True, ""  # No bound project dir = writes proceed without git

    root = Path(root)
    if not root.is_dir():
        return True, ""  # Root doesn't exist or is a file — let the write
                         # tool produce its own error about the path

    # Check: is this directory already inside a git repo?
    if _is_git_repo(root):
        _git_ready[session_id] = True
        return True, ""

    # Not a git repo → init
    ok, error = _git_init(root)
    if not ok:
        logger.error(
            "git init failed in project dir {} for session {}: {}",
            root, session_id, error,
        )
        return False, (
            f"Git initialization failed in the project directory ({root}): "
            f"{error}. Write operation refused — resolve the git issue "
            f"or clear the project directory binding to write without git."
        )

    logger.info("git initialized in project dir {} for session {}", root, session_id)
    _git_ready[session_id] = True
    return True, ""


def _is_git_repo(path: Path) -> bool:
    """True when *path* is inside a git work tree (repo root or subdirectory)."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        # git not installed or timeout — assume not a repo (let init try)
        return False


def _git_init(path: Path) -> tuple[bool, str]:
    """Run `git init` in *path*. Returns (ok, error_message)."""
    try:
        result = subprocess.run(
            ["git", "init", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return False, result.stderr.strip() or f"git init exited {result.returncode}"
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "git init timed out"
    except FileNotFoundError:
        return False, "git not found — install git or clear the project directory binding"
    except OSError as e:
        return False, str(e)
```

### 2.2 调用点

**write_file + patch_file**——在 `_core()` 开头，`resolve_tool_path` 之后：

```python
# write_file.py _core — 在 resolve_tool_path 之后加：
def _core(self, file_path, text, append=False, session_id=""):
    root, resolved, error = resolve_tool_path(file_path, session_id, "write file")
    if error is not None or resolved is None or root is None:
        return error or tool_error("Could not resolve the path")

    # NEW: ensure git before writing (only when a project dir is bound)
    ok, git_error = ensure_git_for_writes(session_id)
    if not ok:
        return tool_error(git_error)

    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        with file_write_lock(resolved):
            ...
```

`patch_file._core` 同理。

**terminal**——在 `_resolve_cwd` + `_session_writable_roots` 之后、
`backend.wrap` 之前：

```python
# terminal.py _run / _arun — 在 cwd 和 writable_roots 解析之后加：
cwd = self._resolve_cwd(bound_session)
writable_roots = self._session_writable_roots(bound_session)

# NEW: ensure git before running (only when a project dir is bound)
ok, git_error = ensure_git_for_writes(bound_session)
if not ok:
    return git_error

# 现有的 sandbox wrap
...
```

**python_repl**——在 `session_workspace_root` 之后：

```python
# python_repl.py _run / _arun — 在 session_workspace_root 之后加：
bound = session_workspace_root(resolve_tool_session_id(session_id, run_manager))

# NEW: ensure git before running (only when a project dir is bound)
ok, git_error = ensure_git_for_writes(resolve_tool_session_id(session_id, run_manager))
if not ok:
    return git_error
...
```

### 2.3 缓存清理

在 project dir 切换时清理缓存——`runtime/session/project_dir.py` 的
park/promote pair 已经处理了 thinking/model controls 的类似清理。加一个
`clear_git_ready` 调用：

```python
# runtime/session/project_dir.py — 在 promote（project dir 变更生效）时：
from agent.tools.pub_base.git_ensure import clear_git_ready

def promote_pending_project_dir(session_id: str) -> None:
    # ... 现有的 promote 逻辑 ...
    clear_git_ready(session_id)  # NEW: git check will re-run on next write
```

### 2.4 为什么 terminal 也检查

terminal 可以用 `echo > file`、`cp`、`mv` 等命令写文件。如果只在
`write_file` / `patch_file` 里检查，agent 可以绕过——用 terminal 写文件
就不触发 git init。所以 terminal 也要检查。

但 terminal 的检查对**读命令**（`ls`、`cat`、`git log`）也生效。这没问题——
`_is_git_repo` 是一个 `git rev-parse` 调用（5s 超时），`_git_ready` 缓存
后只调一次。后续命令走缓存，O(1) dict lookup。

### 2.5 为什么 python_repl 也检查

python_repl 可以用 `open(path, 'w').write(...)` 写文件。和 terminal 同理，
需要检查。

### 2.6 为什么不检查没绑定工作目录的 session

`session_workspace_root(session_id)` 返回 `None` 时，`ensure_git_for_writes`
直接返回 `(True, "")`——不检查、不 init、不拒绝。这是需求第 3 条：
"如果没指定工作目录，允许写操作"。

没绑定工作目录时，tool 解析路径用 `ROOT_DIR`（进程默认），写操作正常进行，
不强制 git。这保持了向后兼容——一个不绑定工作目录的 session 行为不变。

---

## 3. 完整执行流

```
agent 调 write_file("src/foo.py", "content", session_id="abc")
  │
  ├─ resolve_tool_path → root=/home/user/myproject, resolved=/home/user/myproject/src/foo.py
  │
  ├─ ensure_git_for_writes("abc")  ← NEW
  │    ├─ session_workspace_root("abc") → /home/user/myproject
  │    ├─ _is_git_repo(/home/user/myproject)?
  │    │    ├─ YES → cache, return (True, "")
  │    │    └─ NO → _git_init(/home/user/myproject)
  │    │         ├─ success → cache, return (True, "")
  │    │         └─ fail → return (False, "git init failed: ...")
  │    │
  │    └─ (False, error) → return tool_error("git init failed: ...")  ← 拒绝写
  │
  └─ (True, "") → 继续执行 write（mkdir + file_write_lock + atomic_write）

agent 调 terminal("echo hello > out.txt", session_id="abc")
  │
  ├─ _resolve_cwd("abc") → /home/user/myproject
  ├─ _session_writable_roots("abc") → [/home/user/myproject, TEMP_DIR]
  │
  ├─ ensure_git_for_writes("abc")  ← NEW
  │    └─ _git_ready["abc"] = True (cached) → return (True, "")  ← O(1)
  │
  └─ backend.wrap(cmd, env, writable_roots) → 执行

agent 调 write_file("foo.py", ..., session_id="")  ← 无 session 绑定
  │
  ├─ ensure_git_for_writes("") → return (True, "")  ← 无 session = 不检查
  │
  └─ 继续执行 write（用 ROOT_DIR）
```

---

## 4. 文件清单

### 新增文件（1 个）

| 文件 | 内容 |
|------|------|
| `agent/tools/pub_base/git_ensure.py` | `ensure_git_for_writes()` + `_is_git_repo()` + `_git_init()` + `_git_ready` 缓存 + `clear_git_ready()` |

### 修改文件（5 个）

| 文件 | 改动 |
|------|------|
| `agent/tools/file_tools/write_file.py` | `_core()` 在 `resolve_tool_path` 之后调 `ensure_git_for_writes` |
| `agent/tools/file_tools/patch_file.py` | `_core()` 同上 |
| `agent/tools/terminal.py` | `_run()` / `_arun()` 在 `_resolve_cwd` 之后调 `ensure_git_for_writes` |
| `agent/tools/python_repl.py` | `_run()` / `_arun()` 在 `session_workspace_root` 之后调 `ensure_git_for_writes` |
| `runtime/session/project_dir.py` | `promote_pending_project_dir` 调 `clear_git_ready`（project dir 切换时清缓存） |

### 不改的文件

| 文件 | 不改的理由 |
|------|-----------|
| `pub_base/path_utils.py` | `session_workspace_root` 不改——只读 accessor，不在这里加写检查 |
| `pub_base/sandbox.py` | sandbox 不改——git 检查在 tool 层做，不在 sandbox 层做 |
| `pub_base/shell_resolver.py` | 不相关 |
| subagent isolation | 隔离 worktree 已经是 git worktree（`git worktree add` 创建），`_is_git_repo` 返回 True，检查通过 |

---

## 5. 测试

| 测试 | 场景 |
|------|------|
| `test_ensure_git_no_project_dir` | session 无绑定 project dir → `(True, "")`，写操作正常 |
| `test_ensure_git_already_repo` | project dir 已有 `.git` → `(True, "")`，不调 `git init` |
| `test_ensure_git_init_success` | project dir 非 git → `git init` 成功 → `(True, "")` |
| `test_ensure_git_init_fail` | project dir 只读 / 无写权限 → `git init` 失败 → `(False, error)`，写操作被拒 |
| `test_ensure_git_cached` | 同一 session 第二次调用 → 走缓存，不调 `git rev-parse` |
| `test_ensure_git_cache_cleared_on_dir_switch` | project dir 切换后 → 缓存清空，重新检查 |
| `test_write_file_refused_when_git_init_fails` | `write_file` 在非 git + init 失败的目录 → 返回 error |
| `test_terminal_refused_when_git_init_fails` | `terminal` 同上 |
| `test_python_repl_refused_when_git_init_fails` | `python_repl` 同上 |
| `test_read_op_not_affected` | `read_file`、`search_files` 等读操作不受影响（不调 `ensure_git_for_writes`） |
| `test_isolated_worktree_passes` | subagent 隔离 worktree → `_is_git_repo` True → 通过 |
| `test_git_not_installed` | 系统无 git → `git init` 报 "git not found" → 拒绝写 |

---

## 6. 边界场景

| 场景 | 行为 | 理由 |
|------|------|------|
| 无 project dir 绑定 | 不检查，允许写 | 需求第 3 条 |
| project dir 已是 git repo | 复用，不 init | 需求第 1 条"已有的就复用" |
| project dir 非 git，init 成功 | init 后继续写 | 需求第 1 条"没建立则建立" |
| project dir 非 git，init 失败 | 拒绝写，返回错误 | 需求第 2 条 |
| project dir 在父级 git repo 子目录 | `_is_git_repo` 返回 True（`git rev-parse --is-inside-work-tree`） | 不嵌套 init |
| subagent 隔离 worktree | `_is_git_repo` True（worktree 是 git 工作树） | 不重复 init |
| 只读文件系统 | `git init` 失败 → 拒绝写 | init 需要写 `.git/` |
| git 未安装 | `_git_init` 报 "git not found" → 拒绝写 | 无法 init |
| 读操作（read_file / search_files） | 不调 `ensure_git_for_writes` | 只在写操作前检查 |
| terminal 跑 `ls` | 也检查（走缓存，O(1)） | 统一入口，不做命令解析区分读写 |
| project dir 切换 | `clear_git_ready` 清缓存，下次写重新检查 | 新目录可能不是 git repo |

---

## 7. 不做的事

- **不在 read 操作前检查 git**——`read_file`、`search_files`、`terminal`（跑
  `cat`/`ls`）不需要 git。只有写操作需要。但 terminal 不做命令解析区分读写——
  统一检查（走缓存，O(1)）。
- **不检查 git user 配置**——不设 `user.email` / `user.name`，让用户自己配。
  如果 git 没配 user，后续 commit 会报错——但本方案只管 init 不管 commit。
- **不做 initial commit**——只 `git init`，不自动 commit。commit 是 agent
  通过 `terminal` 调 `git commit` 的行为，不是写操作前置条件。
- **不检查 git 分支**——不关心在哪个分支，只关心是不是 git repo。
- **不在 sandbox 层做检查**——sandbox 是 terminal/python_repl 的写屏障，
  但 file tools 不走 sandbox。统一在 tool 层（`_core` / `_run`）做检查。
- **不给 `git init` 设特殊配置**——不设 `--bare`、不设 `--initial-branch`，
  用 git 默认行为。
