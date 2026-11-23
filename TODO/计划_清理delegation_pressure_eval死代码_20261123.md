# 计划：清理 Delegation Pressure eval 死代码

- 日期：2026-11-23
- 状态：**待评审**
- 来源：`agent/middlewares/delegation_pressure/core.py:96` 检查 `session_id.startswith("evals-swe-")`，但没有任何 suite 用这个前缀（deepswe 用 `evals-deepswe-`，devbench 用 `evals-devbench-`，coding 用 `evals-coding-` 等）。env var 开了中间件但 `_active()` 永远返回 False，永不触发。
- 风险等级：**低** — 删死代码
- 依赖：无
- 可独立执行：✅

---

## 0. 问题

```python
# agent/middlewares/delegation_pressure/core.py:96
if os.getenv("SHERRY_EVAL_DELEGATION_PRESSURE") == "1" and session_id.startswith("evals-swe-"):
    return True
```

`specs.py:273-276` 里 `_delegation_pressure_enabled()` 用 `os.getenv("SHERRY_EVAL_DELEGATION_PRESSURE") == "1"` OR `DELEGATION_PRESSURE["enabled"]` 决定是否注册中间件。env var 开了 → 中间件注册了。但 `core.py:96` 的 `_active()` 检查 session 前缀 `evals-swe-` → 没有 suite 用这个前缀 → `_active()` 返回 False → 中间件注册了但不触发。

这是历史遗留：早期 eval session 可能用 `evals-swe-` 前缀，后来 suite 名字分化了但前缀检查没更新。

## 1. 改动

| # | 文件 | 行 | 改动 | 说明 |
|---|------|----|------|------|
| 1 | `agent/middlewares/delegation_pressure/core.py` | 96 | 删除 `session_id.startswith("evals-swe-")` 前缀检查 | 改为：env var 开了就 `_active` 返回 True，不限 session 前缀 |

### 改前

```python
def _active(self, state) -> bool:
    session_id = str(state.get("session_id") or "")
    if os.getenv("SHERRY_EVAL_DELEGATION_PRESSURE") == "1" and session_id.startswith("evals-swe-"):
        return True
    if session_id.startswith(":subagent:") or session_id.startswith(":swarm:"):
        return False
    return bool(DELEGATION_PRESSURE["enabled"])
```

### 改后

```python
def _active(self, state) -> bool:
    session_id = str(state.get("session_id") or "")
    if os.getenv("SHERRY_EVAL_DELEGATION_PRESSURE") == "1":
        return True
    if session_id.startswith(":subagent:") or session_id.startswith(":swarm:"):
        return False
    return bool(DELEGATION_PRESSURE["enabled"])
```

去掉 `and session_id.startswith("evals-swe-")` 条件。env var 开了就生效，不限 session 前缀。

## 2. 不改的东西

| 项 | 理由 |
|----|------|
| `specs.py:273-276` 的 `_delegation_pressure_enabled()` | 已经是 `env var OR config`，不限前缀，正确 |
| `DELEGATION_PRESSURE["enabled"]` 默认 True | 产品侧行为不变 |
| `:subagent:`/`:swarm:` 排除 | 子 agent 不触发 delegation pressure，正确 |

## 3. 验收

- 产品侧（不设 env var）：`_active` 返回 `DELEGATION_PRESSURE["enabled"]`（默认 True），行为不变
- eval 侧（设 `SHERRY_EVAL_DELEGATION_PRESSURE=1`）：`_active` 返回 True，中间件触发（之前不触发）
- `:subagent:`/`:swarm:` session 仍返回 False
- 现有测试通过
