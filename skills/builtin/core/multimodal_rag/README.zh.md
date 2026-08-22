# 🌐 多模态 RAG 技能

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> EMA 内置技能：将多模态文件/文件夹索引进私有知识图谱，并通过**多跳图检索**回答问题。

## ✨ 简介

`multimodal_rag` 是 [EMA AI Agent](https://github.com/your-repo/EMA_AI_agent)（见[项目根 README](../../../../README.md)）的内置核心技能。它提供一个私有、自托管的 RAG 知识库：

- **多模态摄取**：可摄取多种格式文档（TXT / Markdown / PDF 等），由启用的解析器处理。
- **知识图谱**：基于你的文档构建实体–关系图谱，而非扁平向量分块。
- **多跳检索**：回答需要跨多个文档 / 实体推理的复杂问题。
- **原生存储**：整个检索栈已完全 vendored —— RAG-Anything 管线与存储层已[完全融合 SNKV 后端](scripts/graph_rag/vendored_lightrag/README.md)的 LightRAG 引擎。

---

## 🔧 安装

本技能随 EMA 一起分发，无需单独安装。RAG 引擎（`graph_rag`）已完全 vendored 到 `scripts/graph_rag/` 下，分为两部分 —— `vendored_raganything`（RAG-Anything 管线）与 `vendored_lightrag`（存储层与 SNKV 融合的 LightRAG 引擎），并由 `graph_rag/__init__.py` 别名到裸导入名，因此两者均无需从 PIP 安装。其余运行时依赖（SNKV、mineru 解析器、嵌入模型）从 EMA 项目环境中解析。

---

## ▶️ 使用

### Python API

```python
import asyncio
from skills.builtin.core.multimodal_rag.scripts import folder_index, file_index, query

async def main():
    # 1) 将整个文件夹索引为知识图谱分类
    await folder_index("/path/to/documents", "my_docs")

    # 2) 或索引单个文件
    # await file_index("/path/to/a/paper.pdf", "papers")

    # 3) 查询图谱（多跳检索）
    answer = await query("请问巨学力与源野汉娜之间是什么关系？")
    print(answer)

asyncio.run(main())
```

### 命令行

```bash
# 索引文件夹
python rag_index.py "/path/to/documents" my_docs

# 索引单个文件
python rag_index.py "/path/to/a/paper.pdf" papers

# 查询知识图谱
python rag_query.py "请问巨学力与源野汉娜之间是什么关系？"

# 导入多格式示例文档（txt/md/pdf）以验证知识图谱页面
python rag_import_test.py
```

### 关于 RAG 管线的说明

- `get_rag_anything()` 会构建一个基于 vendored LightRAG 引擎的 `RAGAnything` 实例。
- 索引输出写入 `src/rag/graph_rag/<classify_folder>/output/`。
- `parse_method="auto"` 让引擎按文件类型自动选择最佳解析器（如 PDF 用 `mineru`）；`parser="mineru"` 强制 PDF 管线，`backend="pipeline"` 可避免 CPU 上 VLM 冷加载。

---

## 📝 公开 API

| 函数 | 签名 | 说明 |
| :--- | :--- | :--- |
| `folder_index` | `(input_folder_path: str, classify_folder: str) -> str` | 索引文件夹下所有文件到某分类 |
| `file_index` | `(input_file_path: str, classify_folder: str) -> None` | 索引单个文件到某分类 |
| `query` | `(question: str) -> str` | 基于知识图谱提问（多跳） |

三个函数均从 `scripts/__init__.py` 导出。

---

## 🧪 测试

```bash
pytest skills/builtin/core/multimodal_rag/tests/ -q
```

测试套件验证 vendored 的 `graph_rag` 组件能以 SNKV 存储后端正确初始化，且 vendored 包在简短规范导入标识 `graph_rag` 下能正确加载。

---

## 🗂️ 项目结构

```text
multimodal_rag/
├── SKILL.md                    # 技能清单（名称、描述、用法示例）
├── README.md                   # 英文文档（本文件）
├── README.zh.md                # 中文
├── README.ko.md                # 한국어
├── README.ja.md                # 日本語
├── scripts/
│   ├── __init__.py             # 导出 query、folder_index、file_index
│   ├── rag_index.py            # folder_index / file_index
│   ├── rag_query.py            # query
│   ├── rag_import_test.py      # 多格式示例导入
│   ├── rag_import_pdf_test.py  # 经 mineru 解析器的真实 PDF 导入
│   └── graph_rag/           # 已完全 vendored 的 RAG 引擎
│       ├── vendored_raganything/  # vendored RAG-Anything 管线
│       ├── vendored_lightrag/  # vendored LightRAG，存储与 SNKV 融合
│       └── ...
└── tests/
    └── test_rag_anything_vendored.py
```

---

## 📄 许可证

MIT —— 见[项目根许可证](../../../../LICENSE)。
