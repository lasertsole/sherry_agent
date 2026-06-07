# AGoT (Augmented Graph of Thoughts) — 8 阶段推理引擎

[English](README.md) | **中文**

---

## 概述

AGoT（Augmented Graph of Thoughts）是一个基于图结构的多阶段推理框架。它将复杂问题分解为 8 个串联阶段，逐步构建知识图谱，产生结构化的分析结果。

AGoT 将思考过程表示为**有向图**：节点代表不同思考单元（维度、假设、证据），边代表逻辑关系，**超边**（Hyperedge）和多学科桥接（IBN）支持跨学科交叉分析。图结构天然支持剪枝、合并、置信度传播，最终提炼出经过验证的核心结论。

---

## 与 CoT / ToT / GoT 的区别

| 特性 | CoT | ToT | GoT | **AGoT** |
|------|-----|-----|-----|----------|
| **数据结构** | 线性链 | 树 | 有向无环图 | **有向图 + 超边** |
| **分支策略** | 无 | 广度优先搜索 | 任意拓扑 | **分阶段、多维度分解** |
| **回溯机制** | 无 | 剪枝/评分回溯 | 图聚合 | **贝叶斯置信度传播 + 剪枝** |
| **跨学科交叉** | 无 | 无 | 无 | **IBN 跨学科桥接节点** |
| **置信度量化** | 无 | 无 | 有限的评分 | **4 维置信度向量**（经验/理论/方法/共识） |
| **断点续跑** | 无 | 无 | 无 | **内置 Checkpoint 管理** |
| **自我反思** | 有限 | 有限 | 无 | **Stage 8 专有反思阶段**（8 项质量自检） |
| **输出形式** | 文本 | 文本+路径 | 图聚合 | **结构化分析报告 + 超精简思维字符串** |

### 详细对比

#### CoT（Chain-of-Thought, 思维链）
CoT 将推理展开为线性的步骤链，每一步依赖于前一步的输出。**优点**是简单直接；**缺点**是无法探索分支路径、无法回溯错误、无法量化不确定性。

AGoT 的改进：用图替代链，每个维度是独立分支，置信度传播让错误可追溯、可修正。

#### ToT（Tree of Thoughts, 思维树）
ToT 引入树形分支和 BFS/DFS 搜索，可探索多条推理路径并剪枝。**优点**是比 CoT 更灵活；**缺点**是树结构限制了跨分支信息共享，且评分机制单一。

AGoT 的改进：用超边（Hyperedge）连接不同维度的相关节点，实现跨分支信息融合；4 维置信度向量比单维度评分更精细。

#### GoT（Graph of Thoughts, 思维图）
GoT 将推理表示为有向图，支持任意拓扑的思维聚合。**优点**是表达能力最强；**缺点**是缺乏结构化引导——拓扑完全自由发散，结果难以复现，且缺少跨学科交叉和置信度量化。

AGoT 的改进：
- **阶段化**：8 个固定阶段提供结构化引导，保证结果可复现
- **多学科注入**：Stage 1 自动提取学科标签，Stage 4 通过 IBN 节点建立跨学科桥接
- **贝叶斯置信度传播**：每个节点携带 4 维置信度，支持精确的信息可靠性评估
- **反思审计**：Stage 8 独立检查偏差、争议、缺失引用等 8 项质量指标

---

## 项目结构

```
tests/ast_got/
├── agot_processor.py         # 主处理器 — 编排 8 个阶段的执行
├── checkpoint_manager.py     # 断点续跑管理器 — 每阶段完成后存档
├── models/
│   ├── node.py               # 节点模型（node_id, label, type, confidence）
│   ├── edge.py               # 边模型（source, target, edge_type）
│   ├── hyperedge.py          # 超边模型（关联多个维度/假设/证据）
│   └── graph.py              # AGoTGraph — NetworkX DiGraph 封装
├── stages/
│   ├── stage_1_initialization.py   # 初始化：创建根节点，提取学科标签
│   ├── stage_2_decomposition.py    # 分解：将任务拆解为多个分析维度
│   ├── stage_3_hypothesis.py       # 假设：为每个维度生成可验证假设
│   ├── stage_4_evidence.py         # 证据：搜索/整合证据，贝叶斯更新置信度
│   ├── stage_5_pruning.py          # 剪枝：删除低置信节点，合并语义重叠
│   ├── stage_6_subgraph.py         # 子图：提取聚焦子图
│   ├── stage_7_composition.py      # 合成：生成最终分析输出
│   └── stage_8_reflection.py       # 反思：自我审计与质量评估
├── utils/
│   ├── visualization.py      # 图形可视化（matplotlib）
│   ├── metadata_utils.py     # 语义重叠计算、偏差检测等
│   └── math_utils.py         # 贝叶斯更新、熵、KL 散度
├── test_thinking_quick.py    # 快速测试 — mock 数据验证结果提取
├── test_thinking_result.py   # 完整测试 — 运行全部 8 阶段
├── AGOT_STAGES.md            # 8 阶段算法详细文档
├── USAGE_THINKING_RESULT.md  # 思维结果提取使用指南
└── OPTIMIZATION_COMPARISON.md# 结果精简优化对比说明
```

---

## 8 阶段流程

```
Query → Stage 1 → Stage 2 → Stage 3 → Stage 4 → Stage 5 → Stage 6 → Stage 7 → Stage 8
                ↓           ↓          ↓          ↓          ↓           ↓
              Root      Dimensions  Hypotheses  Evidence   Pruned     Subgraphs
                                          ↓       Graph      Graph       ↓
                                     Updated                                    Final
                                     Confidence                                  Output
```

| 阶段 | 名称 | 说明 |
|------|------|------|
| 1 | 初始化 | 创建根节点，理解问题，提取学科标签 |
| 2 | 分解 | 将问题拆解为多个分析维度（默认 7 个，支持 AI 生成） |
| 3 | 假设 | 为每个维度生成可验证的假设 |
| 4 | 证据 | 执行评估计划，整合证据，贝叶斯更新置信度 |
| 5 | 剪枝 | 删除低置信/低影响节点，合并语义重叠节点 |
| 6 | 子图 | 基于置信度/影响/学科等标准提取聚焦子图 |
| 7 | 合成 | 将子图分析合成为结构化输出 |
| 8 | 反思 | 8 项质量自检，输出四维置信度 |

详细阶段说明请参阅 [AGOT_STAGES.md](./AGOT_STAGES.md)。

---

## 使用方式

### 基本使用

```python
from tests.ast_got.agot_processor import AGoTProcessor

processor = AGoTProcessor()
result = processor.process_query("你的问题")

# 提取思考结果字符串（超精简格式）
thinking = processor.extract_thinking_result(result)
print(thinking)
# → "This analysis explores... [Confidence: 74%]"
```

### 断点续跑

AGoT 内置断点管理器：每完成一个阶段自动存档。如果执行中断，下次相同 query 会从断点处继续，无需重跑。

```python
result = processor.process_query("你的问题")

# 如果中断，再次执行会自动从断点恢复
# 全部完成后存档自动清除
```

### 传递参数

```python
result = processor.process_query(
    query="你的问题",
    context={"user_info": "..."},          # 额外上下文
    parameters={"output_dir": "my_output"} # 执行参数
)
```

---

## 核心概念

### 节点类型

| 类型 | 说明 | 产生阶段 |
|------|------|----------|
| `root` | 问题理解根节点 | Stage 1 |
| `dimension` | 分析维度 | Stage 2 |
| `hypothesis` | 研究假设 | Stage 3 |
| `evidence` | 支持证据 | Stage 4 |
| `interdisciplinary_bridge` | 跨学科桥接（IBN） | Stage 4 |

### 边类型

| 类型 | 说明 |
|------|------|
| `decomposition` | 分解关系（root → dimension） |
| `hypothesis` | 假设关系（dimension → hypothesis） |
| `supportive` / `correlative` / `causal` / `temporal` | 证据关系 |
| `hyperedge_virtual` | 超边虚拟连接 |
| `ibn_source` / `ibn_target` | 跨学科桥接 |

### 置信度

置信度是一个 4 维向量 `[empirical, theoretical, methodological, consensus]`，范围 `[0, 1]`，分别衡量经验证据、理论支撑、方法论严谨度和共识程度。

---

## 可视化

每阶段执行后自动生成可视化图片到 `output/` 目录：

- `stage_StageX_XXXStage.png` — 节点与边关系图
- `layers_StageX_XXXStage.png` — 分层结构图

### 节点颜色

| 颜色 | 节点类型 |
|------|----------|
| 🔴 红色 | root |
| 🔵 青色 | dimension |
| 🔷 蓝色 | hypothesis |
| 🟢 绿色 | evidence |
| 🟣 紫色 | interdisciplinary_bridge |

### 边颜色

| 颜色 | 边类型 |
|------|--------|
| 🟢 绿色 | 支持 |
| 🔴 红色 | 矛盾 |
| 🟠 橙色 | 超边虚拟 |
| 🟣 紫色 | 跨学科桥接 |

---

## 思维结果精简

`extract_thinking_result()` 方法将复杂的字典输出转换为**超精简字符串**（仅保留核心结论 + 置信度），适合直接作为 AI 模型的思维结果使用。

---

## 相关文档

| 文档 | 说明 |
|------|------|
| [AGOT_STAGES.md](./AGOT_STAGES.md) | 8 阶段算法详细文档（英文） |
| [OPTIMIZATION_COMPARISON.md](./OPTIMIZATION_COMPARISON.md) | 结果精简优化对比说明 |


## 来源参考
Adaptive Graph of Thoughts
https://github.com/SaptaDey/Adaptive-Graph-of-Thoughts-MCP-server