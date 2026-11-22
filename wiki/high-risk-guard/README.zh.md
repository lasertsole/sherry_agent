# 🛡️ 高危操作防护

[English](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> 不可逆操作如何被识别、只批准一次、重放时拒绝，以及如何从子智能体冒泡到主智能体交给用户审批。

事实来源：`agent/tools/pub_base/high_risk_guard_store.py`、`agent/tools/pub_base/high_risk_policy.py`、`agent/tools/high_risk_guard.py`、`agent/middlewares/tool_guardrails/core.py`、`agent/middlewares/humanInTheLoop/strategies.py`、`config/features/agent_side/high_risk_guard.py`、`server/trigger/http/high_risk_guard.py`、`client/app/pages/home/components/HighRiskGuardPanel.vue`。

## 🎯 威胁模型

一次验证重跑、一次工具重试或一次重新派发的 TaskFlow 步骤，都可能把同一个**不可逆**操作执行两遍：生产部署、支付扣款、强制推送。重写源码是正常工作，所以"非幂等"本身不能作为触发条件——门禁以**高危**为准。

| 层 | 回答什么 | 机制 |
| :---- | :------ | :-------- |
| L1 元数据 | 哪个工具可能造成不可逆后果？ | `metadata.high_risk`（MCP 默认是） |
| L2 关键字账本 | 哪些操作必须让人看到？ | 内置 + session/global 关键字，子串扫描 |
| L3 已执行操作账本 | 这次精确调用是否已经跑过？ | `(根 session, 工具, args_hash)` 行 + 首次结果 |
| L4 审批 | 首次执行由谁批准？ | 主智能体 HITL 卡片；子智能体 `[NEEDS_APPROVAL]` 冒泡 |

各层相互独立：即使用户批准了首次执行，账本仍会拒绝重放；关键字命中会在账本还没有记录时就先询问。

## ⚙️ 关键字账本

一张 SQLite 表（`src/data/high_risk_guard.db` 的 `high_risk_keywords`）同时保存内置覆盖与自定义关键字。关键字按**大小写不敏感的子串**匹配（不区分词边界：`password` 必须能匹配 `user_password=…`），且每个关键字必须**至少 3 个 Unicode 字符**——两个字符的条目会命中几乎所有中文句子。

| 字段 | 含义 |
| :---- | :------ |
| `session_id` | 具体会话 id、`global`，或内置默认 |
| `keyword` | 被扫描的文本 |
| `policy` | `block`（扫描/询问）或 `allow`（不扫描） |
| `source` | `builtin`、`user`（面板）或 `agent`（工具） |
| `executed` | 1 = 本次会话中该操作已执行 |
| `result_summary` | 首次结果，重放被拒时展示 |

解析顺序是 session → global → 内置默认。一条 `allow` 行会把该关键字从生效列表中移除，即使另一个 scope 标的是 `block`。内置清单覆盖凭据、金融标识、破坏性 shell 操作与破坏性 SQL（`rm -rf`、`mkfs`、`git push --force`、`DROP TABLE`、`password`、`private_key` 等）。

## 📒 已执行操作账本

只有**高危且非幂等**的工具进账本：PTC 的 `execute_code` 与所有 MCP 工具（默认）。`terminal`、`python_repl`、`write_file`、`patch_file` 不进账本——shell 命令或一次编辑属于正常工作；让具体命令被询问的是关键字扫描。

账本行以**根主会话**（子智能体解析 `APPROVAL_SCOPE_PARENT`）、工具名和稳定参数摘要（排序后的 JSON，绝不保存原始参数）为键。重复调用在执行前被拒绝，并返回首次的 `result_summary`。行按配置的保留期（24 小时）过期，会话删除时一并清除；面板上的删除就是显式的"再跑一次"。

**已执行**的关键字享受同样的一次性待遇：批准过的操作成功执行后，该关键字被标记为已执行，之后任何参数包含它的调用都会被拒绝并返回首次结果，直到面板（或 `high_risk_guard action=reset_keyword`）清除标记。

## 🔁 主智能体与子智能体

| | 主智能体 | 子智能体 |
| :--- | :--- | :--- |
| 关键字命中 | `HighRiskKeywordApprovalHandler` 弹出批准/拒绝卡片 | `ToolGuardrails` 返回 `[NEEDS_APPROVAL]`，子智能体完成 |
| 账本命中 | 直接用首次结果拒绝（不弹卡） | 相同 |
| 人工批准之后 | 把批准写入本会话的 `KEYWORD_APPROVALS` + 参数摘要 | 父智能体带 `approval_context` 恢复步骤；新子智能体收到该批准 |
| YOLO | 可逆工具跳过卡片；PTC/MCP 仍然询问 | `full_access` 子智能体跳过关键字扫描，账本仍生效 |
| confirm_all | 照常弹卡 | 子智能体直接拒绝（`[BLOCKED: confirm_all mode]`） |

HITL 卡片会在 `interrupt()` 之前冻结会话心跳（`HEARTBEAT_SKIP`），并在恢复的这一遍返回后清除，因此审批再慢也不会让回合因"停滞"被杀。

### 冒泡路径

1. 子智能体的 guardrails 返回 `[NEEDS_APPROVAL: keyword=…, tool=…]`；
2. 子智能体完成；完成消息构造器追加一段 `[SYSTEM: …]` 引导，告诉父智能体用 `question` 并如何恢复；
3. 父智能体通过 `question` 询问用户（走正常 HITL 卡片）；
4. 批准后父智能体调用 `taskflow_step(action="resume", step_outcome="approved", step_id=…, approval_context={keyword, tool})`——直接 spawn 则用 `sessions_spawn(approval_context=…)`；
5. 批准被写进**新子智能体自己的**寄存器（`KEYWORD_APPROVALS`），因此只有这次 spawn 能通过该关键字；下一次无关 spawn 仍会询问；
6. 拒绝时以 `blocked` 恢复该步骤，其依赖保持阻塞。

只有 TaskFlow 步骤会沿 `_OPTIONAL_STEP_FIELDS` → `step_spawn_kwargs` → `dispatch_child` → `SpawnRequest.approval_context` 携带批准；已批准步骤的普通重试会保留该批准（schema 重试是同一操作），新步骤不会继承。

## 🧰 工具、API 与面板

`high_risk_guard`（主智能体与子智能体均可用）管理账本与操作：`add_keyword`、`list_keywords`、`remove_keyword`、`set_policy`、`reset_keyword`、`register_op`、`list_ops`、`remove_op`。子智能体的写入会解析到根主会话。

| 端点 | 用途 |
| :------- | :------ |
| `GET /sessions/:sid/high-risk-keywords` | 关键字 + 账本（面板一次加载） |
| `POST /sessions/:sid/high-risk-keywords` | 添加 `{keyword, scope}` |
| `PUT /sessions/:sid/high-risk-keywords` | 设置 `{keyword, policy}` |
| `DELETE /sessions/:sid/high-risk-keywords/:id` | 删除一行 |
| `POST /sessions/:sid/high-risk-keywords/reset` | 清除已执行标记 |
| `DELETE /sessions/:sid/high-risk-ops/:id` | 删除一条账本记录 |
| `GET /high-risk-keywords/builtin` | 只读内置清单 |

工具栏的盾牌按钮会为当前会话打开 `HighRiskGuardPanel`：添加/删除关键字、在 block↔allow 之间切换、重置标记，以及删除账本记录（显式重跑）。

## 🧪 测试

| 套件 | 覆盖内容 |
| :---- | :----- |
| `tests/agent/tools/pub_base/test_high_risk_guard_store.py` | 校验、扫描、作用域、策略、已执行标记、账本往返、清理、保留期 |
| `tests/agent/middlewares/test_high_risk_guard.py` | 账本拒绝/重放、关键字 NEEDS_APPROVAL、批准、YOLO、confirm_all、根作用域 |
| `tests/agent/middlewares/humanInTheLoop/test_high_risk_keyword_handler.py` | handler 匹配、已执行拒绝、批准/拒绝、心跳冻结、headless 拒绝 |
| `tests/agent/tools/test_high_risk_guard_tool.py` | agent 工具的每个 action + 子→根作用域 |
| `tests/agent/tools/taskflow/test_approval_resume.py` | 批准后重新派发、重试保留批准、校验拒绝 |
| `tests/agent/tools/subagent/test_child_access_inheritance.py` | 访问模式继承、批准作用域到 token 根、批准写入 |
| `tests/server/trigger/http/test_high_risk_guard_api.py` | 关键字 CRUD、策略、重置、账本删除 |
| `client/app/pages/home/components/__tests__/HighRiskGuardPanel.integration.test.ts` | 面板渲染、添加/策略/重置/删除、后端拒绝 |

## ⚠️ 诚实声明与局限

- **扫描是绊线，不是解析器。** 出现在无关字符串里的关键字（例如写文档命令中的 `echo "password"`）仍会询问；而用未注册词语描述的操作会直接通过。清单由操作者/智能体维护。
- **关键字的已执行标记是会话级的。** 计划里"每个步骤重新询问"在这里被刻意收紧为更严格的语义：第一次批准的操作会标记该关键字，之后任何包含它的调用都被拒绝，直到重置标记。这对防重放是安全的，但合法地再跑一次需要面板操作。
- **未标注的工具默认低危。** 显式的 `metadata.high_risk` 优先；没有该键的 `mcp__` 工具是高危，其余未标注工具不是。把危险操作藏在普通名字后面的插件必须自己标注。
- **MCP 按前缀入账本且默认非幂等。** 确实安全的 MCP 读操作可以声明 `high_risk: False` / `idempotent: True`。
- **账本保存参数预览（≤300 字符）。** 它留在本地 `high_risk_guard.db`（绝不进入被跟踪的 workspace）；预览仍可能包含调用时带入的值。
- **冒泡依赖父智能体进入回合。** 完成消息现在会自动带引导，但真正的批准仍是主智能体必须发起的 `question` 调用；不读完成消息的父智能体会让步骤一直 blocked。
- **保留期是惰性的。** 过期行最多每小时在一次账本访问时清扫；一条行可能超过 24 小时窗口存活到下一次清扫。
