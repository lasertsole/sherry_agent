# 计划：高危操作防护——high_risk 标签 + 操作账本 + 关键字 HITL

- 日期：2026-11-17（r2，重构为 high_risk 标签体系 + 关键字 HITL）
- 状态：**待评审**
- 定位：三层高危操作防护：(1) 工具 high_risk 标签区分高低危，(2) 高危操作账本防 verify 重测重复执行，(3) 高危关键字 HITL 审批。
- 风险等级：**中**
- 依赖：无

---

## 0. 为什么需要 high_risk 标签

r1 的问题：账本只按 `idempotent: False` 入表——但 write_file/patch_file/python_repl 都是非幂等的，全部入账本会导致 agent 正常工作流被误拦（write_file 重写同文件 = block → agent 写不了代码）。

**非幂等 ≠ 高危**：

| 工具 | 非幂等？ | 高危？ | 入账本？ |
|------|---------|--------|---------|
| terminal(curl POST /deploy) | ✅ | ✅ | ✅ |
| terminal(pytest) | ✅ | ❌ | ❌ |
| python_repl(写文件) | ✅ | ❌ | ❌ |
| write_file | ✅ | ❌ | ❌ |
| patch_file | ✅ | ❌ | ❌ |
| mcp__stripe__charge | ✅ | ✅ | ✅ |
| read_file | ❌ | ❌ | ❌ |

**用 `high_risk` 标签区分**：只有 `high_risk: True` 的工具入账本 + 被 block 策略管。

---

## 1. high_risk 标签体系

### 1.1 标签规则

```python
# 工具 metadata 加 high_risk 字段
{"high_risk": False}  # Sherry 内置工具默认
{"high_risk": True}   # 高危工具显式标注
# MCP/外部工具无 metadata → 默认 high_risk=True（fail-safe）
```

### 1.2 现有工具标注

| 工具 | high_risk | 理由 |
|------|-----------|------|
| `terminal` | ❌ False | terminal 本身不高危——具体命令才高危（curl POST vs pytest）。高危由命令分类 + 关键字 HITL 管 |
| `python_repl` | ❌ False | 同 terminal——代码内容才决定高危 |
| `write_file` | ❌ False | 文件写可逆（shadow_git） |
| `patch_file` | ❌ False | 文件写可逆 |
| `read_file` / `search_files` / `message_search` | ❌ False | 读操作 |
| `taskflow_*` / `todowrite` / `todoread` | ❌ False | 状态管理 |
| `memory` / `question` / `skill_*` | ❌ False | 内部操作 |
| `execute_code`（PTC） | ✅ True | 任意代码执行，不可预测 |
| `mcp__*`（所有 MCP 工具） | ✅ True（默认） | 外部工具，副作用不可知 |

### 1.3 MCP 工具默认高危

`build_mcp_tools()`（`mcp_plugin.py:168-189`）当前不注入任何 metadata。改为：

```python
# build_mcp_tools 返回前
for tool in tools:
    if not isinstance(tool.metadata, dict):
        tool.metadata = {}
    tool.metadata.setdefault("high_risk", True)  # MCP 默认高危
    tool.metadata.setdefault("idempotent", False)
    tool.metadata.setdefault("side_effect_scope", "system")
```

MCP 工具可通过 SKILL.md 或配置显式声明 `high_risk: False`（如果某个 MCP 工具确实是安全的读操作）。

### 1.4 消费者

```python
def _is_high_risk(tool) -> bool:
    """工具是否高危。无 metadata → 默认 True（fail-safe）。"""
    if tool is None or not isinstance(tool.metadata, dict):
        return True  # 未知工具默认高危
    return tool.metadata.get("high_risk", True)  # 未标注默认高危
```

三个消费者：
- **操作账本**：只记录/检查 `high_risk: True` 的工具
- **关键字 HITL**：对 `high_risk: True` 的工具扫描参数关键字
- **现有 parallel_safety**：可复用 `_is_high_risk` 替代当前的 metadata 猜测

---

## 2. 高危关键字账本（合并设计——关键字即账本）

### 2.1 核心设计

**账本 = 关键字存储**。不记完整命令/参数/hash，只记关键字。关键字本身就是操作的唯一标识。

一张表，同时管：
- 关键字注册（keyword + policy: block/allow + source）
- 首次执行标记（executed: 0/1）
- 首次结果摘要（result_summary，用于 verify 重测时回放）

### 2.2 表结构

```sql
CREATE TABLE IF NOT EXISTS high_risk_keywords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,         -- 'global' = 全局，具体 session_id = session 级
    keyword TEXT NOT NULL,
    policy TEXT NOT NULL DEFAULT 'block',   -- 'block'（拦截）或 'allow'（放行）
    source TEXT NOT NULL,             -- 'builtin' / 'user' / 'agent'
    executed INTEGER NOT NULL DEFAULT 0,    -- 0=未执行过，1=已执行过（block 关键字首次执行后置 1）
    result_summary TEXT,              -- 首次执行结果摘要（executed=1 时填充，用于 verify 重测回放）
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(session_id, keyword)
);
```

**不存**：命令文本、工具参数、op_hash、tool_name。关键字是唯一标识。

### 2.3 扫描 + 拦截条件

**扫描条件**：工具必须同时满足 `high_risk=True AND idempotent=False`（MCP / execute_code）。terminal/python_repl/write_file/patch_file 不扫描。

```
high_risk+非幂等 工具被调用：
  ① 查 KEYWORD_APPROVALS → 匹配 → 跳过关键字扫描（放行）
  ② 扫描参数文本，匹配账本中 policy=block 的关键字
     → 无命中 → 正常执行
     → 命中 + executed=0 → 首次执行：
        → main agent → HITL 审批（用户 approve/reject）
        → 子 agent → [NEEDS_APPROVAL] error → 冒泡 → main agent HITL
        → YOLO 也不能跳过（高危+非幂等，不可逆）
        → 用户 approve → 执行 → 关键字 executed=1 + 存 result_summary
        → 用户 reject → 不执行
     → 命中 + executed=1 → 重复执行（verify 重测等）：
        → 直接 block（返回 result_summary，不弹 HITL）
        → YOLO 也不能跳过
        → 用户如要重跑 → 前端删关键字记录或切 policy=allow
```

### 2.4 回放策略（简化为两种）

| 关键字状态 | 行为 | YOLO 可跳过？ |
|-----------|------|-------------|
| policy=allow | 不扫描（跳过） | N/A |
| policy=block + executed=0 | HITL 审批（首次） | ❌ 不可跳过 |
| policy=block + executed=1 | 直接 block（返回首次结果） | ❌ 不可跳过 |

**只有两种结果**：allow（放行）或 block（拦截）。不再有 warn 策略。

### 2.5 完整 wrap_tool_call 逻辑

```python
async def wrap_tool_call(self, handler, request):
    tool = request.tool
    name = tool.name
    args_str = json.dumps(request.args, ensure_ascii=False)
    session_id = ...

    # 只对 high_risk=True AND idempotent=False 的工具扫描
    if not _should_scan(tool):  # high_risk=True AND idempotent=False
        return await handler(request)  # 其他工具直接放行

    # ① 查 approval（审批后重新 spawn 的子 agent 携带）
    if self._check_approval(session_id, args_str):
        return await handler(request)  # 已审批 → 放行

    # ② 扫描关键字（只扫 policy=block 的）
    blockers = keyword_store.load_active_blockers(session_id)
    hit = scan_high_risk_keywords(args_str, blockers)

    if not hit:
        return await handler(request)  # 无命中 → 正常执行

    # 命中关键字 → 检查是否已执行过
    keyword_entry = keyword_store.get(session_id, hit.keyword)

    if keyword_entry.executed:
        # executed=1 → 重复执行 → 直接 block（返回首次结果）
        # 无论 YOLO 还是任何 HITL 状态都 block
        return ToolMessage(
            content=f"[HIGH-RISK GUARD] 关键字 '{hit.keyword}' 的操作已执行过。"
                    f"首次结果: {keyword_entry.result_summary[:200]}",
            tool_call_id=request.tool_call_id,
            status="error"
        )
    else:
        # executed=0 → 首次执行 → 需要审批
        # main agent: HITL handler 在 after_model 已经检查了
        # 走到 wrap_tool_call 说明 HITL 没拦（或子 agent 无 HITL）
        # → 返回 NEEDS_APPROVAL
        return ToolMessage(
            content=f"[NEEDS_APPROVAL: keyword={hit.keyword}]\n"
                    f"高危非幂等操作含关键字 '{hit.keyword}'，需要用户审批。\n"
                    f"Main agent: use `question` tool.\n"
                    f"Subagent: include this block in your result.",
            tool_call_id=request.tool_call_id,
            status="error"
        )

    # ③ 正常执行（approval 放行 或 无关键字命中）
    result = await handler(request)

    # ④ 执行成功后标记关键字为已执行
    if result.status != "error":
        keyword_store.mark_executed(session_id, hit.keyword, str(result.content)[:500])

    return result
```

### 2.6 HITL handler（main agent 侧）

```python
class HighRiskKeywordApprovalHandler(ToolApprovalHandler):
    def matches(self, tool_call, ctx) -> bool:
        name = tool_call["name"]
        return _should_scan_by_name(name)  # 只匹配 high_risk+非幂等 工具

    async def handle(self, tool_call, ctx) -> bool:
        # YOLO 也不能跳过（高危+非幂等）
        # 不检查 is_yolo_mode

        args_str = json.dumps(tool_call.get("args", {}), ensure_ascii=False)
        blockers = keyword_store.load_active_blockers(ctx.session_id)
        hit = scan_high_risk_keywords(args_str, blockers)

        if not hit:
            return False  # 无命中 → 放行给下一个 handler

        keyword_entry = keyword_store.get(ctx.session_id, hit.keyword)

        if keyword_entry.executed:
            # 已执行过 → 直接 deny（不走 HITL 审批，直接 block）
            ctx.outcome.deny(tool_call, reason=f"已执行过: {keyword_entry.result_summary[:100]}")
            return True
        else:
            # 首次执行 → HITL 审批
            review = ReviewConfig(
                allowed_decisions=["approve", "reject"],
                prompt=f"高危非幂等操作含关键字 '{hit.keyword}'。是否允许执行？",
            )
            action = ActionRequest(tool_name=tool_call["name"], args=tool_call.get("args", {}), review=review)
            try:
                interrupt(HITLRequest(request_id=ctx.request_id, actions=[action], source="high_risk_keyword"))
            except GraphInterrupt:
                raise

            decision = ctx.hitl_response["decisions"][0]
            if decision["type"] == "approve":
                # approve → 写 KEYWORD_APPROVALS → 放行
                approvals = state_register_mem.get_state(ctx.session_id, "KEYWORD_APPROVALS") or []
                approvals.append({"keyword": hit.keyword})
                state_register_mem.set_state(ctx.session_id, "KEYWORD_APPROVALS", approvals)
                ctx.outcome.approve(tool_call)
                # 执行成功后由 wrap_tool_call 标记 executed=1
            else:
                ctx.outcome.deny(tool_call, reason=f"用户拒绝（关键字: {hit.keyword}）")
            return True
```

### 2.7 关键字登记校验

关键字登记时校验长度——**字符数不能小于 3**（按 Unicode 字符计数，不按字节。中英文都一样：`len("密码")=2` 太短，`len("密码箱")=3` 通过；`len("ab")=2` 太短，`len("api_key")=7` 通过）。

```python
def _validate_keyword(keyword: str) -> str | None:
    """校验关键字。返回 None=合法，返回 str=错误原因。"""
    keyword = keyword.strip()
    if not keyword:
        return "关键字不能为空"
    # 按 Unicode 字符计数（Python3 的 len() 天然按字符，不按字节）
    if len(keyword) < 3:
        return f"关键字长度不能小于 3 个字符（当前 {len(keyword)} 个字符）。过短的关键字会误拦大量正常操作。"
    if len(keyword) > 100:
        return "关键字长度不能超过 100 个字符"
    return None
```

**Python3 的 `len()` 天然按 Unicode 字符计数**：
- `len("a")` = 1（太短，拒绝）
- `len("ab")` = 2（太短，拒绝）
- `len("api")` = 3（通过）
- `len("中")` = 1（太短，拒绝）
- `len("密码")` = 2（太短，拒绝）
- `len("银行卡")` = 3（通过）
- `len("card_number")` = 11（通过）

**校验位置**（三处都校验）：
1. `high_risk_keyword_store.add_keyword()` — 后端存储层校验
2. `high_risk_guard` 工具的 `_arun()` — agent 调用工具时校验
3. API `POST /sessions/:sid/high-risk-keywords` — 前端调 API 时校验
4. 前端 `HighRiskGuardPanel.vue` 的添加关键字表单 — 输入时即时校验（`minlength` + 自定义提示）

**内置关键字也需要满足 ≥3 字符**——检查内置列表，移除任何 <3 字符的：
```python
BUILTIN_KEYWORDS = (
    "card_number",      # 11 ✅
    "credit_card",      # 11 ✅
    "银行卡",            # 3 ✅
    "信用卡",            # 3 ✅
    "cvv",              # 3 ✅
    "password",         # 8 ✅
    "passwd",           # 6 ✅
    "api_key",          # 7 ✅
    "secret_key",       # 10 ✅
    "access_token",     # 12 ✅
    "private_key",      # 11 ✅
    "rm -rf",           # 6 ✅
    "format",           # 6 ✅
    "mkfs",             # 4 ✅
    "dd if=",           # 6 ✅
    "shutdown",         # 8 ✅
    "reboot",           # 6 ✅
    "git push --force", # 16 ✅
    "git push -f",      # 12 ✅
    "DROP TABLE",       # 10 ✅
    "DELETE FROM",      # 11 ✅
    "TRUNCATE",         # 8 ✅
)
# 所有内置关键字都 ≥3 字符 ✅
```

### 2.8 登记时机

| 情况 | 登记/标记？ | 说明 |
|------|-----------|------|
| 用户/agent 注册关键字 | ✅ 写入 keyword + policy=block + executed=0 | 预防性登记 |
| 高危+非幂等 工具首次执行成功 | ✅ keyword executed=1 + result_summary | 标记已执行 |
| 高危+非幂等 工具执行失败 | ❌ 不标记 executed | 允许重试 |
| 被 block 的重跑 | ❌ 不再标记 | 已有记录 |
| 低危工具执行 | ❌ 不登记 | 不在扫描范围 |
| 用户 approve 后执行 | ✅ 执行后标记 executed=1 | HITL approve → 执行 → 标记 |

### 2.9 删除/注销时机

| 触发 | 删除？ | 范围 |
|------|--------|------|
| session 结束 | ✅ | 清该 session 的所有记录 |
| 用户前端删除单条 | ✅ | 删一个关键字 → 该关键字不再拦截 + executed 清零 |
| 用户前端切 policy=allow | ❌ 不删 | keyword 保留但 policy=allow → 不拦截 |
| 用户前端清空全部 | ✅ | 清所有自定义关键字 |
| 24h 过期 | ✅ | 清过期记录 |
| shadow_git restore | ❌ | 不清（不可逆操作不能因文件回滚而放行） |
| 内置关键字 | ❌ 永不删 | 只能切 policy=allow |

---

## 3. 高危关键字防护

### 3.0 架构问题：HITL 不在子 agent 链里 + 子 agent 无法 mid-execution 暂停

**代码事实**：
- `HumanInTheLoop` 中间件是 main-only（`specs.py:473-478`）→ 子 agent 无 HITL
- 子 agent 调 `interrupt()` → `GraphInterrupt` 不被 lifecycle 捕获 → spawn task 崩溃 → 无 resumer → 子 graph 挂死
- 子 agent 调 `question` → 同样 `interrupt()` → 同样崩溃
- 子 agent→父 agent 的唯一通道：**完成通知**（TERMINAL 时 announce pipeline + TaskFlow auto-resume）

**解决：三段式冒泡**

```
子 agent 调 terminal(curl POST /api/payment)
  → ToolGuardramids 扫描：含 "payment"（policy=block）
  → 返回 error ToolMessage："[NEEDS_APPROVAL: keyword=payment, tool=terminal, args=curl POST /api/payment]"
  → 子 agent 看到错误，无法继续
  → 子 agent 完成（TERMINAL），result_text 含 "[NEEDS_APPROVAL: ...]"
  → announce pipeline 投递结果到父 agent
  → 父 agent 的 model 读到 "[NEEDS_APPROVAL: ...]"
  → 父 agent 调 question("子 agent 需要执行含 'payment' 的操作，是否允许？")
  → HITL interrupt → 用户审批
  → 用户 approve → 父 agent 调 taskflow_step(action='resume', step_outcome='approved')
    → 新子 agent spawn，context 注入 "keyword 'payment' approved for this step"
    → 新子 agent 调 terminal(curl POST /api/payment) → ToolGuardrails 检查 approval → 放行
  → 用户 reject → 父 agent 调 taskflow_step(action='resume', step_outcome='blocked')
    → 步骤标记 blocked
```

**关键设计**：
1. 子 agent **不调 interrupt**——ToolGuardrails 返回 error，子 agent 自然失败完成
2. 父 agent 读子 agent 结果里的 `[NEEDS_APPROVAL]` 标记 → 父 agent 自己调 `question`（HITL 在 main 链里）
3. 用户审批后 → 父 agent resume TaskFlow step → 新子 agent spawn 带 approval context
4. 新子 agent 的 ToolGuardrails 检查 approval context → 放行

### 3.0.1 approval context 传递

父 agent 审批后，resume TaskFlow step 时注入 approval：

```python
# 父 agent 调 taskflow_step(action='resume', step_id='s1', step_outcome='approved',
#   approval_context={'keyword': 'payment', 'tool': 'terminal'})
```

step_spawn_kwargs 传递 approval_context 到 dispatch_child → spawn_subagent_direct → 子 agent 的 state_register 写入 `KEYWORD_APPROVALS`：

```python
# 子 agent 的 ToolGuardrails 检查
if hit:
    # 检查是否有 approval
    approvals = state_register.get(session_id, "KEYWORD_APPROVALS", [])
    if {"keyword": hit.keyword, "tool": tool_name} in approvals:
        return await handler(request)  # 已审批 → 放行
    else:
        return error_tool_message("[NEEDS_APPROVAL: ...]")
```

approval 是 per-step 的——只对这个 step 的这次 spawn 有效，不永久放行。

### 3.1 agent 登记高危关键字的工具

新增工具 `high_risk_guard`（`scope="all"`，**main + 子 agent 都可用**）：

**文件**：`agent/tools/high_risk_guard.py`（新建）

```python
@tool("high_risk_guard", args_schema=HighRiskGuardInput)
def high_risk_guard(action: str, keyword: str | None = None,
                    tool_name: str | None = None, command: str | None = None) -> str:
    """Manage high-risk keywords and operations.

    Actions:
    - add_keyword: Register a high-risk keyword (tool calls containing it will be blocked/intercepted)
    - list_keywords: List all high-risk keywords (builtin + custom)
    - remove_keyword: Remove a custom keyword
    - register_op: Manually register a high-risk operation (blocks re-execution)
    - list_ops: List registered high-risk operations
    - remove_op: Remove a registered operation
    """
    ...
```

**metadata**：`{"idempotent": True, "side_effect_scope": "session", "high_risk": False}`
**不设 `scope: "main_only"`** → 子 agent 也能调用

**为什么子 agent 需要这个工具**：executor 子 agent 在执行任务时可能发现自己要跑一个危险命令（如 `curl POST /payment`），它可以主动登记关键字 "payment" → 后续（包括它自己和 main agent）调含 "payment" 的工具时被拦截。

### 3.2 关键字扫描（ToolGuardrails 层，main + 子 agent 都生效）

**文件**：`agent/middlewares/tool_guardrails/core.py`

ToolGuardrails 在 main + child 链里（`required_in=_BOTH`），`wrap_tool_call` 对两者都生效。

```python
async def wrap_tool_call(self, handler, request):
    tool = request.tool
    args_str = json.dumps(request.args, ensure_ascii=False)

    # 高危关键字扫描（只扫 policy=block 的关键字）
    blockers = keyword_store.load_active_blockers(session_id)
    hit = scan_high_risk_keywords(args_str, blockers)

    if hit:
        # 检查是否有 approval（子 agent 被 main agent 审批后重新 spawn 时携带）
        approvals = state_register.get(session_id, "KEYWORD_APPROVALS", [])
        if {"keyword": hit.keyword, "tool": tool.name} in approvals:
            return await handler(request)  # 已审批 → 放行

        # main agent → HITL 审批（HITL handler 会接管）
        # 但 wrap_tool_call 在 HITL after_model 之后执行
        # 所以 main agent 的 HITL 在 after_model 已经检查过了
        # 如果走到 wrap_tool_call 说明 HITL 没拦住（或无 HITL）
        # → 直接返回 error，让 main agent 的 model 看到

        # 子 agent → 返回 NEEDS_APPROVAL error（子 agent 自然失败完成 → 冒泡到父 agent）
        return ToolMessage(
            content=(
                f"[NEEDS_APPROVAL: keyword={hit.keyword}, tool={tool.name}] "
                f"This operation contains high-risk keyword '{hit.keyword}'. "
                f"If you are a subagent, return this message in your result so the "
                f"parent agent can ask the user for approval. "
                f"If you are the main agent, use the `question` tool to ask the user."
            ),
            tool_call_id=request.tool_call_id,
            status="error"
        )

    return await handler(request)
```

**main agent 和子 agent 的区别**：

| | main agent | 子 agent |
|---|-----------|---------|
| ToolGuardrails 命中 | 返回 error → main agent 的 model 看到 → model 自己调 `question` 问用户 | 返回 error → 子 agent 的 model 看到 → 无法继续 → 自然完成 |
| HITL handler | `HighRiskKeywordApprovalHandler` 在 `after_model` 先检查 → 命中则 interrupt | 无 HITL → ToolGuardrails 的 error 是唯一拦截 |
| 用户审批 | HITL interrupt → 用户 approve/reject | 子 agent 完成后结果冒泡到父 agent → 父 agent 调 `question` → 用户审批 |
| 审批后放行 | HITL approve → 放行 | 父 agent resume TaskFlow step → 新子 agent spawn 带 `KEYWORD_APPROVALS` → ToolGuardrails 放行 |

**main agent 双重检查**：
1. `HighRiskKeywordApprovalHandler`（HITL after_model）先检查 → 命中 → interrupt → 用户审批
2. 如果 HITL 没拦住（如 YOLO 模式 / handler 顺序问题），ToolGuardrails（wrap_tool_call）兜底 → 返回 error

**子 agent 单层检查**：
1. ToolGuardrails（wrap_tool_call）检查 → 命中 → 返回 NEEDS_APPROVAL error
2. 子 agent 无法继续 → 自然完成 → 结果冒泡到父 agent

### 3.3 main agent 的 HITL 审批

**文件**：`agent/middlewares/humanInTheLoop/strategies.py`

`HighRiskKeywordApprovalHandler`（main-only，在 HITL handler 链中）：

```python
class HighRiskKeywordApprovalHandler(ToolApprovalHandler):
    def matches(self, tool_call, ctx) -> bool:
        return True  # 扫描所有工具

    async def handle(self, tool_call, ctx) -> bool:
        if ctx.is_yolo_mode:
            return False  # YOLO 跳过

        args_str = json.dumps(tool_call.get("args", {}), ensure_ascii=False)
        keywords = high_risk_keyword_store.load_all(ctx.session_id)
        hit = scan_high_risk_keywords(args_str, keywords)

        if not hit:
            return False  # 无命中 → 放行给下一个 handler

        # 命中 → HITL 审批
        review = ReviewConfig(
            allowed_decisions=["approve", "reject"],
            prompt=f"此操作含高危关键字: {hit.keyword}。是否允许执行？",
        )
        action = ActionRequest(tool_name=tool_call["name"], args=tool_call.get("args",{}), review=review)
        try:
            interrupt(HITLRequest(request_id=ctx.request_id, actions=[action], source="high_risk_keyword"))
        except GraphInterrupt:
            raise

        decision = ctx.hitl_response["decisions"][0]
        if decision["type"] == "approve":
            ctx.outcome.approve(tool_call)
        else:
            ctx.outcome.deny(tool_call, reason=f"用户拒绝（高危关键字: {hit.keyword}）")
        return True
```

**注册位置**：handler 链中 `InterruptOnApprovalHandler` 和 `PluginToolApprovalHandler` 之间。

### 3.4 两层防护的完整流程

```
main agent 调 terminal(curl POST /api/payment):
  after_model → HighRiskKeywordApprovalHandler.matches()=True
  → 扫描参数：含 "payment"（用户自定义关键字）
  → HITL interrupt → 用户审批
  → approve → wrap_tool_call 放行 → 执行
  → reject → wrap_tool_call deny → 不执行

子 agent（executor）调 terminal(curl POST /api/payment):
  after_model → 无 HITL（子 agent 链不含 HumanInTheLoop）→ 不检查
  wrap_tool_call → ToolGuardrails 扫描：含 "payment"
  → 子 agent → 直接 deny："[HIGH-RISK KEYWORD BLOCKED] 含 'payment'"
  → 不执行

子 agent（executor）调 high_risk_guard(action="add_keyword", keyword="deploy"):
  → 工具执行：写入关键字存储
  → 后续 main agent 或子 agent 调含 "deploy" 的工具 → 被拦截
```

### 3.5 关键字存储（共享账本 + per-keyword policy）

**文件**：`agent/tools/pub_base/high_risk_keyword_store.py`（新建）

agent 和用户共用同一账本。登记后默认 `block`（拦截），用户可切为 `allow`（放行）。

```python
class HighRiskKeywordStore:
    """高危关键字存储（SQLite，agent 和用户共用）。"""

    BUILTIN_KEYWORDS = (
        "card_number", "credit_card", "银行卡", "信用卡", "cvv",
        "password", "passwd", "api_key", "secret_key", "access_token",
        "private_key", "-----BEGIN RSA PRIVATE KEY-----",
        "rm -rf", "format", "mkfs", "dd if=", "shutdown", "reboot",
        "git push --force", "git push -f",
        "DROP TABLE", "DELETE FROM", "TRUNCATE",
    )

    # SQLite 表
    # CREATE TABLE high_risk_keywords (
    #     id INTEGER PRIMARY KEY AUTOINCREMENT,
    #     session_id TEXT NOT NULL,    -- 'global' = 全局，具体 session_id = session 级
    #     keyword TEXT NOT NULL,
    #     policy TEXT NOT NULL DEFAULT 'block',  -- 'block'（拦截）或 'allow'（放行）
    #     source TEXT NOT NULL,        -- 'builtin' / 'user' / 'agent'
    #     created_at INTEGER NOT NULL,
    #     updated_at INTEGER NOT NULL,
    #     UNIQUE(session_id, keyword)
    # )

    def load_active_blockers(self, session_id: str) -> list[str]:
        """加载 policy=block 的关键字（内置 + 全局 + session 级）。
        只返回 block 策略的——allow 的不拦截。"""
        keywords = list(self.BUILTIN_KEYWORDS)  # 内置默认 block
        # 全局 + session 级自定义，只取 policy=block
        keywords += self._load_custom("global", policy="block")
        keywords += self._load_custom(session_id, policy="block")
        # 减去被用户切为 allow 的内置关键字
        allowed = self._load_custom("global", policy="allow") + self._load_custom(session_id, policy="allow")
        return [kw for kw in keywords if kw not in allowed]

    def add_keyword(self, session_id: str, keyword: str, source: str = "agent", policy: str = "block"):
        """添加关键字。agent 添加默认 block，用户可指定 allow。"""
        ...

    def set_policy(self, session_id: str, keyword: str, policy: str):
        """切换关键字策略：block ↔ allow。用户通过前端调用。"""
        ...

    def load_all(self, session_id: str) -> list[dict]:
        """列出所有关键字（含 policy），前端展示用。"""
        ...
```

**policy 机制**：
- 登记关键字时默认 `policy="block"`（agent 和用户登记的都是 block）
- 用户可在前端把某个关键字从 `block` 切为 `allow` → 该关键字不再拦截
- 内置关键字也可被用户切为 `allow`（如用户确认 "password" 在配置文件操作中不需要拦截）
- `load_active_blockers()` 只返回 `policy=block` 的关键字——扫描时只扫这些

**agent 和用户共用同一账本**：
- agent 调 `high_risk_guard(action="add_keyword", keyword="退款")` → 写入 `source="agent", policy="block"`
- 用户在前端添加 "deploy" → 写入 `source="user", policy="block"`
- 用户在前端把 "退款" 从 block 切为 allow → `set_policy(session_id, "退款", "allow")`
- 扫描时 `load_active_blockers()` 只返回 block 的 → "退款" 不再拦截

### 3.6 scan_high_risk_keywords

**文件**：`agent/middlewares/humanInTheLoop/detection.py`（新增函数）

```python
def scan_high_risk_keywords(text: str, keywords: list[str]) -> KeywordHit | None:
    """扫描文本是否含高危关键字。返回命中的关键字或 None。"""
    text_lower = text.lower()
    for kw in keywords:
        if kw.lower() in text_lower:
            return KeywordHit(keyword=kw, context=text[max(0,pos-20):pos+len(kw)+20])
    return None
```

大小写不敏感、子串匹配（不区分 word boundary——"password" 匹配 "user_password=xxx"）。

### 3.7 关键字管理 API（前端用）

```
GET    /sessions/:sid/high-risk-keywords           列出（内置 + 全局 + session 级）
POST   /sessions/:sid/high-risk-keywords           添加（body: {keyword, scope: 'global'|'session'}）
DELETE /sessions/:sid/high-risk-keywords/:id       删除自定义关键字
GET    /high-risk-keywords/builtin                 内置列表（只读）
```

---

## 3.X 和 r1 的区别（r1 → r2 变更）

| 维度 | r1 | r2 |
|------|-----|-----|
| 账本入表条件 | `not is_idempotent(tool)` | `is_high_risk(tool)` |
| write_file 入表？ | ❌ 会误入 | ✅ 不入（high_risk=False） |
| terminal 入表？ | ❌ 会误入 | ✅ 不入（high_risk=False） |
| 回放策略 | 分 warn/block（需区分文件写/命令） | 统一 block（high_risk 定义就是重跑危险） |
| 关键字 HITL 只对 main？ | 未考虑子 agent | ✅ 两层：ToolGuardrails（both）+ HITL（main-only） |
| agent 登记关键字途径 | 未提供 | ✅ `high_risk_guard` 工具（scope="all"） |
| 子 agent 被拦截？ | ❌ HITL 不在子链 | ✅ ToolGuardrails 直接 deny |

---

## 4. 前端 UI

### 4.1 工具栏按钮

工具栏右侧组加 🛡️ 盾牌图标 → 右栏打开 `HighRiskGuardPanel`。

### 4.2 面板内容

```
┌──────────────────────────────────────────┐
│ 🛡️ 高危操作防护              [刷新]      │
├──────────────────────────────────────────┤
│                                          │
│ ── 高危操作账本（2 条）───────────────── │
│                                          │
│ ┌──────────────────────────────────────┐ │
│ │ 🔴 mcp__stripe__charge              │ │
│ │ amount=100, currency=usd            │ │
│ │ 2026-11-17 14:35 | block            │ │
│ │ 结果: {"charged": true}    [删除]   │ │
│ ├──────────────────────────────────────┤ │
│ │ 🔴 execute_code                     │ │
│ │ deploy_script.py                    │ │
│ │ 2026-11-17 14:40 | block            │ │
│ │ 结果: Deployed to prod     [删除]   │ │
│ └──────────────────────────────────────┘ │
│                                          │
│ [+ 手动登记高危操作]                     │
│                                          │
│ ── 高危关键字 ─────────────────────────  │
│                                          │
│ 内置关键字（只读）：                     │
│ card_number, credit_card, 银行卡, CVV,  │
│ password, api_key, rm -rf, DROP TABLE... │
│                                          │
│ 自定义关键字（3 条）：                   │
│ ┌──────────────────────────────────────┐ │
│ │ deploy            [删除]             │ │
│ ├──────────────────────────────────────┤ │
│ │ publish           [删除]             │ │
│ ├──────────────────────────────────────┤ │
│ │ send_email        [删除]             │ │
│ └──────────────────────────────────────┘ │
│                                          │
│ [+ 添加关键字]                          │
│ [___________] [添加]                    │
│                                          │
│ ── 说明 ──────────────────────────────  │
│ • 高危操作账本：high_risk 工具执行后    │
│   自动登记，重跑时被 block              │
│ • 高危关键字：工具参数含关键字时        │
│   强制 HITL 审批                        │
│ • 两者独立，可同时生效                  │
└──────────────────────────────────────────┘
```

### 4.3 面板功能

| 功能 | 说明 |
|------|------|
| **高危操作列表** | 显示 high_risk 工具的已执行记录（工具名 + 参数 + 时间 + 结果摘要） |
| **删除单条** | 删除账本记录 → 该操作可重新执行 |
| **手动登记** | 用户手动添加一条操作到账本 → 预防性阻止 |
| **内置关键字** | 只读显示内置高危关键字列表 |
| **自定义关键字** | 用户添加/删除自定义关键字 → 工具参数含关键字时走 HITL |
| **清空全部** | 清空当前 session 的操作账本 |

### 4.4 右栏注册

```typescript
// stores/right-sidebar.ts
// RightSidebarPanelKind 加
| 'highRiskGuard'

// SCOPE_BY_KIND
highRiskGuard: 'session'

// PANELS
highRiskGuard: () => import('../components/HighRiskGuardPanel.vue')
```

### 4.5 HighRiskGuardPanel.vue

```vue
<template>
  <div class="high-risk-guard-panel p-4 overflow-y-auto h-full">
    <!-- 标题 -->
    <div class="flex items-center justify-between mb-4">
      <span class="font-medium flex items-center gap-2">
        <i class="pi pi-shield" /> {{ t('highRiskGuard.title') }}
      </span>
      <Button icon="pi pi-refresh" size="small" text @click="loadAll" :loading="loading" />
    </div>

    <!-- 高危操作账本 -->
    <SectionTitle :title="t('highRiskGuard.opsSection')" />
    <div v-if="ops.length === 0" class="text-center py-4 text-gray-400 text-sm">
      {{ t('highRiskGuard.noOps') }}
    </div>
    <div v-else class="space-y-2 mb-4">
      <div v-for="op in ops" :key="op.id" class="border border-red-300 dark:border-red-900 rounded-lg p-3">
        <div class="flex items-center justify-between">
          <span class="font-medium text-sm flex items-center gap-1">
            <i class="pi pi-lock text-red-500" /> {{ op.tool_name }}
          </span>
          <Button icon="pi pi-trash" size="small" severity="danger" text @click="deleteOp(op.id)" />
        </div>
        <div class="text-xs text-gray-500 mt-1 font-mono break-all">{{ op.command_text }}</div>
        <div class="text-xs text-gray-400 mt-1">{{ formatTime(op.executed_at) }} | {{ op.result_summary?.slice(0,80) }}</div>
      </div>
    </div>

    <!-- 手动登记 -->
    <Button :label="t('highRiskGuard.addOp')" icon="pi pi-plus" size="small" text @click="showAddOp = !showAddOp" />
    <div v-if="showAddOp" class="mt-2 p-3 border rounded-lg space-y-2">
      <FormField :label="t('highRiskGuard.toolName')">
        <Select v-model="addOpForm.tool_name" :options="['execute_code', 'mcp__custom']" class="w-full" />
      </FormField>
      <FormField :label="t('highRiskGuard.command')">
        <Textarea v-model="addOpForm.command" rows="3" class="w-full font-mono text-sm" />
      </FormField>
      <Button :label="t('highRiskGuard.add')" size="small" @click="addOp" :loading="addingOp" />
    </div>

    <Divider class="my-4" />

    <!-- 高危关键字 -->
    <SectionTitle :title="t('highRiskGuard.keywordsSection')" />

    <!-- 内置关键字 -->
    <div class="mb-3">
      <div class="text-xs text-gray-400 mb-1">{{ t('highRiskGuard.builtinKeywords') }}</div>
      <div class="flex flex-wrap gap-1">
        <Chip v-for="kw in builtinKeywords" :key="kw" :label="kw" class="text-xs" />
      </div>
    </div>

    <!-- 自定义关键字 -->
    <div class="text-xs text-gray-400 mb-1">{{ t('highRiskGuard.customKeywords') }}</div>
    <div class="space-y-1 mb-2">
      <div v-for="kw in customKeywords" :key="kw.id" class="flex items-center justify-between">
        <Chip :label="kw.keyword" />
        <Button icon="pi pi-times" size="small" text severity="danger" @click="deleteKeyword(kw.id)" />
      </div>
    </div>

    <!-- 添加关键字 -->
    <div class="flex gap-2">
      <InputText
        v-model="newKeyword"
        :placeholder="t('highRiskGuard.keywordPlaceholder')"
        class="flex-1 text-sm"
        :invalid="newKeyword.length > 0 && newKeyword.length < 3"
        @keyup.enter="addKeyword"
      />
      <Button icon="pi pi-plus" size="small" @click="addKeyword" :loading="addingKw" :disabled="newKeyword.trim().length < 3" />
    </div>
    <div v-if="newKeyword.length > 0 && newKeyword.length < 3" class="text-xs text-red-500 mt-1">
      {{ t('highRiskGuard.keywordTooShort') }}
    </div>
  </div>
</template>
```

---

## 5. 文件变更清单

### 后端

| 文件 | 操作 | 行数 |
|------|------|------|
| `agent/tools/pub_base/non_idempotent_ledger.py` | 新建：高危操作账本（只管 high_risk 工具） | ~120 |
| `agent/tools/pub_base/high_risk_keyword_store.py` | 新建：关键字存储（内置 + 全局 + session 级）+ scan 函数 | ~100 |
| `agent/tools/high_risk_guard.py` | 新建：`high_risk_guard` 工具（scope="all"，main+子 agent 可用） | ~80 |
| `agent/tools/__init__.py` | 加 `build_high_risk_guard_tool` 到 `_MAIN_TOOLS_BUILDERS` | ~3 |
| `agent/tools/mcp_plugin.py` | 改：`build_mcp_tools` 注入 `high_risk: True` 等默认 metadata | ~10 |
| 现有工具 metadata | 改：terminal/python_repl/write_file/patch_file 加 `high_risk: False`；execute_code 加 `high_risk: True` | ~10 |
| `agent/middlewares/tool_guardrails/core.py` | wrap_tool_call 加关键字扫描：子 agent 命中 → 直接 deny | ~50 |
| `agent/middlewares/humanInTheLoop/strategies.py` | 新增 `HighRiskKeywordApprovalHandler` + 注册到 handler 链（main-only HITL） | ~80 |
| `agent/middlewares/humanInTheLoop/detection.py` | 新增 `scan_high_risk_keywords()` | ~20 |
| `config/features/agent_side/hitl_defaults.py` | 加内置关键字列表 | ~20 |
| `server/trigger/http/high_risk_guard.py` | 新建：操作账本 CRUD + 关键字 CRUD API | ~120 |

### 前端

| 文件 | 操作 | 行数 |
|------|------|------|
| `pages/home/config.ts` + `dialogs.ts` | 加 🛡️ 按钮到工具栏 | ~8 |
| `stores/right-sidebar.ts` | 加 `highRiskGuard` panel kind | ~5 |
| `components/HighRiskGuardPanel.vue` | 新建：操作列表 + 关键字管理 | ~250 |
| `locales/zh.json`（+ en/ja/ko） | 加 `highRiskGuard.*` i18n | ~30 |

### 测试

| 文件 | 操作 | 行数 |
|------|------|------|
| `tests/` | 账本 + 关键字 + high_risk 标签 + main/子 agent 拦截 + handler + 前端 | ~200 |

| **总计** | | ~1096 |

---

## 6. 配置

```python
# config/features/agent_side/hitl_defaults.py 加
HIGH_RISK_KEYWORDS_BUILTIN = (
    # 金融
    "card_number", "credit_card", "银行卡", "信用卡", "cvv", "expiry_date",
    "account_number", "routing_number", "iban", "swift_code",
    "stripe.com", "paypal.com", "alipay.com",
    # 认证
    "password", "passwd", "api_key", "secret_key", "access_token",
    "private_key", "-----BEGIN RSA PRIVATE KEY-----",
    # 敏感操作
    "rm -rf", "format", "mkfs", "dd if=", "shutdown", "reboot",
    "git push --force", "git push -f",
    "DROP TABLE", "DELETE FROM", "TRUNCATE",
)

# config/features/agent_side/non_idempotent_guard.py
NON_IDEMPOTENT_GUARD = {
    "enabled": True,
    "retention_hours": 24,
}
```

---

## 7. 完整推演

### 情况 1：MCP 工具首次执行

```
agent 调 mcp__stripe__charge(amount=100)
  → high_risk=True（MCP 默认）
  → 关键字扫描：参数含 "stripe.com"? 不含（只有 amount=100）→ 无命中 → 放行
  → 账本检查：无记录 → 正常执行
  → 执行成功 → 账本登记 hash=h1
```

✅ 首次执行正常。

### 情况 2：MCP 工具参数含高危关键字

```
agent 调 mcp__stripe__charge(card_number="4242424242424242", amount=100)
  → high_risk=True
  → 关键字扫描：参数含 "card_number" → 命中！
  → HighRiskKeywordApprovalHandler 拦截 → HITL 审批
  → 用户 approve → 执行
  → 执行成功 → 账本登记
  或 用户 reject → 拒绝执行
```

✅ 含关键字 → HITL 审批。

### 情况 3：verify 重测 MCP 工具

```
turn 1: mcp__stripe__charge(amount=100) → 执行 → 登记 hash=h1
turn 3: verify 重测 → mcp__stripe__charge(amount=100)
  → high_risk=True
  → 关键字扫描：无关键字命中 → 放行
  → 账本检查：hash=h1 命中 → block
  → 返回首次结果摘要
```

✅ verify 重测被 block。

### 情况 4：terminal 正常写代码

```
agent 调 write_file(app.py, "code...")
  → high_risk=False → 不入账本、不检查
  → 正常执行 ✅
  → verify 重测 → 不检查 → 正常执行（同内容覆盖无害）✅
```

✅ write_file 不被误拦。

### 情况 5：terminal 跑 curl POST（无关键字）

```
agent 调 terminal(curl POST https://api.example.com/deploy)
  → high_risk=False（terminal 标注非高危）
  → 不入账本
  → 关键字扫描：参数含 "deploy"? 
    → 如果用户在前端添加了自定义关键字 "deploy" → 命中 → HITL 审批
    → 如果没添加 → 无命中 → 放行
```

⚠️ terminal 的 curl POST 不入账本（因为 terminal high_risk=False）。防重复靠**关键字 HITL**——用户添加 "deploy" 关键字后，每次 curl 含 deploy 都走 HITL。

如果用户没添加关键字 → terminal curl POST 不被防 → **这是设计选择**：terminal 太通用了（图灵完备），不可能对所有写命令入账本（会误拦 pytest/build/git commit）。用户通过关键字自定义高危命令。

### 情况 6：terminal 跑 rm -rf

```
agent 调 terminal(rm -rf /)
  → high_risk=False
  → 关键字扫描：参数含 "rm -rf"（内置关键字）→ 命中！
  → HITL 审批 → 用户 reject → 拒绝 ✅
```

✅ 内置关键字拦住 rm -rf。

### 情况 7：python_repl 写文件

```
agent 调 python_repl(code="open('app.py','w').write('code...')")
  → high_risk=False → 不入账本
  → 关键字扫描：无关键字命中 → 放行
  → 正常执行 ✅
```

✅ 不被误拦。

### 情况 8：execute_code（PTC）

```
agent 调 execute_code(script="deploy.py")
  → high_risk=True → 入账本
  → 关键字扫描：无命中 → 放行
  → 账本检查：无记录 → 正常执行 → 登记
verify 重测 → execute_code(script="deploy.py")
  → 账本检查：命中 → block ✅
```

✅ execute_code 入账本，verify 重测被 block。

### 情况 9：用户手动登记 + 手动添加关键字

```
用户在前端：
  1. 手动登记操作：tool=execute_code, command="deploy_prod.py"
     → 账本写入 → agent 调 execute_code(deploy_prod.py) → block ✅
  2. 添加关键字："退款"
     → agent 调 terminal(curl POST /api/refund) → 参数含 "退款"? 
       → 不含（参数是 curl 命令不是中文）
     → agent 调 mcp__payment__refund(reason="退款处理") → 参数含 "退款" → HITL ✅
```

✅ 用户手动防御路径。

### 情况 10：YOLO 模式

```
YOLO 模式开启
  → HighRiskKeywordApprovalHandler.handle: is_yolo_mode=True → return False → 跳过
  → 关键字 HITL 不触发
  → 账本仍生效（账本不受 YOLO 影响）
```

⚠️ YOLO 跳过关键字 HITL 但账本仍 block。这是合理的——YOLO 是用户信任 agent，但账本防的是系统级重复执行（verify 重测），不是 agent 的主动决策。

---

## 8. 验收

### 后端——high_risk 标签 + 账本

| # | 场景 | 预期 |
|---|------|------|
| 1 | MCP 工具首次执行（无关键字） | 正常执行 + 账本登记 |
| 2 | MCP 工具 verify 重测 | 账本 block |
| 3 | write_file 执行 | 不入账本（high_risk=False） |
| 4 | execute_code 首次执行 | 入账本 |
| 5 | execute_code verify 重测 | 账本 block |
| 6 | session 结束 | 账本清空 |

### 后端——关键字 HITL（main agent）

| # | 场景 | 预期 |
|---|------|------|
| 7 | main agent 调 terminal(rm -rf /) | HITL 审批（内置关键字） |
| 8 | main agent 调 MCP(card_number=...) | HITL 审批（内置关键字） |
| 9 | main agent 调 terminal(pytest) | 无关键字 → 正常执行 |
| 10 | main agent 调 terminal(curl POST /deploy)（用户添加 "deploy" 关键字） | HITL 审批 |
| 11 | YOLO 模式 + 关键字命中 | HITL 跳过（YOLO），账本仍生效 |

### 后端——关键字拦截（子 agent）

| # | 场景 | 预期 |
|---|------|------|
| 12 | executor 子 agent 调 terminal(rm -rf /) | ToolGuardrails 直接 deny（无 HITL） |
| 13 | executor 子 agent 调 MCP(card_number=...) | ToolGuardrails 直接 deny |
| 14 | executor 子 agent 调 terminal(pytest) | 无关键字 → 正常执行 |
| 15 | general 子 agent 调 terminal(git push --force) | deny（内置关键字） |

### 后端——agent 登记关键字

| # | 场景 | 预期 |
|---|------|------|
| 16 | main agent 调 high_risk_guard(action="add_keyword", keyword="退款") | 关键字写入存储 |
| 17 | executor 子 agent 调 high_risk_guard(action="add_keyword", keyword="deploy") | 关键字写入存储（scope="all" 可用） |
| 18 | 添加关键字后，main agent 调含该关键字的工具 | HITL 审批 |
| 19 | 添加关键字后，子 agent 调含该关键字的工具 | ToolGuardrails deny |
| 20 | high_risk_guard(action="list_keywords") | 返回内置 + 自定义列表 |
| 21 | high_risk_guard(action="remove_keyword") | 删除自定义关键字 |

### 前端

| # | 场景 | 预期 |
|---|------|------|
| 22 | 工具栏 🛡️ 按钮 | 右栏打开防护面板 |
| 23 | 操作列表 | 显示 high_risk 工具已执行记录 |
| 24 | 删除操作记录 | 该操作可重新执行 |
| 25 | 内置关键字列表 | 只读显示 |
| 26 | 添加自定义关键字 | 写入 → agent 调用时拦截 |
| 27 | 删除自定义关键字 | 不再拦截 |
| 28 | 四语言 | 面板文案正确 |

---

## 9. Kill criteria

- 如果 `high_risk` 标注遗漏某些高危内置工具（漏标 = 该工具不防护），审计所有工具 metadata 确认标注完整
- 如果关键字 HITL 误拦太多（常见操作含关键字如 "password" 在配置文件里），用户可通过前端删除自定义关键字 + 调整内置列表
- 如果 MCP 工具全部 `high_risk=True` 导致所有 MCP 操作都入账本（正常 MCP 读操作也被 block），允许 MCP 工具通过配置声明 `high_risk: False`
- 如果 HighRiskKeywordApprovalHandler 在 handler 链中位置不当（被前面的 handler 消费），调整注册顺序
