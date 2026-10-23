# 计划：terminal_bg 工具合并（2→1，action 模式）

- 建档日期：2026-10-23
- 状态：**计划（未开工）**
- **文档范围**：把 `terminal_bg_list` + `terminal_bg_output` 合并为 `terminal_bg(action=list/output)`。零影响——零中间件检查、零模型面文本、零客户端引用、零配置 tuple。
- 关联：`调研_工具合并影响面分析.md` §8、`计划_工具合并推荐方案.md` §1.1

---

## 1. 当前状态

### 1.1 两个工具

| 工具 | 文件 | 作用 | metadata |
|---|---|---|---|
| `terminal_bg_list` | `agent/tools/terminal_bg_tools.py:25` | 列出当前 session 的后台终端任务 | `READ_ONLY_TOOLS`、`scope="main_only"` |
| `terminal_bg_output` | `agent/tools/terminal_bg_tools.py:51` | 读取一个后台任务的尾部输出 | `READ_ONLY_TOOLS`、`scope="main_only"` |

两个都是 `@tool` 装饰器函数，操作同一个 `background_registry`。

### 1.2 当前引用点（全量，确认零影响）

| 文件 | 行 | 引用内容 | 合并后 |
|---|---|---|---|
| `agent/tools/catalog.py` | 46 | `TOOL_GROUPS["terminal"]=("terminal","python_repl","terminal_bg_list","terminal_bg_output")` | 改为 `("terminal","python_repl","terminal_bg")` |
| `agent/tools/parallel_safety.py` | 56-57 | `READ_ONLY_TOOLS` 含两个名 | 改为 `"terminal_bg"` |
| `agent/tools/__init__.py` | 18-19 | 导入 `build_terminal_bg_list_tool`/`build_terminal_bg_output_tool` | 改为导入 `build_terminal_bg_tool` |
| `agent/tools/__init__.py` | 60-61 | `_MAIN_TOOLS_BUILDERS` 含两个 builder | 改为 1 个 |
| `agent/tools/terminal_bg_tools.py` | 全文件 | 2 个 `@tool` 函数 + 2 个 builder | 合并为 1 个 |
| `agent/tools/terminal.py` | 207, 605, 688-689 | 输出文本提到 `terminal_bg_list`/`terminal_bg_output` | 改文本 |
| `agent/tools/terminal_bg_registry.py` | 205, 219 | docstring | 改文本 |
| `config/features/agent_side/terminal_bg.py` | 13 | docstring | 改文本 |
| `evals/external_bench.py` | 58-59 | `DISABLED_TOOLS` 含两个名 | 改为 `"terminal_bg"` |

**零中间件字符串检查、零模型面 prompt 文本、零客户端引用。**

---

## 2. 合并方案

### 2.1 合并后的工具

```python
# agent/tools/terminal_bg_tools.py

from typing import Literal
from langchain_core.tools import tool

@tool("terminal_bg")
async def terminal_bg(
    action: Literal["list", "output"],
    task_id: str | None = None,
    lines: int = 50,
    session_id: str = "",
) -> str:
    """List this session's background terminal tasks, or read the tail output of one.

    Actions:
      list   — List background terminal tasks for this session.
      output — Read the tail output of one task (requires task_id).
    """
    if action == "list":
        return await _terminal_bg_list(session_id)
    elif action == "output":
        if not task_id:
            return _param_error(action, missing=["task_id"])
        return await _terminal_bg_output(task_id, lines, session_id)
    return _param_error(action, unknown=True)
```

### 2.2a action-aware 校验层

合并后所有参数都是 optional——Pydantic schema 校验不拦"缺必填参数"。handler dispatch 前加校验表，缺失时返回**结构化错误 + 该 action 的参数规则**让模型自我纠正：

```python
_ACTION_RULES: dict[str, dict] = {
    "list":   {"required": [], "optional": {"lines": "int (tail line count, default 50)"}},
    "output": {"required": {"task_id": "str — background task ID"}, "optional": {"lines": "int (tail line count, default 50)"}},
}

def _param_error(action: str, *, missing: list[str] | None = None, unknown: bool = False) -> str:
    """返回给模型的参数错误反馈，包含该 action 的完整参数规则。"""
    if unknown or action not in _ACTION_RULES:
        return (
            f"Unknown action '{action}'. "
            f"Available actions: {', '.join(sorted(_ACTION_RULES.keys()))}"
        )
    rules = _ACTION_RULES[action]
    parts = [f"Error: action='{action}' has incorrect parameters."]
    if missing:
        parts.append(f"Missing required: {', '.join(missing)}")
    # 列出该 action 的参数规则
    if rules["required"]:
        parts.append("Required: " + ", ".join(f"{k} ({v})" for k, v in rules["required"].items()))
    if rules["optional"]:
        parts.append("Optional: " + ", ".join(f"{k} ({v})" for k, v in rules["optional"].items()))
    # 示例
    req_example = ", ".join(f'{p}=<value>' for p in (missing or list(rules["required"])))
    parts.append(f'Example: terminal_bg(action="{action}"{", " + req_example if req_example else ""})')
    return " ".join(parts)
```

**模型看到的错误反馈示例**：

```
模型调用: terminal_bg(action="output")  # 缺 task_id

返回: Error: action='output' has incorrect parameters.
      Missing required: task_id
      Required: task_id (str — background task ID)
      Optional: lines (int — tail line count, default 50)
      Example: terminal_bg(action="output", task_id=<value>)

模型下一轮: terminal_bg(action="output", task_id="bg-task-3")  ✅
```

```
模型调用: terminal_bg(action="invalid")

返回: Unknown action 'invalid'. Available actions: list, output

模型下一轮: terminal_bg(action="list")  ✅
```

### 2.2 Schema

```python
class TerminalBgActionSchema(BaseModel):
    action: Literal["list", "output"] = Field(
        description="'list' to list tasks, 'output' to read one task's tail output."
    )
    task_id: str | None = Field(default=None, description="Task ID (required for 'output').")
    lines: int = Field(default=50, description="Number of tail lines to read (for 'output').")
```

### 2.3 Builder

```python
def build_terminal_bg_tool() -> list[BaseTool]:
    """Build the merged terminal_bg tool."""
    terminal_bg.handle_tool_error = True
    terminal_bg.metadata = {
        **(terminal_bg.metadata or {}),
        "scope": "main_only",
        "idempotent": True,      # 只读
        "side_effect_scope": "none",
    }
    return [terminal_bg]
```

### 2.4 改动文件清单

| 文件 | 改动 |
|---|---|
| `agent/tools/terminal_bg_tools.py` | 2 个 `@tool` → 1 个；保留原 `_terminal_bg_list`/`_terminal_bg_output` 内部函数 |
| `agent/tools/__init__.py` | 导入改为 `build_terminal_bg_tool`；`_MAIN_TOOLS_BUILDERS` 2 个 → 1 个 |
| `agent/tools/catalog.py` | `TOOL_GROUPS["terminal"]` 两个名 → `"terminal_bg"` |
| `agent/tools/parallel_safety.py` | `READ_ONLY_TOOLS` 移除两个名，加 `"terminal_bg"` |
| `agent/tools/terminal.py` | 输出文本更新工具名 |
| `agent/tools/terminal_bg_registry.py` | docstring 更新 |
| `config/features/agent_side/terminal_bg.py` | docstring 更新 |
| `evals/external_bench.py` | `DISABLED_TOOLS` 更新 |

---

## 3. 测试集（验证功能无影响）

### 3.1 现有测试（必须全部通过——证明功能不变）

| 测试文件 | 验证内容 | 合并后改动 |
|---|---|---|
| `tests/agent/tools/test_terminal_background.py` | 后台终端任务的生命周期（创建/列表/输出/清理） | 调用方式从 `terminal_bg_list.coroutine()` / `terminal_bg_output.coroutine()` 改为 `terminal_bg.coroutine(action="list")` / `terminal_bg.coroutine(action="output", task_id=...)` |
| `tests/agent/tools/test_tool_schema_goldens.py:59` | 工具 schema golden | 更新 golden 为 merged schema |
| `tests/server/test_agent_config.py:202-203` | `tool_required("terminal_bg_list") is False` 断言 | 改为 `tool_required("terminal_bg") is False` |

### 3.2 新增测试

| 测试 | 验证内容 |
|---|---|
| `test_terminal_bg_action_list` | `action="list"` 返回与原 `terminal_bg_list` 相同的结果 |
| `test_terminal_bg_action_output` | `action="output"` + `task_id` 返回与原 `terminal_bg_output` 相同的结果 |
| `test_terminal_bg_output_without_task_id` | `action="output"` 不传 `task_id` → 返回错误消息 |
| `test_terminal_bg_unknown_action` | `action="invalid"` → 返回 `"Unknown action"` |
| `test_terminal_bg_metadata` | `metadata["scope"]=="main_only"` + `metadata["idempotent"]==True` |
| `test_terminal_bg_in_read_only_tools` | `"terminal_bg" in READ_ONLY_TOOLS`（parallel_safety 正确分类） |
| `test_terminal_bg_in_catalog` | `tool_catalog()` 返回的 tools 列表里有 `"terminal_bg"`，组为 `"terminal"`，不在 `REQUIRED_TOOLS` 里 |
| `test_terminal_bg_old_names_absent` | `"terminal_bg_list"` 和 `"terminal_bg_output"` 不在 catalog / `READ_ONLY_TOOLS` / `build_main_tools()` 输出中 |

### 3.3 测试策略

```
现有测试改调用方式 → 验证功能行为不变
新增测试 → 验证 merged 工具的 action 路由 + metadata + 注册正确
```

---

## 4. 评判标准（功能无影响）

| 标准 | 验收方式 | 通过条件 |
|---|---|---|
| **S1：后台任务列表结果不变** | 创建 3 个后台任务 → `action="list"` | 返回的任务列表与原 `terminal_bg_list` 完全相同（task_id、command、status 字段一致） |
| **S2：后台任务输出结果不变** | 创建 1 个后台任务 → 等 1s → `action="output"` + `task_id` | 返回的尾部输出与原 `terminal_bg_output` 完全相同（行数、内容一致） |
| **S3：并行安全分类不变** | 检查 `tool_is_parallel_safe("terminal_bg", tool)` | 返回 `True`（只读，与原两个工具一致） |
| **S4：作用域不变** | 检查 `metadata["scope"]` | `== "main_only"`（子代理不可用，与原两个工具一致） |
| **S5：catalog 正确** | `tool_catalog()` 输出 | 包含 `terminal_bg`，组为 `terminal`，`required=False`；不包含 `terminal_bg_list`/`terminal_bg_output` |
| **S6：REQUIRED_TOOLS 不含** | `"terminal_bg" not in REQUIRED_TOOLS` | `True`（与原两个工具一致——都是可选的） |
| **S7：现有测试全过** | `pytest tests/agent/tools/test_terminal_background.py tests/server/test_agent_config.py -q` | 全 pass |
| **S8：agent_config_service 验证通过** | `PUT /sessions/agent_config` 带 `tools: ["terminal_bg"]` | 200 OK（工具名在 catalog 里）；带 `tools: ["terminal_bg_list"]` → 400（旧名不在 catalog 里） |
