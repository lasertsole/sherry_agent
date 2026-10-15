# 计划：禁止嵌套 worktree——子代理复用父代理的隔离工作区

- 建档日期：2026-10-15
- 状态：**计划（未开工）**
- 关联代码：`agent/tools/subagent/spawn/core.py::_create_isolated_child_workspace`、
  `agent/tools/subagent/isolation/tree.py::create_isolated_workspace`、
  `agent/tools/subagent/spawn/runtime_isolation.py::resolve_spawned_workspace_inheritance`
- 触发问题：当一个 `isolation=True` 的子代理（ORCHESTRATOR，depth 1）在
  自己的 git worktree 里工作，它 spawn 的孙子代理（LEAF，depth 2）通过
  `resolve_spawned_workspace_inheritance` 继承了 worktree 路径作为 cwd。
  但如果模型给孙子显式传 `isolation=True`，`create_isolated_workspace`
  会在父的 worktree 上再 `git worktree add`——创建 worktree 的 worktree。
  嵌套 worktree 多一层 merge-back、一层冲突面、一层 CAS 检查，收益为零。
- 结论先行：
  1. **静默降级，不拒绝**——父 cwd 已在 isolated worktree 里时，子代理的
     `isolation=True` 被降级为 `False`（复用父 worktree），打 info 日志。
     模型不需要处理错误，行为和默认一致。
  2. **检测靠路径**——`cwd` 是否在 `isolated_workspaces_dir()` 下面，
     一个 `Path.relative_to` 检查就够。
  3. **只改一个函数**——`_create_isolated_child_workspace` 入口加一个
     前置检查，其余代码不变。

---

## 1. 为什么嵌套 worktree 不该存在

### 1.1 max_spawn_depth=2 下没有隔离收益

链路是 MAIN → ORCHESTRATOR(worktree) → LEAF。LEAF 是终端 worker，
做完就回报，不会再 spawn。ORCHESTRATOR 已经有 worktree 隔离——它的
worktree 和主仓库之间有 merge-back。LEAF 在 ORCHESTRATOR 的 worktree 里
干活，改的文件随 ORCHESTRATOR 的 merge 一起回主仓库。中间多一层 worktree
只是多一次 merge-back，不增加隔离边界。

### 1.2 嵌套 merge-back 增加复杂度

```
当前（无嵌套）:
  LEAF 改文件 → 在 ORCHESTRATOR 的 worktree 里 → ORCHESTRATOR merge 回主仓库
  一层 merge-back，一次 CAS 冲突检查

嵌套 worktree:
  LEAF 在自己的 worktree 里改 → LEAF merge 回 ORCHESTRATOR 的 worktree
  → ORCHESTRATOR merge 回主仓库
  两层 merge-back，两次 CAS 冲突检查，中间态可能不一致
```

### 1.3 并发冲突是编排问题不是隔离问题

两个 LEAF 编辑同一个文件会冲突——但 ORCHESTRATOR 应该协调（不同文件、
串行化）。加 worktree 只是把冲突推迟到 merge 时，不是消除冲突。

### 1.4 git worktree of worktree 是反模式

git 技术上支持从 worktree 再切 worktree，但这不是设计意图——worktree 的
设计场景是"同一个仓库的多个工作树并行开发"，不是"worktree 的 worktree"。
嵌套 worktree 的 branch 关系、stash 传递、ignored 路径 materialize 都
比单层复杂得多。

---

## 2. 现状

### 2.1 当前行为（默认 isolation=False）

```
ORCHESTRATOR (worktree, cwd=/workspaces/<slug>/tree/)
  │
  └─ spawn LEAF (isolation 未传，默认 False)
       ├─ resolve_spawned_workspace_inheritance
       │    └─ session_workspace_root(parent_key) = /workspaces/<slug>/tree/
       ├─ isolation=False → 不创建新 worktree
       └─ LEAF cwd = /workspaces/<slug>/tree/  ← 复用父 worktree ✓
```

**默认行为已经是对的**——LEAF 复用父 worktree，改动对父立即可见，随父
merge 一起回主仓库。

### 2.2 缺口（显式 isolation=True）

```
ORCHESTRATOR (worktree, cwd=/workspaces/<slug>/tree/)
  │
  └─ spawn LEAF (isolation=True)
       ├─ _create_isolated_child_workspace(isolation=True, cwd=/workspaces/<slug>/tree/)
       │    └─ create_isolated_workspace(/workspaces/<slug>/tree/, grandchild_key)
       │         └─ git worktree add ...  ← 在 worktree 上再切 worktree ❌
       └─ LEAF cwd = /workspaces/<slug2>/tree2/  ← 嵌套 worktree
```

模型可能传 `isolation=True`——`sessions_spawn` schema 暴露了这个参数，
description 没说"父已在 worktree 里时无效"。需要拦掉。

---

## 3. 设计方案

### 3.1 检测函数

```python
def _is_inside_isolated_worktree(path: str | None) -> bool:
    """True when *path* is inside an isolated worktree directory.

    Isolated worktrees live under ``isolated_workspaces_dir()/<slug>/tree/``,
    so a simple ``Path.relative_to`` check suffices — no git probing needed.
    """
    if not path:
        return False
    from config.path import isolated_workspaces_dir
    try:
        Path(path).resolve().relative_to(isolated_workspaces_dir().resolve())
        return True
    except ValueError:
        return False
```

**为什么用路径检查而不是 git 命令**：`isolated_workspaces_dir()` 是固定的
目录（`config.path` 解析），所有 isolated worktree 都在它下面。一个
`relative_to` 比跑 `git rev-parse --show-toplevel` 快得多，且不依赖 git
可用性。

### 3.2 降级逻辑（`_create_isolated_child_workspace` 入口）

```python
def _create_isolated_child_workspace(
    isolation: bool, cwd: str | None, child_session_key: str
) -> tuple[str | None, Any, SpawnResult | None]:
    if not isolation:
        return cwd, None, None

    # 嵌套隔离降级：父 cwd 已在 isolated worktree 里时，不再开嵌套 worktree。
    # 子代理直接在父的 worktree 里工作，改动随父的 merge 一起回主仓库。
    # 静默降级而非拒绝——模型不需要处理错误，行为和 isolation=False 一致。
    if _is_inside_isolated_worktree(cwd):
        logger.info(
            "spawn: isolation=True ignored for child {} — parent cwd {} is "
            "already an isolated worktree; child reuses it",
            child_session_key, cwd,
        )
        return cwd, None, None

    if not cwd:
        return (
            cwd,
            None,
            SpawnResult(
                status="forbidden",
                error=(
                    "isolation requires a project directory to copy: the caller has "
                    "neither a bound project directory nor an explicit cwd"
                ),
            ),
        )
    # ... 现有的 create_isolated_workspace 逻辑不变 ...
```

### 3.3 不改的地方

| 不改 | 理由 |
|------|------|
| `resolve_spawned_workspace_inheritance` | 已经正确——读父的 project dir（worktree 路径），子继承 |
| `sessions_spawn` schema | `isolation` 参数保留——降级是运行时行为，不在 schema 层隐藏 |
| `sessions_spawn` tool description | 可选加一句"isolation is ignored when the parent is already in an isolated worktree"——但不必须，info 日志足够 |
| `create_isolated_workspace` | 不改——降级在调用前拦截，不会走到这里 |
| `merge_isolated_workspace` | 不改——没有嵌套 worktree 就没有嵌套 merge |

---

## 4. 完整执行流

```
ORCHESTRATOR (worktree, cwd=/workspaces/<slug>/tree/)
  │
  ├─ spawn LEAF (isolation 未传)
  │    └─ _create_isolated_child_workspace(False, /workspaces/<slug>/tree/, ...)
  │         └─ isolation=False → return (cwd, None, None)
  │    └─ LEAF cwd = /workspaces/<slug>/tree/  ← 复用父 worktree ✓
  │
  └─ spawn LEAF (isolation=True)
       └─ _create_isolated_child_workspace(True, /workspaces/<slug>/tree/, ...)
            ├─ _is_inside_isolated_worktree(/workspaces/<slug>/tree/) → True
            ├─ logger.info("isolation=True ignored ... child reuses it")
            └─ return (cwd, None, None)  ← 降级为复用 ✓
       └─ LEAF cwd = /workspaces/<slug>/tree/  ← 同样复用父 worktree ✓
```

两种情况行为一致——LEAF 都在父的 worktree 里工作。

---

## 5. 文件清单

### 修改文件（1 个）

| 文件 | 改动 |
|------|------|
| `agent/tools/subagent/spawn/core.py` | 新增 `_is_inside_isolated_worktree()` 函数；`_create_isolated_child_workspace` 入口加嵌套检测 + 降级 |

### 测试文件（1 个）

| 文件 | 新增测用例 |
|------|-----------|
| `tests/agent/tools/subagent/test_spawn_isolation.py`（或扩展现有） | 见下 |

### 新增测用例

1. **`test_nested_isolation_downgraded`** — 父在 worktree 里，子传
   `isolation=True`，验证不创建新 worktree（`isolated_workspace` 为 None），
   cwd = 父的 worktree 路径
2. **`test_non_isolated_parent_still_creates_worktree`** — 父在普通目录里，
   子传 `isolation=True`，验证正常创建 worktree（不受降级影响）
3. **`test_is_inside_isolated_worktree_detects_correctly`** — 路径在
   `isolated_workspaces_dir()` 下 → True；不在 → False；None → False
4. **`test_downgrade_logs_info`** — 验证降级时打 info 日志

---

## 6. 边界场景

| 场景 | 行为 | 理由 |
|------|------|------|
| 父在 worktree，子不传 isolation | 复用父 worktree | 默认行为，不变 |
| 父在 worktree，子传 isolation=True | 降级为复用，打 info 日志 | 本方案新增 |
| 父在普通目录，子传 isolation=True | 正常创建 worktree | 不受降级影响 |
| 父在普通目录，子不传 isolation | 复用父目录 | 默认行为，不变 |
| MAIN spawn 子代理 (isolation=True) | 正常创建 worktree | MAIN 的 cwd 不在 isolated 目录下 |
| cwd 为 None + isolation=True | 报 forbidden 错误 | 已有逻辑，不变 |
| 路径在 isolated_workspaces_dir 下但不是 worktree | 仍降级 | 路径检查是保守的，不会误创建 |

---

## 7. 不做的事

- **不拒绝 isolation=True**——静默降级而非报错。模型不需要处理错误，
  行为和 isolation=False 一致。拒绝会浪费模型一轮 tool call。
- **不改 sessions_spawn schema**——`isolation` 参数保留。降级是运行时
  行为，不在 schema 层隐藏参数。可选在 tool description 加一句说明，
  但不必须。
- **不做 git 命令检测**——不跑 `git rev-parse --show-toplevel` 检测 cwd
  是否是 worktree。路径检查（`relative_to(isolated_workspaces_dir)`）更
  快、更可靠、不依赖 git 可用性。
- **不支持嵌套 worktree**——不尝试让 worktree of worktree "正确工作"。
  嵌套 worktree 是 git 反模式，`max_spawn_depth=2` 下没有隔离收益，
  禁止比支持更简单。
- **不改 merge 逻辑**——没有嵌套 worktree 就没有嵌套 merge。
  `merge_isolated_workspace` 只处理单层 ORCHESTRATOR worktree → 主仓库。
