# 计划：借鉴 ZCode turn 折叠展示（协议层分类 + structured_response 增强）

- 建档日期：2026-10-19
- 状态：**已实现（2026-10-20，视图层派生分类）**。折叠由已加载的 message 分组派生（`client/app/composables/use-chat-turn-fold.ts`），未新增服务端 render_unit API/WS frame：message 行只有 `turn_num`、trajectory 账本不含 assistant 文本，服务端分类只会重复客户端已有的行；structured_response 摘要仍为未启用的可选项。
- **文档范围**：让 sherry 前端能像 ZCode 一样折叠单轮 turn 的执行流程（工具调用、reasoning）而直接展示最终结果（assistant text）。**主方案是协议层分类（借鉴 ZCode + 复用 sherry 已有 trajectory_store turn_id 分组），可选增强用 langchain structured_response 让 LLM 生成语义化摘要**。
- **硬约束**：**不破坏现有 WS message 协议 + 不破坏现有 message store + 不强制 LLM 配合**（structured_response 是可选增强，不是必做），详见 §1
- 关联代码：
  - sherry 已有基础设施（复用）：
    - `server/service/trajectory_store.py:72, 84-85`（`trajectory_events` 表 + `turn_id` 列 + `idx_trajectory_turn` 索引——**已按 turn 分组**）
    - `server/service/trajectory_store.py:52-65`（`turn_scope(turn_id, session_id)` 上下文管理器）
    - `server/service/trajectory_store.py:226`（按 turn_id 查事件的 SQL）
    - `agent/tools/taskflow/tools/taskflow_plan.py:33, 94, 166`（taskflow 已用 `response_schema`——structured_response 在 sherry 已有先例）
  - ZCode 参考（对齐目标）：
    - `ZCode-main/.../packages/ui/src/v4/ConversationTurnGroup.tsx:724-887`（`ConversationWorkSegmentFlow` 用 Radix `<Collapsible>` 折叠 history）
    - `ZCode-main/.../packages/ui/src/v4/ConversationTurnGroup.tsx:634-722`（`AssistantHistoryStatus` 折叠触发器 + 工时摘要）
    - `ZCode-main/.../packages/ui/src/v4/conversationTurnRenderUnits.ts`（`ConversationTurnRenderUnit` 类型——协议层分类 `assistantHistoryRows` / `latestAssistantTextRow`）
  - 前端改造对象：
    - `client/app/pages/home/components/ChatBox.vue` 或类似对话渲染组件
    - `client/app/composables/use-chat-stream.ts`（WS frame 处理）
  - 关联 TODO：跟其他 TODO 正交（压缩/检索/任务编排都跟前端展示独立）

---

## 0. 背景：sherry 现状 vs ZCode（已核实）

### 0.1 sherry 当前对话渲染是线性 message 列表

sherry 的 message store 是 `messages: list[AIMessage | HumanMessage | ToolMessage]`，按顺序展示：
- 用户消息 → 工具调用 → 工具结果 → 工具调用 → 工具结果 → ... → 最终 assistant 回复
- 前端按顺序渲染所有 message
- **用户要翻滚过所有工具调用 + 工具结果才能看到最终回复**
- 没有"执行流程 vs 最终结果"的分层

### 0.2 ZCode 的折叠设计（已核实）

ZCode 在**数据模型层**就把 turn 的 rows 分成三类：

| row 类型 | 含义 | 渲染处理 |
|---|---|---|
| `assistantHistoryRows` | 历史工作行（工具调用、reasoning） | 用 `<CollapsibleContent>` 包裹，**折叠时隐藏** |
| `assistantWorkRows` | 当前正在执行的工作行 | 不折叠（运行中要看） |
| `latestAssistantTextRow` | 最新的助手文本回复 | **始终可见** |

渲染层用 Radix `<Collapsible>` 折叠 history，只展示：
- `AssistantHistoryStatus`（工时摘要按钮："Worked for 3.2s ▸"）
- `latestAssistantTextRow`（最终结果）

### 0.3 sherry 已有基础设施（关键发现）

**sherry 的 trajectory_store 已经按 turn_id 分组**（`trajectory_store.py:72, 84-85`）：

```python
# trajectory_events 表 schema（trajectory_store.py:72-84）
"turn_id TEXT NOT NULL",
...
"CREATE INDEX IF NOT EXISTS idx_trajectory_turn ON trajectory_events(turn_id, seq)"
```

**sherry 的 taskflow 已在用 structured_response**（`taskflow_plan.py:33, 94, 166`）：

```python
# taskflow step 可以配 response_schema
"response_schema": {optional JSON schema}  # taskflow_plan.py:33
```

这意味着：
- **协议层按 turn 分组的能力已经有了**——只需要加一个 API 把 turn 的 events 按"history + latest_assistant_text"分类返回
- **structured_response 在 sherry 有先例**——taskflow 已经在用,可以扩展到主 LLM

---

## 1. ⚠️ 硬约束

| 约束 | 含义 | 验收方式 |
|---|---|---|
| **F1**：现有 WS message 协议向后兼容 | 现有 `messages` / `turn_started` / `done` 等 frame 不动；新 frame `turn_render_unit` 是追加 | 老前端版本访问新后端不崩 |
| **F2**：现有 message store 不动 | `MesMemory` / `messages` 表 schema 不变；新分类是 derived（从现有 messages 按 turn 分组提取） | 现有 message 读写 byte-identical |
| **F3**：structured_response 是可选增强 | 主 LLM 不配 `TurnResult` schema 时，走协议层分类（方案 A）；配了才叠加 LLM 摘要（方案 B） | 不配 structured_response 时行为同方案 A |
| **F4**：trajectory_store 不破坏现有查询 | `GET /sessions/:id/trajectory` 现有 API 不变；新 API `GET /sessions/:id/turns/:turn_id/render_unit` 是追加 | 现有 trajectory 查询 byte-identical |
| **F5**：前端 collapsible 组件不破坏现有渲染 | 现有 message 渲染（user bubble / assistant text / tool call card）保留；collapsible 是外层包裹 | 现有渲染 byte-identical |
| **F6**：不强制 LLM 配合 | 方案 A 不需要 LLM 改输出格式；方案 B 是可选增强，用户不配就不走 | 方案 A 独立可用 |
| **F7**：turn 分组逻辑 fail-open | 如果某个 turn 没有明确 final assistant text（异常中断），退化到线性展示（不折叠） | 异常 turn 不崩 |

---

## 2. 任务一：协议层分类（方案 A，主方案，必做）

### 2.1 目标

借鉴 ZCode 的 `ConversationTurnRenderUnit`，在 sherry 服务端按 turn 把 messages 分类成 `history_rows` + `latest_assistant_text` + `work_status`，通过新 WS frame / HTTP API 传给前端。

### 2.2 改动清单

- [ ] **2.2.1** 新建 `server/service/turn_render_service.py`，实现 `build_turn_render_unit(session_id, turn_id)`：

  ```python
  def build_turn_render_unit(session_id: str, turn_id: str) -> dict:
      """把一个 turn 的 messages 按 ZCode ConversationTurnRenderUnit 分类。

      复用 trajectory_store 的 turn_id 分组能力（trajectory_store.py:72, 84）。
      """
      # 1. 从 message store 读这个 turn 的所有 messages（按 turn_id 过滤）
      messages = message_store.get_messages_by_turn(session_id, turn_id)

      # 2. 分类
      history_rows = []        # 工具调用 + 工具结果 + 中间 assistant text
      latest_assistant_text = None  # 最后一条 AIMessage（非工具调用）
      work_status = "completed"  # running / completed / interrupted

      for msg in messages:
          if isinstance(msg, AIMessage) and not msg.tool_calls:
              # 纯文本回复——可能是最终结果
              latest_assistant_text = msg
          else:
              # 工具调用 / 工具结果 / 带工具调用的 AIMessage
              history_rows.append(msg)

      # 3. work_status 判断
      if latest_assistant_text is None:
          # turn 还没结束（没有最终回复）或异常中断
          work_status = "running" if turn_is_running(turn_id) else "interrupted"

      return {
          "turn_id": turn_id,
          "history_rows": [serialize(m) for m in history_rows],
          "latest_assistant_text": serialize(latest_assistant_text) if latest_assistant_text else None,
          "work_status": work_status,
          "duration_ms": calc_turn_duration(session_id, turn_id),
      }
  ```

- [ ] **2.2.2** `server/DAO/messages.py`（或 message store）加 `get_messages_by_turn(session_id, turn_id)`：
  - 从 message store 按 turn_id 过滤 messages
  - 复用 trajectory_store 的 turn_id 索引（或从 messages 表的 metadata 里读 turn_id）

- [ ] **2.2.3** 新 HTTP API `GET /sessions/:session_id/turns/:turn_id/render_unit`：
  - 返回 `{turn_id, history_rows, latest_assistant_text, work_status, duration_ms}`
  - 前端 reconnect 时调这个 API 重建 turn render unit

- [ ] **2.2.4** 新 WS frame `turn_render_unit`：
  - 在 turn 结束（`done` frame）后推送
  - payload 同 HTTP API
  - 前端收到后切换到折叠渲染

- [ ] **2.2.5** turn 进行中推送 `turn_render_unit_update`（可选）：
  - 工具调用完成时更新 `history_rows`
  - 让前端能实时折叠已完成的工具调用

### 2.3 风险与约束

- **F2 message store 不动**：`get_messages_by_turn` 是 derived 查询，不改 schema
- **F7 fail-open**：`latest_assistant_text is None` 时退化到线性展示
- **turn_id 来源**：sherry 的 messages 表是否带 turn_id？需要核实——如果不带，要么从 trajectory_store 关联，要么给 messages 表加 turn_id 列
- **性能**：每个 turn 结束时 build_turn_render_unit——如果 turn 有 50 条 messages，分类是 O(N)，可接受

**验收**：F2 + F4 + F7。新建 `tests/server/test_turn_render_service.py`。

---

## 3. 任务二：structured_response 增强（方案 B，可选）

### 3.1 目标

让主 LLM 在最终输出里**显式分类** `final_answer` + `execution_summary`，前端在折叠按钮上展示 LLM 生成的语义化摘要（替代 ZCode 的 "Worked for 3.2s"）。

### 3.2 langchain structured_response 能力边界（关键）

**能做什么**：
- 让 LLM 最终输出 `{final_answer: str, execution_summary: str}` 结构化 JSON
- 前端从 `final_answer` 取最终结果，从 `execution_summary` 取摘要展示在折叠按钮上

**不能做什么**：
- **不能折叠工具调用本身**——工具调用是 turn 进行中的 message 历史，不在 LLM 最终输出里
- 工具调用的折叠仍然要靠方案 A（协议层分类）

**结论**：structured_response 是**方案 A 的增强**，不是替代。它能改善折叠按钮的展示（LLM 生成语义化摘要），但不能单独实现折叠。

### 3.3 改动清单

- [ ] **3.3.1** 定义 `TurnResult` schema（Pydantic / TypedDict）：

  ```python
  from pydantic import BaseModel, Field

  class TurnResult(BaseModel):
      """主 LLM 最终输出的结构化分类（可选增强，方案 B）。"""
      final_answer: str = Field(description="给用户看的主要回答")
      execution_summary: str = Field(
          description="本轮执行流程的简短摘要（1-2 句话），用于折叠按钮展示。"
          "例如：'查看了 schema,跑了迁移脚本,验证了数据'"
      )
  ```

- [ ] **3.3.2** 主 LLM 加 `with_structured_output(TurnResult)` 配置（per-session 可选）：
  - 新 state key `TURN_STRUCTURED_OUTPUT_ENABLED`（默认 false）
  - 用户开启后，主 LLM 用 `with_structured_output(TurnResult)` 输出
  - 不开启时，走方案 A（协议层分类），不强制 LLM

- [ ] **3.3.3** 前端折叠按钮展示 `execution_summary`（如果有）：
  - 方案 A 的 `AssistantHistoryStatus` 按钮显示 "Worked for 3.2s"
  - 方案 B 叠加：如果 `latest_assistant_text` 带 `execution_summary` 字段，按钮显示 LLM 生成的摘要 + 工时
  - 例：`"查看了 schema,跑了迁移脚本 (3.2s) ▸"`

### 3.4 风险与约束

- **F3 可选**：不配 `TurnResult` schema 时走方案 A，不影响主流程
- **token 成本**：LLM 多输出 `execution_summary` 字段，增加约 50-100 tokens/turn
- **LLM 如实性**：LLM 可能不如实写 `execution_summary`（编造或遗漏）——这跟 step_judge 的判断准确性问题同性质
- **structured_response 跟 thinking 模式兼容性**：thinking 模式下 LLM 输出 reasoning + structured output，需要确认 langchain `with_structured_output` 跟 thinking payload 兼容（参考 `models/LLMs/reasoning_payload.py`）

**验收**：F3 + F6。新建 `tests/models/test_turn_result_schema.py`。

---

## 4. 任务三：前端 collapsible 渲染（必做，依赖 §2）

### 4.1 目标

借鉴 ZCode 的 `ConversationTurnGroup` + `Collapsible`，在 sherry 前端（Nuxt/Vue）实现 turn 折叠渲染。

### 4.2 改动清单

- [ ] **4.2.1** 新建 Vue 组件 `TurnCollapsible.vue`（对标 ZCode `ConversationTurnGroup`）：
  - 用 `<details>`/`<summary>` 或 Vue collapsible 库（如 `@headlessui/vue` 的 `Disclosure`）
  - 接收 `turn_render_unit` prop（`{history_rows, latest_assistant_text, work_status, duration_ms}`）
  - 折叠 `history_rows`，始终展示 `latest_assistant_text`

- [ ] **4.2.2** 新建 `TurnHistoryStatus.vue`（对标 ZCode `AssistantHistoryStatus`）：
  - 折叠触发器按钮
  - 显示工时摘要："运行 3.2s" / "已完成 (3.2s)" / "已中断"
  - 如果 §3 的 `execution_summary` 有值，叠加显示：`"查看了 schema,跑了迁移 (3.2s) ▸"`

- [ ] **4.2.3** `ChatBox.vue`（或对话渲染组件）改造：
  - 收到 `turn_render_unit` WS frame 后，用 `TurnCollapsible` 渲染该 turn
  - turn 进行中（未收到 `turn_render_unit`）仍走线性渲染（向后兼容）
  - reconnect 时调 `GET /sessions/:id/turns/:turn_id/render_unit` 重建

- [ ] **4.2.4** `use-chat-stream.ts` 加 `turn_render_unit` frame 处理：
  - 收到 frame 后更新 turn 的渲染状态（从线性切换到折叠）

### 4.3 风险与约束

- **F1 向后兼容**：老前端版本收不到 `turn_render_unit` frame（不崩），仍走线性渲染
- **F5 不破坏现有渲染**：现有 message 渲染（user bubble / assistant text / tool call card）保留，collapsible 是外层包裹
- **Vue collapsible 库选择**：第一版可以用原生 `<details>`/`<summary>`（零依赖），后续再换 Headless UI
- **SSR 兼容**：Nuxt SSR 时 `<details>` 是 HTML 原生标签，SSR 友好

**验收**：F1 + F5。新建 `client/app/pages/home/components/__tests__/TurnCollapsible.integration.test.ts`。

---

## 5. 执行顺序与依赖

```
§2 协议层分类（方案 A，必做，2-3 天）
  ├─ turn_render_service.build_turn_render_unit
  ├─ get_messages_by_turn DAO
  ├─ HTTP API + WS frame
  │
  ↓ （§4 依赖 §2 的数据结构）
§4 前端 collapsible 渲染（必做，2-3 天）
  ├─ TurnCollapsible.vue
  ├─ TurnHistoryStatus.vue
  ├─ ChatBox.vue 改造
  │
  ↓ （§3 叠加在 §4 上）
§3 structured_response 增强（可选，1-2 天）
  ├─ TurnResult schema
  ├─ 主 LLM with_structured_output
  ├─ 前端按钮叠加 execution_summary

总工程量：4-6 天（必做）/ 5-8 天（含可选）
```

**关键路径**：§2 是基础，§4 依赖 §2 的数据结构，§3 叠加在 §4 上。

---

## 6. 验收标准

### 6.1 硬约束验收

- [ ] **F1**：老前端访问新后端不崩（收不到 `turn_render_unit` frame 走线性渲染）。
- [ ] **F2**：现有 message store schema 不变；`get_messages_by_turn` 是 derived 查询。
- [ ] **F3**：不配 `TurnResult` schema 时走方案 A，行为同协议层分类。
- [ ] **F4**：现有 `GET /sessions/:id/trajectory` API 不变。
- [ ] **F5**：现有 message 渲染保留，collapsible 是外层包裹。
- [ ] **F6**：方案 A 不需要 LLM 改输出格式。
- [ ] **F7**：异常 turn（无 final assistant text）退化到线性展示。

### 6.2 功能验收

- [ ] turn 结束后前端收到 `turn_render_unit` frame，切换到折叠渲染。
- [ ] 折叠状态：只看到工时摘要按钮 + 最终 assistant text。
- [ ] 展开状态：看到完整执行流程（工具调用、reasoning） + 最终 assistant text。
- [ ] reconnect 时调 `GET /sessions/:id/turns/:turn_id/render_unit` 重建折叠渲染。
- [ ] （可选）配 `TurnResult` schema 后，折叠按钮显示 LLM 生成的 `execution_summary` + 工时。

### 6.3 回归验收

- [ ] 现有 `tests/server/test_trajectory_store.py` 全套通过。
- [ ] 现有 `client/app/composables/use-chat-stream.ts` 测试通过。
- [ ] 现有 ChatBox 渲染测试 byte-identical（在线性模式下）。

---

## 7. 不做的事（边界）

| 项 | 不做的原因 |
|---|---|
| 把所有 turn 都折叠 | 只折叠已完成 + 有明确 final assistant text 的 turn；运行中 turn 仍线性展示（要看执行进度） |
| 让 LLM 决定哪些工具调用折叠 | 工具调用折叠是协议层做的（按 turn_id 分组），不依赖 LLM 判断 |
| 用 structured_response 替代协议层分类 | structured_response 只影响最终输出，不能折叠工具调用本身（§3.2 能力边界） |
| 改 message store schema 加 turn_id 列 | 第一版从 trajectory_store 关联 turn_id（已有索引）；如果性能不够再加列 |
| 实现 ZCode 的 Highspeed / workflow 卡片等高级特性 | 本 TODO 只做"折叠 history + 展示最终结果"，其他 ZCode 特性另起 TODO |
| 后台结果轮（backgroundResult）特殊渲染 | ZCode 有 `ConversationBackgroundResultWork` 处理后台结果轮，sherry 第一版不做 |

---

## 8. 关键参考

### sherry 内部（复用/改造）

- `server/service/trajectory_store.py:72, 84-85`（`trajectory_events` 表 + `turn_id` 索引——已按 turn 分组）
- `server/service/trajectory_store.py:52-65`（`turn_scope` 上下文管理器）
- `server/service/trajectory_store.py:226`（按 turn_id 查事件 SQL）
- `agent/tools/taskflow/tools/taskflow_plan.py:33, 94, 166`（taskflow 已用 `response_schema`——structured_response 先例）
- `agent/state_port.py`（transcript port，读 message list）
- `client/app/composables/use-chat-stream.ts`（WS frame 处理）
- `client/app/pages/home/components/ChatBox.vue`（对话渲染）

### ZCode 参考（对齐目标）

- `ZCode-main/.../packages/ui/src/v4/ConversationTurnGroup.tsx:724-887`（`ConversationWorkSegmentFlow` 用 Radix `<Collapsible>`）
- `ZCode-main/.../packages/ui/src/v4/ConversationTurnGroup.tsx:634-722`（`AssistantHistoryStatus` 折叠触发器）
- `ZCode-main/.../packages/ui/src/v4/ConversationTurnGroup.tsx:475-483`（`<CollapsibleContent>` 包裹 history）
- `ZCode-main/.../packages/ui/src/v4/conversationTurnRenderUnits.ts`（`ConversationTurnRenderUnit` 类型——协议层分类）

### langchain structured_response 参考

- `langchain_core.language.models.BaseChatModel.with_structured_output`（Pydantic schema → 结构化 JSON 输出）
- `models/LLMs/reasoning_payload.py`（thinking 模式 + structured output 兼容性参考）
- sherry taskflow `response_schema` 先例（`taskflow_plan.py:33`——subagent 已经在用 structured output）

### 关联 TODO

- 跟其他 TODO（压缩/检索/任务编排）正交——本 TODO 只改前端展示 + 协议层分类，不改压缩/检索/编排逻辑
