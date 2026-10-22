# 计划：全量替换为 shadow git，git graph 显示 shadow 快照历史

- 建档日期：2026-10-22
- 状态：**计划（未开工）**
- **文档范围**：把 sherry 所有 git 操作从"操作用户 `.git/`"替换为 shadow git（`.sherry/shadow-git/` store + `GIT_DIR`/`GIT_WORK_TREE`/`GIT_INDEX_FILE` 环境变量隔离）。git graph 面板从显示用户主 git 历史改为显示 shadow 快照历史（自动快照 + 手动恢复）。子代理 worktree 从用户 repo 的 worktree 改为基于 shadow git store 的 worktree。用户项目的 `.git/` 完全不被 sherry 碰。
- **硬约束**：用户的 `.git/` 零干扰（不 init、不 add、不 commit、不 reset、不 checkout、不 worktree）、shadow store 在 `.sherry/shadow-git/`（per-project，同文件系统）、自动快照 fail-open（快照失败不阻塞写入）、向后兼容（用户自己的 git 操作不受影响），详见 §3
- 关联代码：见 §1 全量 git 操作清单
- 关联 TODO：`计划_会话工作目录不可变.md`（目录不可变后 shadow store 路径稳定）、`计划_统一项目隐藏目录到sherry.md`（`.sherry/` 已存在，shadow-git 作为子目录加入）

---

## 0. 背景：当前 git 对用户仓库的干扰

### 0.1 sherry 当前碰用户 `.git/` 的所有操作

| 操作 | 文件 | 对用户 `.git/` 的影响 |
|---|---|---|
| `git init`（非 repo 时） | `git_ensure.py:126` / `worktree.py:85` | **在用户项目里创建 `.git/`** |
| `git add -A` + `git commit` | `worktree.py:102-106` | **在用户 repo 里创建 baseline commit**（author: sherry-isolation） |
| `git worktree add -b sherry/<branch>` | `worktree.py:171` | **在用户 repo 里创建 `sherry/<run8>` 分支** |
| `git worktree remove` + `git branch -D` | `worktree.py:186-187` | **删除用户 repo 里的分支** |
| `git stash create` | `worktree.py:144` | **在用户 repo 里创建 stash commit** |
| `git reset --hard/mixed/soft` | `git_graph_service.py:289` | **重置用户 repo 的工作区和索引** |
| `git checkout <ref>` | `git_graph_service.py:316` | **切换用户 repo 的分支** |
| `git log --all` | `git_graph_service.py:225` | 读用户 repo 的全部提交历史 |
| `git rev-parse HEAD` | `git_head.py:51` | 读用户 repo 的当前 HEAD |
| `git status --porcelain` | `git_graph_service.py:158` | 读用户 repo 的脏文件 |
| `git for-each-ref refs/heads` | `git_graph_service.py:185` | 读用户 repo 的本地分支 |
| `.git/info/exclude` 写入 | `worktree.py:89-100` | **修改用户 repo 的 exclude 文件** |
| `.sherry-isolation-repo` 标记 | `worktree.py:118-131` | 在用户项目根创建标记文件 |

**问题**：用户的 `git log` 出现 sherry 的 baseline commit；用户的 `git branch` 出现 `sherry/<run8>` 分支；用户的 `.git/info/exclude` 被追加 sherry 的 deny patterns；非 repo 的项目被 sherry 强制 `git init`。

### 0.2 shadow git 如何消除干扰

shadow git 用三个环境变量把所有操作重定向到独立 store：

```bash
GIT_DIR=$SHERRY_HOME/shadow-git/           # 对象 + 引用存这里
GIT_WORK_TREE=/home/user/myproject          # 要快照的文件在这里
GIT_INDEX_FILE=/tmp/sherry-index-<hash>     # 暂存区用临时文件
```

- `git add -A` → 写临时 index，不碰用户 `.git/index`
- `git write-tree` + `git commit-tree` → 对象存 shadow store，不碰用户 `.git/objects`
- `git update-ref refs/sherry/<hash>` → 引用存 shadow store，不碰用户 `refs/heads/`
- `git checkout <commit> -- .` → 从 shadow store 恢复文件到工作区，不碰用户分支
- 用户的 `git log` / `git status` / `git branch` 完全看不到 shadow 操作

---

## 1. 全量 git 操作清单（迁移目标）

### 1.1 要替换为 shadow git 的

| 文件 | 函数 | 当前 git 命令 | shadow 替换 |
|---|---|---|---|
| `agent/tools/pub_base/git_ensure.py` | `_is_git_repo` | `git rev-parse --is-inside-work-tree` | **删除**——shadow store 总是存在，不需要确保用户 repo |
| `agent/tools/pub_base/git_ensure.py` | `_git_init` | `git init` | **删除**——不在用户项目里 init |
| `server/service/git_graph_service.py` | `_header` | `rev-parse` + `status --porcelain` | 读 shadow store 的 HEAD + 脏文件检测 |
| `server/service/git_graph_service.py` | `_local_branches` | `for-each-ref refs/heads` | **删除**——shadow store 无用户分支 |
| `server/service/git_graph_service.py` | `read_graph` | `git log --all` | `git log refs/sherry/<hash>`（shadow 快照历史） |
| `server/service/git_graph_service.py` | `reset_to` | `git reset --<mode>` | `git checkout <commit> -- .`（从 shadow 恢复文件） |
| `server/service/git_graph_service.py` | `checkout_ref` | `git checkout <ref>` | **删除**——shadow store 无用户分支可切换 |
| `server/service/git_graph_service.py` | `read_commit_files` | `git show --name-status` | 同命令但带 shadow env |
| `server/service/git_graph_service.py` | `_blob` | `git show <revision>` | 同命令但带 shadow env |
| `server/service/git_graph_service.py` | `_parents_of` | `git rev-list --parents` | 同命令但带 shadow env |
| `agent/tools/subagent/isolation/gitrun.py` | `run_git` | 通用 git 包装器 | 加 shadow env 参数 |
| `agent/tools/subagent/isolation/worktree.py` | `ensure_repository` | `git init` + `git add -A` + `git commit` | **删除**——shadow store 已有快照 |
| `agent/tools/subagent/isolation/worktree.py` | `baseline_revision` | `git stash create` / `rev-parse HEAD` | 读 shadow store 最新快照 |
| `agent/tools/subagent/isolation/worktree.py` | `create_worktree` | `git worktree add -b sherry/<branch>` | `GIT_DIR=shadow git worktree add` |
| `agent/tools/subagent/isolation/worktree.py` | `remove_worktree` | `git worktree remove` + `branch -D` | 同命令但带 shadow env |
| `agent/tools/subagent/isolation/materialize.py` | `_is_ignored` | `git check-ignore` | 带 shadow env（或改为读 `.sherryignore`） |
| `agent/tools/subagent/isolation/materialize.py` | `copy_untracked_files` | `git status --porcelain -z` | 带影子 env |
| `agent/tools/subagent/isolation/sweep.py` | `_prune_worktrees` | `git worktree prune` | 同命令但带 shadow env |
| `runtime/session/git_head.py` | `read_git_head` | `git rev-parse HEAD --abbrev-ref HEAD` | 读 shadow store 最新快照 ref |

### 1.2 新增的（shadow 快照机制）

| 新增 | 作用 |
|---|---|
| `agent/tools/pub_base/shadow_git.py` | shadow git store 管理 + 自动快照 + 恢复 |
| shadow store 初始化 | `SHERRY_HOME/shadow-git/` 目录 + per-project 分支 `refs/sherry/<project_hash>` |

### 1.3 不变的

| 文件 | 理由 |
|---|---|
| `agent/tools/subagent/isolation/merge.py` | 纯文件级 CAS 合并，不跑 git 命令 |
| `agent/tools/subagent/isolation/tree.py` | 编排器，只调 worktree.py + materialize.py |
| `agent/tools/terminal.py` | 用户通过 terminal 跑 `git status` 是用户自己的操作——不经 shadow git |
| 客户端组件 | 调 HTTP API，API 签名不变（后端内部切换到 shadow git） |

### 1.4 gate 调用方（简化）

当前 `ensure_git_for_writes` 被 `write_file` / `patch_file` / `terminal` / `python_repl` 调用。shadow git 后：
- **`write_file` / `patch_file`**：调用 `shadow_git.ensure_checkpoint(session_id)` 替代 `ensure_git_for_writes`——写入前自动拍快照
- **`terminal` / `python_repl`**：不再调用 `ensure_git_for_writes`——shadow git 不需要用户 repo 存在。如果用户想在自己的 repo 里用 git，那是 terminal 命令的事

---

## 2. shadow git store 设计

### 2.1 存储布局

shadow store 放在项目目录的 `.sherry/shadow-git/` 下——per-project 隔离，同文件系统，不需要 project_hash：

```
项目根/
├── .sherry/                               ← 已有（计划_统一项目隐藏目录到sherry.md）
│   ├── codeintel/index.db                 ← 已有
│   ├── isolation-repo                    ← 已有
│   ├── memory/FACTS.md                    ← 已有
│   └── shadow-git/                        ← 新增：shadow store
│       ├── objects/                        ← 本项目的 commit/tree/blob 对象
│       ├── refs/heads/main                ← 快照链 HEAD（单链，不需要 per-project hash）
│       ├── index                          ← 暂存区（per-project，天然隔离）
│       ├── info/exclude                   ← 含 deny_patterns（替代写用户 .git/info/exclude）
│       └── worktrees/                     ← 子代理 worktree 注册
├── .git/                                  ← 用户的 git（完全不碰）
└── ...（项目本身的文件）
```

**为什么放 `.sherry/` 而不是 `SHERRY_HOME`：**

| 维度 | `.sherry/shadow-git/`（本方案） | `SHERRY_HOME/shadow-git/`（原方案） |
|---|---|---|
| per-project 隔离 | ✅ 天然——每个项目自己的 store | 需要 project_hash 分支隔离 |
| 同文件系统 | ✅ 天然——`GIT_DIR` 和 `GIT_WORK_TREE` 都在项目目录内 | 不保证——SHERRY_HOME 可能在不同 mount |
| 跨项目对象去重 | ❌ 不去重 | ✅ 共享 objects/ |
| project_hash 管理 | 不需要 | 需要 SHA-256 + per-project ref |
| 全局 GC | 不需要——per-project GC 即可 | 需要——全局 store 大小管理 |
| 跟着项目走 | ✅ clone = 得到快照历史 | ❌ 在 SHERRY_HOME，不跟项目走 |
| 代码复杂度 | 低——无 hash、无全局管理 | 高——hash + 全局 ref 扫描 + 跨项目 GC |

**跨项目去重价值极小**：不同项目的文件内容几乎不重叠，共享 objects 的去重收益可忽略。per-project 隔离的简洁性远大于去重收益。

### 2.2 环境变量封装

```python
# agent/tools/pub_base/shadow_git.py

SHADOW_REF = "refs/heads/main"  # 单链，不需要 per-project hash

def shadow_env(project_root: Path) -> dict[str, str]:
    """构建 shadow git 的环境变量。

    GIT_DIR 指向项目目录内的 .sherry/shadow-git/——同文件系统，
    不需要 project_hash（每个项目有自己的 store）。
    """
    store = project_root.resolve() / ".sherry" / "shadow-git"
    return {
        "GIT_DIR": str(store),
        "GIT_WORK_TREE": str(project_root.resolve()),
        "GIT_INDEX_FILE": str(store / "index"),
    }

def run_shadow(project_root: Path, args: list[str], **kw) -> subprocess.CompletedProcess:
    """在 shadow env 下跑 git 命令。"""
    env = {**os.environ, **shadow_env(project_root)}
    return subprocess.run(["git", *args], env=env, capture_output=True, text=True, **kw)
```

### 2.1a 配置上限（同 hermes `CheckpointManager` 构造参数）

```python
# config/features/agent_side/shadow_git.py

class ShadowGitConfig(TypedDict):
    enabled: bool               # 总开关，默认 True
    max_snapshots: int          # per-project 快照上限，默认 20（超了删最旧的）
    max_total_size_mb: int      # per-project store 大小上限，默认 500（超了跑 gc）
    max_file_size_mb: int       # 单文件大小上限，默认 10（超过的文件不进快照）
    git_timeout_s: float        # 单条 git 命令超时，默认 30
    gc_interval_turns: int      # 每 N 轮跑一次 gc + 清理，默认 50

SHADOW_GIT: ShadowGitConfig = {
    "enabled": True,
    "max_snapshots": 20,
    "max_total_size_mb": 500,
    "max_file_size_mb": 10,
    "git_timeout_s": 30.0,
    "gc_interval_turns": 50,
}
```

**与原方案（SHERRY_HOME 全局 store）的差异**：
- 不需要 `stale_ref_days`——per-project store 跟着项目走，不需要清理跨项目的过期 ref
- `max_total_size_mb` 是 per-project 而非全局——每个项目自己的 store 不超过 500 MB
- 不需要 `project_hash`——路径即项目身份，`GIT_DIR` 直接是 `.sherry/shadow-git/`

### 2.3 自动快照（替代 `ensure_git_for_writes`）

```python
_dedup: set[tuple[str, str]] = set()  # (session_id, project_root) per-turn 去重

def ensure_checkpoint(session_id: str, *, reason: str = "") -> bool:
    """写入前自动拍快照。fail-open——快照失败不阻塞写入。

    per-turn per-dir 去重：同一 session 同一目录同一轮只拍一次。
    """
    if not SHADOW_GIT["enabled"]:
        return True  # no-op
    root = current_project_dir(session_id)
    key = (session_id, str(root))
    if key in _dedup:
        return True  # 本轮已快照
    try:
        _take_snapshot(root, reason)
        _dedup.add(key)
        return True
    except Exception:
        logger.warning("shadow checkpoint failed for {}", root, exc_info=True)
        return False  # fail-open

def new_turn(session_id: str):
    """轮开始时重置去重集 + GC 计数（同 hermes 的 new_turn）。"""
    root = current_project_dir(session_id)
    _dedup.discard((session_id, str(root)))
    _turn_counter[session_id] = _turn_counter.get(session_id, 0) + 1
    if _turn_counter[session_id] % SHADOW_GIT["gc_interval_turns"] == 0:
        _maybe_gc(root)

def _take_snapshot(root: Path, reason: str):
    """拍一个 shadow 快照：init（首次）→ 写 exclude → add -A → write-tree → commit-tree → update-ref。

    commit message 用结构化管道分隔格式，确保 checkpoint_list 能可靠解析。
    """
    env = shadow_env(root)
    timeout = SHADOW_GIT["git_timeout_s"]

    # 首次初始化 shadow store
    store = root.resolve() / ".sherry" / "shadow-git"
    if not store.exists():
        subprocess.run(["git", "init", "-q", str(store)],
                       capture_output=True, timeout=10)
        _write_exclude(store)  # 写 deny_patterns 到 shadow store 的 info/exclude

    # 暂存全部文件（shadow env 下的 git add 不碰用户 .git/index）
    subprocess.run(["git", "add", "-A"], env=env, cwd=str(root),
                   capture_output=True, timeout=timeout)
    # 建树
    tree = subprocess.run(["git", "write-tree"], env=env, capture_output=True,
                          text=True, timeout=10).stdout.strip()
    # 父 commit（链上最新）
    parent = subprocess.run(["git", "rev-parse", "--verify", SHADOW_REF], env=env,
                           capture_output=True, text=True, timeout=5)
    parent_args = ["-p", parent.stdout.strip()] if parent.returncode == 0 else []
    # 建 commit——结构化 message（管道分隔，可机器解析 + 人类可读）
    msg = _format_checkpoint_message(reason)
    commit = subprocess.run(
        ["git", "commit-tree", tree, *parent_args, "-m", msg],
        env={**env, "GIT_AUTHOR_NAME": "sherry", "GIT_AUTHOR_EMAIL": "sherry@localhost",
             "GIT_COMMITTER_NAME": "sherry", "GIT_COMMITTER_EMAIL": "sherry@localhost"},
        capture_output=True, text=True, timeout=10
    ).stdout.strip()
    # 更新 ref
    subprocess.run(["git", "update-ref", SHADOW_REF, commit], env=env,
                   capture_output=True, timeout=5)

    # 快照后检查 per-project 上限
    _maybe_prune_snapshots(root)
```

#### commit message 格式规范

```
sherry|turn={n}|type={type}|tool={tool}|target={target}
```

| 字段 | 值域 | 示例 |
|---|---|---|
| `turn` | 整数，per-session turn 计数 | `turn=5` |
| `type` | `before_write` / `pre_restore` / `manual` | `type=before_write` |
| `tool` | 触发快照的工具名 | `tool=write_file` / `tool=patch_file` / `tool=terminal` |
| `target` | 操作目标（文件路径 / 命令 / 恢复目标 hash） | `target=src/main.py` / `target=npm test` / `target=f6e7d8a` |

**实际 commit message 示例**：

```
sherry|turn=5|type=before_write|tool=write_file|target=src/main.py
sherry|turn=5|type=before_write|tool=patch_file|target=src/app.py
sherry|turn=6|type=pre_restore|target=f6e7d8a
sherry|turn=7|type=before_write|tool=terminal|target=npm test
sherry|turn=7|type=manual|target=user requested snapshot
```

**格式构建函数**：

```python
def _format_checkpoint_message(reason: str) -> str:
    """构建结构化 commit message。

    reason 由调用方传入，格式为 "before_write:write_file:src/main.py" 或
    "pre_restore:f6e7d8a" 或 "manual:user requested"——冒号分隔的
    type:tool:target（或 type:target）。
    """
    parts = reason.split(":", 2)
    cp_type = parts[0] if parts else "unknown"
    if cp_type == "before_write" and len(parts) == 3:
        tool, target = parts[1], parts[2]
    elif cp_type == "pre_restore" and len(parts) == 2:
        tool, target = "", parts[1]
    elif cp_type == "manual":
        tool, target = "", parts[1] if len(parts) > 1 else ""
    else:
        tool, target = "", reason
    turn = _turn_counter.get(_current_session_id, 0)
    return f"sherry|turn={turn}|type={cp_type}|tool={tool}|target={target}"
```

**调用方传入的 reason 格式**：

```python
# write_file.py
shadow_git.ensure_checkpoint(session_id, reason="before_write:write_file:src/main.py")

# patch_file.py
shadow_git.ensure_checkpoint(session_id, reason="before_write:patch_file:src/app.py")

# restore_checkpoint 内部的 pre-restore
_take_snapshot(root, "pre_restore:f6e7d8a")

# 手动快照（如果将来加 manual snapshot 工具）
ensure_checkpoint(session_id, reason="manual:user requested")
```

#### `checkpoint_list` 工具返回结构化数据

`list_checkpoints` 不再返回原始 commit message 文本，而是解析为结构化字段：

```python
def list_checkpoints(session_id: str, limit: int, skip: int) -> list[dict]:
    """读 shadow 快照历史，返回结构化数据。"""
    root = current_project_dir(session_id)
    env = shadow_env(root)
    result = subprocess.run(
        ["git", "log", SHADOW_REF, "--date-order",
         f"-n{limit}", f"--skip={skip}",
         "--pretty=format:%H%x1f%h%x1f%aI%x1f%s"],
        env=env, capture_output=True, text=True, timeout=10
    )
    checkpoints = []
    for line in result.stdout.strip().split("\n"):
        if "\x1f" not in line:
            continue
        full_hash, short_hash, timestamp, message = line.split("\x1f", 3)
        # 解析结构化 message: "sherry|turn=5|type=before_write|tool=write_file|target=src/main.py"
        fields = _parse_checkpoint_message(message)
        checkpoints.append({
            "hash": short_hash,
            "full_hash": full_hash,
            "timestamp": timestamp,
            "turn": fields.get("turn", "?"),
            "type": fields.get("type", "unknown"),
            "tool": fields.get("tool", ""),
            "target": fields.get("target", ""),
            "summary": _human_summary(fields),  # 人类可读摘要给 agent 展示
        })
    return checkpoints

def _parse_checkpoint_message(msg: str) -> dict:
    """解析 'sherry|turn=5|type=before_write|tool=write_file|target=src/main.py'。"""
    if not msg.startswith("sherry|"):
        return {"type": "unknown", "raw": msg}  # 向后兼容：旧格式或不认识的 message
    parts = msg.split("|")
    fields = {}
    for part in parts[1:]:  # 跳过 "sherry"
        if "=" in part:
            k, v = part.split("=", 1)
            fields[k] = v
    return fields

def _human_summary(fields: dict) -> str:
    """生成人类可读摘要——agent 展示给用户看的。"""
    cp_type = fields.get("type", "unknown")
    if cp_type == "before_write":
        tool = fields.get("tool", "")
        target = fields.get("target", "")
        return f"before {tool} {target}".strip()
    elif cp_type == "pre_restore":
        return f"pre-restore (undo point)"
    elif cp_type == "manual":
        return f"manual: {fields.get('target', '')}"
    return fields.get("raw", "unknown checkpoint")
```

**agent 看到的 `checkpoint_list` 返回**：

```json
[
  {
    "hash": "f6e7d8a",
    "timestamp": "2026-10-22T14:30:00",
    "turn": "5",
    "type": "before_write",
    "tool": "write_file",
    "target": "src/main.py",
    "summary": "before write_file src/main.py"
  },
  {
    "hash": "e5d6c7b",
    "timestamp": "2026-10-22T14:25:00",
    "turn": "5",
    "type": "before_write",
    "tool": "patch_file",
    "target": "src/app.py",
    "summary": "before patch_file src/app.py"
  },
  {
    "hash": "d4c5b6a",
    "timestamp": "2026-10-22T14:10:00",
    "turn": "6",
    "type": "pre_restore",
    "target": "f6e7d8a",
    "summary": "pre-restore (undo point)"
  }
]
```

agent 可以精确匹配：
- 用户说"回滚到改 main.py 之前" → 匹配 `type=before_write` + `tool=write_file` + `target=src/main.py` → `hash=f6e7d8a`
- 用户说"回滚到上一轮" → 匹配最大的 `turn` 值减 1 → 找到该 turn 的第一个 `before_write`
- 用户说"撤销刚才的回滚" → 匹配 `type=pre_restore` 的最新条目

**向后兼容**：`_parse_checkpoint_message` 对不认识的格式返回 `{"type": "unknown", "raw": msg}`——旧版本的 checkpoint（如果格式变了）不会被丢弃，只是缺少结构化字段，agent 仍能看到 raw message。

def _write_exclude(store: Path):
    """把 deny_patterns 写到 shadow store 的 info/exclude。

    替代当前 worktree.py 写用户 .git/info/exclude 的行为——
    现在写到 shadow store 里，不碰用户的 .git。
    """
    exclude = store / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    header = "# sherry shadow-git exclude: secrets + bulk dirs never snapshotted"
    lines = [header, *SUBAGENT_ISOLATION["deny_patterns"]]
    exclude.write_text(
        existing + ("\n" if existing and not existing.endswith("\n") else "")
        + "\n".join(lines) + "\n",
        encoding="utf-8"
    )
```

### 2.4 恢复（替代 `git reset`，支持单文件恢复）

```python
def restore_checkpoint(
    session_id: str,
    commit: str,
    *,
    file_path: str | None = None,   # None = 恢复整个快照；指定 = 只恢复一个文件
) -> dict:
    """从 shadow 快照恢复文件到工作区。

    用 git checkout <commit> -- <target> 恢复——不碰用户 .git。
    恢复前先拍一个 pre-restore 快照（undo the undo，同 hermes）。

    file_path 为 None 时恢复整个快照（git checkout <commit> -- .）；
    指定时只恢复该文件（git checkout <commit> -- <file_path>）。
    """
    root = current_project_dir(session_id)
    # 安全网：恢复前先拍快照（同 hermes 的 pre-rollback snapshot）
    _take_snapshot(root, "pre-restore (restoring to checkpoint)")
    env = shadow_env(root)
    target = file_path if file_path else "."
    subprocess.run(
        ["git", "checkout", commit, "--", target],
        env=env, cwd=str(root), capture_output=True, timeout=30
    )
    return {"restored_to": commit, "file": file_path or "all"}

def list_restorable_files(session_id: str, commit: str) -> list[str]:
    """列出某个快照里包含的文件（git ls-tree --name-only）。"""
    root = current_project_dir(session_id)
    env = shadow_env(root)
    result = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", commit],
        env=env, capture_output=True, text=True, timeout=10
    )
    return [line for line in result.stdout.strip().split("\n") if line]
```

**单文件恢复的场景**：用户只想撤销某个文件的修改，不影响其他文件。hermes 的 `restore(file_path=...)` 支持这个——`git checkout <commit> -- <path>` 只恢复指定路径。

### 2.5 读取快照历史（替代 `git log`）

```python
def list_checkpoints(session_id: str, limit: int, skip: int) -> list[dict]:
    """读 shadow 快照历史。"""
    root = current_project_dir(session_id)
    env = shadow_env(root)
    result = subprocess.run(
        ["git", "log", SHADOW_REF, "--date-order",
         f"-n{limit}", f"--skip={skip}",
         "--pretty=format:%H%x1f%h%x1f%aI%x1f%s"],
        env=env, capture_output=True, text=True, timeout=10
    )
    # 解析...
```

### 2.6 自动 GC + 快照裁剪（per-project，同 hermes auto-pruning）

per-project store 不需要跨项目 ref 扫描——只管自己的 `refs/heads/main`。

#### 2.6.1 per-project 快照数上限（`max_snapshots`）

每次 `_take_snapshot` 后检查——超过 `max_snapshots`（默认 20）则截断 ref 到第 N 个 commit：

```python
def _maybe_prune_snapshots(root: Path):
    """快照后检查 per-project 上限，超了删最旧。"""
    env = shadow_env(root)
    result = subprocess.run(
        ["git", "rev-list", "--count", SHADOW_REF],
        env=env, capture_output=True, text=True, timeout=5
    )
    count = int(result.stdout.strip()) if result.returncode == 0 else 0
    if count <= SHADOW_GIT["max_snapshots"]:
        return
    # 把 ref 指向第 max_snapshots 个 commit（从 HEAD 往回数）
    keep = SHADOW_GIT["max_snapshots"]
    result = subprocess.run(
        ["git", "rev-list", f"--skip={count - keep}", "-n1", SHADOW_REF],
        env=env, capture_output=True, text=True, timeout=10
    )
    new_tip = result.stdout.strip()
    if new_tip:
        subprocess.run(["git", "update-ref", SHADOW_REF, new_tip],
                       env=env, capture_output=True, timeout=5)
```

#### 2.6.2 per-project 容量上限 + git gc（`max_total_size_mb`）

定时检查 store 大小——超过 `max_total_size_mb`（默认 500 MB）则跑 `git gc`：

```python
def _maybe_gc(root: Path):
    """检查 per-project store 大小，超限则跑 gc。"""
    store = root.resolve() / ".sherry" / "shadow-git"
    total = sum(f.stat().st_size for f in (store / "objects").rglob("*") if f.is_file())
    total_mb = total / (1024 * 1024)
    if total_mb <= SHADOW_GIT["max_total_size_mb"]:
        return
    logger.info("shadow git: store {}MB exceeds limit {}MB, running gc", total_mb, SHADOW_GIT["max_total_size_mb"])
    env = shadow_env(root)
    # 先裁剪快照（减少可达对象）
    _maybe_prune_snapshots(root)
    # 跑 gc 清理不可引用对象
    subprocess.run(["git", "gc", "--prune=now", "--aggressive"],
                   env=env, capture_output=True, timeout=120)
```

#### 2.6.3 定时触发

GC 不是每次快照都跑——每 `gc_interval_turns`（默认 50）轮触发一次（在 `new_turn` 里，见 §2.3）。

### 2.7 子代理 worktree 完全基于 shadow git

子代理 isolation 的 worktree 从用户 repo 切换到 shadow store——不再碰用户的 `.git/`。

#### 2.7.1 当前 worktree 流程（碰用户 .git）

```
ensure_repository(project_root)     → git init + git add -A + git commit（用户 repo）
baseline_revision(project_root)     → git stash create / git rev-parse HEAD（用户 repo）
create_worktree(project_root, tree) → git worktree add -b sherry/<branch>（用户 repo）
remove_worktree(project_root, tree) → git worktree remove + branch -D（用户 repo）
```

#### 2.7.2 shadow git 后的 worktree 流程

```
ensure_checkpoint(session_id)                → shadow store 已有最新快照（§2.3）
create_worktree(project_root, tree, child_key)
  → env = shadow_env(project_root)
  → git worktree prune           （清理 shadow store 的过期 worktree 注册）
  → git branch -D sherry/<child>  （清理残留分支）
  → git worktree add -b sherry/<child> <tree_path> <shadow_HEAD>
    ↑ base commit 来自 shadow store 的 refs/heads/main（最新快照）
    ↑ worktree 注册在 .sherry/shadow-git/worktrees/
    ↑ 工作树在 SHERRY_HOME/src/data/isolated/<child_key>/tree/（不在项目目录）
remove_worktree(project_root, tree, branch)
  → env = shadow_env(project_root)
  → git worktree remove --force <tree_path>  （shadow env）
  → git branch -D sherry/<child>             （shadow env）
  → git worktree prune                        （shadow env）
```

**关键变化**：
- `ensure_repository` **删除**——shadow store 在首次 `_take_snapshot` 时 `git init`，不需要在用户项目里 init
- `baseline_revision` **删除**——shadow store 的 `refs/heads/main` 就是 baseline（最新快照）
- `create_worktree` 用 `shadow_env`——worktree 注册在 `.sherry/shadow-git/worktrees/`，不在用户 `.git/worktrees/`
- `remove_worktree` 用 `shadow_env`——只删 shadow store 的分支和 worktree
- `.git/info/exclude` 写入 **删除**——deny_patterns 改写到 `.sherry/shadow-git/info/exclude`（`_write_exclude`，§2.3）
- `.sherry-isolation-repo` 标记 **删除**——不再 init 用户 repo，不需要标记

#### 2.7.3 worktree 的 deny_patterns

当前 `ensure_repository` 在用户 `.git/info/exclude` 里写 deny_patterns（`.env`/`.models`/密钥等），防止 baseline commit 包含密钥。

shadow git 后，deny_patterns 写到 `.sherry/shadow-git/info/exclude`（`_write_exclude` 函数，§2.3）——shadow store 的 exclude，不碰用户的 `.git/info/exclude`。密钥不会进 shadow 快照。

#### 2.7.4 materialize.py 的 git 命令

`_is_ignored`（`git check-ignore`）和 `copy_untracked_files`（`git status --porcelain`）改为 shadow env：

```python
def _is_ignored(project_root: Path, relpath: str) -> bool:
    env = shadow_env(project_root)
    result = subprocess.run(
        ["git", "check-ignore", "-q", "--", relpath],
        env=env, cwd=str(project_root), capture_output=True, timeout=5
    )
    return result.returncode == 0
```

注意：`_is_ignored` 检查的是 shadow store 的 exclude 规则（deny_patterns），不是用户 `.gitignore` 的规则。这是**有意的行为变化**：当前 `_is_ignored` 检查用户 `.gitignore`，改为检查 shadow store 的 deny_patterns——即检查"sherry 是否应该快照这个文件"，不是"用户的 git 是否忽略这个文件"。用户自己的 `.gitignore` 与 shadow git 无关。

### 2.8 enabled 开关

`SHADOW_GIT["enabled"] = False` 时：
- `ensure_checkpoint` 直接返回 `True`（no-op，不拍快照）
- `restore_checkpoint` 抛 `RuntimeError("shadow git is disabled")`
- `list_checkpoints` 返回空列表
- shadow store 不初始化
- `write_file` / `patch_file` 正常工作（快照是 fail-open 的，没有快照不阻塞写入）
- git graph 面板显示"shadow git 未启用"

---

## 3. 硬约束

| 约束 | 含义 |
|---|---|
| **C1**：用户 `.git/` 零干扰 | sherry 不在用户项目里 init/add/commit/reset/checkout/worktree——全部走 shadow env |
| **C2**：shadow store 在 `.sherry/shadow-git/` | per-project 隔离，同文件系统，不需要 project_hash |
| **C3**：自动快照 fail-open | 快照失败不阻塞写入——记录 warning，继续写入 |
| **C4**：per-turn per-dir 去重 | 同一 session 同一目录同一轮只拍一次快照 |
| **C5**：恢复带安全网 | 恢复前先拍 pre-restore 快照——undo the undo |
| **C6**：用户 git 操作不受影响 | 用户通过 terminal 跑 `git status`/`git commit` 是用户自己的事——不经 shadow git |
| **C7**：非 git 项目也可快照 | shadow git 不需要用户项目是 git repo——`GIT_WORK_TREE` 指向项目目录即可 |
| **C8**：shadow store 在项目 `.sherry/` 下 | 不在 `SHERRY_HOME`——跟着项目走，clone = 得到快照历史 |
| **C9**：HTTP API 签名不变 | `GET /git/graph` 等端点签名不变——后端内部切换到 shadow git，前端零改动 |
| **C10**：配置上限 | per-project `max_snapshots=20`、`max_total_size_mb=500`、单文件 `max_file_size_mb=10`——同 hermes 默认值 |
| **C11**：per-project GC | 每 `gc_interval_turns=50` 轮检查 store 大小，超限跑 `git gc --prune=now` + 裁剪最旧快照 |
| **C12**：单文件恢复 | `restore(file_path=...)` 支持只恢复一个文件——同 hermes |
| **C13**：enabled 开关 | `SHADOW_GIT["enabled"]=False` 完全关闭快照——fail-open 不阻塞写入 |
| **C14**：worktree 完全基于 shadow git | `create_worktree`/`remove_worktree` 用 shadow env——worktree 注册在 `.sherry/shadow-git/worktrees/`，不碰用户 `.git/worktrees/` |
| **C15**：deny_patterns 写 shadow store | 密钥/大目录排除规则写到 `.sherry/shadow-git/info/exclude`，不写用户 `.git/info/exclude` |

---

## 4. 改动清单

### 4.1 新建 `agent/tools/pub_base/shadow_git.py`

核心模块，~300 行（含全部 hermes 机制）：
- `shadow_env(project_root)` —— 环境变量构建（§2.2）
- `run_shadow(project_root, args)` —— shadow env 下跑 git
- `project_hash(root)` —— SHA-256[:12]
- `ensure_checkpoint(session_id, reason)` —— 自动快照 + per-turn per-dir 去重 + fail-open（§2.3）
- `new_turn(session_id)` —— per-turn 去重重置 + GC 计数（§2.3 + §2.6.4）
- `_take_snapshot(root, phash, reason)` —— 拍快照：add → write-tree → commit-tree → update-ref（§2.3）
- `restore_checkpoint(session_id, commit, file_path=None)` —— 恢复 + pre-restore 安全网 + 单文件支持（§2.4）
- `list_restorable_files(session_id, commit)` —— 列快照内文件（§2.4）
- `list_checkpoints(session_id, limit, skip)` —— 读快照历史（§2.5）
- `read_commit_files(session_id, commit)` / `read_commit_diff(session_id, commit, path)` —— 读 commit 详情
- `init_shadow_store()` —— boot 时初始化（§4.9，首次快照时懒创建）

### 4.1b 新增 agent 工具：`checkpoint`（三合一，action 参数）

一个工具三个 action——同 `memory(action=add/replace/remove)` 模式。`scope: main_only`，`concurrent_safe: False`（restore 是写，整个工具是 barrier）。

```
name: checkpoint
description: |
  Shadow git checkpoint management — automatic snapshots taken before each
  file modification (write_file, patch_file). Three actions:

  list    — List recent checkpoints with structured metadata (hash, turn,
            type, tool, target, timestamp). Use when user says "undo" /
            "回滚" / "撤销" / "revert" / "rollback" / "之前的版本" /
            "回到之前" / "看看历史".

  show    — Show what a checkpoint changed. Without file_path: list changed
            files with insertions/deletions. With file_path: show the diff.
            Use when user asks "改了什么" / "what changed" / "看看这个快照".

  restore — Restore files from a checkpoint. Automatically takes a
            pre-restore snapshot first (undo the undo). Checks for
            conflicts: if files were externally modified since the
            checkpoint, refuses unless force=true. Without commit_hash:
            undo last change. With file_path: restore only that file.
            Use when user says "回滚到..." / "恢复到..." / "undo".

args:
  action: "list" | "show" | "restore"  (required)
  commit_hash: checkpoint hash from list action (required for show/restore)
  file_path: optional — show: diff one file; restore: restore one file only
  force: optional — restore: overwrite externally modified files (default false)
  limit: optional — list: number of checkpoints (default 10, max 50)
```

| action | 调用 | 返回 |
|---|---|---|
| `list` | `checkpoint(action="list", limit=10)` | `[{hash, turn, type, tool, target, timestamp, summary}]` |
| `show` | `checkpoint(action="show", commit_hash="f6e7d8a")` | `{changed_files: [{path, status, +insertions, -deletions}]}` |
| `show` (带 file) | `checkpoint(action="show", commit_hash="f6e7d8a", file_path="src/main.py")` | `{file, diff: "@@ ..."}` |
| `restore` | `checkpoint(action="restore", commit_hash="f6e7d8a")` | `{restored_to, pre_restore_hash}` |
| `restore` (带 file) | `checkpoint(action="restore", commit_hash="f6e7d8a", file_path="src/main.py")` | `{restored_to, file}` |
| `restore` (省略 hash) | `checkpoint(action="restore")` | `{restored_to}` — 撤销上一次修改 |
| `restore` (force) | `checkpoint(action="restore", commit_hash="f6e7d8a", force=true)` | `{restored_to, forced: true}` |

以下章节（`checkpoint_list` 工具 / `checkpoint_restore` 工具 / 冲突检测 / 恢复预览 / `checkpoint_show`）描述的是各 action 的行为细节——它们现在是同一个工具的 action，不再是独立工具。

#### `checkpoint_list` 工具（已合并为 action=list）

```python
# agent/tools/checkpoint_tools.py

def build_checkpoint_tools():
    """构建快照回滚工具集。main_only——子代理不能回滚主会话的快照。"""
    return [
        checkpoint_list_tool,
        checkpoint_restore_tool,
    ]
```

工具描述（模型看到这段决定何时调用）：

```
name: checkpoint_list
description: |
  List recent shadow git checkpoints (automatic snapshots taken before each
  file modification). Use when the user asks to undo, rollback, revert, or
  see previous states — e.g. "undo", "回滚", "撤销", "revert", "rollback",
  "restore to before", "回到之前". Each checkpoint shows a short hash,
  timestamp, and the reason it was taken (e.g. "before write_file src/main.py").
args:
  limit: number of checkpoints to list (default 10, max 50)
```

返回示例：
```
Recent checkpoints (newest first):
  f6e7d8a  2026-10-22 14:30  before write_file src/main.py
  e5d6c7b  2026-10-22 14:25  before patch_file app.py
  d4c5b6a  2026-10-22 14:10  before terminal: npm test
  c3b4a5f  2026-10-22 13:50  pre-restore (restoring to checkpoint)
  b2a3c4d  2026-10-22 13:45  before write_file test.py
```

#### `checkpoint_restore` 工具

```
name: checkpoint_restore
description: |
  Restore files from a shadow git checkpoint. A pre-restore snapshot is taken
  automatically first (undo the undo). Call checkpoint_list first to see
  available checkpoints and their hashes.

  Before restoring, checks for conflicts: if a file was modified externally
  (user edited in IDE, terminal command, etc.) since the checkpoint, the
  restore is REFUSED with a conflict list. The user must confirm force=true
  to overwrite external changes.

  If file_path is specified, only that file is restored (others untouched).
  If commit_hash is omitted, restores to the most recent checkpoint (undo last change).
args:
  commit_hash: checkpoint hash from checkpoint_list (omit to undo last change)
  file_path: optional — restore only this file (relative to project root)
  force: optional — if true, overwrite files that were externally modified
         since the checkpoint (default false — refuse with conflict list)
```

#### 对话回滚流程

```
用户: "刚才改 main.py 改错了，回滚回去"
  ↓ agent 判断是回滚请求（工具描述里的触发词匹配）
  ↓ agent 调 checkpoint_list(limit=5)
  ↓ 看到: f6e7d8a "before write_file src/main.py"  ← 这个就是改之前的快照
  ↓ agent 调 checkpoint_restore(commit_hash="f6e7d8a")
  ↓ shadow_git.restore_checkpoint(session_id, "f6e7d8a")
  ↓   → 先拍 pre-restore 快照（undo the undo）
  ↓   → git checkout f6e7d8a -- .  （shadow env，恢复文件）
  ↓ agent 告诉用户: "已恢复到修改 main.py 之前的状态（快照 f6e7d8a）"

用户: "不对，只恢复 main.py，其他文件保留"
  ↓ agent 调 checkpoint_restore(commit_hash="f6e7d8a", file_path="src/main.py")
  ↓ shadow_git.restore_checkpoint(session_id, "f6e7d8a", file_path="src/main.py")
  ↓   → git checkout f6e7d8a -- src/main.py  （只恢复一个文件）

用户: "算了，把刚才的恢复也撤销"
  ↓ agent 调 checkpoint_list(limit=5)
  ↓ 看到: 最新的快照是 "pre-restore (restoring to checkpoint)"
  ↓ agent 调 checkpoint_restore(commit_hash=<pre-restore 的 hash>)
  ↓   → 回到恢复之前的状态
```

#### 工具注册

- 加到 `agent/tools/catalog.py` 的 `TOOL_GROUPS`——新 group `"checkpoint"` 或归入 `"file"` group
- 加到 `agent/core.py::build_main_tools()` 构建列表
- `metadata["scope"] = "main_only"`——子代理不能回滚主会话的快照
- `metadata["concurrent_safe"] = False`——回滚是写操作，是 batch barrier（不能与其他写并行）；`checkpoint_list` 和 `checkpoint_show` 是只读，`concurrent_safe = True`
- 加到 `agent/tools/parallel_safety.py` 的 unsafe 列表（仅 `checkpoint_restore`）

#### `checkpoint_show` 工具——查看快照改了什么内容

让 agent 能看某个快照具体改了什么——对标 `git show <commit>`：

```
name: checkpoint_show
description: |
  Show what a checkpoint changed — which files were modified, added, or deleted,
  and optionally the diff content of a specific file. Use when the user asks
  "what did this checkpoint change" / "改了什么" / "看看这个快照" or when you
  need to verify which checkpoint to restore to. Call checkpoint_list first to
  get the hash.
args:
  commit_hash: checkpoint hash from checkpoint_list
  file_path: optional — show diff of only this file (omit to list all changed files)
```

**无 file_path**——列出快照改了哪些文件（`git show --name-status --stat`）：

```python
def checkpoint_show(session_id: str, commit: str, file_path: str | None = None) -> dict:
    root = current_project_dir(session_id)
    env = shadow_env(root)

    if file_path is None:
        # 列出改了哪些文件
        result = run_shadow(root, ["git", "show", "--no-patch", "--name-status", "--stat", commit])
        # 解析为 [{path, status: modified/added/deleted, insertions, deletions}]
        ...
    else:
        # 看某个文件的 diff 内容
        result = run_shadow(root, ["git", "show", commit, "--", file_path])
        # 返回 unified diff 文本
        ...
```

**agent 看到的返回**（无 file_path）：

```json
{
  "hash": "f6e7d8a",
  "message": "before write_file src/main.py",
  "changed_files": [
    {"path": "src/main.py", "status": "modified", "insertions": 15, "deletions": 3},
    {"path": "src/utils.py", "status": "modified", "insertions": 2, "deletions": 8},
    {"path": "src/new.py", "status": "added", "insertions": 20, "deletions": 0}
  ]
}
```

**agent 看到的返回**（有 file_path）：

```json
{
  "hash": "f6e7d8a",
  "file": "src/main.py",
  "diff": "@@ -10,7 +10,22 @@\n def old_func():\n-    return None\n+    return new_value\n..."
}
```

**对话场景**：

```
用户: "刚才改了什么？"
  ↓ checkpoint_list → 最新快照 f6e7d8a "before write_file src/main.py"
  ↓ checkpoint_show("f6e7d8a")
  ↓ agent: "这个快照相比上一个改了 3 个文件：
    src/main.py  修改  (+15 -3)
    src/utils.py 修改  (+2 -8)
    src/new.py   新增   (+20)
    要看哪个文件的详细 diff？"

用户: "看 main.py 的"
  ↓ checkpoint_show("f6e7d8a", file_path="src/main.py")
  ↓ agent: [展示 diff]

用户: "回滚到这个快照"
  ↓ checkpoint_restore("f6e7d8a")
```

`checkpoint_show` 是只读（`concurrent_safe = True`），可以与其他读操作并行——不影响 `checkpoint_restore` 的 barrier 语义。

#### HITL

`checkpoint_restore` 应该经过 HITL 审批——回滚会覆盖文件，是不可逆操作（虽然有 pre-restore 快照）。可以复用 `FirstCallConfirmationHandler`（每个 session 首次使用时确认一次），或者每次都弹审批卡片。

第一版建议：**不加 HITL**——回滚是用户主动要求的，agent 只是在执行用户的意图。而且有 pre-restore 快照作为安全网。如果用户后悔了，可以再回滚回来。

#### 2.8b 冲突检测（同 ZCode 的三方比对）

恢复前检查每个文件的实际内容是否与"恢复目标"快照一致——如果不一致，说明文件被外部改过（用户在 IDE 里手动改了、terminal 命令改了、另一个 session 改了），**拒绝恢复**并返回冲突列表。

```python
def check_conflicts(root: Path, from_commit: str) -> list[dict]:
    """三方比对：检查恢复是否安全。

    from_commit = 要恢复到的快照
    对比每个文件的 on-disk blob hash vs from_commit 的 tree blob hash。
    如果文件在快照之后被外部修改过（on-disk ≠ from_commit），返回冲突。
    """
    env = shadow_env(root)
    # from_commit 快照里的所有文件
    result = subprocess.run(
        ["git", "ls-tree", "-r", "--format=%(path)%1f%(objectname)", from_commit],
        env=env, capture_output=True, text=True, timeout=10
    )
    conflicts = []
    for line in result.stdout.strip().split("\n"):
        if "\x1f" not in line:
            continue
        path, blob_hash = line.split("\x1f", 1)
        file_path = root / path
        if not file_path.is_file():
            conflicts.append({"path": path, "type": "missing-in-worktree"})
            continue
        # 计算 on-disk 文件的 blob hash
        on_disk = subprocess.run(
            ["git", "hash-object", "--no-filters", str(file_path)],
            env=env, capture_output=True, text=True, timeout=5
        ).stdout.strip()
        if on_disk != blob_hash:
            conflicts.append({
                "path": path,
                "type": "content-mismatch",
                "checkpoint_hash": blob_hash[:8],
                "on_disk_hash": on_disk[:8],
            })
    # 检查 worktree 里多出来的文件（快照里没有的）
    # ... (可选：git status --porcelain 在 shadow env 下检测 untracked)
    return conflicts

def restore_checkpoint_safe(
    session_id: str, commit: str, *, file_path: str | None = None, force: bool = False
) -> dict:
    """带冲突检测的恢复。同 ZCode 的 restoreBetweenCheckpoints。"""
    root = current_project_dir(session_id)
    if not force:
        conflicts = check_conflicts(root, commit)
        if conflicts:
            return {
                "restored": False,
                "conflicts": conflicts,
                "hint": "Files were modified externally since this checkpoint. "
                        "Pass force=true to overwrite, or restore specific files only.",
            }
    # 无冲突（或 force）→ 正常恢复
    return restore_checkpoint(session_id, commit, file_path=file_path)
```

`checkpoint_restore` 工具的 `commit_hash` 参数加 `force: bool = False`——有冲突时 agent 告诉用户哪些文件被外部改了，用户决定是否强制覆盖。

#### 2.8c 恢复预览（同 ZCode 的 rewind preview）

恢复前展示"哪些文件会变、怎么变"——让用户确认再执行：

```python
def preview_restore(session_id: str, commit: str) -> dict:
    """预览恢复会改变哪些文件。同 ZCode 的 conversationFileRewindPreview。"""
    root = current_project_dir(session_id)
    env = shadow_env(root)
    # 当前最新快照 vs 目标快照的 diff
    result = subprocess.run(
        ["git", "diff", "--name-status", "--stat", commit, "HEAD"],
        env=env, capture_output=True, text=True, timeout=10
    )
    # 解析为 [{path, status: modified/added/deleted, changes: "+10 -5"}]
    ...
```

`checkpoint_restore` 工具先调 `preview_restore` 把变更列表给用户看，用户确认后再恢复。agent 可以在对话里展示：

```
Agent: 恢复到 "before write_file src/main.py" 会改变以下文件：
  src/main.py    修改  (+10 -5 行)
  src/utils.py   修改  (+3 -2 行)
  src/new.py     删除
确认要恢复吗？

User: 确认

Agent: [调 checkpoint_restore 执行恢复]
```
- `_maybe_prune_project(phash)` —— per-project 快照数上限裁剪（§2.6.1）
- `_prune_stale_refs()` —— 过期 ref 清理（§2.6.2）
- `_maybe_gc()` —— 全局容量上限 + git gc（§2.6.3）
- `_ensure_size_exclude(exclude_path)` —— 大文件排除占位（§2.3，第一版靠 GC 兜底）

### 4.1a 新建 `config/features/agent_side/shadow_git.py`

配置文件，~15 行：
- `ShadowGitConfig` TypedDict + `SHADOW_GIT` 实例（§2.1a）
- 字段：`enabled` / `max_snapshots` / `max_total_size_mb` / `max_file_size_mb` / `git_timeout_s` / `gc_interval_turns` / `stale_ref_days`
- 在 `config/features/__init__.py` 注册导出

### 4.2 替换 `agent/tools/pub_base/git_ensure.py`

- **删除** `ensure_git_for_writes` / `_is_git_repo` / `_git_init` / `_git_ready` 缓存
- `write_file.py` / `patch_file.py` 的调用从 `ensure_git_for_writes(session_id)` 改为 `shadow_git.ensure_checkpoint(session_id, reason="before write_file")`
- `terminal.py` / `python_repl.py` 的调用删除——不再需要确保用户 repo 存在

### 4.3 替换 `server/service/git_graph_service.py`

所有函数从 `subprocess.run(["git", ...], cwd=root)` 改为 `shadow_git.run_shadow(root, [...])` 或调 `shadow_git` 的封装函数：

| 原函数 | 替换 |
|---|---|
| `_header(root)` | 读 shadow ref HEAD + `git status --porcelain`（shadow env） |
| `_local_branches(root)` | **删除**——shadow store 无用户分支 |
| `read_graph(session_id, limit, skip)` | `shadow_git.list_checkpoints(session_id, limit, skip)` |
| `reset_to(session_id, target, mode)` | `shadow_git.restore_checkpoint(session_id, target)`——`mode` 参数保留（soft = 只恢复索引, mixed = 恢复索引+工作区, hard = 恢复全部——用 shadow env 的 `git checkout` 实现） |
| `checkout_ref(session_id, ref)` | **删除**——无用户分支可切换 |
| `read_commit_files(session_id, hash)` | `shadow_git.read_commit_files(session_id, hash)` |
| `read_commit_diff(session_id, hash, path)` | `shadow_git.read_commit_diff(session_id, hash, path)` |
| `_blob(root, revision)` | shadow env 下的 `git show <revision>` |
| `_parents_of(root, hash)` | shadow env 下的 `git rev-list --parents -n1` |

### 4.4 替换 `agent/tools/subagent/isolation/gitrun.py`

`run_git(cwd, args)` 加 `shadow: bool = True` 参数——默认走 shadow env：

```python
def run_git(cwd, args, *, shadow=True, timeout_s=None, check=False):
    env = shadow_env(Path(cwd)) if shadow else None
    full_env = {**os.environ, **env} if env else os.environ
    return subprocess.run(["git", *args], env=full_env, cwd=str(cwd),
                          capture_output=True, text=True, timeout=timeout_s or DEFAULT_TIMEOUT)
```

### 4.5 替换 `agent/tools/subagent/isolation/worktree.py`

详细流程见 §2.7（子代理 worktree 完全基于 shadow git）。

| 原函数 | 替换 |
|---|---|
| `ensure_repository` | **删除**——shadow store 在首次 `_take_snapshot` 时 `git init`（§2.3） |
| `baseline_revision` | **删除**——shadow store 的 `refs/heads/main` 就是 baseline |
| `create_worktree` | shadow env 下 `git worktree add -b sherry/<child> <tree> <shadow_HEAD>`（§2.7.2） |
| `remove_worktree` | shadow env 下 `git worktree remove --force` + `git branch -D` + `git worktree prune`（§2.7.2） |
| `_repo_toplevel` | **删除**——不需要探测用户 repo |
| `.git/info/exclude` 写入 | **删除**——deny_patterns 改写到 `.sherry/shadow-git/info/exclude`（`_write_exclude`，§2.3） |
| `.sherry-isolation-repo` 标记 | **删除**——不再 init 用户 repo |

### 4.6 替换 `runtime/session/git_head.py`

`read_git_head(directory)` 改为读 shadow store 的最新快照 ref：

```python
def read_git_head(directory):
    env = shadow_env(Path(directory))
    result = subprocess.run(
        ["git", "rev-parse", "--short", SHADOW_REF],  # refs/heads/main
        env=env, capture_output=True, text=True, timeout=GIT_GRAPH["timeout_s"])
    if result.returncode != 0:
        return None
    short_hash = result.stdout.strip()[:8]
    return f"checkpoint@{short_hash}"
```

### 4.7 替换 `agent/tools/subagent/isolation/materialize.py`

`_is_ignored` 和 `copy_untracked_files` 改为 shadow env 下的 git 命令。

### 4.8 替换 `agent/tools/subagent/isolation/sweep.py`

`_prune_worktrees` 改为 shadow env 下的 `git worktree prune`。

### 4.9 boot 初始化

`server/__main__.py` 加 `shadow_git.init_shadow_store()`——确保 `SHADOW_GIT_DIR` 目录存在。

### 4.10 per-turn 快照去重重置

`WorkspaceNoticeMiddleware.abefore_agent` 或 `DynamicReminderMiddleware.abefore_model` 首调时调 `shadow_git.new_turn(session_id)`——重置本轮去重集。

### 4.11 删除的文件/代码

| 删除 | 理由 |
|---|---|
| `git_ensure.py` 整个文件 | shadow git 不需要确保用户 repo 存在 |
| `worktree.py::ensure_repository` | 不在用户项目里 init |
| `worktree.py` 的 `.git/info/exclude` 写入 | 不碰用户 repo 的 exclude |
| `worktree.py` 的 `.sherry-isolation-repo` 标记 | 不在用户项目里 init |
| `git_graph_service.py::_local_branches` | shadow store 无用户分支 |
| `git_graph_service.py::checkout_ref` | 无用户分支可切换 |
| `http/git.py` 的 `POST /git/checkout` 路由 | 无用户分支可切换 |

### 4.12 客户端

- `GitGraphPanel.vue`——API 签名不变（`GET /git/graph` 返回快照历史）；`POST /git/checkout` 按钮删除（无分支切换）；分支名显示从用户分支改为 "checkpoint"
- `bridge/git.ts`——删除 `checkoutGitRef` 函数
- `GitDiffPanel.vue`——不变（`GET /git/commit/file` 签名不变）

---

## 5. git graph 面板的变化

### 5.1 当前（用户主 git）

```
分支: main
HEAD: a1b2c3d "feat: add login page"
┌──────────────────────────────────────────────┐
│ ● a1b2c3d  Alice  2026-10-20  feat: add login  │
│ ● b2c3d4e  Alice  2026-10-19  fix: header bug  │
│ ● c3d4e5f  Alice  2026-10-18  init project     │
└──────────────────────────────────────────────┘
[回退 ▾] [切换分支 ▾]
```

### 5.2 改动后（shadow 快照历史）

```
快照链: checkpoint
最新: f6e7d8a "sherry checkpoint: before write_file src/main.py"
┌──────────────────────────────────────────────────────────────┐
│ ● f6e7d8a  sherry  2026-10-22 14:30  before write_file main.py │
│ ● e5d6c7b  sherry  2026-10-22 14:25  before patch_file app.py  │
│ ● d4c5b6a  sherry  2026-10-22 14:10  before terminal: npm test │
│ ● c3b4a5f  sherry  2026-10-22 13:50  pre-restore (undo)        │
│ ● b2a3c4d  sherry  2026-10-22 13:45  before write_file test.py │
└──────────────────────────────────────────────────────────────┘
[恢复到此快照 ▾]
```

- 每条是一个自动快照（before write_file / before patch_file / before terminal）
- "恢复到此快照"替代"回退"——从 shadow 快照恢复文件，不碰用户 git
- 没有"切换分支"——shadow store 只有快照链，没有用户分支
- `pre-restore` 快照让用户可以 undo the undo

---

## 6. 改动量

| 类别 | 文件 | 估算 |
|---|---|---|
| 新建 shadow_git.py（含全部 hermes 机制） | 1 文件 | ~300 行 |
| 新建 config/features/agent_side/shadow_git.py | 1 文件 | ~15 行 |
| 新建 checkpoint_tools.py（agent 工具 + 冲突检测 + 预览） | 1 文件 | ~150 行 |
| 工具注册（catalog + core + parallel_safety） | 3 文件 | ~15 行 |
| 替换 git_ensure.py | 1 文件（删除） + 4 调用方 | 删除 ~100 行，改 ~20 行 |
| 替换 git_graph_service.py | 1 文件 | 改 ~150 行 |
| 替换 isolation/gitrun.py | 1 文件 | 改 ~10 行 |
| 替换 isolation/worktree.py | 1 文件 | 删除 ~80 行，改 ~40 行 |
| 替换 git_head.py | 1 文件 | 改 ~15 行 |
| 替换 materialize.py + sweep.py | 2 文件 | 改 ~20 行 |
| boot 初始化 | 1 文件 | 加 ~5 行 |
| 客户端（删 checkout 按钮 + bridge） | 2 文件 | 删 ~30 行 |
| 测试 | ~12 文件 | ~300 行 |
| **总计** | | **~730 行新增，~250 行删除** |

净增 ~480 行，但消除全部对用户 `.git/` 的干扰 + 获得完整回滚能力（自动快照 + 对话回滚 + 冲突检测 + 恢复预览 + 单文件恢复 + undo the undo + GC + 容量上限 + worktree 基于 shadow git）——对标 ZCode 的 rewind 能力。

---

## 7. 不做的事（边界）

| 项 | 不做的原因 |
|---|---|
| 三方冲突检测（ZCode 模式） | 第一版不做——恢复时直接 `git checkout <commit> -- .`，用户自己确认 |
| 内容级 turn 追踪（ZCode `FileSnapshot`） | shadow git 的 commit 历史已经是 per-turn 快照——不需要额外内容追踪 |
| 快照预览（ZCode rewind preview） | git graph 面板已显示 commit 列表——点击查看 diff即够 |
| 修改 terminal 工具的 git 行为 | 用户通过 terminal 跑 `git status`/`git commit` 是用户自己的操作——不经 shadow git |
| 在项目目录建 shadow store | shadow store 在 `SHERRY_HOME/shadow-git/`——不污染项目目录 |
| 修改 `merge.py` | 纯文件级 CAS 合并，不跑 git 命令——不变 |
| 修改客户端 diff 面板 | `GET /git/commit/file` 签名不变——后端内部切换 |

---

## 8. 执行顺序

```
§4.1 新建 shadow_git.py（基础设施）
  ├─ shadow_env / run_shadow
  ├─ ensure_checkpoint / new_turn（自动快照）
  ├─ restore_checkpoint（恢复 + 单文件 + pre-restore 安全网）
  ├─ list_checkpoints / read_commit_files / read_commit_diff（读取）
  ├─ _maybe_prune_snapshots / _maybe_gc（清理 + GC）
  └─ _write_exclude（deny_patterns 写 shadow store）
  ↓
§4.1a 新建 config/features/agent_side/shadow_git.py
  ↓
§4.1b 新建 checkpoint_tools.py（agent 工具——对话回滚能力）
  ├─ checkpoint_list（列快照，agent 可看）
  ├─ checkpoint_restore（恢复快照，agent 可调）
  └─ 工具注册（catalog + core + parallel_safety）
  ↓（自动快照 + 对话回滚已可用）
§4.2 替换 git_ensure.py + write_file/patch_file 调用方
  ↓（自动快照接入 write 工具）
§4.3 替换 git_graph_service.py
  ↓（git graph 面板显示 shadow 快照）
§4.4-4.5 替换 isolation（gitrun + worktree）—— §2.7 详述
  ↓（子代理隔离走 shadow）
§4.6 替换 git_head.py
  ↓（git HEAD 通知走 shadow）
§4.7-4.8 替换 materialize + sweep
§4.9 boot 初始化
§4.10 per-turn 去重重置
§4.11-4.12 删除旧代码 + 客户端
```

**关键路径**：shadow_git.py → checkpoint_tools.py（对话回滚可用）→ git_ensure 替换（自动快照可用）→ git_graph 替换（面板可用）→ isolation 替换（子代理可用）。前三步完成后用户已能通过对话说"回滚"让 agent 调 checkpoint_restore。

---

## 9. 关键参考

### hermes 参照（shadow git 设计来源）

- `D:\selfProj\hermes-agent\tools\checkpoint_manager.py:608`（`CheckpointManager`——shadow git store + 自动快照 + 恢复）
- hermes 的 shadow store：`~/.hermes/checkpoints/store/`，`GIT_DIR`+`GIT_WORK_TREE`+`GIT_INDEX_FILE` 隔离
- hermes 的自动触发：`agent/tool_executor.py:507,517`——每个文件修改工具调用前 `ensure_checkpoint`
- hermes 的恢复安全网：`restore()` 恢复前先拍 pre-rollback 快照

### ZCode 参照（git 快照 + 冲突检测）

- `D:\selfProj\ZCode-main\ZCode-main\packages\services\src\git\repo\gitCheckpointRepo.ts:255`（`createCheckpoint`——temp GIT_INDEX_FILE + write-tree + commit-tree + update-ref）
- ZCode 的冲突检测：`restoreBetweenCheckpoints`——三方比对 on-disk blob vs checkpoint tree

### sherry 内部（迁移对象）

- `agent/tools/pub_base/git_ensure.py`——**删除**（shadow git 不需要）
- `server/service/git_graph_service.py`——**全替换**（shadow env 下的 git 命令）
- `agent/tools/subagent/isolation/gitrun.py:26`——`run_git` 加 shadow env
- `agent/tools/subagent/isolation/worktree.py`——`ensure_repository` 删除，`create_worktree`/`remove_worktree` 改 shadow env
- `runtime/session/git_head.py:51`——`read_git_head` 改 shadow env
- `config/path.py`——新增 `SHADOW_GIT_DIR = APP_HOME / "shadow-git"`

### 关联 TODO

- `计划_会话工作目录不可变.md`——目录不可变后 per-project hash 稳定，shadow 分支不需要处理切换
- `计划_统一项目隐藏目录到sherry.md`——shadow store 在 SHERRY_HOME 不在项目目录；`.sherry-isolation-repo` 标记删除（不再 init 用户 repo）
