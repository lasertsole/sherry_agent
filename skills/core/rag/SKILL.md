---
name: rag_multi_hop_graph
description: 多跳图检索系统（Multi-hop GraphRAG），支持动态构建知识图谱、插入节点和边、执行多跳语义检索。适用于需要复杂推理和多步关联查询的场景。
---

**功能说明：**

这是一个基于 SQLite + FTS5 + 向量嵌入的多跳图检索系统，能够：
1. 动态添加文档节点（自动计算向量嵌入）
2. 建立节点间的语义关系边
3. 执行多跳 BFS 检索，LLM 智能判断相关性
4. 混合搜索（向量相似度 + 全文检索）
5. 持久化存储到 SQLite 数据库

**使用示例：**

```python
import sys
from pathlib import Path

# 动态添加项目根目录到 sys.path
current_file = Path(__file__).resolve()
project_root: Path = current_file.parents[3]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from rag.mutil_hop_graphrag.dynamic_graph_builder import DynamicGraphBuilder
from rag.mutil_hop_graphrag.core import multi_hop_search
from models.chat_model import chat_model


def build_and_search():
    """构建知识图谱并执行检索"""
    
    # === 步骤 1: 创建动态图构建器 ===
    # db_path=":memory:" 表示内存数据库（临时）
    # db_path="./my_rag.db" 表示持久化到文件
    builder = DynamicGraphBuilder(db_path=":memory:")
    
    # === 步骤 2: 添加文档节点 ===
    documents = [
        "Albert Einstein was a theoretical physicist born in Germany in 1879.",
        "Einstein developed the theory of relativity, one of the pillars of modern physics.",
        "The theory of relativity includes the famous equation E=mc2.",
        "E=mc2 shows that energy and mass are equivalent.",
        "Nuclear energy is released from atomic nuclei via fission or fusion.",
        "Marie Curie was a Polish-born physicist who researched radioactivity.",
        "Radioactivity is the spontaneous emission of radiation from unstable nuclei.",
    ]
    
    node_ids = builder.add_documents(documents)
    print(f"添加了 {len(documents)} 个文档节点，ID: {node_ids}")
    
    # === 步骤 3: 添加语义关系边 ===
    edges = [
        (0, 1, "developed"),           # Einstein -> relativity
        (1, 2, "includes"),            # relativity -> E=mc2
        (2, 3, "shows"),               # E=mc2 -> mass-energy equivalence
        (3, 4, "enables"),             # mass-energy -> nuclear energy
        (5, 6, "researched"),          # Marie Curie -> radioactivity
        (0, 5, "both Nobel laureates"), # Einstein <-> Marie Curie
    ]
    
    builder.add_edges(edges)
    print(f"添加了 {len(edges)} 条关系边")
    
    # === 步骤 4: 构建图 ===
    graph = builder.build()
    stats = graph.get_stats()
    print(f"图谱构建完成: {stats['num_nodes']} 节点, {stats['num_edges']} 边")
    
    # === 步骤 5: 执行多跳检索 ===
    query = "What did Einstein discover that leads to nuclear energy?"
    result = multi_hop_search(
        graph=graph,
        query=query,
        llm=chat_model,
        max_hops=3,           # 最大跳跃步数
        top_k_entry=3,        # 入口节点数量
        use_hybrid=True       # 启用混合搜索（向量+FTS）
    )
    
    print(f"\n查询: {query}")
    print(f"答案: {result['answer']}")
    print(f"访问节点数: {result['visited']}")
    print(f"检索路径:")
    for path in result['paths']:
        print(f"  Hop {path['hop']}: node_{path['node_id']} via '{path['relation']}'")
    
    return result


def add_more_documents(builder: DynamicGraphBuilder):
    """演示如何向已构建的图中追加更多节点和边"""
    
    # 注意：DynamicGraphBuilder 不支持直接修改已构建的图
    # 正确做法是：重新收集所有文档和边，然后重新 build()
    
    # 获取现有文档
    existing_docs = builder.documents.copy()
    existing_edges = builder.edges.copy()
    
    # 添加新文档
    new_docs = [
        "Quantum mechanics describes nature at atomic scales.",
        "Lasers operate based on quantum mechanical principles.",
    ]
    all_docs = existing_docs + new_docs
    
    # 重新构建
    builder.clear()
    builder.add_documents(all_docs)
    
    # 添加原有边 + 新边
    builder.add_edges(existing_edges)
    builder.add_edges([
        (len(existing_docs), len(existing_docs) + 1, "uses"),  # quantum -> lasers
    ])
    
    graph = builder.build()
    print(f"更新后图谱: {graph.get_stats()}")


def search_with_persistent_db():
    """演示如何使用持久化数据库"""
    
    db_path = "./rag_knowledge.db"
    
    # 第一次运行：构建并保存
    builder = DynamicGraphBuilder(db_path=db_path)
    docs = [
        "Python is a high-level programming language.",
        "Machine learning uses algorithms to learn from data.",
        "TensorFlow is a machine learning framework by Google.",
    ]
    builder.add_documents(docs)
    builder.add_edges([
        (0, 1, "used_in"),
        (1, 2, "implemented_by"),
    ])
    graph = builder.build()
    
    # 后续运行：直接从数据库加载（不需要重新构建）
    from rag.mutil_hop_graphrag.store import PersistentGraph
    loaded_graph = PersistentGraph()
    loaded_graph.build_embedding_index()
    
    query = "What frameworks implement machine learning?"
    result = multi_hop_search(loaded_graph, query, chat_model, max_hops=2)
    print(f"持久化检索结果: {result['answer']}")


if __name__ == '__main__':
    # 运行示例
    build_and_search()
```

**核心 API 说明：**

### 1. DynamicGraphBuilder（动态图构建器）

```python
from rag.mutil_hop_graphrag.dynamic_graph_builder import DynamicGraphBuilder
builder = DynamicGraphBuilder(db_path=":memory:")  # 或 "./my.db"

# 添加单个文档，返回 node_id
node_id = builder.add_document("文本内容")

# 批量添加文档，返回 node_ids 列表
node_ids = builder.add_documents(["文本1", "文本2"])

# 添加单条边
builder.add_edge(source_id=0, target_id=1, relation="描述关系")

# 批量添加边
builder.add_edges([
    (0, 1, "关系1"),
    (1, 2, "关系2"),
])

# 构建图（必须先添加文档和边）
graph = builder.build()

# 清空重建
builder.clear()

# 查看状态
stats = builder.get_stats()  # {"num_documents": N, "num_edges": M, "is_built": bool}
```

### 2. multi_hop_search（多跳检索）

```python
from models import chat_model
from rag.mutil_hop_graphrag import multi_hop_search
from rag.mutil_hop_graphrag.store import PersistentGraph

result = multi_hop_search(
    graph=PersistentGraph(),              # PersistentGraph 实例
    query="你的问题",         # 查询字符串
    llm=chat_model,           # LLM 模型实例
    max_hops=3,               # 最大跳跃步数（默认3）
    top_k_entry=3,            # 入口节点数量（默认3）
    use_hybrid=True           # 是否启用混合搜索（默认True）
)

# 返回结果结构
{
    "answer": "LLM 生成的答案",
    "entry_nodes": [(node_id, score), ...],  # 入口节点及相似度分数
    "paths": [
        {
            "hop": 1,                        # 第几跳
            "node_id": 5,                    # 节点ID
            "text": "节点文本",
            "relation": "边的关系描述",
            "path": "完整路径描述"
        },
        ...
    ],
    "visited": 10                            # 访问的节点总数
}
```

### 3. PersistentGraph（底层图存储）

```python
from rag.mutil_hop_graphrag.store import PersistentGraph

graph = PersistentGraph()

# 手动添加节点（需要预先计算嵌入向量）
import numpy as np
from models.embed_model.core import embed_model

text = "示例文本"
embedding = np.array(embed_model.embed_query(text))
graph.add_node(node_id=0, text=text, embedding=embedding)

# 添加边
graph.add_edge(source_id=0, target_id=1, relation="关系")

# 构建索引（必须调用）
graph.build_embedding_index()

# 混合搜索
matches = graph.hybrid_search(query="关键词", query_embedding=embedding, top_k=5)

# 获取统计信息
stats = graph.get_stats()  # {"num_nodes": N, "num_edges": M}
```

**注意事项：**

1. **节点 ID 规则**：节点 ID 从 0 开始连续递增，由 `add_document()` 自动分配
2. **边的有效性**：添加边时，source_id 和 target_id 必须是已存在的节点 ID
3. **重新构建**：`DynamicGraphBuilder` 不支持增量更新，修改后需 `clear()` 再重新 `build()`
4. **持久化**：指定 `db_path` 参数可保存到 SQLite 文件，重启后可直接加载
5. **混合搜索**：`use_hybrid=True` 同时使用向量相似度和 FTS5 全文检索，效果更佳
6. **LLM 依赖**：多跳检索需要 LLM 判断每跳的相关性，确保 `chat_model` 已正确初始化

**适用场景：**

- 知识库问答（需要多步推理）
- 概念关联分析
- 学术研究文献检索
- 复杂事实查询（如"A 通过什么连接到 B"）
- 跨领域知识发现

**性能提示：**

- 小规模图谱（<1000 节点）：内存数据库即可
- 大规模图谱（>1000 节点）：建议使用持久化 SQLite
- 嵌入计算较慢，建议批量添加文档而非逐个添加
- `max_hops` 不宜过大（建议 ≤5），避免检索范围过广
