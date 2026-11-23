# 计划：Eval Reviewer Checklist 提取到 shared

- 日期：2026-11-23
- 状态：**待评审**
- 来源：deepswe 给 reviewer 子 agent 追加 acceptance checklist（`evals/external_bench.py:911-943`），其他 coding suite 不加。同类 benchmark reviewer 行为不同。
- 风险等级：**低** — 加法式
- 依赖：无
- 可独立执行：✅

---

## 0. 问题

deepswe 的 `set_reviewer_checklist`（`evals/external_bench.py:911-943`）在 `sessions_spawn(functional_role="reviewer")` 时给 task 追加 acceptance checklist。只有 deepswe 调 `set_reviewer_checklist`（`suite.py:779`），devbench/multi_swe_bench 不调。

reviewer 是验证 step 完成质量的关键角色——有 checklist 的 reviewer 按条款逐条 PASS/FAIL，没有 checklist 的 reviewer 自由发挥。同类 benchmark 应统一。

## 1. 改动

| # | 文件 | 改动 | 说明 |
|---|------|------|------|
| 1 | `evals/devbench/suite.py` | 调 `set_reviewer_checklist(checklist_items)` | 和 deepswe 一致 |
| 2 | `evals/multi_swe_bench/suite.py` | 同上 | |

**不改**：`evals/external_bench.py` 的 `set_reviewer_checklist` / `_augment_reviewer_task` 逻辑（已经实现，只是其他 suite 没调）。

### checklist 来源

deepswe 从任务文本提取 acceptance checklist（`evals/deepswe/suite.py` 的 `_extract_checklist`）。devbench/multi_swe_bench 的任务文本格式不同，需要各自的 checklist 提取逻辑。

如果 devbench/multi_swe_bench 的任务文本不适合提取 checklist（没有明确的 normative keywords），可以传空列表（= 不追加，和当前行为一致）或用一个通用的 fallback checklist。

## 2. 验收

- deepswe：reviewer 追加 checklist（不变）
- devbench：reviewer 追加 checklist（新增）
- multi_swe_bench：reviewer 追加 checklist（新增）
- 三者行为一致
