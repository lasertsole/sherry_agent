# Agent Middlewares — Agent 中间件系统

> **Agent Middlewares** 是 EMA AI Agent 的中间件层，位于 Agent 核心执行流程的关键节点，负责在**模型调用前**和**模型调用后**进行上下文增强、对话压缩和记忆管理。

---

## 目录

- [概述](#概述)
- [架构](#架构)
- [中间件详解](#中间件详解)
- [技术栈](#技术栈)
- [FAQ](#faq)

---

## 概述

### 设计定位

Agent Middlewares 基于 LangChain 的中间件体系（`AgentMiddleware` / `SummarizationMiddleware`）实现，通过**切面编程（AOP）**的方式挂载到 Agent 执行流水线中，在每个推理周期的特定时机执行横切逻辑。

| 中间件 | 时机 | 职责 |
|--------|------|------|
| `ContextEngineHook` | Agent 推理前 & 推理后 | 从 Context Engine 检索技能记忆和长期记忆，构造增强提示词；推理完成后持久化对话 |
| `Summarization` | 模型调用前 | 对话历史过长时压缩上下文窗口，触发用户偏好提取 |

### 核心能力

1. **上下文增强** — 在 Agent 推理前，从 Skill Memory Graph 检索相关技能和记忆，构造增强 prompt
2. **对话压缩** — 在模型调用前压缩超长上下文窗口，防止 token 超限
3. **记忆提取** — 压缩时同步触发用户偏好提取，将对话中的偏好写入长期 memory store
4. **自动持久化** — 每轮推理结束后自动将对话写入 MesMemory

---

## 架构

```
Agent 执行流水线:

┌─────────────────────────────────────────────────────────────┐
│                    Agent Runtime (LangGraph)                 │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ① abefore_agent()                                          │
│     └─ ContextEngineHook.abefore_agent                       │
│        ├─ retrieve_history_by_last_n_prompt() → 最近对话     │
│        ├─ build_mixed_query() → 构造增强查询                  │
│        └─ assemble() → 从 Skill Memory Graph 检索技能记忆    │
│           └─ 结果拼接到用户消息前作为 system prompt          │
│                                                              │
│  ② abefore_model()                                           │
│     └─ Summarization.abefore_model (继承 SummarizationMW)    │
│        ├─ 复制消息列表，剥离 system message 提高压缩密度      │
│        ├─ 保留最后一条 HumanMessage                           │
│        ├─ 调用父类压缩逻辑 → reduce_messages                 │
│        ├─ 插回 system message 和最后一条 human message       │
│        ├─ 重置 memory_store                                  │
│        ├─ 调用 nudge_messages() → 提取用户偏好                │
│        └─ 再次重置 memory_store                              │
│                                                              │
│  ③ LLM 推理                                                  │
│                                                              │
│  ④ aafter_agent()                                            │
│     └─ ContextEngineHook.aafter_agent                        │
│        ├─ slice_last_turn() → 提取最后一轮对话                │
│        ├─ sanitize_tool_use_result_pairing() → 清理工具调用    │
│        ├─ 从拼接后的消息中还原原始用户输入（去除增强前缀）     │
│        ├─ after_turn() → 异步后处理（Skill Memory 学习）      │
│        └─ add_messages() → 持久化到 MesMemory                │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### 执行顺序

```
1. abefore_agent  (ContextEngineHook)  —→  上下文增强
2. abefore_model  (Summarization)      —→  上下文压缩 + 偏好提取
3. LLM 模型调用
4. aafter_agent   (ContextEngineHook)  —→  记忆持久化 + 知识学习
```

---

## 中间件详解

### ContextEngineHook

**文件：** `context_engine_hook.py`

**类：** `ContextEngineHook(AgentMiddleware)`

在 Agent 推理**之前**增强用户消息，在 Agent 推理**之后**持久化对话。

#### `abefore_agent(state, runtime)`

```
输入: 用户原始消息 "如何部署 Docker?"
        │
        ▼
1. 过滤 state["messages"] 中的 SystemMessage
2. 提取最后一条 HumanMessage 的内容
3. _build_turn_prompt(query_text):
   ├─ retrieve_history_by_last_n_prompt() → 最近 5 轮对话
   ├─ build_mixed_query(历史 + 当前查询)  → 构造增强查询
   └─ assemble(增强查询)                  → 从 Skill Memory Graph 检索
      └─ 返回 system_prompt_addition (相关技能说明 + 长期记忆)
4. 将 system_prompt_addition 拼接到原始用户消息前

输出: "【技能记忆+长期记忆】如何部署 Docker?"
```

**关键细节：**

- 支持多种消息格式：纯文本 `str`、单媒体 `dict`、多媒体列表 `list[dict]`
- 对多媒体消息（如图+文本），只取 `type="text"` 的部分进行增强，增强后写回原位置
- 系统提示消息在增强前被移除，避免干扰检索过程

#### `aafter_agent(state, runtime)`

```
输入: 完整的推理结果消息列表
        │
        ▼
1. slice_last_turn() → 提取最后一轮对话（用户 + AI + 工具调用）
2. sanitize_tool_use_result_pairing() → 确保工具调用和结果配对正确
3. 从用户消息中去除之前拼接的增强前缀，还原原始输入
4. 异步执行:
   ├─ after_turn(session_id, last_turn_messages)
   │   └─ 触发 Skill Memory 学习流程（知识提取 + 图谱更新）
   └─ add_messages(session_id, messages)
       └─ 持久化到 MesMemory SQLite 存储
```

**关键细节：**

- 使用 `asyncio.create_task` 异步执行后处理，不阻塞 Agent 响应
- 用户输入在持久化前被还原（去除增强前缀），防止提示词占满上下文窗口
- 使用 `sanitize_tool_use_result_pairing` 清理工具调用配对，确保数据完整性

---

### Summarization

**文件：** `summarization.py`

**类：** `Summarization(SummarizationMiddleware)`

在模型调用前压缩过长的对话历史，并在压缩时触发用户偏好提取。

#### `abefore_model(state, runtime)`

```
输入: 可能超长的消息列表（如 100K+ token）
        │
        ▼
1. 复制消息列表（避免修改原始 state）
2. 剥离 SystemMessage（保留后重新插入，避免污染压缩结果）
3. 保留最后一条 HumanMessage（确保最新用户问题不丢失）
4. 调用父类 SummarizationMiddleware.abefore_model
   └─ 对历史消息进行 LLM 摘要压缩
   └─ 返回 reduce_messages（包含 RemoveMessage 标记）
5. 插回 SystemMessage（在第一个 RemoveMessage 之后）
6. 如果最后一条 HumanMessage 未被保留，重新插入
7. 重置 memory_store（从磁盘重新加载）
8. 调用 nudge_messages(session_id, nudge_turn=0) → 强制提取用户偏好
9. 再次重置 memory_store
```

**关键细节：**

- **为什么剥离 SystemMessage？** 系统提示信息（角色设定、工具说明等）与被压缩的历史消息语义差异大，混在一起会降低压缩摘要的信息密度
- **为什么压缩前后重置 memory_store？** 确保偏好提取前后 memory store 状态与磁盘文件一致，避免并发读写导致的数据不一致
- **`nudge_turn=0`**：强制触发偏好提取，不考虑轮次间隔
- **保留最新 HumanMessage**：避免压缩时将最新的用户输入也摘要化，导致 LLM 无法看到原始提问

---

## 中间件对比

| 特性 | ContextEngineHook | Summarization |
|------|-------------------|---------------|
| **基类** | `AgentMiddleware` | `SummarizationMiddleware` |
| **触发时机** | Agent 前后 (before/after agent) | 模型调用前 (before model) |
| **核心操作** | 上下文增强 + 持久化 | 对话压缩 + 偏好提取 |
| **阻塞性** | 异步非阻塞（after 部分） | 同步阻塞 |
| **依赖** | Context Engine (assemble, after_turn) | MesMemory (nudge_messages) |
| **频率** | 每轮 Agent 推理 | 仅在上下文过长时（父类判断） |

---

## 技术栈

| 组件 | 技术选型 |
|------|----------|
| **中间件框架** | LangChain `AgentMiddleware` / `SummarizationMiddleware` |
| **Agent 运行时** | LangGraph `Runtime` |
| **消息模型** | LangChain `BaseMessage` / `SystemMessage` / `HumanMessage` / `AIMessage` |
| **记忆系统** | Context Engine (Skill Memory Graph + MesMemory) |
| **存储** | MesMemory SQLite + Memory Store (markdown 文件) |

---

## FAQ

### Q1: ContextEngineHook 为什么要剥离 SystemMessage？

在 `abefore_agent` 阶段，过滤 SystemMessage 是为了避免系统提示（角色设定、工具定义等）被当作查询上下文传给 Context Engine 检索，干扰技能记忆和长期记忆的召回准确性。检索完成后再通过返回值传递 system_prompt_addition。

---

### Q2: Summarization 为什么要在压缩时强制提取偏好？

压缩意味着上下文窗口正在变小，旧的对话历史即将被摘要替代。如果不在此刻提取偏好，被压缩掉的细节（如用户明确表达的个人喜好）将永久丢失。强制提取确保偏好被写入长期 memory store，即使原始对话被摘要化。

---

### Q3: aafter_agent 使用 asyncio.create_task 有什么风险？

`after_turn` 和 `add_messages` 使用 `asyncio.create_task` 异步执行，不阻塞 Agent 返回响应。这意味着：
- 如果 Agent 进程异常退出，未完成的异步任务可能丢失
- 如果异步任务本身抛出异常，可能被 `asyncio` 静默吞掉
- 设计上是可接受的权衡：响应速度优先于后处理的可靠性

---

### Q4: 两个中间件的执行顺序如何保证？

执行顺序由 LangGraph Runtime 内部的中间件链保证。中间件按注册顺序执行：
1. `abefore_agent` → `abefore_model` → LLM → `aafter_agent`
2. 同一阶段的多个中间件按注册先后顺序执行

---

## 许可证

本项目遵循 EMA AI Agent 的开源协议。

---

**最后更新：** 2026-05-30
