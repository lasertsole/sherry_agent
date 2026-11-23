# 计划：Eval Plan Continuation 上限统一

- 日期：2026-11-23
- 状态：**待评审**
- 来源：deepswe continuation 上限 3 轮，devbench 上限 2 轮。同类 coding benchmark 行为不同，跨 suite 比较不公平。
- 风险等级：**低** — 改常量
- 依赖：无
- 可独立执行：✅

---

## 0. 问题

| Suite | Continuation 上限 | 位置 |
|-------|-------------------|------|
| deepswe | 3 | `evals/deepswe/suite.py:156` `_MAX_PLAN_CONTINUATIONS = 3` |
| devbench | 2 | `evals/devbench/suite.py:366` `range(1 + 2)` |

产品的 PlanContinuationEnforcer 上限是 `max_continuation_turns = 15`（`config/features/agent_side/todolist_infra.py:77`），但 eval 没有 server，harness 自己驱动 continuation，用更短的上限（3 或 2）。

## 1. 改动

统一为 3。3 已经是 deepswe 的值，且比 devbench 的 2 更接近产品行为（给 agent 更多自动续跑机会）。

| # | 文件 | 行 | 改动 | 说明 |
|---|------|----|------|------|
| 1 | `evals/devbench/suite.py` | 366 | `range(1 + 2)` → `range(1 + 3)` | 上限 2→3，和 deepswe 一致 |

**不改**：deepswe（已经是 3）、产品侧（15，eval 不用）、multi_swe_bench（如果也驱动 continuation 的话一并检查）。

## 2. 为什么统一为 3 不统一为 2

- 3 给 agent 更多自动续跑机会，更接近产品的 15 轮行为
- deepswe 已经用 3 且实测可接受
- 2 太短，complex fix 可能在第 2 轮还在进行就被截断

## 3. 验收

- devbench continuation 上限 = 3
- deepswe 不变（= 3）
- 两者一致
