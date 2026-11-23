# 计划：Iteration Budget 产品侧和 eval 侧统一可配

- 日期：2026-11-23
- 状态：**待评审**
- 来源：iteration budget 产品侧已有 session 级覆盖（Extraction Settings 面板），但 eval 侧只有 coding_capability 有 env 覆盖，其他 suite 不改。同类 benchmark 预算不一致。需要两侧都可配且行为统一。
- 风险等级：**低** — eval 侧加法式，产品侧不改
- 依赖：无
- 可独立执行：✅

---

## 0. 现状

### 产品侧（已可配，不改）

```
全局默认：ITERATION_BUDGET["main_agent_max_iterations"] = 180
  ↓
session 覆盖：effective_extraction_config(session_id)["iteration_budget_main"]
  ↓
IterationBudget 中间件通过 cap_resolver 读 session 覆盖，clamp [1, 1000]
  ↓
UI：预设角色 → 高级 → 经验学习面板的"主智能体迭代上限"输入框
```

产品侧已经有完整链路：全局默认 → session 覆盖 → 中间件读取 → UI 可调。**不改。**

### eval 侧（只有 coding_capability 可配）

| Suite | 能改 iteration budget？ | 机制 |
|-------|:-:|------|
| coding_capability | ✅ | `SHERRY_EVAL_MAX_ITERATIONS` env，`pin_eval_budget()` 直接改全局 `ITERATION_BUDGET` dict |
| deepswe | ❌ | 继承产品默认 180 |
| devbench | ❌ | 继承产品默认 180 |
| multi_swe_bench | ❌ | 继承产品默认 180 |
| osworld | ❌ | 继承产品默认 180 |

coding_capability 的 `pin_eval_budget()`（`evals/coding_capability/suite.py:127-133`）直接改全局 config dict，不 restore，泄露到后续 suite。

## 1. 设计

### 1.1 eval 侧统一

把 `pin_eval_budget()` 提取到 `evals/shared/`，所有 coding benchmark suite 调用：

```python
# evals/shared/eval_iteration_budget.py

def pin_eval_budget(max_iterations: int = 0) -> int:
    """Pin the main agent's iteration budget for eval runs.

    0 = 不改（继承产品默认 180）。
    正数 = 替换全局上限。
    返回生效的值（用于报告）。
    """
    if max_iterations <= 0:
        from config.features import ITERATION_BUDGET
        return ITERATION_BUDGET["main_agent_max_iterations"]
    from config.features import ITERATION_BUDGET
    ITERATION_BUDGET["main_agent_max_iterations"] = max_iterations
    return max_iterations


def resolve_eval_iteration_budget() -> int:
    """从 env 读取 eval iteration budget，默认 0（不改）。

    SHERRY_EVAL_MAX_ITERATIONS=300 → 钉 300
    不设 / =0 → 继承产品默认
    """
    raw = os.getenv("SHERRY_EVAL_MAX_ITERATIONS", "0").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 0
```

每个 suite 在 `_run` 开头：

```python
from evals.shared.eval_iteration_budget import pin_eval_budget, resolve_eval_iteration_budget

budget = pin_eval_budget(resolve_eval_iteration_budget())
logger.info("Eval iteration budget: {}", budget)
```

### 1.2 防泄露

`pin_eval_budget` 直接改全局 dict，eval 结束后不 restore。如果同进程跑多个 suite，泄露。

修法：在 `EvalSandbox` 里记录原值，restore 时恢复：

```python
class EvalSandbox:
    def apply(self):
        from config.features import ITERATION_BUDGET
        self._original_iteration_budget = ITERATION_BUDGET["main_agent_max_iterations"]
        # ... existing apply ...

    def restore(self):
        from config.features import ITERATION_BUDGET
        ITERATION_BUDGET["main_agent_max_iterations"] = self._original_iteration_budget
        # ... existing restore ...
```

### 1.3 产品侧（不改，确认链路完整）

```
全局默认：sherry.jsonc extraction.iteration_budget_main = 180
  → PUT /extraction/global 修改（UI：预设角色 → 高级 → 经验学习面板）
  → 改了影响所有未覆盖的 session
  ↓
session 覆盖：effective_extraction_config(session_id)["iteration_budget_main"]
  → PUT /sessions/extraction 修改（同面板，session 级覆盖全局）
  → 不设就继承全局
  ↓
IterationBudget 中间件通过 cap_resolver 读 session 覆盖，clamp [1, 1000]
```

产品侧已有完整链路：全局 UI + session UI + 中间件读取。**不改。**

### 1.4 两侧对比

| | 产品侧 | eval 侧（改后） |
|---|---|---|
| 默认值 | 180 | 180（继承产品默认） |
| 调整方式 | UI（全局 + session 级） | env `SHERRY_EVAL_MAX_ITERATIONS` |
| 作用范围 | 全局改影响所有 session，session 改只影响自己 | 全局（eval 进程内） |
| restore | 不需要（持久化配置） | EvalSandbox.restore() 恢复 |
| clamp | [1, 1000] | 无（eval 信任调用者） |

两侧机制不同但目标一致：产品侧 UI 两级可配（全局 + session），eval 侧 env 进程级可配。

---

## 2. 改动

| # | 文件 | 改动 | 说明 |
|---|------|------|------|
| 1 | `evals/shared/eval_iteration_budget.py` | **新建**：`pin_eval_budget` + `resolve_eval_iteration_budget` | 从 coding_capability 提取 |
| 2 | `evals/coding_capability/suite.py` | import 改为从 shared 导入 | 去重 |
| 3 | `evals/deepswe/suite.py` | `_run` 中调 `pin_eval_budget(resolve_eval_iteration_budget())` | 统一 |
| 4 | `evals/devbench/suite.py` | 同上 | |
| 5 | `evals/multi_swe_bench/suite.py` | 同上 | |
| 6 | `evals/osworld/suite.py` | 同上 | |
| 7 | `evals/sandbox.py` | `EvalSandbox.apply` 记录原值，`restore` 恢复 | 防泄露 |

**不改**：`config/features/agent_side/iteration_budget.py`、`runtime/session/extraction.py`、`agent/middlewares/specs.py`、UI（产品侧链路完整）。

---

## 3. 改后行为

### 不设 env（默认）

```
所有 coding suite → pin_eval_budget(0) → 不改 → 继承产品默认 180
```

### 设 env

```bash
SHERRY_EVAL_MAX_ITERATIONS=300 python evals/deepswe/suite.py
# → deepswe 钉 300
# → eval 结束后 EvalSandbox.restore() 恢复 180
# → 同进程跑 devbench → devbench 也读 env 钉 300（如果 env 还在）
```

### 产品侧

```
用户在 UI 设"主智能体迭代上限"= 250
→ 该 session 的 IterationBudget 用 250
→ 不影响其他 session
→ 不影响 eval（eval 不读 session 级覆盖）
```

---

## 4. 测试

| 测试 | 内容 |
|------|------|
| `test_pin_eval_budget_zero` | `pin_eval_budget(0)` → 不改，返回当前值 |
| `test_pin_eval_budget_positive` | `pin_eval_budget(300)` → 改为 300，返回 300 |
| `test_resolve_eval_iteration_budget_env` | `SHERRY_EVAL_MAX_ITERATIONS=300` → 返回 300 |
| `test_resolve_eval_iteration_budget_default` | 不设 env → 返回 0 |
| `test_resolve_eval_iteration_budget_invalid` | `SHERRY_EVAL_MAX_ITERATIONS=abc` → 返回 0 |
| `test_evalsandbox_restore_iteration_budget` | apply 记录原值 180 → pin 300 → restore 恢复 180 |
| `test_evalsandbox_restore_after_no_change` | apply 记录 180 → 不 pin → restore 恢复 180（不变） |

---

## 5. 验收

- 不设 env 时所有 coding suite 跑产品默认 180（行为不变）
- 设 `SHERRY_EVAL_MAX_ITERATIONS=300` 时所有 coding suite 钉 300
- eval 结束后 `ITERATION_BUDGET["main_agent_max_iterations"]` 恢复原值
- 同进程连续跑多个 suite，后续 suite 不继承前一个的修改
- 产品侧 session 级覆盖不受影响
- coding_capability 行为不变（只是 import 来源变了）
