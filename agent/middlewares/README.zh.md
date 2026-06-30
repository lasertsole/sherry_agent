# Agent Middlewares — Agent 中间件系统

**中文** | [**English**](README.md)

> **Agent Middlewares** 是 EMA AI Agent 的中间件层，位于 Agent 核心执行流程的关键节点，通过 LangChain 的 AOP 风格中间件框架，在模型推理的**前、中、后**阶段负责**上下文增强**、**对话压缩**、**记忆管理**、**工具调用安全**、**消息标准化**和**多模态转码**。

---

## 目录

- [概述](#概述)
- [架构](#架构)
- [中间件详解](#中间件详解)
  - [ContextEngineHook](#contextenginehook)
  - [Summarization](#summarization)
  - [ToolLoopPrevention](#toolloopprevention)
  - [ToolCallNormalize](#toolcallnormalize)
  - [ToolTimeout](#tooltimeout)
  - [MultimodalProcessor](#multimodalprocessor)
- [对比](#对比)
- [工作流（时序图）](#工作流时序图)
- [生命周期](#生命周期)
- [核心机制](#核心机制)
- [数据模型](#数据模型)
- [配置](#配置)
- [使用示例](#使用示例)
- [FAQ](#faq)
- [技术栈](#技术栈)
- [许可证](#许可证)

---

## 概述

### 设计定位

Agent Middlewares 基于 LangChain 的中间件体系（`AgentMiddleware` / `SummarizationMiddleware`）实现，通过**切面编程（AOP）**的方式挂载到 Agent 执行流水线中，在每个推理周期的特定时机执行横切逻辑。

| 中间件 | 时机 | 职责 |
|--------|------|------|
| `ContextEngineHook` | Agent 推理前 & 推理后 | 从 Context Engine 检索技能记忆和长期记忆，构造增强提示词；推理完成后持久化对话 |
| `Summarization` | 模型调用前 | 对话历史过长时压缩上下文窗口，触发用户偏好提取 |
| `ToolLoopPrevention` | 每次工具调用前 (via `awrap_tool_call`) | 检测并阻止单轮内同一工具调用超过阈值的循环 |
| `ToolCallNormalize` | Agent 推理前 | 修复工具调用/结果配对不一致、缺少参数等问题 |
| `ToolTimeout` | 每次工具调用前 (via `awrap_tool_call`) | 为工具调用设置超时阈值，超时后返回错误 ToolMessage |
| `MultimodalProcessor` | Agent 推理前 & 推理后 | 将 base64 图片解码为临时文件供模型消费；推理后清理过期临时文件 |

### 核心能力

1. **上下文增强** — 在 Agent 推理前，从 Skill Memory Graph 检索相关技能和记忆，构造增强 prompt
2. **对话压缩** — 在模型调用前压缩超长上下文窗口，防止 token 超限
3. **偏好提取** — 在压缩时同步触发用户偏好提取，将偏好写入长期 memory store
4. **自动持久化** — 每轮推理结束后通过 `asyncio.create_task` 自动将对话写入 MesMemory
5. **工具循环防护** — 自动检测并阻止同一工具在单轮内被反复调用
6. **工具调用标准化** — 修复工具调用/结果配对不一致，清理孤立消息
7. **工具超时控制** — 为每个工具调用设置超时阈值，防止无限等待
8. **多模态图片处理** — 将 base64 编码的图片解码为临时文件，推理后自动清理

---

## 架构

```
Agent 执行流水线：

┌─────────────────────────────────────────────────────────────┐
│                    Agent Runtime (LangGraph)                 │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ① abefore_agent()                                           │
│     ├─ ContextEngineHook.abefore_agent                       │
│     │  ├─ 从 state["messages"] 中过滤 SystemMessage          │
│     │  ├─ 提取最后一条 HumanMessage 内容                     │
│     │  └─ _build_turn_prompt(query_text):                    │
│     │     ├─ retrieve_history_by_last_n_prompt() → 对话轮次  │
│     │     ├─ build_mixed_query() → 增强后的查询              │
│     │     └─ assemble() → Skill Memory Graph 上下文          │
│     │        └─ 结果作为系统提示拼接到用户消息前              │
│     ├─ ToolCallNormalize.abefore_agent                       │
│     │  └─ sanitize_tool_use_result_pairing() → 修复消息配对  │
│     └─ MultimodalProcessor.abefore_agent                     │
│        └─ base64 图片解码 → 写入临时文件 → 替换消息内容      │
│                                                              │
│  ② abefore_model()                                           │
│     └─ Summarization.abefore_model (继承 SummarizationMW)    │
│        ├─ 复制消息列表，剥离 SystemMessage                    │
│        ├─ 保留最后一条 HumanMessage                           │
│        ├─ 调用父类压缩逻辑 → reduce_messages                 │
│        ├─ 重新插入 SystemMessage 和最后一条 HumanMessage      │
│        ├─ memory_store.load_from_disk()  (nudge 前)           │
│        ├─ nudge_memory(session_id, nudge_turn=0)           │
│        └─ memory_store.load_from_disk()  (nudge 后)           │
│                                                              │
│  ③ LLM 推理                                                  │
│     ├─ awrap_tool_call(request, handler) → ToolLoopPrevention │
│     ├─ awrap_tool_call(request, handler) → ToolTimeout        │
│     └─ (工具调用在 agent 推理循环中执行)                      │
│                                                              │
│  ④ aafter_agent()                                            │
│     ├─ ContextEngineHook.aafter_agent                        │
│     │  ├─ slice_last_turn() → 提取最后一轮对话               │
│     │  ├─ sanitize_tool_use_result_pairing() → 清理工具配对  │
│     │  ├─ 去除增强前缀，还原原始用户输入                      │
│     │  ├─ asyncio.create_task(after_turn())   → 异步学习     │
│     │  └─ asyncio.create_task(add_messages()) → 持久化       │
│     └─ MultimodalProcessor.aafter_agent                      │
│        └─ 清理超过 7 天的临时文件                              │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### 执行顺序

```
1. abefore_agent
   ├─ ToolCallNormalize     —→  修复工具调用/结果配对
   ├─ MultimodalProcessor   —→  base64 图片解码为临时文件
   └─ ContextEngineHook     —→  上下文增强
2. abefore_model
   └─ Summarization         —→  上下文压缩 + 偏好提取
3. LLM 模型调用
   ├─ awrap_tool_call(ToolLoopPrevention)  —→  循环检测（每次调用）
   └─ awrap_tool_call(ToolTimeout)         —→  超时控制（每次调用）
4. aafter_agent
   ├─ ContextEngineHook     —→  记忆持久化 + 知识学习
   └─ MultimodalProcessor   —→  清理过期临时文件
```

---

## 中间件详解

### ContextEngineHook

**文件：** `context_engine_hook.py`

**类：** `ContextEngineHook(AgentMiddleware)`

在 Agent 推理**之前**增强用户消息，在 Agent 推理**之后**持久化对话。

#### `__init__(session_id: str)`

```python
hook = ContextEngineHook(session_id="session_001")
```

存储会话 ID 并初始化一个空的 `_turn_prompt` 字符串，该字符串将在 `abefore_agent` 期间被填充。

---

#### `_build_turn_prompt(query_text: str) -> None`

内部方法，通过编排三个 Context Engine 调用来构造增强前缀：

```python
async def _build_turn_prompt(self, query_text: str) -> None:
    # 1. 检索最近的对话轮次
    recent_messages_addition = retrieve_history_by_last_n_prompt(session_id=self._session_id)

    # 2. 基于历史重写查询（代词 → 实体）
    transformer_query_text = build_mixed_query(
        turns_of_history=recent_messages_addition,
        query=query_text
    )

    # 3. 检索 Skill Memory Graph 上下文
    assemble_result = await assemble(user_text=transformer_query_text)
    skill_system_prompt_addition = assemble_result.get("system_prompt_addition", "")

    # 构造结构化内容：上下文 + 指令
    self._turn_prompt = textwrap.dedent(f"""\
        {skill_system_prompt_addition}\n\n
        Using the reference materials above (note: they may contain inaccuracies,
        so use them critically), answer the user's actual question below.\n\n
    """)
```

**关键细节：**
- 增强前缀既包含 Skill Memory 图上下文，也包含一条批判性使用指令
- 前缀存储在 `self._turn_prompt` 中，稍后在 `aafter_agent` 中移除，防止上下文窗口膨胀

---

#### `abefore_agent(state, runtime)`

```
输入：用户原始消息 "如何部署 Docker？"
        │
        ▼
1. 从 state["messages"] 中过滤 SystemMessage（反向迭代，原地删除）
2. 提取最后一条 HumanMessage 的内容
3. 处理三种消息格式：
   ├─ 纯文本 str       → _build_turn_prompt() + 前置拼接
   ├─ 单媒体 dict      → 仅增强 "type":"text" 部分
   └─ 多媒体 list      → 找到文本项，原地增强
4. 将 self._turn_prompt 前置拼接到原始消息

输出："[技能记忆上下文 + 指令] 如何部署 Docker？"
```

**消息格式支持：**

| 输入类型 | 行为 |
|----------|------|
| `str` | 直接通过字符串拼接增强 |
| `dict`（单媒体） | 原地增强 `text` 键 |
| `list[dict]`（多媒体） | 找到 `type="text"` 项，原地增强 |
| 空/None 内容 | 返回 `None`（跳过） |

---

#### `aafter_agent(state, runtime)`

```
输入：完整的推理结果消息列表
        │
        ▼
1. slice_last_turn(all_messages) → 提取最后一轮对话
2. sanitize_tool_use_result_pairing(last_turn) → 清理工具调用/结果配对
3. 从清理后的最后一条人类消息中提取 user_text
4. 去除增强前缀：user_text = user_text.removeprefix(self._turn_prompt)
5. 将还原后的原始用户输入写回 last_human_message.content
6. 从后续消息中提取 AI 回复文本
7. 并发启动两个异步任务：
   ├─ after_turn(session_id, last_turn_messages)
   │   └─ Skill Memory 学习流水线（知识提取 + 图谱更新）
   └─ add_messages(session_id, messages)
       └─ 持久化到 MesMemory SQLite 存储
   └─ await asyncio.gather(task1, task2)
```

**关键细节：**

| 关注点 | 解决方案 |
|--------|----------|
| 非阻塞持久化 | `asyncio.create_task` + `asyncio.gather` |
| 上下文窗口管理 | 存储前去除增强前缀 |
| 工具调用完整性 | `sanitize_tool_use_result_pairing` 修复不均衡配对 |
| 多格式用户输入 | 与 `abefore_agent` 同理处理 `str`、`dict`、`list[dict]` |

---

### Summarization

**文件：** `summarization.py`

**类：** `Summarization(SummarizationMiddleware)`

在模型调用前压缩过长的对话历史，并在压缩时触发用户偏好提取。

#### `__init__(session_id: str, **kwargs)`

```python
summarizer = Summarization(session_id="session_001", ...)
```

`**kwargs` 转发给父类 `SummarizationMiddleware`（基础的压缩配置）。

---

#### `abefore_model(state, runtime)`

```
输入：可能超长的消息列表（如 100K+ token）
        │
        ▼
1. 复制状态 + 消息列表（避免修改原始）
2. 剥离 SystemMessage → 保存引用，从副本中删除
3. 保留最后一条 HumanMessage → 保存引用
4. 调用父类 SummarizationMiddleware.abefore_model(copy_state, runtime)
   └─ 对历史消息进行 LLM 摘要压缩
   └─ 返回 reduce_messages（包含 RemoveMessage 标记）
5. 在 reduce_messages 中的第一个 RemoveMessage 之后重新插入 SystemMessage
6. 如果保存的最后一条 HumanMessage != reduce_messages 中的最后一条，重新插入
7. memory_store.load_from_disk()  — 同步内存状态与磁盘
8. nudge_memory(session_id, nudge_turn=0)  — 强制偏好提取
9. memory_store.load_from_disk()  — 重新加载以捕获 nudge 写入
10. 返回 res（父类的结果字典）
```

**关键细节：**

| 关注点 | 解决方案 |
|--------|----------|
| SystemMessage 分离 | 压缩前剥离，避免污染语义密度 |
| 最新用户输入保留 | 压缩后重新插入最后一条 `HumanMessage`，确保 LLM 看到原始问题 |
| 数据一致性 | `memory_store.load_from_disk()` 在 nudge **前**和**后**各调用一次，确保内存状态与磁盘同步 |
| 强制提取 | `nudge_turn=0` 绕过正常的轮次间隔检查 |
| 不可变状态 | 消息列表被克隆，避免对原始 agent 状态的副作用 |

**为什么要分离 SystemMessage？**

系统提示（角色设定、工具定义等）与历史对话消息的语义分布有本质差异。将它们混入同一压缩过程会降低信息密度 — 摘要器会浪费容量，把（不变的系统提示）和（变化的对话）一起编码。压缩前剥离、压缩后重新插入，能显著提升摘要质量。

**为什么在 nudge 前后重载 memory_store？**

`memory_store` 是一个单例内存缓存，底层由磁盘上的 markdown 文件支持。如果其他 agent 或进程在上次加载后写入过磁盘，它可能已过时。nudge 前重载确保提取器看到最新状态；nudge 后重载确保后续读取能看到新写入的偏好。

---

### ToolLoopPrevention

**文件：** `tool_loop_prevention.py`

**类：** `ToolLoopPrevention(AgentMiddleware)`

检测并阻止同一工具在单轮 Agent 推理中被反复调用的循环行为。

#### `__init__(session_id: str, threshold: int = 5)`

```python
preventer = ToolLoopPrevention(session_id="session_001", threshold=5)
```

- `threshold`：同一工具每轮最大允许调用次数（默认 5）

---

#### `abefore_agent(state, runtime)`

重置当前轮的调用计数器。

```
输入：任何状态
        │
        ▼
1. 重置 self._tool_call_counts = {}
2. 允许新的 agent 推理轮次从零开始计数
```

---

#### `awrap_tool_call(request, handler)`

拦截每次工具调用，检测是否超过阈值。

```
输入：ToolCallRequest + 下一个处理函数
        │
        ▼
1. 提取 tool_call.name（工具名称）
2. 自增计数器：self._tool_call_counts[name] += 1
3. 如果计数 > threshold：
   └─ 返回 ToolMessage(content="循环检测：工具 {name} 已调用 N/N 次", 
                       status="error", tool_call_id=...)
4. 否则：调用 handler(request) 继续正常流程
```

**关键细节：**

| 关注点 | 解决方案 |
|--------|----------|
| 无状态清洗 | 每轮 `abefore_agent` 重置计数器，防止跨轮污染 |
| 细粒度检测 | 按工具名称独立计数，不影响其他工具 |
| 非侵入式 | 通过 `awrap_tool_call` 包装，不影响正常工具执行 |

---

### ToolCallNormalize

**文件：** `tool_call_normalize.py`

**类：** `ToolCallNormalize(AgentMiddleware)`

在 Agent 推理前修复消息列表中工具调用（`tool_calls`）与工具结果（`ToolMessage`）之间的配对不一致问题。

#### `__init__(session_id: str)`

```python
normalizer = ToolCallNormalize(session_id="session_001")
```

---

#### `abefore_agent(state, runtime)`

```
输入：包含可能未配对的工具调用/结果的消息列表
        │
        ▼
1. 提取 state["messages"]
2. 调用 sanitize_tool_use_result_pairing(messages)
   └─ 从所有消息中重建工具调用/结果配对
   └─ 移除孤立的 ToolMessage（无对应 tool_call）
   └─ 移除孤立的 tool_call_block（无对应 ToolMessage）
3. 将清理后的消息列表写回 state["messages"]
```

**与 ContextEngineHook 后处理的区别：**

| 方面 | ToolCallNormalize | ContextEngineHook.aafter_agent |
|------|-------------------|-------------------------------|
| 时机 | Agent 推理**前**（预先清理） | Agent 推理**后**（持久化前） |
| 目的 | 防止因配对不一致导致的推理错误 | 确保存储到 MesMemory 前数据整洁 |
| 范围 | 整个消息列表 | 仅最后一轮 |
| 操作 | 移除不配对项 | 调用 `sanitize_tool_use_result_pairing` |

---

### ToolTimeout

**文件：** `tool_timeout.py`

**类：** `ToolTimeout(AgentMiddleware)`

为工具调用设置超时阈值，防止工具无限挂起阻塞 Agent 推理。

#### `__init__(session_id: str, timeout_seconds: float = TOOL_TIMEOUT)`

```python
timeout_mw = ToolTimeout(session_id="session_001", timeout_seconds=30.0)
```

默认超时时间从环境变量 `TOOL_CALL_TIMEOUT_MINUTES` 读取，转换为秒。未设置时使用 `TOOL_TIMEOUT` 常量（默认 120 秒）。

---

#### `awrap_tool_call(request, handler)`

```
输入：ToolCallRequest + 下一个处理函数
        │
        ▼
1. 调用 asyncio.wait_for(handler(request), timeout=timeout_seconds)
2. 如果 handler 在超时内完成：
   └─ 正常返回结果
3. 如果 asyncio.TimeoutError：
   └─ 返回 ToolMessage(content="工具 {name} 在 {timeout}s 内未完成",
                       status="error", tool_call_id=...)
```

**关键细节：**

| 关注点 | 解决方案 |
|--------|----------|
| 异步安全 | 使用 `asyncio.wait_for`，与 LangGraph 的异步执行模型兼容 |
| 非中断式 | 超时后返回错误消息而非抛出异常，允许 Agent 优雅恢复 |
| 可配置 | 支持全局默认值和实例级别覆盖 |

---

### MultimodalProcessor

**文件：** `multimodal_processor.py`

**类：** `MultimodalProcessor(AgentMiddleware)`

将消息中的 base64 编码图片解码为临时文件，使模型能够消费图片内容；推理后清理过期临时文件。

#### `__init__(session_id: str)`

```python
processor = MultimodalProcessor(session_id="session_001")
```

---

#### `abefore_agent(state, runtime)`

```
输入：可能包含 base64 图片的消息列表
        │
        ▼
1. 遍历所有消息，查找 type="image_url" 的内容块
2. 对于每个图片块：
   ├─ 从 data:image/{fmt};base64,{data} 中解析格式和数据
   ├─ 如果格式是 png/jpeg/webp：
   │  ├─ 用 PIL.Image.open(BytesIO(base64_data)) 验证图片
   │  └─ 写入 SRC_DIR/mutil_temp/{timestamp}.{fmt}
   ├─ 如果格式是 audio/webm/audio/mpeg（TODO 桩）：
   │  └─ 记录路径到 self._audio_paths
   ├─ 如果格式是 video/mp4（TODO 桩）：
   │  └─ 记录路径到 self._video_paths
   └─ 替换原始 content 块为文件路径描述
```

---

#### `aafter_agent(state, runtime)`

```
输入：推理完成后的状态
        │
        ▼
1. 扫描 mutil_temp 目录中的所有文件
2. 删除最后修改时间超过 7 天的文件
3. 日志记录已清理的文件数量
```

**关键细节：**

| 关注点 | 解决方案 |
|--------|----------|
| 临时文件管理 | 统一目录 `SRC_DIR/mutil_temp/`，避免散落各处 |
| TTL 清理 | 7 天过期策略平衡磁盘占用与调试需求 |
| 非关键路径 | 清理失败不影响 Agent 核心逻辑 |
| 格式扩展性 | audio/video 使用 TODO 桩设计，便于后期接入语音转文字/视频转文字管线 |

---

## 对比

| 特性 | ContextEngineHook | Summarization | ToolLoopPrevention | ToolCallNormalize | ToolTimeout | MultimodalProcessor |
|------|-------------------|---------------|---------------------|-------------------|-------------|---------------------|
| **基类** | `AgentMiddleware` | `SummarizationMiddleware` | `AgentMiddleware` | `AgentMiddleware` | `AgentMiddleware` | `AgentMiddleware` |
| **触发时机** | Agent 前后 | 模型调用前 | 每次工具调用（`awrap_tool_call`） | Agent 前 | 每次工具调用（`awrap_tool_call`） | Agent 前后 |
| **核心操作** | 上下文增强 + 持久化 | 压缩 + 偏好提取 | 循环检测 + 阻断 | 消息配对修复 | 超时控制 + 错误返回 | 图片解码 + 临时文件清理 |
| **阻塞性** | 异步非阻塞（after 部分） | 同步阻塞 | 同步 | 同步 | 异步阻塞 | 同步 |
| **依赖** | Context Engine | MesMemory、`memory_store` | 无 | `sanitize_tool_use_result_pairing` | `asyncio.wait_for` | PIL (Pillow) |
| **频率** | 每轮 Agent 推理 | 仅上下文过长时 | 每次工具调用 | 每轮 Agent 推理 | 每次工具调用 | 每轮 Agent 推理 |
| **消息变更** | 原地修改 | 克隆 + 修改副本 | 无（仅返回 error ToolMessage） | 替换整个消息列表 | 无（仅返回 error ToolMessage） | 替换图片内容块 |

---

## 工作流（时序图）

```mermaid
sequenceDiagram
    participant User as 用户
    participant Agent as Agent Runtime
    participant Norm as ToolCallNormalize
    participant Proc as MultimodalProcessor
    participant CEHook as ContextEngineHook
    participant Summ as Summarization
    participant CE as Context Engine
    participant LLM
    participant Loop as ToolLoopPrevention
    participant TO as ToolTimeout

    User->>Agent: 发送消息
    Agent->>Norm: abefore_agent(state, runtime)
    
    rect rgb(245, 240, 255)
        Note over Norm: 阶段 0：预处理
        Norm->>Norm: sanitize_tool_use_result_pairing()
        Norm->>Norm: 移除孤立配对
    end
    
    Agent->>Proc: abefore_agent(state, runtime)
    
    rect rgb(255, 248, 240)
        Note over Proc: 阶段 0.5：图片解码
        Proc->>Proc: 查找 image_url 内容块
        Proc->>Proc: base64 解码 → 写入临时文件
        Proc->>Proc: 替换消息内容为文件路径
    end

    Agent->>CEHook: abefore_agent(state, runtime)
    
    rect rgb(240, 248, 255)
        Note over CEHook: 阶段 1：上下文增强
        CEHook->>CEHook: 过滤 SystemMessages
        CEHook->>CE: retrieve_history_by_last_n_prompt()
        CE-->>CEHook: 最近对话文本
        CEHook->>CE: build_mixed_query(历史, 当前查询)
        CE-->>CEHook: 增强后的查询
        CEHook->>CE: assemble(增强后的查询)
        CE-->>CEHook: system_prompt_addition (XML)
        CEHook->>CEHook: 前置拼接到用户消息
    end
    
    Agent->>Summ: abefore_model(state, runtime)
    
    rect rgb(255, 245, 238)
        Note over Summ: 阶段 2：对话压缩
        Summ->>Summ: 克隆消息，剥离 SystemMessage
        Summ->>Summ: 保留最后一条 HumanMessage
        Summ->>Summ: 调用父类压缩逻辑
        Summ->>Summ: 插回 SystemMessage + 最后 HumanMessage
        Summ->>Summ: memory_store.load_from_disk()
        Summ->>CE: nudge_memory(session_id, nudge_turn=0)
        Summ->>Summ: memory_store.load_from_disk()
    end
    
    Agent->>LLM: 调用模型
    
    rect rgb(240, 255, 240)
        Note over LLM: 阶段 3：LLM 推理（含工具调用循环）
        loop 工具调用循环
            LLM->>Loop: awrap_tool_call(request, handler)
            Loop->>Loop: 自增计数器
            alt 计数 > 阈值
                Loop-->>LLM: ToolMessage(status="error")
            else
                Loop->>TO: 转发 handler
                TO->>TO: asyncio.wait_for(handler, timeout)
                alt 超时
                    TO-->>LLM: ToolMessage(status="error")
                else 成功
                    TO-->>LLM: 正常结果
                end
            end
        end
        LLM-->>Agent: 回复
    end
    
    Agent->>CEHook: aafter_agent(state, runtime)
    
    rect rgb(240, 248, 255)
        Note over CEHook: 阶段 4：后处理
        CEHook->>CEHook: slice_last_turn()
        CEHook->>CEHook: sanitize_tool_use_result_pairing()
        CEHook->>CEHook: 去除增强前缀，还原输入
        par 异步持久化
            CEHook->>CE: after_turn(session_id, messages)
            CEHook->>CE: add_messages(session_id, messages)
        end
    end
    
    Agent->>Proc: aafter_agent(state, runtime)
    
    rect rgb(255, 248, 240)
        Note over Proc: 阶段 5：临时文件清理
        Proc->>Proc: 扫描 mutil_temp 目录
        Proc->>Proc: 删除超过 7 天的文件
    end
    
    Agent->>User: 回复
```

---

## 生命周期

| 阶段 | ToolCallNormalize | MultimodalProcessor | ContextEngineHook | Summarization | ToolLoopPrevention | ToolTimeout |
|------|-------------------|---------------------|-------------------|---------------|---------------------|-------------|
| **Before Agent** | `sanitize_tool_use_result_pairing()` → 修复工具配对，移除孤立消息 | 查找 image_url → base64 解码 → 写入临时文件 → 替换内容块 | 剥离系统消息 → 提取查询 → 构造增强 → 前置拼接 | — | 重置调用计数器 | — |
| **Before Model** | — | — | — | 克隆 → 剥离系统消息 → 压缩 → 插回 → nudge | — | — |
| **LLM 推理（每次工具调用）** | — | — | — | — | 自增计数器 → 超过阈值返回 error | `asyncio.wait_for` → 超时返回 error |
| **After Agent** | — | 扫描 mutil_temp → 删除超 7 天文件 | 提取最后一轮 → 清理工具配对 → 还原输入 → 异步持久化 | — | — | — |

---

## 核心机制

### 1. 基于 AOP 的中间件钩子

两个中间件都使用 LangChain 的 AOP 风格中间件框架。`ContextEngineHook` 继承 `AgentMiddleware` 以挂载到 Agent 生命周期（`abefore_agent` / `aafter_agent`）。`Summarization` 继承 `SummarizationMiddleware` 以挂载到模型生命周期（`abefore_model`）。

这种设计允许横切关注点（记忆、压缩）与核心 Agent 逻辑清晰地分离，无需修改 Agent 本身。

### 2. 三格式消息支持

`ContextEngineHook` 透明地处理三种不同的消息内容格式：

| 格式 | 示例 | 增强策略 |
|------|------|----------|
| `str` | `"如何部署？"` | 字符串拼接 |
| `dict` | `{"type": "text", "text": "你好"}` | 原地修改 `text` 键 |
| `list[dict]` | `[{"type": "text", ...}, {"type": "image_url", ...}]` | 找到文本项，原地增强 |

这确保了对纯文本和多模态工作流的兼容性。

### 3. 增强前缀生命周期

增强前缀在 `abefore_agent` 中注入，在 `aafter_agent` 中剥离：

```
注入（abefore_agent）：
  "[技能上下文 + 指令] 如何部署 Docker？"
                                   ↑ 增强部分
剥离（aafter_agent）：
  user_text.removeprefix(self._turn_prompt)
  → "如何部署 Docker？"   ← 还原原始
```

这防止了增强前缀在各轮之间积累到 MesMemory 中，否则会迅速消耗上下文窗口。

### 4. 压缩时强制 Nudge

Summarization 通过 `nudge_turn=0` 在压缩时强制进行偏好提取。这是一个刻意的权衡：

- **不强制**：嵌入在旧对话轮次中的偏好会在这些轮次被压缩为摘要时丢失
- **强制**：潜在偏好（如"我喜欢简洁的回答"）会在原始消息被摘要替代之前被提取并持久化

### 5. 异步非阻塞后处理

`ContextEngineHook.aafter_agent` 将 `after_turn()` 和 `add_messages()` 作为并发的 `asyncio.create_task` 调用启动，通过 `asyncio.gather` 聚合。这确保了：

- Agent 的响应延迟不受持久化或知识提取的影响
- 两个任务并发运行（提取和持久化并行）
- 如果任一任务失败，异常通过 `asyncio.gather` 传播（不会静默吞掉）

### 6. 工具调用包装器模式

`ToolLoopPrevention` 和 `ToolTimeout` 都使用了相同的设计模式 — `awrap_tool_call` 包装器。LangGraph Runtime 会在每次工具调用时调用 `awrap_tool_call`，传入原始请求和一个 `handler` 函数（代表下一个中间件或实际的工具执行器）：

```
Agent Runtime
    │
    ▼
awrap_tool_call(request, handler)  ← ToolLoopPrevention
    │
    ▼
awrap_tool_call(request, handler)  ← ToolTimeout
    │
    ▼
实际工具执行
```

这种链式模式允许中间件在不修改工具代码的情况下，透明地添加横切关注点（循环检测、超时控制）。

### 7. 分段式前处理编排

`abefore_agent` 阶段不再由单一中间件独占。三个中间件按分工依次执行：

| 顺序 | 中间件 | 职责 |
|------|--------|------|
| 1 | `ToolCallNormalize` | 修复工具配对，确保消息列表一致 |
| 2 | `MultimodalProcessor` | 解码图片为临时文件 |
| 3 | `ContextEngineHook` | 检索技能记忆，构造增强 prompt |

这种分段设计保持了单一职责原则 — 每个中间件只做一件事，且彼此解耦。

### 8. 工具安全双层防护

ToolLoopPrevention 和 ToolTimeout 构成了工具调用的双层安全防护：

```
工具调用抵达
    │
    ▼
┌─ ToolLoopPrevention ──┐
│ 同一工具已调用 N 次？  │──超过阈值──→ 返回错误 ToolMessage
│ 未超阈值，放行          │
└────────┬──────────────┘
         ▼
┌─ ToolTimeout ──────────┐
│ handler 在超时内完成？  │──超时──→ 返回错误 ToolMessage
│ 成功返回结果            │
└────────┬──────────────┘
         ▼
    正常结果
```

---

## 数据模型

### 状态消息类型

```python
from langchain_core.messages import BaseMessage, SystemMessage, HumanMessage, AIMessage, RemoveMessage
```

| 类型 | 在中间件中的角色 |
|------|------------------|
| `SystemMessage` | 在增强前（ContextEngineHook）和压缩前（Summarization）被剥离，防止污染 |
| `HumanMessage` | 作为增强的用户查询来源；在压缩期间保留最后一条 |
| `AIMessage` | 在 `aafter_agent` 中提取的 AI 回复来源 |
| `RemoveMessage` | 由父类 `SummarizationMiddleware` 插入的标记，用于标记要移除的消息 |

### 上下文增强状态

```
self._turn_prompt: str
  └─ 在 abefore_agent 期间构建的增强前缀
  └─ 格式：[skill_memory_context] + instruction_text
  └─ 使用：abefore_agent（前置拼接）→ aafter_agent（removeprefix）
```

### Memory Store 状态

`memory_store` 是由 `Summarization` 中间件管理的单例模块级对象（`from tools import memory_store`）：

- **类型**：内存缓存，底层由磁盘上的 markdown 文件支持
- **读取**：`memory_store.load_from_disk()` — 将内存状态与磁盘同步
- **写入**：`nudge_memory()` — 将提取的偏好写入 markdown 文件
- **一致性**：在 nudge 前后各加载一次，防止读取过期数据

### 工具调用计数器

```python
self._tool_call_counts: dict[str, int] = {}
```

由 `ToolLoopPrevention` 管理，每轮在 `abefore_agent` 中重置。

### 多模态临时文件

```
SRC_DIR/mutil_temp/{timestamp}.{fmt}
├─ {timestamp} = datetime.now().strftime("%Y%m%d%H%M%S%f")
└─ {fmt}      = png / jpeg / webp（当前支持）
```

由 `MultimodalProcessor` 管理，推理后通过 7 天 TTL 策略清理。

---

## 配置

| 配置项 | ContextEngineHook | Summarization | ToolLoopPrevention | ToolCallNormalize | ToolTimeout | MultimodalProcessor |
|--------|-------------------|---------------|---------------------|-------------------|-------------|---------------------|
| **会话 ID** | `session_id`（构造函数） | `session_id`（构造函数） | `session_id`（构造函数） | `session_id`（构造函数） | `session_id`（构造函数） | `session_id`（构造函数） |
| **主要参数** | — | 通过 `**kwargs` 转发给父类 | `threshold=int`（默认 5） | — | `timeout_seconds=float` | — |
| **环境变量** | — | — | — | — | `TOOL_CALL_TIMEOUT_MINUTES` | — |
| **临时目录** | — | — | — | — | — | `SRC_DIR/mutil_temp/` |
| **TTL** | — | — | — | — | — | 7 天 |
| **历史轮次** | 委托给 `retrieve_history_by_last_n_prompt()`（默认 5 轮） | — | — | — | — | — |
| **强制 Nudge** | — | `nudge_turn=0`（始终强制提取） | — | — | — | — |
| **消息格式** | `str`、`dict`、`list[dict]` | `list[BaseMessage]` | — | `list[BaseMessage]` | — | `list[dict]`（含 `image_url`） |

---

## 使用示例

### 注册中间件

```python
from agent.middlewares import (
    ContextEngineHook,
    Summarization,
    ToolLoopPrevention,
    ToolCallNormalize,
    ToolTimeout,
    MultimodalProcessor,
)

# 创建中间件实例
context_hook = ContextEngineHook(session_id="session_001")
summarizer = Summarization(session_id="session_001")
loop_preventer = ToolLoopPrevention(session_id="session_001", threshold=5)
normalizer = ToolCallNormalize(session_id="session_001")
timeout_mw = ToolTimeout(session_id="session_001", timeout_seconds=30.0)
processor = MultimodalProcessor(session_id="session_001")

# 注册到 LangGraph Runtime
# abefore_agent 执行顺序：ToolCallNormalize → MultimodalProcessor → ContextEngineHook
# awrap_tool_call 执行顺序：ToolLoopPrevention → ToolTimeout → 实际工具
# aafter_agent 执行顺序：ContextEngineHook → MultimodalProcessor
runtime = Runtime(
    agent=my_agent,
    middlewares=[normalizer, processor, context_hook, summarizer, loop_preventer, timeout_mw]
)
```

### 独立使用 ContextEngineHook

```python
from agent.middlewares import ContextEngineHook

hook = ContextEngineHook(session_id="session_001")

# 通常由 LangGraph Runtime 调用，但也可直接调用以进行测试：
await hook.abefore_agent(state, runtime)
# → state["messages"][-1].content 现在已被增强

# ... LLM 推理之后 ...
await hook.aafter_agent(state, runtime)
# → 对话持久化到 MesMemory，Skill Memory 更新
```

### 独立使用 Summarization

```python
from agent.middlewares import Summarization
from langgraph.runtime import Runtime

summarizer = Summarization(
    session_id="session_001",
    # 额外的 SummarizationMiddleware 关键字参数放在这里
)

# 由 LangGraph Runtime 在模型推理前调用：
await summarizer.abefore_model(state, runtime)
# → 长上下文被压缩，偏好被提取
```

### 独立使用 ToolLoopPrevention

```python
from agent.middlewares import ToolLoopPrevention

preventer = ToolLoopPrevention(session_id="session_001", threshold=5)

# 每轮开始时重置计数器：
await preventer.abefore_agent(state, runtime)

# 包装工具调用：
result = await preventer.awrap_tool_call(request, handler)
# → 如果同一工具在本轮已调用超过5次，返回 error ToolMessage
```

### 独立使用 ToolTimeout

```python
from agent.middlewares import ToolTimeout

timeout_mw = ToolTimeout(session_id="session_001", timeout_seconds=30.0)

# 包装工具调用（带超时）：
result = await timeout_mw.awrap_tool_call(request, handler)
# → 如果工具执行超过30秒，返回 error ToolMessage 附带 status="error"
```

### 独立使用 MultimodalProcessor

```python
from agent.middlewares import MultimodalProcessor

processor = MultimodalProcessor(session_id="session_001")

# 推理前解码图片：
await processor.abefore_agent(state, runtime)
# → base64 图片解码到 SRC_DIR/mutil_temp/{timestamp}.png
# → image_url 块从历史消息中被替换

# ... LLM 推理之后 ...
await processor.aafter_agent(state, runtime)
# → 清理超过7天的临时文件
```

---

## FAQ

### Q1: ContextEngineHook 为什么要过滤 SystemMessage？

在 `abefore_agent` 中过滤 SystemMessage，是为了防止系统提示（角色设定、工具定义等）被作为查询上下文传给 Context Engine，从而确保技能记忆和长期记忆的召回准确性。`system_prompt_addition` 通过增强前缀独立返回。

### Q2: Summarization 为什么要在压缩时强制提取偏好？

压缩意味着上下文窗口正在缩小，旧的对话历史将被摘要替代。如果不在此时提取偏好，隐含在旧轮次中的细节（如用户明确陈述的偏好）将永久丢失。强制提取确保即使在原始对话被摘要化之后，偏好仍能持久化到长期 memory store 中。

### Q3: 在 `aafter_agent` 中使用 `asyncio.create_task` 有什么风险？

`after_turn` 和 `add_messages` 通过 `asyncio.create_task` 异步运行，并通过 `asyncio.gather` 聚合。与原始的 `create_task`（可能静默吞掉异常）不同，`gather` 会传播异常。但：
- 如果 Agent 进程在 `create_task` 和 `gather` 之间异常退出，未完成的任务仍可能丢失
- `gather` 确保两个任务在 `aafter_agent` 完成前执行完毕 — 因此异常处理是有保障的
- 这是一个可接受的权衡：后处理可靠性受限于异步事件循环的生命周期

### Q4: 中间件的执行顺序如何保证？

执行顺序由 LangGraph Runtime 内部的中间件链控制。顺序为：
1. `abefore_agent` → `abefore_model` → LLM → `aafter_agent`
2. 同一阶段的多个中间件按注册先后顺序执行

### Q5: 如果 `_build_turn_prompt` 失败会怎样？

如果 `_build_turn_prompt` 抛出异常（例如 Context Engine 不可用），`abefore_agent` 会将错误向上传播到 LangGraph Runtime。中间件框架默认不捕获异常 — 如果增强是关键逻辑，调用方应在运行时层面处理错误。

### Q6: Summarization 为什么要克隆消息列表？

Summarization 中间件在处理前克隆消息列表，以避免对原始 `state["messages"]` 产生副作用。这是因为：
- 父类 `SummarizationMiddleware.abefore_model` 需要一个它可以自由修改的可变副本
- 在 Runtime 正式应用中间件结果之前，原始状态不应被触碰
- 克隆可以防止下游处理器看到部分修改后的状态

### Q7: 多媒体内容在增强时如何处理？

对于 `list[dict]`（多媒体）消息，仅增强 `type="text"` 部分。图片和其他媒体项保持不变。增强后的内容会原地写回到同一文本项中，保持原始消息结构不变。

### Q8: 什么时候应该使用 `ToolLoopPrevention` vs `ToolTimeout`？

它们解决不同的问题：
- **ToolLoopPrevention** 防护的是*调用序列失控* — 模型在单轮内反复调用同一个工具（例如连续调用 `web_search` 50 次翻页）。它限制的是调用*次数*。
- **ToolTimeout** 防护的是*单次调用挂起* — 某个工具永不返回（例如网络搜索在慢端点上挂起）。它限制的是每次调用的*时长*。

两者互补：通常应同时启用。

### Q9: 为什么 `ToolCallNormalize` 要移除所有消息，而不是只修复异常的配对？

因为检测异常配对本身就很脆弱 — 简单的启发式判断可能判断一组看似"正常"的配对实则在语义上已损坏（例如工具结果在 Agent 已经开始生成新回复后才追加）。通过 `sanitize_tool_use_result_pairing` 从零开始重建，标准化器能保证全局一致的消息列表。由于这仅在内存中的消息列表上操作，性能开销可以忽略。

### Q10: `MultimodalProcessor` 遇到不支持的媒体类型会怎样？

音频和视频类型在代码中有 `TODO` 桩 — 处理器会识别这些类型并将路径收集到内部追踪变量中，但暂时不会解码。文本内容仍然保留，图片正常处理。这些桩是为后续接入语音转文字和视频转文字管线预留的位置。

---

## 技术栈

| 组件 | 技术选型 |
|------|----------|
| **中间件框架** | LangChain `AgentMiddleware` / `SummarizationMiddleware` |
| **Agent 运行时** | LangGraph `Runtime` |
| **消息模型** | LangChain `BaseMessage` / `SystemMessage` / `HumanMessage` / `AIMessage` / `RemoveMessage` |
| **记忆系统** | Context Engine（Skill Memory Graph + MesMemory） |
| **存储（MesMemory）** | SQLite + FTS5 |
| **存储（Memory Store）** | 磁盘 markdown 文件（`.md`），加载到内存单例 |
| **异步框架** | `asyncio.create_task` + `asyncio.gather` |
| **工具函数** | `textwrap.dedent`（增强 prompt 格式化） |
| **图片处理** | PIL (Pillow) — base64 解码 + 文件写入 |
| **工具调用安全** | 自定义 `awrap_tool_call` 包装（限流 + 超时） |
| **消息标准化** | `sanitize_tool_use_result_pairing`（全量列表重写） |
| **配置** | `.env`（`TOOL_CALL_TIMEOUT_MINUTES`）、构造函数参数 |
| **临时文件管理** | `SRC_DIR/mutil_temp/`，7 天 TTL 清理 |

---

## 许可证

本项目遵循 EMA AI Agent 的 MIT 开源协议。

---

**作者：** MOYE  
**最后更新：** 2026-06-02
