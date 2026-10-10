# 计划：隔离子代理 workspace 崩溃恢复扫描

- 建档日期：2026-10-09
- 状态：**计划（未开工）**
- 触发问题：隔离子代理 workspace（git worktree + branch + snapshot.json）在进程崩溃后可能永久孤儿。sweeper 只扫 run registry，不扫 `isolated_workspaces_dir()`。4 个缺口：run 不存在→永久孤儿、冲突保留 workspace 永不清理、无 git worktree prune、无 boot-time 扫描

## 现状

### 正常路径（能工作）

```
spawn → create_isolated_workspace → work → announce → merge
  ├─ 干净 → discard_isolated_workspace（注销 worktree + 删 branch + 删目录）
  └─ 冲突 → snapshot.json 改名 merged.json → tree 保留供检查
```

### 崩溃恢复路径（部分能工作）

```
崩溃 → 重启 → sweeper 周期扫描 run registry
  → recover_orphaned_runs()
  → orphan 路由到 announce flow
  → merge_isolated_workspace_for_run(run)
  → isolated_workspace_meta(run.spawned_cwd) 检测 snapshot.json
    ├─ 找到 → merge → 干净 discard / 冲突保留  ✅
    └─ 没找到（merged.json 或目录已删）→ 跳过  ⚠️
```

**前提**：run record 必须存在于 registry 中，且 `spawned_cwd` 正确指向 workspace。

## 4 个缺口

### 缺口 1：run record 不存在 → workspace 永久孤儿

崩溃发生在 `create_isolated_workspace` 之后、run record 写入 registry 之前。workspace 目录已创建（worktree + branch + snapshot.json），但 registry 里没有这条 run。

sweeper 只扫 registry，不扫 `isolated_workspaces_dir()`。workspace 永远不被清理——worktree registration 和 branch 一直留在父 repo 里。

### 缺口 2：`merged.json`（冲突保留）永不清理

冲突合并后 `snapshot.json` 改名 `merged.json`，tree 保留供检查。但没有 TTL，没有清理命令，没有 sweeper 扫描——冲突 workspace 永远留在磁盘上。

### 缺口 3：无 `git worktree prune`

workspace 目录被外部删除（手动 rm、磁盘清理工具）但 git registration 还在 → `git worktree list` 显示 stale 条目。`git worktree prune` 从不被调用。

### 缺口 4：无 boot-time 扫描

sweeper 是周期性的（间隔运行），但启动时不立即扫一次。重启后到第一次 sweeper 运行之间，stale workspaces 无人处理。

## 设计

### 1. 新增 `sweep_stale_isolated_workspaces()`（~80 行）

`agent/tools/subagent/isolation/sweep.py`：

```python
"""Sweep stale isolated workspaces: orphaned, expired-conflict, corrupted.

Called by the sweeper daemon on every cycle and at boot. Never raises — a
sweep failure must not break the sweeper or the server start.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from loguru import logger

from config.features import SUBAGENT_ISOLATION
from config.path import isolated_workspaces_dir
from .tree import (
    MERGED_NAME,
    SNAPSHOT_NAME,
    TREE_DIRNAME,
    _manifest_meta,
    discard_isolated_workspace,
    _slug,
)

__all__ = ["sweep_stale_isolated_workspaces"]


def sweep_stale_isolated_workspaces() -> int:
    """Scan isolated_workspaces_dir, clean up workspaces with no live run record.

    Returns the number of workspaces cleaned. Never raises — called from the
    sweeper and at boot, a failure is logged and swallowed.

    Three classes of stale workspace:
    1. Orphaned (snapshot.json exists, run not in registry) — crash during spawn
       before registration. Clean up.
    2. Expired conflict (merged.json exists, older than TTL) — conflict-kept
       workspace past retention. Clean up.
    3. Corrupted (neither snapshot nor merged) — damaged workspace. Clean up.

    Workspaces with a live run record are left untouched — orphan recovery
    or the announce flow will handle them.
    """
    try:
        return _do_sweep()
    except Exception:
        logger.warning("stale workspace sweep failed", exc_info=True)
        return 0


def _do_sweep() -> int:
    base = isolated_workspaces_dir()
    if not base.is_dir():
        return 0

    # 收集所有 run record 的 workspace slug —— run 在 registry 里就不动
    active_slugs = _collect_active_slugs()

    ttl_days = SUBAGENT_ISOLATION.get("stale_workspace_ttl_days", 7)
    ttl_seconds = ttl_days * 86400
    now = time.time()
    cleaned = 0
    parent_roots: set[str] = set()

    for ws_dir in base.iterdir():
        if not ws_dir.is_dir() or not ws_dir.name.startswith("iso-"):
            continue
        slug = ws_dir.name
        if slug in active_slugs:
            continue  # run 还在 registry → orphans / announce 会处理

        snapshot_path = ws_dir / SNAPSHOT_NAME
        merged_path = ws_dir / MERGED_NAME

        if snapshot_path.is_file():
            # 未合并 + run 不在 registry → 崩溃 spawn 遗留
            logger.warning("sweep: orphaned workspace (no run record): {}", ws_dir)
        elif merged_path.is_file():
            # 冲突保留 → 检查 age
            age = now - merged_path.stat().st_mtime
            if age < ttl_seconds:
                continue  # 还在保留期内
            logger.info("sweep: expired conflict workspace ({:.1f} days old): {}",
                        age / 86400, ws_dir)
        else:
            # 既没 snapshot 也没 merged → 损坏
            logger.warning("sweep: corrupted workspace (no manifest): {}", ws_dir)

        # 记录 parent_root 用于后续 prune
        meta = _manifest_meta(ws_dir)
        parent_root = meta.get("parent_root")
        if parent_root:
            parent_roots.add(str(parent_root))

        # 清理：discard 会注销 git worktree + 删 branch，再 rmtree 目录
        discard_isolated_workspace(ws_dir)
        cleaned += 1

    # 清理父 repo 的 stale worktree registrations（目录已删但 git 还记着）
    for root in parent_roots:
        _prune_worktrees(Path(root))

    return cleaned


def _collect_active_slugs() -> set[str]:
    """All workspace slugs that have a run record in the registry."""
    try:
        from agent.tools.subagent.registry.memory import get_all_runs
        runs = get_all_runs()
        return {_slug(r.child_session_key) for r in runs if r.child_session_key}
    except Exception:
        return set()


def _prune_worktrees(parent_root: Path) -> None:
    """Run 'git worktree prune' to clean stale registrations."""
    try:
        from .gitrun import run_git
        run_git(parent_root, ["worktree", "prune"])
    except Exception:
        pass  # best-effort, never break the sweep
```

### 2. 接入 sweeper 周期循环

`agent/tools/subagent/registry/sweeper.py` 的 `_run_sweep_cycle()` 加一行：

```python
async def _run_sweep_cycle():
    await recover_orphaned_runs()
    # ... 现有逻辑 ...
    from ..isolation.sweep import sweep_stale_isolated_workspaces
    stale_ws = sweep_stale_isolated_workspaces()
    if stale_ws:
        logger.info("Sweeper cleaned {} stale isolated workspace(s)", stale_ws)
```

### 3. Boot-time 立即扫描

`server/__main__.py` 启动时（在 sweeper 启动之后）立即调一次，不等第一次周期：

```python
# server/__main__.py，在 sweeper 启动后
from agent.tools.subagent.isolation.sweep import sweep_stale_isolated_workspaces
stale = sweep_stale_isolated_workspaces()
if stale:
    logger.info("Boot: cleaned {} stale isolated workspace(s)", stale)
```

或者更干净的方式：在 sweeper 的 `start()` 里启动后立即跑一次 `_run_sweep_cycle()`（而不是等第一次 interval）。

### 4. 配置

`config/features/agent_side/subagent_isolation.py` 新增一个字段：

```python
class SubagentIsolationConfig(TypedDict):
    # ... 现有字段 ...
    #: 冲突保留 workspace 的过期天数。sweeper 超过此天数后清理。
    stale_workspace_ttl_days: int

SUBAGENT_ISOLATION: SubagentIsolationConfig = {
    # ... 现有值 ...
    "stale_workspace_ttl_days": 7,
}
```

### 5. 测试

`tests/agent/tools/subagent/isolation/test_sweep.py`：

- `test_orphaned_workspace_cleaned` — 创建 workspace（写 snapshot.json），不注册 run → sweep 清理 + 验证 worktree 注销 + branch 删除 + 目录删除
- `test_conflict_workspace_expired` — 创建 workspace，snapshot 改名 merged.json，mtime 设为 8 天前 → sweep 清理
- `test_conflict_workspace_within_ttl` — merged.json mtime 设为 3 天前 → 不清理
- `test_active_workspace_not_touched` — 创建 workspace + 注册 run → sweep 不动
- `test_corrupted_workspace_cleaned` — 创建 workspace 目录但无 snapshot/merged → 清理
- `test_git_worktree_prune_called` — 验证清理后父 repo 调了 `git worktree prune`
- `test_sweep_never_raises` — 模拟各种异常（目录不可读、git 命令失败）→ sweep 返回 0 不 crash
- `test_empty_dir_skipped` — `isolated_workspaces_dir()` 不存在 → 返回 0

## 验收

- 崩溃在 spawn 后 registry 前 → 重启 → boot sweep 扫到 orphaned workspace → discard（注销 worktree + 删 branch + 删目录）
- 冲突保留 workspace 8 天后 → sweeper 清理
- 冲突保留 workspace 3 天内 → 不清理
- run 还在 registry 的 workspace → sweep 不动
- 父 repo 的 `git worktree list` 不显示已清理的 stale 条目
- sweeper 周期运行时调用 sweep
- boot 时立即调用一次 sweep（不等第一次周期）
- sweep 异常不 crash sweeper / server

## 风险

- **discard 正在使用的 workspace**：如果 run 还在 registry 但 sweeper 的 `get_all_runs()` 漏了它（registry 读取失败、并发写入窗口）→ 误删。缓解：`_collect_active_slugs()` fail-open 返回空集 → 不删（当读取失败时跳过清理，等下一轮）。更保险：discard 前再读一次 meta 确认 snapshot.json 的 `child_session_key` 不在 registry
- **git worktree prune 误删**：`git worktree prune` 只删 prune-vulnerable 条目（目录不存在或 gitdir 指向不存在）。不会删有目录的 worktree。安全
- **磁盘 I/O**：扫描 `isolated_workspaces_dir()` + 读 meta + git worktree prune。通常 workspace 数量 < 10，I/O 可忽略

## 不做的事

- **不做 workspace 复用**——不从 stale workspace 池里挑一个给新 spawn 复用。每次 spawn 创建新 workspace。复用需要版本匹配（baseline revision 一致）+ 清理子代理改动，复杂度不值
- **不做手动清理命令**——不暴露 `POST /sessions/cleanup_workspaces` 之类的 REST 端点。sweeper 自动清理 + boot 扫描已够。需要手动清理时用 `terminal` 跑 `git worktree prune` + `rm -rf` 即可
- **不清理非 isolated 的 worktree**——只扫 `isolated_workspaces_dir()` 下的 `iso-*` 目录。用户自己创建的 worktree 不碰

## 关键文件路径

| 文件 | 角色 |
|---|---|
| `agent/tools/subagent/isolation/sweep.py` | 新建：workspace 扫描器 |
| `agent/tools/subagent/registry/sweeper.py` | 修改：周期循环调 sweep |
| `server/__main__.py` | 修改：boot 时调一次 sweep |
| `config/features/agent_side/subagent_isolation.py` | 修改：加 `stale_workspace_ttl_days` |
| `agent/tools/subagent/isolation/tree.py` | 参考：`discard_isolated_workspace` / `_manifest_meta` / `_slug` |
| `agent/tools/subagent/isolation/gitrun.py` | 参考：`run_git` |
| `agent/tools/subagent/registry/memory.py` | 参考：`get_all_runs` |
| `tests/agent/tools/subagent/isolation/test_sweep.py` | 新建：测试 |
