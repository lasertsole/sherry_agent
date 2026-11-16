# 计划：LDW journal 路径修复——锚从 ROOT_DIR 改为 SRC_DIR

- 日期：2026-11-16
- 状态：**待评审**
- 来源：多租户审计发现 `workflow_runs.db` 是可写持久化 DB，但锚在 ROOT_DIR（代码目录）而非 SRC_DIR（APP_HOME 数据目录），导致多租户隔离时不会随 `SHERRY_HOME` 移动
- 风险等级：**低** — 改 1 行解析逻辑 + 1 行默认值，单调用点
- 依赖：无

---

## 0. 问题

### 当前行为

`config/features/agent_side/ldw.py:32`：
```python
"journal_path": "src/data/workflow_runs.db",
```

`agent/tools/ldw/replay.py:162-168`：
```python
if LDW["journal_path"] and SqliteWorkflowStore is not None:
    path = Path(LDW["journal_path"])
    if not path.is_absolute():
        path = Path(ROOT_DIR) / path          # ← 锚在 ROOT_DIR（代码目录）
    path.parent.mkdir(parents=True, exist_ok=True)
    store = await SqliteWorkflowStore.open(path)
```

默认值 `"src/data/workflow_runs.db"` 是相对路径，被 join 到 `ROOT_DIR`，产出 `ROOT_DIR/src/data/workflow_runs.db`。

### 为什么是 bug

`ROOT_DIR` 是代码目录（`config/path.py:12-14`，`Path(__file__).parent / ".."`）。在 checkout 模式下 `APP_HOME == ROOT_DIR`，所以这个 bug 不可见。但在多租户/打包模式下 `APP_HOME != ROOT_DIR`：

- **Fleet cell 模式**：容器内 `APP_HOME=/app/data`，但 `ROOT_DIR` 指向代码安装目录（`/app/sherry_agent/` 或 PyInstaller bundle）。`workflow_runs.db` 写到代码目录而非数据卷——容器重建后丢失。
- **bare-metal 多进程模式**：`SHERRY_HOME=/data/cells/<user>` 使 `APP_HOME` per-user，但 `ROOT_DIR` 不变（共享代码目录）。所有租户的 `workflow_runs.db` 写到同一个 `ROOT_DIR/src/data/workflow_runs.db`——跨租户冲突。

### 对比其他 DB 的锚

| DB | 锚 | 路径 |
|----|-----|------|
| registry.db | SRC_DIR | `SRC_DIR/data/registry.db` |
| auth.db | SRC_DIR | `SRC_DIR/data/auth.db` |
| mes_memory | SRC_DIR | `SRC_DIR/store/mes_memory/mes_memory.db` |
| state_register | SRC_DIR | `SRC_DIR/data/state_register.db` |
| **workflow_runs.db** | **ROOT_DIR** ← 唯一异常 | `ROOT_DIR/src/data/workflow_runs.db` |

所有其他可写 DB 都锚在 SRC_DIR（= `APP_HOME/src`），随 `SHERRY_HOME` 移动。只有 LDW journal 锚在 ROOT_DIR。

---

## 1. 修复

### 1.1 replay.py 解析逻辑

**文件**：`agent/tools/ldw/replay.py`

当前 import（line 14）：
```python
from config.path import PLAN_KNOWLEDGE_DIR, ROOT_DIR
```

`ROOT_DIR` 在 replay.py 中**只有 1 处使用**——line 165 的 journal path 解析。`PLAN_KNOWLEDGE_DIR`（line 43 用于 replay-count 文件）不涉及 `ROOT_DIR`。

改为：
```python
from config.path import PLAN_KNOWLEDGE_DIR, SRC_DIR
```

line 165：
```python
# 修复前
path = Path(ROOT_DIR) / path

# 修复后
path = Path(SRC_DIR) / path
```

`SRC_DIR` 已从 `config/__init__.py` 导出（`config/path.py:119` 定义）。replay.py 当前从 `config.path` import（不是 `config`），所以 `from config.path import SRC_DIR` 可行。

### 1.2 默认值去掉 src/ 前缀

**文件**：`config/features/agent_side/ldw.py:32`

```python
# 修复前
"journal_path": "src/data/workflow_runs.db",

# 修复后
"journal_path": "data/workflow_runs.db",
```

因为 `SRC_DIR` 已经是 `APP_HOME/src`，join `"data/workflow_runs.db"` 产出 `APP_HOME/src/data/workflow_runs.db`——和其他 DB 一致。

### 1.3 不改的

- `scripts.py`——不引用 `journal_path`，无需改
- `SqliteWorkflowStore.open(path)`——接收已解析的 path，不关心锚（`vendor/ldw/_persistence.py:336`）
- `ldw_shim.py`——纯 re-export，不涉及路径
- `PLAN_KNOWLEDGE_DIR` import——不涉及 ROOT_DIR，不受影响

---

## 2. 迁移

### 现有数据

修复后，`workflow_runs.db` 的路径从 `ROOT_DIR/src/data/workflow_runs.db` 变为 `SRC_DIR/data/workflow_runs.db`。

- **checkout 模式**（APP_HOME == ROOT_DIR）：旧路径 = 新路径——**相同**，无需迁移。
- **打包模式 / 多租户模式**（APP_HOME != ROOT_DIR）：旧路径在代码目录（通常不存在或为只读），新路径在数据目录。首次运行自动创建新文件，无需迁移。

### 配置兼容

不做兼容。完全删除旧行为：

1. **删除旧默认值**——`"src/data/workflow_runs.db"` 从 `ldw.py:32` 中彻底移除，替换为 `"data/workflow_runs.db"`
2. **删除旧解析逻辑**——`replay.py:165` 的 `Path(ROOT_DIR) / path` 彻底替换为 `Path(SRC_DIR) / path`
3. **移除 ROOT_DIR import**——如果 `replay.py` 不再使用 `ROOT_DIR`（grep 确认），删除该 import
4. **不保留任何前缀检测/剥离/回退逻辑**——旧路径格式不再被识别

如果有人在 `sherry.jsonc` 中手动设过 `ldw.journal_path` 为旧的 `"src/data/workflow_runs.db"` 格式，新的解析会产出 `SRC_DIR/src/data/workflow_runs.db`（多一层 src/）——这是配置错误，应该修正配置，而不是代码兜底。

---

## 3. 测试

| 场景 | 预期 |
|------|------|
| 默认值 `"data/workflow_runs.db"` | 解析为 `SRC_DIR/data/workflow_runs.db` |
| 绝对路径 `/custom/path.db` | 原样使用，不 join |
| `SHERRY_HOME=/data/cells/user1` | journal 在 `/data/cells/user1/src/data/workflow_runs.db` |
| `SHERRY_HOME=/data/cells/user2` | journal 在 `/data/cells/user2/src/data/workflow_runs.db` |

---

## 4. 变更清单

| 文件 | 行号 | 变更 |
|---|---|---|
| `agent/tools/ldw/replay.py` | line 14 | import 从 `ROOT_DIR` 改为 `SRC_DIR`（`from config.path import PLAN_KNOWLEDGE_DIR, SRC_DIR`） |
| `agent/tools/ldw/replay.py` | line 165 | `Path(ROOT_DIR) / path` → `Path(SRC_DIR) / path` |
| `config/features/agent_side/ldw.py` | line 32 | `"journal_path": "src/data/workflow_runs.db"` → `"journal_path": "data/workflow_runs.db"` |
| `tests/agent/tools/ldw/test_replay.py`（或新建） | — | 验证路径解析：默认值 → SRC_DIR/data/；绝对路径不变；SHERRY_HOME 隔离 |

---

## 5. Kill criteria

- 无。这是 1 行 bug 修复，不改变任何行为逻辑（只改 DB 文件落盘位置），checkout 模式下路径不变。
