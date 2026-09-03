# Subagent 限制对齐 OpenClaw — 修改计划

## 背景

Sherry 当前缺少 openclaw 的 subagent 全局并发限制，且部分默认值与 openclaw 差异较大。本计划将 sherry 的 subagent 配置对齐 openclaw，同时引入派生深度硬上限保护。

## 变更对照

| 配置项                      | 当前值     | 目标值  | 说明                                                        |
| --------------------------- | ---------- | ------- | ----------------------------------------------------------- |
| `max_concurrent` (全局并发) | **不存在** | **8**   | 新增，对应 openclaw `DEFAULT_SUBAGENT_MAX_CONCURRENT = 8`   |
| `max_spawn_depth`           | 3          | **2**   | 默认降低，硬上限 2（不可超过）                              |
| `archive_after_minutes`     | 1440       | **60**  | 对齐 openclaw `DEFAULT_SUBAGENT_ARCHIVE_AFTER_MINUTES = 60` |
| `run_timeout_seconds`       | 300.0      | **0.0** | 无超时，对齐 openclaw 默认 `runTimeoutSeconds = 0`          |

新增约束：`max_spawn_depth` 自定义值不能 > 2（硬上限），否则抛 `ValueError`。

---

## 详细步骤

### Step 1: `agent/tools/subagent/config.py` — 字段 + 默认值 + 验证

**改动内容：**

1. 新增模块级常量 `MAX_SPAWN_DEPTH_CAP: int = 2`
2. 新增字段 `max_concurrent: int = 8`
3. 修改 `max_spawn_depth` 默认值 `3 → 2`
4. 修改 `archive_after_minutes` 默认值 `1440 → 60`
5. 修改 `run_timeout_seconds` 默认值 `300.0 → 0.0`
6. 新增 `model_config = ConfigDict(validate_assignment=True)` — 使直接赋值也触发字段验证器
7. 新增 `@field_validator("max_spawn_depth")` 验证器：值 > `MAX_SPAWN_DEPTH_CAP`（即 > 2）时 raise `ValueError`

**覆盖路径分析：**

| 赋值路径                                        | 是否触发验证                    | 备注                                                  |
| ----------------------------------------------- | ------------------------------- | ----------------------------------------------------- |
| `SubagentConfig(max_spawn_depth=5)`             | 构造时触发                      | Pydantic 构造验证                                     |
| `cfg.max_spawn_depth = 5`                       | `validate_assignment=True` 触发 | 覆盖 delegate.py 的直接赋值                           |
| `cfg.model_copy(update={"max_spawn_depth": 5})` | 不触发                          | Pydantic v2 限制，需在 `_dispatch_async` 中加显式检查 |

---

### Step 2: `agent/tools/subagent/spawn/depth.py` — 全局并发校验

**改动内容：**

新增函数：

```python
def validate_global_concurrent(current_count: int) -> tuple[bool, str]:
    """Check whether the global subagent concurrency has reached the configured max."""
    config = get_config()
    if current_count >= config.max_concurrent:
        return (
            False,
            f"Global concurrent subagents {current_count} already at max {config.max_concurrent}",
        )
    return True, ""
```

---

### Step 3: `agent/tools/subagent/registry/queries.py` — 全局活跃计数

**改动内容：**

新增函数：

```python
def count_all_active_runs() -> int:
    """Count all RUNNING-status runs across all sessions (global concurrency view)."""
    return sum(
        1
        for run in memory.values()
        if run.execution.status == ExecutionStatus.RUNNING
    )
```

---

### Step 4: `agent/tools/subagent/registry/read.py` + `__init__.py` — 导出

**`read.py`：**

新增只读包装：

```python
def count_all_active_runs_readonly() -> int:
    """Count all active runs globally without modifying state."""
    return queries.count_all_active_runs()
```

**`__init__.py`：**

- import 中新增 `count_all_active_runs`, `count_all_active_runs_readonly`
- `__all__` 列表新增两个函数名

---

### Step 5: `agent/tools/subagent/spawn/core.py` — spawn pipeline 加入全局门控

**改动内容：**

在 Phase 2（depth/concurrency gate）中，`validate_concurrent_children` 校验之后，新增全局并发检查：

```python
# --- Phase 2: Policy & depth/concurrency gate ---
# ... (existing depth check) ...
# ... (existing per-session children check) ...

global_active = count_all_active_runs_readonly()
allowed, reason = validate_global_concurrent(global_active)
if not allowed:
    return SpawnResult(status="forbidden", error=reason)
```

import 新增：

```python
from .depth import get_subagent_depth, validate_spawn_depth, validate_concurrent_children, validate_global_concurrent
from ..registry import count_all_active_runs_readonly
```

---

### Step 6: `agent/tools/subagent/spawn/plan.py` + `spawn/core.py` — timeout=0 处理

**`plan.py`：**

`resolve_run_timeout_seconds` 逻辑不变（当 `timeout` 为 None 或 ≤ 0 时返回 `get_config().run_timeout_seconds`，现在是 `0.0`）。

**`core.py` 的 `_execute_subagent`：**

当前代码：

```python
agent_result = await asyncio.wait_for(
    child_agent.ainvoke(...),
    timeout=timeout_seconds,
)
```

改为：

```python
if timeout_seconds > 0:
    agent_result = await asyncio.wait_for(
        child_agent.ainvoke(...),
        timeout=timeout_seconds,
    )
else:
    agent_result = await child_agent.ainvoke(...)
```

同时更新 `except asyncio.TimeoutError` 分支 — 仅在 `timeout_seconds > 0` 时可能触发。

---

### Step 7: `agent/tools/subagent/delegate.py` — per-call 覆盖 + 硬上限检查

**`_dispatch_async` 函数：**

1. 新增参数 `max_concurrent: int | None = None`
2. 保存/恢复 `cfg.max_concurrent`（与 `max_spawn_depth` 同模式）
3. 在 `cfg.max_spawn_depth = max_spawn_depth` 之前加显式检查：

```python
from ..config import MAX_SPAWN_DEPTH_CAP

if max_spawn_depth is not None and max_spawn_depth > MAX_SPAWN_DEPTH_CAP:
    raise ValueError(f"max_spawn_depth cannot exceed {MAX_SPAWN_DEPTH_CAP}")
```

4. 设置 `cfg.max_concurrent`：

```python
prev_max_concurrent = cfg.max_concurrent
if max_concurrent is not None:
    cfg.max_concurrent = max_concurrent
```

5. finally 块新增恢复 `cfg.max_concurrent = prev_max_concurrent`

**`delegate_task` 公开函数：**

1. 签名新增 `max_concurrent: int | None = None` 参数
2. 文档字符串新增 `max_concurrent` 说明
3. 透传到 `_dispatch_async`

---

### Step 8: `agent/tools/subagent/capabilities/core.py` — 无需改动

`resolve_subagent_capabilities` 已通过 `get_config().max_spawn_depth` 动态读取。默认改为 2 后自动生效：

| depth | max_depth=2 (新默认) | 角色         |
| ----- | -------------------- | ------------ |
| 0     | < 2                  | MAIN         |
| 1     | < 2                  | ORCHESTRATOR |
| 2     | >= 2                 | LEAF         |

三级树结构：MAIN → ORCHESTRATOR → LEAF

`max_spawn_depth` 硬上限为 2，不可设为更大的值。

---

### Step 9: 更新测试

#### `tests/unit/subagent/test_config.py`

- `test_defaults`：
  - 新增 `assert c.max_concurrent == 8`
  - 修改 `assert c.max_spawn_depth == 2`
  - 修改 `assert c.run_timeout_seconds == 0.0`
  - 新增 `assert c.archive_after_minutes == 60`
- `test_custom`：
  - 可选新增 `max_concurrent=10` 覆盖测试
- 新增 `test_max_spawn_depth_cap_rejected`：
  - `SubagentConfig(max_spawn_depth=3)` 应 raise `ValueError`
- 新增 `test_max_spawn_depth_assignment_cap_rejected`：
  - `cfg = SubagentConfig(); cfg.max_spawn_depth = 3` 应 raise `ValueError`
- 新增 `test_max_spawn_depth_at_cap_ok`：
  - `SubagentConfig(max_spawn_depth=2)` 应成功

#### `tests/unit/subagent/test_spawn.py`

- `TestPlan`：
  - `test_default_timeout`：断言改为 `0.0`
  - `test_invalid_timeout_uses_default`：断言改为 `0.0`
- `TestDepth`：
  - 新增 `test_validate_global_concurrent_ok`：`validate_global_concurrent(0)` 返回 `(True, "")`
  - 新增 `test_validate_global_concurrent_exceeded`：`validate_global_concurrent(8)` 返回 `(False, ...)`
- `TestSpawnSubagentDirect`：
  - `test_spawn_exceeds_max_depth`：更新深度测试数据（默认 max_depth=2，需构造 depth=2 的 session key 才会触发 depth=3 被拒）
  - 新增 `test_global_concurrent_limit`：
    - 设置 `max_concurrent=2`，注册 2 个 RUNNING run，第 3 次 spawn 返回 forbidden
    - 错误信息包含 "Global concurrent"

---

### Step 10: 更新文档

**文件列表：**

| 文件                                | 修改内容   |
| ----------------------------------- | ---------- |
| `agent/tools/subagent/README.md`    | 配置表更新 |
| `agent/tools/subagent/README.zh.md` | 配置表更新 |
| `agent/tools/subagent/README.ko.md` | 配置表更新 |
| `agent/tools/subagent/README.ja.md` | 配置表更新 |

**各文件修改项：**

1. 配置表新增 `max_concurrent` 行（默认 8，全局并发上限）
2. `max_spawn_depth` 默认值 `3 → 2`，注明硬上限 2（不可超过）
3. `archive_after_minutes` 默认值 `1440 → 60`
4. `run_timeout_seconds` / `run_timeout` 默认值 `300 → 0`
5. 树形结构图更新（默认从四级树变为三级树）

---

## 影响面分析

| 影响项           | 说明                                                          | 风险等级                                                                        |
| ---------------- | ------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| **树深度缩减**   | 默认从 4 级（MAIN→ORCH→ORCH→LEAF）变为 3 级（MAIN→ORCH→LEAF） | 低 — 硬上限 2 不可放宽，确保树深度可控                                          |
| **全局并发限制** | 最多 8 个并发 subagent，超限 spawn 返回 forbidden             | 低 — 可通过 `set_config()` 或 `delegate_task(max_concurrent=...)` 调整          |
| **无超时**       | subagent 不再自动超时终止                                     | 中 — 依赖 sweeper stale 检测（2h 阈值）兜底，长时间卡住的 run 会被 sweeper 回收 |
| **归档加速**     | 完成的 subagent 会话 60min 后归档（原 24h）                   | 低 — 仅影响历史记录保留时长                                                     |
| **硬上限保护**   | `max_spawn_depth` 无论如何不能超过 2                          | 无 — 纯保护性约束                                                               |

## 变更文件清单

| #   | 文件路径                                    | 改动类型                       |
| --- | ------------------------------------------- | ------------------------------ |
| 1   | `agent/tools/subagent/config.py`            | 新增字段 + 修改默认值 + 验证器 |
| 2   | `agent/tools/subagent/spawn/depth.py`       | 新增函数                       |
| 3   | `agent/tools/subagent/registry/queries.py`  | 新增函数                       |
| 4   | `agent/tools/subagent/registry/read.py`     | 新增函数                       |
| 5   | `agent/tools/subagent/registry/__init__.py` | 导出新增                       |
| 6   | `agent/tools/subagent/spawn/core.py`        | 全局门控 + timeout 处理        |
| 7   | `agent/tools/subagent/spawn/plan.py`        | 无需改动（逻辑兼容）           |
| 8   | `agent/tools/subagent/delegate.py`          | per-call 覆盖 + 硬上限检查     |
| 9   | `agent/tools/subagent/capabilities/core.py` | 无需改动                       |
| 10  | `tests/unit/subagent/test_config.py`        | 更新断言 + 新增用例            |
| 11  | `tests/unit/subagent/test_spawn.py`         | 更新断言 + 新增用例            |
| 12  | `agent/tools/subagent/README.md`            | 文档更新                       |
| 13  | `agent/tools/subagent/README.zh.md`         | 文档更新                       |
| 14  | `agent/tools/subagent/README.ko.md`         | 文档更新                       |
| 15  | `agent/tools/subagent/README.ja.md`         | 文档更新                       |

## 验证方式

```bash
cd D:\selfProj\sherry_agent
python -m pytest tests/unit/subagent/test_config.py tests/unit/subagent/test_spawn.py -v
```
