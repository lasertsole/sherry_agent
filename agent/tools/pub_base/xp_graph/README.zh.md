# XpGraph — AI Agent 经验图谱

[**English**](README.md) | **中文文档**

> **XpGraph**（Experience Graph）是 AI Agent 的核心知识引擎，通过蒸馏式提取将任务执行中的高信噪比经验沉淀到结构化知识图谱，实现跨会话的知识复用，token 开销极低。

---

## 目录

- [概述](#概述)
- [架构](#架构)
- [数据流](#数据流)
- [核心概念](#核心概念)
- [知识注入](#知识注入)
- [经验蒸馏管线](#经验蒸馏管线)
- [多角色知识库](#多角色知识库)
- [数据模型](#数据模型)
- [召回机制](#召回机制)
- [图谱维护](#图谱维护)
- [使用示例](#使用示例)
- [技术栈](#技术栈)

---

## 概述

### 设计理念

XpGraph 是**蒸馏优先**的知识图谱系统。与传统 RAG 从原始对话消息中提取不同，XpGraph 只存储**预蒸馏的经验对象**——通过专门的 LLM 调用提取的高信噪比、可复用知识。

| 传统 RAG | XpGraph |
|----------|---------|
| 摄取原始消息 → 事后提取 | 先蒸馏经验 → 直写图谱 |
| 信噪比低 | 信噪比高 |
| 扁平向量检索 | 结构化图谱 + 多跳推理 |
| 单一知识库 | 角色分离的知识库（策略级 / 操作级） |
| 静态知识库 | 动态演化、自动合并、社区检测 |

### 核心能力

1. **蒸馏式提取** — 三层提取：草稿工具 → 压缩前 fork → 任务结束蒸馏
2. **多角色知识库** — Commander 与主 agent 共享策略级知识库；Worker 独立使用操作级知识库
3. **知识注入** — 召回经验以 `AIMessage(content="<thinking>...")` 形式注入到第一个 HumanMessage 之后
4. **图谱社区检测** — Leiden 算法自动聚类相关知识领域
5. **个性化 PageRank** — 基于查询上下文的动态节点排序
6. **混合检索** — 向量相似度 + FTS5 全文搜索 + 图遍历

---

## 架构

```
┌──────────────────────────────────────────────────────────────────┐
│                       XpGraph Core                               │
├──────────────┬──────────────┬──────────────┬───────────────────┤
│  Distiller   │   Recaller   │    Graph     │      Store        │
├──────────────┼──────────────┼──────────────┼───────────────────┤
│ • 压缩前fork │ • 双路召回   │ • 社区检测   │ • SQLite（按角色  │
│ • 任务结束   │ • PPR 排序   │ • PageRank   │ • FTS5            │
│ • 策略级/    │ • 重排序    │ • 去重合并   │ • 向量存储        │
│   操作级     │              │              │                   │
└──────────────┴──────────────┴──────────────┴───────────────────┘
                      ↕ 草稿（由 subagent 拥有）
```

### 模块职责

| 模块 | 文件路径 | 核心功能 |
|------|---------|----------|
| **Distiller** | `extractor/distiller.py` | 任务结束后蒸馏策略级/操作级经验 |
| **Extractor** | `extractor/core.py` | 从预蒸馏输入中提取节点/边；会话结束终审 |
| **Recaller** | `recaller/core.py` | 执行双路召回（精确 + 泛化）；合并结果 |
| **Graph** | `graph/*.py` | 社区检测、PageRank 计算、去重合并、图谱维护 |
| **Store** | `store/core.py` | SQLite CRUD、向量存储、FTS5 搜索 |
| **Core** | `core.py` | `XpGraphInstance` 工厂；编排各模块 |

---

## 数据流

```
 ┌─────────────────────────────────────────────────────────────────┐
 │                     Commander 执行                              │
 │                                                                 │
 │  1. 知识注入                                                    │
 │     任务描述 → assemble() → AIMessage<thinking>                │
 │                                                                 │
 │  2. 执行过程中                                                  │
 │     Agent 调用 draft tool → 洞察存入 state_register            │
 │                                                                 │
 │  3. 压缩前蒸馏                                                  │
 │     SummarizationMiddleware 触发 → fork 提取即将被丢弃消息      │
 │     中的洞察 → 追加为草稿                                      │
 └────────────────────────┬────────────────────────────────────────┘
                          │
          ┌───────────────┴───────────────┐
          ▼                               ▼
 ┌─────────────────┐           ┌─────────────────────┐
 │  Worker 任务 A   │           │   Worker 任务 B      │
 │                  │           │                      │
 │  相同三步骤：    │           │  相同三步骤：        │
 │  注入 →         │           │  注入 →              │
 │  草稿 →         │           │  草稿 →              │
 │  压缩前蒸馏     │           │  压缩前蒸馏          │
 └────────┬────────┘           └──────────┬──────────┘
          │                               │
          └───────────────┬───────────────┘
                          ▼
 ┌─────────────────────────────────────────────────────────────────┐
 │                    任务结束蒸馏                                  │
 │                                                                 │
 │  1. 收集：任务描述 + 最终结果 + 所有草稿                        │
 │  2. 蒸馏策略级经验 → default DB（commander 共享）               │
 │  3. 蒸馏操作级经验 → worker DB（独立）                          │
 └─────────────────────────────────────────────────────────────────┘
```

---

## 核心概念

### 经验节点

| 类型 | 说明 | 命名惯例 | 示例 |
|------|------|---------|------|
| **TASK** | 用户请求的任务或主题 | `动词-宾语` | `deploy-bilibili-mcp` |
| **SKILL** | 可复用的策略或操作 | `工具-操作` | `conda-env-create` |
| **EVENT** | 一次性错误或陷阱 | `现象-工具` | `importerror-libgl1` |

### 经验边

| 类型 | 方向约束 | 含义 | `instruction` 内容 |
|------|---------|------|-------------------|
| **USED_SKILL** | TASK → SKILL | 任务使用了该技能 | 哪一步、如何调用 |
| **SOLVED_BY** | EVENT → SKILL | 错误由此技能解决 | 执行的具体命令/操作 |
| **REQUIRES** | SKILL → SKILL | 前置依赖 | 为何依赖、如何判断 |
| **PATCHES** | SKILL → SKILL | 新技能修正旧技能 | 旧方案的问题、新修复 |
| **CONFLICTS_WITH** | SKILL ↔ SKILL | 互斥关系 | 冲突表现、选择哪个 |

---

## 知识注入

当 Commander 或 Worker 启动任务时，会从对应角色的知识库中召回相关经验，以 `AIMessage` 的 `<thinking>` 内容注入到消息流中，紧跟在第一个 `HumanMessage` 之后。

**Commander（策略级，从 default DB）：**

```python
messages = [
    HumanMessage(content="部署 Python Web 应用到 Kubernetes"),
    AIMessage(content="<thinking>\n<xp_graph>...策略知识 XML...</xp_graph>\n</thinking>")
]
```

**Worker（操作级，从 worker DB）：**

```python
messages = [
    HumanMessage(content="在 conda 环境中安装 Python 依赖"),
    AIMessage(content="<thinking>\n<xp_graph>...操作知识 XML...</xp_graph>\n</thinking>")
]
```

这确保了 agent 能获取相关的历史经验，而不会污染系统提示词或消耗上下文窗口。

---

## 经验蒸馏管线

XpGraph 采用**三层蒸馏**方式，而非摄取原始对话消息：

### 第一层：草稿工具（主动）

`draft` 工具对所有 agent 可用。当 agent 发现值得记录的内容时调用：

```python
draft(insight="配置文件必须在 init() 之前加载，否则会静默使用默认空值")
```

草稿存储在 `state_register` 中，每个会话上限 10 条。

### 第二层：压缩前 Fork（自动）

当 `SummarizationMiddleware` 触发消息压缩时，`CommanderDistillSummarization` 或 `WorkerDistillSummarization` 中间件拦截即将被丢弃的消息，将其发送给 `auxiliary_llm` 进行蒸馏提示，提取的洞察追加为草稿。

- Commander：**策略级**蒸馏提示（任务拆分策略、并行模式、依赖陷阱）
- Worker：**操作级**蒸馏提示（工具使用模式、API 陷阱、错误规避方法）

### 第三层：任务结束蒸馏（后处理）

子 agent 任务完成后，在 `finally` 块中触发 `distill_and_ingest()`：

1. 收集原始任务、最终结果和所有累积的草稿
2. 使用角色特定的提示词调用 `auxiliary_llm.with_structured_output(DistillResult)`
3. 生成结构化的 `DistillNode` 和 `DistillEdge` 对象
4. 策略级经验写入 default DB，操作级经验写入 worker DB

---

## 多角色知识库

XpGraph 为不同角色维护独立的 SQLite 数据库：

| 角色 | DB 路径 | 知识层级 | 共享范围 |
|------|---------|---------|---------|
| `default` | `store/xp_graph/xp_graph.db` | 策略级（任务拆分、调度、并行度） | 主 agent + Commander |
| `worker` | `store/xp_graph/worker/xp_graph.db` | 操作级（工具用法、API 模式、错误修复） | 仅 Worker |

### XpGraphInstance 工厂

```python
from agent.tools.xp_graph.core import get_instance

commander_memory = get_instance("default")
worker_memory = get_instance("worker")

# 每个实例有独立的 db、recaller、extractor、config
await commander_memory.ingest_experiences(session_id, experiences)
await worker_memory.assemble(task_description)
```

---

## 数据模型

### 节点（gm_nodes）

```python
class GmNode(BaseModel):
    id: str                      # "n-{timestamp}-{random}"
    type: Literal["TASK", "SKILL", "EVENT"]
    name: str                    # 标准化名称（小写、连字符分隔）
    description: str             # 一行摘要
    content: str                 # 详细的可复用知识
    validated_count: int = 1     # 重复出现时累积
    source_sessions: List[str]   # 出现过的会话 ID
    community_id: Optional[str]  # 社区聚类 ID
    pagerank: float = 0          # 全局 PageRank 分数
    created_at: int
    updated_at: int
```

### 边（gm_edges）

```python
class GmEdge(BaseModel):
    id: str                      # "e-{timestamp}-{random}"
    from_id: str
    to_id: str
    type: str                    # USED_SKILL / SOLVED_BY / REQUIRES / PATCHES / CONFLICTS_WITH
    instruction: str             # 如何/何时使用此关系
    condition: Optional[str]     # 触发条件（SOLVED_BY 必需）
    session_id: str
    created_at: int
```

### 配置（GmConfig）

```python
class GmConfig(BaseModel):
    db_path: str = "xp_graph.db"
    compact_turn_count: int = 7       # 社区维护间隔
    recall_max_nodes: int = 6         # 最大召回节点数
    recall_max_depth: int = 2         # 最大图遍历深度
    fresh_tail_count: int = 10        # 新鲜尾部节点数
    dedup_threshold: float = 0.90     # 去重相似度阈值
    pagerank_damping: float = 0.85    # PageRank 阻尼因子
    pagerank_iterations: int = 20     # PageRank 迭代次数
    embedding: Embeddings             # 嵌入模型
    llm: BaseChatModel                # LLM 模型
```

---

## 召回机制

### 双路召回

```
用户查询
  ├─ 精确路径
  │   ├─ 向量搜索 / FTS5 → 种子节点
  │   ├─ 社区扩展
  │   ├─ 图遍历（BFS 最大深度=2）
  │   └─ PPR 排序
  │
  └─ 泛化路径
      ├─ 社区向量搜索 → 匹配社区
      ├─ 获取社区代表节点
      ├─ 图遍历（BFS 最大深度=1）
      └─ PPR 排序

  ↓ 合并 & 去重
最终结果（节点 + 边）→ 格式化为 XML 上下文
```

### 经验 → 技能自动升级

当 EVENT 节点的 `validated_count` 达到阈值（默认 3），会话结束终审器评估是否将其升级为 SKILL 节点。这在 `rectification_and_standardization()` 中自动完成。

---

## 图谱维护

### 定期维护

每 N 轮触发（通过 `compact_turn_count` 配置）：

1. 社区检测（Leiden 算法）
2. 社区摘要生成（LLM + 嵌入向量）
3. 缓存失效

### 会话结束维护

通过 `rectification_and_standardization()` 触发：

1. 终审（EVENT → SKILL 升级、补边、标记无效节点）
2. 全局 PageRank 更新
3. 节点去重与合并

---

## 使用示例

### 获取知识实例

```python
from agent.tools.xp_graph.core import get_instance

# Commander/主 agent（策略级）
memory = get_instance("default")

# Worker（操作级）
memory = get_instance("worker")
```

### 召回并注入知识

```python
result = await memory.assemble(
    user_text="如何用 Docker 部署应用？",
    messages=conversation_history
)

if "system_prompt_addition" in result:
    knowledge_xml = result["system_prompt_addition"]
    # 以 AIMessage<thinking> 注入到第一个 HumanMessage 之后
```

### 写入预蒸馏经验

```python
from agent.tools.xp_graph.extractor.distiller import distill_and_ingest

await distill_and_ingest(
    task="部署 Python 应用到 Kubernetes",
    result="使用 helm chart 成功部署",
    session_id="session_001",
    commander_session_id="commander-session_001",
)
```

### 记录草稿洞察

```python
# Agent 执行过程中作为工具调用
draft(insight="Docker build cache 在 requirements.txt 变更时必须清除")
```

### 查询统计信息

```python
from agent.tools.xp_graph.store import get_db, all_active_nodes, all_edges

db = get_db()  # default 角色
nodes = all_active_nodes(db)
edges = all_edges(db)

for node in nodes:
    print(f"[{node.type}] {node.name}: {node.description}")
```

---

## 技术栈

| 组件 | 技术 |
|------|------|
| **数据库** | SQLite 3 + FTS5（按角色隔离） |
| **向量存储** | SQLite BLOB 字段 |
| **图算法** | igraph + Leiden 算法 |
| **PageRank** | 自定义实现（Python） |
| **嵌入模型** | BGE/BAAI 系列 |
| **LLM** | auxiliary_llm（蒸馏）、main_llm（agent） |
| **异步框架** | asyncio |
