# Question Tool 实施计划

> 向用户弹出多选项弹窗的工具，通过 `metadata["skip_heartbeat"]` 标识让 HeartbeatStaleness 中间件放行。

## 背景

参考 opencode-dev 的 `question` 工具实现：LLM 调用一个名为 `question` 的工具，工具内部通过 LangGraph `interrupt()` 暂停图执行，等待用户选择后恢复，将答案返回给 LLM。

sherry_agent 已有完整的 HITL interrupt/resume 管道（`agent/middlewares/humanInTheLoop/`），可直接复用。唯一问题：`HeartbeatStaleness` 中间件会在工具长时间等待用户输入时判定 agent 卡死并杀掉。解决方案：工具 metadata 加 `skip_heartbeat` 标识，中间件见到就跳过心跳跟踪。

## 改动范围

| 文件                                       | 操作     | 说明                                         |
| ------------------------------------------ | -------- | -------------------------------------------- |
| `agent/tools/question.py`                  | **新建** | Question 工具定义                            |
| `agent/tools/__init__.py`                  | **修改** | 注册工具到 `_MAIN_TOOLS_BUILDERS`            |
| `agent/middlewares/heartbeat_staleness.py` | **修改** | 检测 `skip_heartbeat` metadata，跳过心跳跟踪 |

**不改动**：`agent/core.py`、WS handler、service 层、HITL 中间件、前端（现有 interrupt/resume 管道完全兼容）。

## 步骤 1：新建 `agent/tools/question.py`

### Schema

```python
class QuestionOption(BaseModel):
    label: str        # 1-5 词短标签
    description: str  # 选项解释

class QuestionInput(BaseModel):
    question: str                     # 向用户展示的问题
    header: str                       # 极短标签（max 30 chars）
    options: list[QuestionOption]     # 2-6 个选项
    multiple: bool = False            # 是否允许多选
```

### 工具类

```python
class QuestionTool(BaseTool):
    name: str = "question"
    description: str = """Ask the user a question with multiple options when you need to:
- Gather user preferences or requirements
- Clarify ambiguous instructions
- Get a decision on implementation choices
If you recommend a specific option, put it first and add "(Recommended)"."""
    args_schema: type[BaseModel] = QuestionInput
    metadata: dict = {"idempotent": True, "skip_heartbeat": True, "nudge": True}
```

metadata 三个标志的用途：

- `idempotent: True` — SmartToolNode 并行安全
- `skip_heartbeat: True` — HeartbeatStaleness 跳过心跳跟踪（**核心**）
- `nudge: True` — NudgeLimitTool 在 nudge 阶段放行

### `_arun` 核心逻辑

```python
async def _arun(self, question, header, options, multiple, **kwargs):
    # 1. 构造 HITLRequest（复用现有 interrupt/resume 管道）
    hitl_request = HITLRequest(
        action_requests=[ActionRequest(
            name="question",
            args={"question": question, "header": header,
                  "options": [o.model_dump() for o in options],
                  "multiple": multiple},
            description=question,
        )],
        review_configs=[ReviewConfig(
            action_name="question",
            allowed_decisions=["approve", "reject"],
        )],
    )
    # 2. interrupt() 暂停图执行，等待用户回复
    response = interrupt(hitl_request)
    # 3. 提取用户选择，格式化返回给 LLM
    decisions = response.get("decisions", [])
    if decisions and decisions[0]["type"] == "approve":
        answer = decisions[0].get("message", "")
        return f'User answered: "{answer}". You can now continue.'
    return "User declined to answer. Proceed without this information."
```

### 工厂函数

```python
def build_question_tool() -> QuestionTool:
    tool = QuestionTool()
    tool.handle_tool_error = True
    return tool
```

## 步骤 2：修改 `agent/tools/__init__.py`

```diff
+ from .question import build_question_tool

  _MAIN_TOOLS_BUILDERS = [
      build_python_repl_tool,
      build_read_file_tool,
      build_write_file_tool,
      build_patch_file_tool,
      build_memory_tool,
      build_web_search_tool,
      build_terminal_tool,
      build_mcp_tools,
      build_skill_manage_tool,
      build_skill_list_tool,
      build_skill_view_tool,
      build_message_search_tool,
      build_subagent_runtime_tools,
      build_taskflow_tools,
+     build_question_tool,
  ]
```

## 步骤 3：修改 `agent/middlewares/heartbeat_staleness.py`

### 3a. 新增状态键

```python
_STATE_KEY_SKIP = "heartbeat_skip"
```

### 3b. `_wrap_tool_call_impl` — 检测 skip_heartbeat，跳过心跳跟踪

**当前代码（行 235-253）：**

```python
def _wrap_tool_call_impl(self, request):
    session_id = self._sid(request.state)
    if self._is_killed(session_id):
        ...raise...
    tool_name = request.tool_call.get("name", "unknown")
    state_register_mem.set_state(session_id, _STATE_KEY_TOOL, tool_name)
    return None
```

**修改为：**

```python
def _wrap_tool_call_impl(self, request):
    session_id = self._sid(request.state)
    if self._is_killed(session_id):
        ...raise...

    tool_name = request.tool_call.get("name", "unknown")

    # 检查工具 metadata 中的 skip_heartbeat 标识
    # 与 ToolGuardrails._is_idempotent / ContextEngineHook._wrap_tool_call_impl
    # 完全相同的 request.tool.metadata 访问模式
    tool = getattr(request, "tool", None)
    metadata = getattr(tool, "metadata", None)
    if isinstance(metadata, dict) and metadata.get("skip_heartbeat", False):
        state_register_mem.set_state(session_id, _STATE_KEY_SKIP, True)
        logger.debug(
            "[HeartbeatStaleness] session={} tool=[{}] skip_heartbeat=True, "
            "bypassing heartbeat tracking",
            session_id, tool_name,
        )
        return None

    # 原有逻辑
    state_register_mem.set_state(session_id, _STATE_KEY_TOOL, tool_name)
    return None
```

### 3c. `_check_progress` — 开头加 skip 检查

**当前代码（行 101-104）：**

```python
def _check_progress(self, session_id):
    if self._is_killed(session_id):
        return
    # ...原有逻辑
```

**修改为：**

```python
def _check_progress(self, session_id):
    if self._is_killed(session_id):
        return

    # 如果当前工具标记了 skip_heartbeat，跳过心跳检测
    if state_register_mem.get_state(session_id, _STATE_KEY_SKIP, False):
        logger.debug(
            "[HeartbeatStaleness] session={} heartbeat_skip=True, "
            "skipping progress check (tool waiting for human input)",
            session_id,
        )
        return

    # ...原有逻辑不变
```

### 3d. `_after_tool_call_impl` — 清除 skip 标识

**当前代码（行 255-257）：**

```python
def _after_tool_call_impl(self, request):
    session_id = self._sid(request.state)
    state_register_mem.set_state(session_id, _STATE_KEY_TOOL, None)
```

**修改为：**

```python
def _after_tool_call_impl(self, request):
    session_id = self._sid(request.state)
    state_register_mem.set_state(session_id, _STATE_KEY_TOOL, None)
    state_register_mem.set_state(session_id, _STATE_KEY_SKIP, False)
```

## 执行时序验证

```
1. LLM 调用 question 工具
2. HeartbeatStaleness.awrap_tool_call → _wrap_tool_call_impl:
   → 检测 request.tool.metadata["skip_heartbeat"] == True
   → 设 heartbeat_skip=True，不设 heartbeat_tool
   → return None（放行）
3. handler(request) → QuestionTool._arun → interrupt(HITLRequest)
   → GraphInterrupt 抛出，图暂停，流结束
   → _after_tool_call_impl 未执行（异常传播跳过）
   ✓ heartbeat_skip 保持 True
4. 心跳定时器每分钟触发 → _check_progress:
   → heartbeat_skip == True → return（跳过计数）
   ✓ 不会判定 stale，不会 kill
5. 用户回复 → resume_agent → Command(resume=...) → 图恢复
   → ToolNode 重新执行 → QuestionTool._arun 重新执行
   → interrupt() 返回用户选择
   → 工具格式化答案返回
6. handler(request) 返回 → _after_tool_call_impl:
   → 清除 heartbeat_tool=None, heartbeat_skip=False
   ✓ 恢复正常
7. 后续 wrap_model_call 正常执行，heartbeat_killed 始终为 False
```

## 不需要改动的部分

| 文件                                       | 原因                                                                                      |
| ------------------------------------------ | ----------------------------------------------------------------------------------------- |
| `agent/core.py`                            | 中间件列表不变，`request.tool.metadata` 直接在中间件内读取                                |
| `server/trigger/ws/messages.py`            | 现有 `hitl_response` 处理逻辑完全兼容                                                     |
| `server/service/messages.py`               | `resume_agent` + `get_pending_interrupt` 无需修改                                         |
| `agent/middlewares/humanInTheLoop/core.py` | `after_model` 不会拦截 `question`（不在 terminal/python_repl/memory/interrupt_on 列表中） |
| `agent/middlewares/tool_guardrails.py`     | `GraphInterrupt` 传播时不记录，resume 后正常记录                                          |
| `agent/middlewares/iteration_budget.py`    | 消耗 2 个预算单位（90 中可忽略）                                                          |
| 前端                                       | 现有 HITL 审批弹窗即可工作（用户在 message 字段输入选择）；后续可增强为选项按钮 UI        |

## 前端适配（可选，后续增强）

当前前端收到 `get_pending_interrupt` 返回的：

```json
{
  "tool_name": "question",
  "tool_args": {"question": "...", "header": "...", "options": [...], "multiple": false},
  "description": "问题文本",
  "allowed_decisions": ["approve", "reject"]
}
```

现有 HITL 审批弹窗直接可用（用户在文本框输入选择，点 approve）。后续可增强：检测 `tool_name === "question"` 时渲染选项按钮 UI。
