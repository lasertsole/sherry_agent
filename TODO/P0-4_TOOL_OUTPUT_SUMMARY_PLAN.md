# P0-4 工具输出一行摘要替代 — 实施计划

> 日期: 2026-09-11
> 来源: SESSION_MEMORY_BORROWING_PLAN.md §P0-4 (行 599-694)
> 决策: 修改 prune_tool_outputs + 新增 _summarize_tool_result + 工具特定模板
> 预估工时: 0.5 天

---

## 1. 问题

`prune_tool_outputs` 策略直接删除旧工具输出或截断到 `MAX_TOOL_OUTPUT_CHARS`。模型面对被删除工具输出的空消息（`_PRUNE_MARKER = "[Old tool result content cleared]"`），无法理解之前做了什么。

**当前状态：**

- `prune_tool_outputs`（`pub/func/message/tool_output_prune.py:28`）遍历 `ToolMessage`，超限的用 `_PRUNE_MARKER = "[Old tool result content cleared]"` 替换
- `_find_tool_name()`（行 16）从 AIMessage 的 tool_calls 中查找工具名
- `PROTECTED_TOOLS = frozenset({"memory", "skill_view", "skill_list"})`（`config/num.py:45`）受保护不被裁剪
- `MAX_TOOL_OUTPUT_CHARS = 2_000`（`config/num.py:23`）
- `_PRUNE_MARKER` 仅 38 chars，无语义信息
- `target_truncate_tool_outputs`（`pub/func/message/target_truncation.py`）在 prune 之后进一步截断到 `MAX_TOOL_OUTPUT_CHARS`

---

## 2. 设计决策

### 2.1 用一行摘要替代 `_PRUNE_MARKER`

当 `prune_tool_outputs` 标记一个 ToolMessage 为需要裁剪时，不替换为无意义的 `_PRUNE_MARKER`，而是根据工具名生成一行语义摘要（如 `[bash] exit_code=0, 1200 chars`）。

### 2.2 关键决策

| 决策点       | 选择                                    | 理由                                                      |
| ------------ | --------------------------------------- | --------------------------------------------------------- |
| 模板位置     | `pub/func/message/tool_output_prune.py` | 与现有 prune 逻辑同模块                                   |
| 模板格式     | 按工具名分发 lambda                     | 简单可扩展；无需新依赖                                    |
| 未知工具     | `_default` 模板：前 100 chars + 长度    | 兜底，至少保留一些语义                                    |
| 受保护工具   | 不裁剪                                  | 与现有逻辑一致                                            |
| 截断 vs 摘要 | 先摘要再截断                            | `prune_tool_outputs` 用摘要替代；`target_truncate` 仍截断 |
| 摘要失败     | 降级为 `_PRUNE_MARKER`                  | 现有兜底不变                                              |

---

## 3. 涉及文件

| 文件                                               | 操作                                 | 预估行数 |
| -------------------------------------------------- | ------------------------------------ | -------- |
| `pub/func/message/tool_output_prune.py`            | 修改：新增模板 + 摘要函数 + 替换逻辑 | ~50      |
| `tests/pub/func/message/test_tool_output_prune.py` | 修改/追加测试                        | ~60      |

---

## 4. 详细设计

### 4.1 pub/func/message/tool_output_prune.py — 修改

```python
from config.num import PRUNE_PROTECT_TOKENS, PRUNE_MIN_REDUCTION_TOKENS
from langchain_core.messages import (
    BaseMessage,
    ToolMessage,
    AIMessage,
)

_PRUNE_MARKER = "[Old tool result content cleared]"
_SUMMARY_LC_SOURCE = "summarization"


# === 新增：工具输出摘要模板 (P0-4) ===

def _extract_exit_code(result_text: str) -> str:
    """尝试从 bash 输出中提取退出码。"""
    import re
    # 常见格式: "Exit code: 0" / "exit code 1" / "退出码: 0"
    match = re.search(r"[Ee]xit\s*[Cc]ode[:\s]+(\d+)", result_text)
    if match:
        return match.group(1)
    return "unknown"


def _count_lines(text: str) -> int:
    return text.count("\n") + 1 if text.strip() else 0


_TOOL_SUMMARY_TEMPLATES: dict[str, "callable"] = {
    # 文件操作类
    "read": lambda r: f"[read] read file, {len(r)} chars, {_count_lines(r)} lines",
    "read_file": lambda r: f"[read_file] read file, {len(r)} chars, {_count_lines(r)} lines",
    "write": lambda r: f"[write] wrote file, {len(r)} chars",
    "write_file": lambda r: f"[write_file] wrote file, {len(r)} chars",
    "edit": lambda r: f"[edit] edited file, {len(r)} chars",
    "edit_file": lambda r: f"[edit_file] edited file, {len(r)} chars",

    # 搜索类
    "grep": lambda r: f"[grep] search done, {_count_lines(r)} matches",
    "glob": lambda r: f"[glob] matched {len(r.strip().splitlines())} files",

    # 代码执行类
    "bash": lambda r: (
        f"[bash] exit_code={_extract_exit_code(r)}, "
        f"output {len(r)} chars"
    ),
    "shell": lambda r: (
        f"[shell] exit_code={_extract_exit_code(r)}, "
        f"output {len(r)} chars"
    ),

    # 任务流类
    "taskflow_summary": lambda r: f"[taskflow_summary] {len(r)} chars",
    "taskflow_run_task": lambda r: f"[taskflow_run_task] dispatched, {len(r)} chars",
    "taskflow_resume": lambda r: f"[taskflow_resume] injected result, {len(r)} chars",

    # 记忆类
    "memory": lambda r: f"[memory] {r[:80]}...",
}


_DEFAULT_TEMPLATE = lambda r: (
    f"[tool] output {len(r)} chars, "
    f"first 100: {r[:100]}..."
)


def _summarize_tool_result(tool_name: str, result_text: str) -> str:
    """为工具输出生成一行摘要。

    按工具名分发到特定模板；未知工具用 _default。
    任何异常降级为通用格式。
    """
    template = _TOOL_SUMMARY_TEMPLATES.get(tool_name, _DEFAULT_TEMPLATE)
    try:
        summary = template(result_text)
        # 限制摘要长度（一行，max 200 chars）
        if len(summary) > 200:
            summary = summary[:197] + "..."
        return summary
    except Exception:
        return f"[{tool_name}] output {len(result_text)} chars"
```

修改 `prune_tool_outputs()` 中的裁剪逻辑：

```python
def prune_tool_outputs(
    messages: list[BaseMessage],
    protect_tokens: int = PRUNE_PROTECT_TOKENS,
    min_reduction_tokens: int = PRUNE_MIN_REDUCTION_TOKENS,
    protected_tools: set[str] | None = None,
    estimator=None,
) -> tuple[list[BaseMessage], int]:
    # ... 现有逻辑：遍历、找 tool_name、检查 protected ...

    # 现有裁剪逻辑（简化示意）:
    # for idx in to_prune:
    #     messages[idx] = msg.model_copy(update={"content": _PRUNE_MARKER})
    #     messages[idx].additional_kwargs["status"] = "compacted"

    # 修改为:
    for idx in to_prune:
        msg = messages[idx]
        tool_name = _find_tool_name(messages, idx, getattr(msg, "tool_call_id", ""))
        content = msg.content if isinstance(msg.content, str) else str(msg.content)

        # 用一行摘要替代完整输出
        summary = _summarize_tool_result(tool_name, content)
        messages[idx] = msg.model_copy(
            update={
                "content": summary,
                "additional_kwargs": {
                    **getattr(msg, "additional_kwargs", {}),
                    "status": "compacted",
                    "original_length": len(content),
                },
            }
        )

    # ... 返回 ...
```

---

## 5. 实施顺序

```
Step 1: pub/func/message/tool_output_prune.py — 新增 _TOOL_SUMMARY_TEMPLATES + _summarize_tool_result()
Step 2: pub/func/message/tool_output_prune.py — 修改 prune_tool_outputs() 用摘要替代 _PRUNE_MARKER
Step 3: tests/pub/func/message/test_tool_output_prune.py — 追加测试
Step 4: 运行测试 + lint + typecheck
```

---

## 6. 测试计划

| 测试                                      | 说明                                                               |
| ----------------------------------------- | ------------------------------------------------------------------ |
| `test_summarize_bash`                     | bash 输出 → `[bash] exit_code=0, output 1200 chars`                |
| `test_summarize_bash_no_exit_code`        | 无退出码 → `exit_code=unknown`                                     |
| `test_summarize_read`                     | read 输出 → `[read] read file, N chars, M lines`                   |
| `test_summarize_grep`                     | grep 输出 → `[grep] search done, N matches`                        |
| `test_summarize_glob`                     | glob 输出 → `[glob] matched N files`                               |
| `test_summarize_unknown_tool`             | 未知工具 → `[tool] output N chars, first 100: ...`                 |
| `test_summarize_truncates_at_200`         | 超长摘要 → 截断到 200 chars                                        |
| `test_summarize_exception_fallback`       | 模板异常 → `[tool_name] output N chars`                            |
| `test_prune_uses_summary_not_marker`      | prune 后 content 不含 `_PRUNE_MARKER`，含摘要                      |
| `test_prune_protected_tools_not_affected` | 受保护工具 → 不裁剪                                                |
| `test_prune_marks_additional_kwargs`      | 裁剪后 additional_kwargs 有 `status=compacted` + `original_length` |

### 运行验证

```bash
python -m pytest tests/pub/func/message/test_tool_output_prune.py -v
python -m pytest tests/agent/middlewares/test_summarization_comprehensive.py -v  # 确保不破坏
ruff check pub/func/message/tool_output_prune.py
pyright pub/func/message/tool_output_prune.py
```

---

## 7. 数据流

```
压缩触发 → _run_non_llm_strategies(messages)
  → prune_tool_outputs(messages, ...)
    → 遍历 ToolMessages（从后往前，跳过 protected）
    → 找到超限的 ToolMessage: content = 5000 chars, tool_name = "bash"
    → _summarize_tool_result("bash", content)
      → _extract_exit_code(content) → "0"
      → 返回 "[bash] exit_code=0, output 5000 chars"
    → 替换 content = "[bash] exit_code=0, output 5000 chars"
    → additional_kwargs: status=compacted, original_length=5000
    → 节省 ~4800 chars (~1200 tokens)
  → target_truncate_tool_outputs → 进一步截断其他工具输出
  → truncate_tool_args → 截断工具调用参数
```

**效果对比：**

```
Before (P0-4): content = "[Old tool result content cleared]"
  → 模型完全不知道之前执行了什么命令

After (P0-4):  content = "[bash] exit_code=0, output 5000 chars"
  → 模型知道：执行了 bash 命令，成功（exit_code=0），输出较大
  → 可据此决定是否需要重新执行
```

---

## 8. 与其他方案的关系

| 方案         | 关系                                                                                           |
| ------------ | ---------------------------------------------------------------------------------------------- |
| **P0-1**     | 独立。P0-4 在非 LLM 策略中摘要工具输出；P0-1 在 LLM 摘要前提取事实。不同阶段。                 |
| **LT-7**     | 独立。P0-4 优化非 LLM 策略；LT-7 优化 LLM 摘要 prompt。不同阶段。                              |
| **压缩管线** | 协作。P0-4 是 `_run_non_llm_strategies` 的第一步改进，与 dedup/truncate/target_truncate 级联。 |

---

## 9. 风险与缓解

| 风险                              | 缓解                                                                                                                         |
| --------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| 工具名不在模板中                  | `_default` 模板兜底：前 100 chars + 长度                                                                                     |
| 摘要模板异常                      | try/except 降级为 `[tool_name] output N chars`                                                                               |
| 摘要仍占 token                    | 限制 200 chars（~50 tokens）；比原输出少很多                                                                                 |
| 工具名查找失败（_find_tool_name） | 返回 ""，用 `_default` 模板                                                                                                  |
| 与 target_truncate 冲突           | 不冲突：prune 用摘要替代（~200 chars），target_truncate 截断到 MAX_TOOL_OUTPUT_CHARS（2000 chars），但 prune 后内容已 < 2000 |
| 工具命名不一致                    | 模板同时支持 `read` 和 `read_file`、`bash` 和 `shell` 等别名                                                                 |
