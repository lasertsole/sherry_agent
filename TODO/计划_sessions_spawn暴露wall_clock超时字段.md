# 计划：sessions_spawn 暴露 wall-clock 超时字段

- 建档日期：2026-10-15
- 状态：**计划（未开工）**
- 关联文档：`TODO/计划_续跑enforcer加turn级硬上限.md` §8.1（缺口 #1：默认无 wall-clock 超时）
- 触发问题：`SessionsSpawnSchema`（`agent/tools/subagent/tools/sessions_spawn.py:21-73`）
  没有 `run_timeout_seconds` 字段。模型调 `sessions_spawn` 时无法 per-spawn 设
  置超时，永远走全局 config 默认值（现状 `0.0` = 无超时）。关联文档 §8.1 提出
  把全局默认从 `0.0` 改成 `600.0`，但那只是全局兜底——模型仍然无法根据任务
  复杂度自己决定"这个任务很重，给 30 分钟"或"这个任务简单，10 分钟够了"。
  弱模型 + 复杂任务的场景下，模型反复 spawn → 每轮被杀 → 零结果 → 再 spawn →
  又被杀，没有自适应能力。
- 结论先行：
  1. **缺口在 tool schema 侧**——内部 API（`spawn_subagent_direct`）已经有
     `run_timeout_seconds` 参数（`spawn/core.py:445`），但 LLM tool schema 没
     暴露它，模型调不到。
  2. **clamp 而非拒绝**——模型传的值超出 `[600, 7200]` 时 clamp 到边界，不报错。
     模型传 60（想短）→ clamp 600，保留"短"的意图；传 99999（想长）→ clamp 7200，
     保留"长"的意图。拒绝会让模型困惑并浪费一轮 tool call。
  3. **全局默认同步改 `0.0 → 600.0`**——让不走 tool 的代码路径（TaskFlow dispatch、
     `delegate_task`）也有 10 分钟兜底。与关联文档 §8.1 一致，本文档将其拆为
     独立计划。
  4. **clamp 边界值放 `SubagentConfig`**——`run_timeout_min_seconds` 和
     `run_timeout_max_seconds` 放在 `run_timeout_seconds` 同层，不散落到
     `config/features/`。

---

## 1. 现状分析

### 1.1 三层 API 的参数流转

```
LLM 调 sessions_spawn
  │
  ├─ SessionsSpawnSchema (tool schema，模型看到的参数表)
  │    ├─ task, task_name, label, agent_id, thinking, cleanup,
  │    │  attachments, goal_max_turns, functional_role, extra_tools,
  │    │  cwd, isolation
  │    └─ ← 没有 run_timeout_seconds
  │
  ├─ SessionsSpawnTool._arun
  │    └─ spawn_subagent_direct(...)  ← 没传 run_timeout_seconds，默认 None
  │
  ├─ spawn_subagent_direct (内部 API，core.py:433)
  │    ├─ run_timeout_seconds: float | None = None  ← 参数存在
  │    └─ resolve_run_timeout_seconds(run_timeout_seconds)  ← core.py:676
  │
  └─ resolve_run_timeout_seconds (plan.py:6-10)
       ├─ timeout 不为 None 且 > 0 → 原样返回（无 clamp）
       └─ 否则 → get_config().run_timeout_seconds（全局默认，现状 0.0）
            │
            └─ asyncio.wait_for(child_agent.ainvoke(...), timeout=...)
                 （0.0 → 跳过 wait_for，无超时）
```

**问题在第一层**：schema 没暴露参数，模型无法传递。第三层（`resolve_run_timeout_seconds`）
虽然能接受正值，但永远收到 `None`。

### 1.2 已有的 clamp 先例

项目里 clamp 数值的先例极少——子代理 tool schema 全部裸传，不做边界约束。唯一的
clamp 先例在 terminal 工具：

```python
# agent/tools/pub_base/_resolve_timeout.py
# terminal 的 timeout 被 clamp 到 TOOLS_TIMEOUTS["terminal_max_timeout_seconds"] = 600
```

`SubagentConfig` 里唯一的 `@field_validator` 是 `max_spawn_depth`（`config.py:52-57`），
它**拒绝**（raise `ValueError`）超限值而非 clamp。但 spawn_depth 是硬约束（>2 会
破坏递归上限），而 wall-clock 超时的边界是软约束——clamp 更合理。

### 1.3 其他能传 run_timeout_seconds 的路径

| 路径 | 能传 per-call 超时？ | 说明 |
|------|---------------------|------|
| `sessions_spawn`（LLM tool） | **不能** ← 本方案要修 | schema 没字段 |
| `spawn_subagent_direct`（内部 API） | 能（`core.py:445`） | TaskFlow、delegate_task 走这条 |
| `delegate_task`（内部 API） | 能（`delegate.py:356`） | 代码直接调 |
| TaskFlow `_dispatch` | 能（`_dispatch.py:40`） | 通过 `step_timeout_seconds` |

这些代码路径不需要本方案改动——它们已经有传参能力，只是全局默认值（`0.0`）需要
改成 `600.0` 让兜底生效。

### 1.4 模型看不到超时的后果

弱模型 + 复杂任务的场景链：

1. 模型调 `sessions_spawn` → 超时 = 全局默认（若改成 600s = 10 分钟）
2. 子代理每轮 invoke 平均 80s/迭代 × 8 轮 = 640s → 超 600s 被杀
3. `result_text = None`（第一 turn 超时，没来得及提取结果）
4. announce 传回 "Status: timed out"（无 Result 段）
5. 主 agent 收到超时 + 零结果 → 可能重试 spawn → 又被杀 → 反复白干
6. 模型无法说"我知道这个任务重，给 30 分钟"——schema 没这个参数

---

## 2. 设计方案

### 2.1 Schema 加字段（`agent/tools/subagent/tools/sessions_spawn.py`）

`SessionsSpawnSchema` 加一个可选字段：

```python
class SessionsSpawnSchema(BaseModel):
    # ... 现有字段 ...

    run_timeout_seconds: float | None = Field(
        default=None,
        description=(
            "Wall-clock timeout for this subagent in seconds. "
            "Default 600 (10 min), min 600, max 7200 (2 h). "
            "Values outside [600, 7200] are clamped to the nearest bound. "
            "Increase for complex tasks (e.g. large codebase refactors); "
            "the minimum already covers most tasks."
        ),
    )
```

**为什么 description 写英文**：现有 schema 的 `description` 全是英文（模型读的
API 契约），保持一致。

**为什么 `None` 而非 `600` 作默认值**：`None` = "用全局默认"，让全局默认的
改动（`0.0 → 600.0`）自然生效，不在 schema 里硬编码 600。如果未来改全局默认，
schema 不用跟着改。

### 2.2 `_arun` 里 clamp + 透传（同文件）

```python
async def _arun(
    self,
    task: str,
    # ... 现有参数 ...
    run_timeout_seconds: float | None = None,
) -> str:
    # ... 现有逻辑 ...

    # Clamp the model-provided timeout to [min, max]. None or non-positive
    # → None (falls back to the global default through resolve_run_timeout_seconds).
    effective_timeout = self._clamp_timeout(run_timeout_seconds)

    result = await spawn_subagent_direct(
        task=task,
        requester_session_key=requester_session_key,
        # ... 现有参数 ...
        run_timeout_seconds=effective_timeout,
    )
    # ... 现有返回逻辑 ...
```

新增 helper（模块级或类方法）：

```python
def _clamp_timeout(value: float | None) -> float | None:
    """Clamp a model-provided wall-clock timeout to the configured bounds.

    None or non-positive → None (caller falls back to the global default).
    Otherwise clamp to [run_timeout_min_seconds, run_timeout_max_seconds].
    """
    if value is None or value <= 0:
        return None
    from ..config import get_config

    config = get_config()
    return max(config.run_timeout_min_seconds, min(config.run_timeout_max_seconds, value))
```

**为什么 clamp 而非拒绝**：
- 模型传 60（想短）→ clamp 600：保留了"短"的意图，且 600 已经是下限
- 模型传 99999（想长）→ clamp 7200：保留了"长"的意图，且 7200 = 2h 够用
- 拒绝（pydantic `ge=600, le=7200`）会让模型收到 validation error，浪费一轮
  tool call 重试——模型可能反复试不同值，不如直接 clamp
- 与 terminal 工具的 `_resolve_timeout` 模式一致（clamp 到上限，不拒绝）

### 2.3 全局默认 + bounds（`agent/tools/subagent/config.py`）

```python
class SubagentConfig(BaseModel):
    # ... 现有字段 ...

    # Wall-clock timeout for child agent execution. 0 = no timeout (legacy
    # behavior; background sweeps provide the safety net). Default 600s (10 min)
    # gives a sensible floor for non-tool callers (TaskFlow dispatch,
    # delegate_task). The LLM tool (sessions_spawn) clamps model input to
    # [run_timeout_min_seconds, run_timeout_max_seconds]; internal callers
    # bypass the clamp and are trusted to pass sensible values.
    run_timeout_seconds: float = 600.0
    run_timeout_min_seconds: float = 600.0
    run_timeout_max_seconds: float = 7200.0
```

**为什么 bounds 放 `SubagentConfig` 而非 `config/features/`**：
- `run_timeout_seconds` 已经在 `SubagentConfig`（不在 `config/features/`），bounds
  放同层最自然
- `SubagentConfig` 是 Pydantic 模型，已有 `validate_assignment=True`，未来可加
  `@field_validator` 保证 `min <= default <= max`
- `config/features/agent_side/subagent_infra.py`（`SUBAGENT_INFRA`）管的是 yield/send/
  delivery 等基础设施超时，和 run timeout 语义不同

### 2.4 不改 `resolve_run_timeout_seconds`

```python
# plan.py:6-10 — 不改
def resolve_run_timeout_seconds(timeout: float | None = None) -> float:
    if timeout is not None and timeout > 0:
        return timeout
    return get_config().run_timeout_seconds
```

**理由**：
- 它是通用解析函数，被 TaskFlow dispatch、steer、followup scanner 共用
- 在这里加 clamp 会影响所有调用方——有些调用方（TaskFlow `step_timeout_seconds`）
  可能有自己的边界意图，不应该被 subagent 的全局 bounds 截断
- clamp 应在**模型输入入口**（`_arun`）做，因为只有模型输入是不可信的；内部 API
  调用方是代码，传的值可信

### 2.5 完整执行流

```
模型调 sessions_spawn(task=..., run_timeout_seconds=1800)
  │
  ├─ SessionsSpawnSchema 解析 → run_timeout_seconds=1800.0
  │
  ├─ _arun:
  │    ├─ _clamp_timeout(1800) → max(600, min(7200, 1800)) = 1800
  │    └─ spawn_subagent_direct(run_timeout_seconds=1800)
  │
  ├─ spawn_subagent_direct:
  │    └─ resolve_run_timeout_seconds(1800) → 1800 > 0 → 返回 1800
  │
  └─ _invoke: asyncio.wait_for(child_agent.ainvoke(...), timeout=1800)

模型不传 run_timeout_seconds:
  │
  ├─ SessionsSpawnSchema → run_timeout_seconds=None
  ├─ _arun → _clamp_timeout(None) → None
  ├─ spawn_subagent_direct(run_timeout_seconds=None)
  ├─ resolve_run_timeout_seconds(None) → get_config().run_timeout_seconds = 600.0
  └─ _invoke: asyncio.wait_for(..., timeout=600)

模型传 run_timeout_seconds=60:
  │
  ├─ _clamp_timeout(60) → max(600, min(7200, 60)) = 600
  └─ → 同"不传"路径，timeout=600

模型传 run_timeout_seconds=99999:
  │
  ├─ _clamp_timeout(99999) → max(600, min(7200, 99999)) = 7200
  └─ → timeout=7200（2 小时）
```

---

## 3. 文件清单

### 修改文件（2 个）

| 文件 | 改动 |
|------|------|
| `agent/tools/subagent/tools/sessions_spawn.py` | `SessionsSpawnSchema` 加 `run_timeout_seconds: float \| None = None` 字段（Field description 标明 bounds）；`_arun` 签名加参数；新增模块级 `_clamp_timeout()` helper；`_arun` 里 clamp 后传给 `spawn_subagent_direct` |
| `agent/tools/subagent/config.py` | `run_timeout_seconds` 默认值 `0.0 → 600.0`；新增 `run_timeout_min_seconds: float = 600.0` 和 `run_timeout_max_seconds: float = 7200.0` |

### 不改的文件

| 文件 | 不改的原因 |
|------|------------|
| `agent/tools/subagent/spawn/plan.py` | `resolve_run_timeout_seconds` 是通用函数，不在通用层加 clamp |
| `agent/tools/subagent/spawn/core.py` | `spawn_subagent_direct` 已有参数，不需改 |
| `config/features/agent_side/subagent_infra.py` | bounds 放 `SubagentConfig` 同层，不散落到 config/features |
| `agent/tools/taskflow/tools/_dispatch.py` | TaskFlow 已有自己的 `step_timeout_seconds` 传参路径 |

### 测试文件（1 个，扩展现有或新建）

| 文件 | 新增测用例 |
|------|-----------|
| `tests/agent/tools/subagent/test_sessions_spawn.py`（或 `test_spawn_tool_schema.py`） | 见下 |

### 新增测用例

1. **`test_spawn_passes_timeout_to_direct`** — mock `spawn_subagent_direct`，模型传
   `run_timeout_seconds=1800`，验证 mock 收到 `run_timeout_seconds=1800`
2. **`test_spawn_clamps_below_min`** — 模型传 `60`，验证传给 direct 的是 `600`
3. **`test_spawn_clamps_above_max`** — 模型传 `99999`，验证传给 direct 的是 `7200`
4. **`test_spawn_none_falls_back_to_global`** — 模型不传（`None`），验证传给 direct
   的是 `None`（由 `resolve_run_timeout_seconds` 回退到全局 `600.0`）
5. **`test_spawn_zero_or_negative_treated_as_none`** — 模型传 `0` 和 `-1`，验证
   clamp 返回 `None`（非正值 = "未设"语义，和 `resolve_run_timeout_seconds` 一致）
6. **`test_spawn_in_bounds_unchanged`** — 模型传 `600`（边界）、`7200`（边界）、
   `1200`（中间），验证原样传

---

## 4. 边界场景

| 场景 | 输入 | clamp 结果 | 传给 direct | 最终 timeout | 理由 |
|------|------|-----------|-------------|-------------|------|
| 模型不传 | `None` | `None` | `None` | 600（全局默认） | 全局兜底 |
| 模型传下限 | `600` | `600` | `600` | `600` | 边界值原样 |
| 模型传上限 | `7200` | `7200` | `7200` | `7200` | 边界值原样 |
| 模型传中间值 | `1800` | `1800` | `1800` | `1800` | 在范围内原样 |
| 模型传低于下限 | `60` | `600` | `600` | `600` | clamp 到下限 |
| 模型传高于上限 | `99999` | `7200` | `7200` | `7200` | clamp 到上限 |
| 模型传 0 | `0` | `None` | `None` | `600` | 非正值 = 未设 |
| 模型传负数 | `-1` | `None` | `None` | `600` | 非正值 = 未设 |
| 模型传浮点数 | `900.5` | `900.5` | `900.5` | `900.5` | float 合法 |
| server 重启 | — | — | — | `600`（config 重载） | config 是单例，重启读默认 |
| 全局默认被覆盖 | — | `None` | `None` | 调用方设的值 | `set_config()` 覆盖全局，schema 不受影响 |
| bounds 被改 config | — | 新 bounds | — | — | 运营调 `run_timeout_min/max_seconds`，clamp 自动跟 |

---

## 5. 与关联文档的关系

| 文档 | 管什么 | 本文档管什么 |
|------|--------|-------------|
| `计划_续跑enforcer加turn级硬上限.md` §8.1 | 全局默认 `0.0 → 600.0`（一行改动，堵住不走 tool 的路径） | 本文档把这一行改动纳入，并加上 tool schema 暴露 + clamp |
| `计划_续跑enforcer加turn级硬上限.md` §3 | 主 agent 续跑 turn 计数器（enforcer 侧） | 不相关——enforcer 管的是续跑轮数，本文档管的是子代理 wall-clock |
| `计划_续跑enforcer加turn级硬上限.md` §7 | Completion Judge 性价比 | 不相关 |

**不冲突**：§8.1 改全局默认值是"兜底"，本文档加 schema 字段 + clamp 是"让模型
自己调"。两者互补：全局默认保证不传也有 10 分钟，schema 字段让模型能传更大的值。

---

## 6. 不做的事

- **不改 `resolve_run_timeout_seconds`** — 通用函数，被多条路径共用，在通用层加
  clamp 会影响 TaskFlow dispatch 和 steer 等调用方
- **不改 TaskFlow `_dispatch`** — 已有 `step_timeout_seconds` 传参路径，TaskFlow
  planner 在创建 step 时可以设 per-step 超时，不需要从 tool schema 层走
- **不加 per-goal-loop-turn 的总时长限制** — 现状每轮 invoke 各自 `wait_for`，
  5 轮 goal loop 最坏 5 × 7200s = 10 h。加总时长限制需要基于 `started_at` 的
  deadline 检查（`resolve_run_deadline_ms` 已定义但无人调用），是独立缺口
- **不持久化模型选的超时值** — 超时是 per-spawn 的，不需要跨 session 记忆
- **不把 bounds 放 `config/features/`** — `run_timeout_seconds` 已在
  `SubagentConfig`，bounds 放同层，不散落到两个 config 模块
- **不验证 `min <= default <= max`** — 可加 `@model_validator` 但本方案不做；
 三个值都是固定的 `600/600/7200`，运营改 config 时自行保证
- **不改 `_arun` 的返回字符串格式** — 现有返回 `Subagent spawned: status=...`
  的格式不变，不把 timeout 值写进返回文本（模型不需要知道自己被 clamp 了）
