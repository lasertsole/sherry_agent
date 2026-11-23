# 计划：Reviewer 子 agent 自动提取 acceptance checklist（产品侧）

- 日期：2026-11-23
- 状态：**待评审**
- 来源：eval 侧 deepswe 已有 `set_reviewer_checklist` 给 reviewer 追加验收清单，产品侧没有。reviewer 自由发挥不如按条款逐条 PASS/FAIL 严格。
- 风险等级：**低-中** — 在 spawn 管线加一步 checklist 提取，fail-open
- 依赖：无
- 可独立执行：✅

---

## 0. 现状

### reviewer 子 agent

- **何时 spawn**：TaskFlow step 的 `functional_role="reviewer"` 时，在 dispatch 阶段 spawn（和 executor/researcher 一样是普通 step 子 agent）
- **模型**：auxiliary LLM（不共享主 agent 模型）
- **上下文**：完全独立（自己的 checkpointer、中间件、system prompt、迭代预算）
- **工具**：read_file, terminal, share_finding, peers_findings（只读，不能改文件）
- **质量门**：reviewer 产出后，`step_judge.py` 做一次单独的 LLM PASS/RETRY/BLOCK 判断（不是 reviewer 自己判）

### eval 侧已有

`evals/external_bench.py:911-943` 的 `set_reviewer_checklist` + `_augment_reviewer_task`：
- 在 spawn 时拦截 `sessions_spawn` 参数
- 从任务文本提取规范性关键词（must/raise/reject/only/charset 等）
- 编成编号 checklist 追加到 `args["task"]` 末尾
- reviewer 拿到带 checklist 的任务，逐条 PASS/FAIL

### 产品侧没有

reviewer 拿到的是原始 task 文本，自由发挥写 prose review，没有结构化验收。

---

## 1. 设计

### 1.1 在 spawn 管线注入 checklist

在 `dispatch_child` 或 `spawn_subagent_direct` 中，当 `functional_role="reviewer"` 时，从 task 文本提取规范性关键词，编成 checklist 追加到 task 末尾。

**注入点**：`agent/tools/taskflow/tools/_dispatch.py` 的 `dispatch_child` 函数，在构造 `SpawnRequest` 之前。这里是所有 TaskFlow step spawn 的入口，产品侧和 eval 侧都经过。

**eval 侧兼容**：eval 的 `set_reviewer_checklist` 通过 monkeypatch 注入，优先级更高。产品侧提取的 checklist 如果 eval 已设了 checklist，不覆盖（eval 的优先）。

### 1.2 checklist 提取逻辑

从 eval 的 `_augment_reviewer_task` 提取核心逻辑，做成产品侧通用函数：

```python
# agent/tools/taskflow/checklist_extractor.py

def extract_acceptance_checklist(task_text: str) -> list[str]:
    """从任务文本提取规范性关键词，编成 acceptance checklist。

    匹配 must/should/raise/reject/only/charset/must not 等关键词所在的句子，
    提取成编号条款。返回空列表表示没有可提取的规范性条款。
    """
    # 复用 eval 的提取逻辑（关键词集合 + 句子分割）
    # ...
```

### 1.3 开关

```python
# config/features/agent_side/taskflow_infra.py
"reviewer_checklist_enabled": True,  # 默认开
```

UI 可调（System Config 或 Extraction Settings）。

### 1.4 注入格式

```
{原始 task 文本}

---
ACCEPTANCE CHECKLIST — verify each clause, answer PASS or FAIL:
1. {条款1}
2. {条款2}
3. {条款3}
```

reviewer 的角色定义（`reviewer/AGENTS.md`）已有"逐条验证"的指引，checklist 格式与之兼容。

---

## 2. 改动

| # | 文件 | 改动 | 说明 |
|---|------|------|------|
| 1 | `agent/tools/taskflow/checklist_extractor.py` | **新建**：`extract_acceptance_checklist(task_text)` | 从 eval 提取逻辑复用 |
| 2 | `agent/tools/taskflow/tools/_dispatch.py` | `dispatch_child` 中 reviewer 角色时调用 checklist 提取 + 追加 | 注入点 |
| 3 | `config/features/agent_side/taskflow_infra.py` | 新增 `reviewer_checklist_enabled: True` | 开关 |
| 4 | `evals/external_bench.py` | `_augment_reviewer_task` 检查产品侧是否已追加 checklist，避免重复 | eval 兼容 |

---

## 3. 改动细节

### 3.1 checklist 提取函数

```python
# agent/tools/taskflow/checklist_extractor.py

import re

_NORMATIVE_PATTERNS = [
    r"\bmust\b",
    r"\bmust not\b",
    r"\bshould\b",
    r"\bshall\b",
    r"\braise\b",
    r"\breject\b",
    r"\baccept\b",
    r"\bonly\b",
    r"\bnever\b",
    r"\balways\b",
    r"\bcharset\b",
    r"\breturn\b",
    r"\berror\b",
]

def extract_acceptance_checklist(task_text: str) -> list[str]:
    """从任务文本提取规范性条款。

    扫描每个句子，如果包含规范性关键词，提取成 checklist 条款。
    返回条款列表（空列表 = 无可提取条款，不追加 checklist）。
    """
    if not task_text or not task_text.strip():
        return []

    # 按句号/换行分割成句子
    sentences = re.split(r'[.\n]', task_text)
    clauses = []
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence or len(sentence) < 5:
            continue
        for pattern in _NORMATIVE_PATTERNS:
            if re.search(pattern, sentence, re.IGNORECASE):
                clauses.append(sentence)
                break  # 每个句子只提取一次

    return clauses[:20]  # 上限 20 条
```

### 3.2 dispatch_child 注入

在 `agent/tools/taskflow/tools/_dispatch.py` 的 `dispatch_child` 中，构造 `SpawnRequest` 之前：

```python
from config.features import TASKFLOW_INFRA
from agent.tools.taskflow.checklist_extractor import extract_acceptance_checklist

# 在构造 spawn kwargs 时
if (
    TASKFLOW_INFRA.get("reviewer_checklist_enabled", True)
    and functional_role == "reviewer"
    and task
):
    checklist = extract_acceptance_checklist(task)
    if checklist:
        checklist_text = "\n\n---\nACCEPTANCE CHECKLIST — verify each clause, answer PASS or FAIL:\n"
        checklist_text += "\n".join(f"{i+1}. {clause}" for i, clause in enumerate(checklist))
        task = task + checklist_text
```

### 3.3 eval 兼容

eval 的 `_augment_reviewer_task`（`evals/external_bench.py:928`）检查 task 是否已含 `ACCEPTANCE CHECKLIST`：

```python
def _augment_reviewer_task(args: dict) -> bool:
    task = str(args.get("task") or "")
    if "ACCEPTANCE CHECKLIST" in task:
        return False  # 产品侧已追加，不重复
    # ... eval 自己的 checklist 追加 ...
```

### 3.4 配置

```python
# config/features/agent_side/taskflow_infra.py
TASKFLOW_INFRA = {
    # ... existing ...
    "reviewer_checklist_enabled": True,  # reviewer 自动提取 acceptance checklist
}
```

---

## 4. 执行效果

### 有规范性关键词的任务

```
task = "Fix the JSON parser. It must handle UTF-8 BOM.
         Reject empty charset. Raise ValueError on invalid encoding."

→ extract_acceptance_checklist 返回:
  ["It must handle UTF-8 BOM", "Reject empty charset",
   "Raise ValueError on invalid encoding"]

→ reviewer 拿到:
  {原始 task}

  ---
  ACCEPTANCE CHECKLIST — verify each clause, answer PASS or FAIL:
  1. It must handle UTF-8 BOM
  2. Reject empty charset
  3. Raise ValueError on invalid encoding
```

reviewer 逐条验证，每条 PASS/FAIL，比自由发挥"看起来不错"严格得多。

### 无规范性关键词的任务

```
task = "Refactor the module for readability."
→ extract_acceptance_checklist 返回 []
→ 不追加 checklist
→ reviewer 自由发挥（和当前行为一致）
```

### eval 侧

eval 的 `set_reviewer_checklist` 设了自定义 checklist → 产品侧追加的 `ACCEPTANCE CHECKLIST` 已在 task 里 → eval 的 `_augment_reviewer_task` 检测到已存在 → 不重复追加。

---

## 5. 不改的东西

| 项 | 理由 |
|----|------|
| reviewer 角色定义（`reviewer/AGENTS.md`） | 已有"逐条验证"指引，不改 |
| `step_judge.py` | 质量门是独立 LLM 调用，不改 |
| reviewer 的模型/工具/上下文隔离 | 不改——checklist 只改 task 文本 |
| eval 的 `set_reviewer_checklist` / `_augment_reviewer_task` | 保留——eval checklist 优先，产品侧不覆盖 |

---

## 6. 测试

| 测试 | 内容 |
|------|------|
| `test_extract_checklist_basic` | task 含 must/raise/reject → 提取 3 条 |
| `test_extract_checklist_no_keywords` | task = "Refactor for readability" → 返回空列表 |
| `test_extract_checklist_empty_task` | task = "" → 返回空列表 |
| `test_extract_checklist_max_20` | task 含 25 个规范性句子 → 只返回 20 条 |
| `test_dispatch_appends_checklist` | reviewer step dispatch → task 末尾有 ACCEPTANCE CHECKLIST |
| `test_dispatch_no_checklist_for_executor` | executor step dispatch → task 不变 |
| `test_dispatch_checklist_disabled` | `reviewer_checklist_enabled=False` → 不追加 |
| `test_dispatch_no_checklist_when_empty` | task 无关键词 → 不追加（不追加空 checklist） |
| `test_eval_does_not_duplicate` | task 已含 ACCEPTANCE CHECKLIST → eval 不重复追加 |
| `test_extract_checklist_case_insensitive` | "MUST" / "must" / "Must" 都匹配 |

---

## 7. 风险

| 风险 | 缓解 |
|------|------|
| 提取的 checklist 质量差（非规范性句子被误提取） | 关键词集合保守（must/raise/reject 等强规范性词），弱词不收 |
| checklist 过长占 reviewer 上下文 | 上限 20 条，每条是一个句子，通常 < 2K 字符 |
| 产品侧和 eval 侧重复追加 | eval 检查 `ACCEPTANCE CHECKLIST` 是否已存在，已存在不重复 |
| reviewer 角色不按 checklist 逐条验证 | 角色定义已有"逐条验证"指引；checklist 格式明确要求 PASS/FAIL |
| 非 reviewer 角色被误注入 | 只在 `functional_role == "reviewer"` 时触发 |

### Kill criteria

- 若提取质量差导致 reviewer 混淆，加 LLM 二次过滤（用 auxiliary LLM 判断句子是否是真正的验收条款）
- 若 checklist 过长影响 reviewer 上下文，降到上限 10 条
- 若产品侧和 eval 冲突难处理，改为只在 eval 不设 checklist 时产品侧才提取

---

## 8. 验收

- task 含 must/raise/reject → reviewer 的 task 末尾有 ACCEPTANCE CHECKLIST
- task 无规范性关键词 → 不追加（不追加空 checklist）
- `reviewer_checklist_enabled=False` → 不追加
- executor/researcher 角色 → 不追加
- eval 已设 checklist → 产品侧不重复追加
- checklist 上限 20 条
- 大小写不敏感
- reviewer 模型/工具/上下文隔离不受影响（只改 task 文本）
