# 🌐 Multimodal RAG Skill

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> A built-in EMA skill that indexes multimodal files/folders into a private knowledge graph, then answers questions via **multi-hop graph retrieval**.

## ✨ Overview

`multimodal_rag` is a built-in core skill of the [EMA AI Agent](https://github.com/your-repo/EMA_AI_agent) (see the [top-level README](../../../../README.md)). It provides a private, self-hosted RAG knowledge base:

- **Multimodal ingestion**: indexes documents of many formats (TXT / Markdown / PDF / and more) by any enabled parser.
- **Knowledge graph**: builds entity–relationship graphs from your documents instead of flat vector chunks.
- **Multi-hop retrieval**: answers complex questions that require reasoning across multiple documents / entities.
- **Native storage**: the entire retrieval stack is fully vendored — the RAG-Anything pipeline and a LightRAG engine whose storage layer is [fully fused with the SNKV backend](scripts/graph_rag/vendored_lightrag/README.md).

---

## 🔧 Installation

The skill ships with the EMA agent — no separate installation is needed. The RAG engine (`graph_rag`) is fully vendored under `scripts/graph_rag/` as two parts — `vendored_raganything` (the RAG-Anything pipeline) and `vendored_lightrag` (the LightRAG engine whose storage layer is fused with SNKV) — aliased onto their bare import names by `graph_rag/__init__.py`, so neither needs to be installed from PIP. Remaining runtime dependencies (SNKV, mineru parser, embedding models) are resolved from the EMA project environment.

---

## ▶️ Usage

### Python API

```python
import asyncio
from skills.builtin.core.multimodal_rag.scripts import folder_index, file_index, query

async def main():
    # 1) Index an entire folder into a knowledge-graph category
    await folder_index("/path/to/documents", "my_docs")

    # 2) Or index a single file
    # await file_index("/path/to/a/paper.pdf", "papers")

    # 3) Query the graph (multi-hop retrieval)
    answer = await query("What is the relationship between JuXueLi and YuanYe HanNa?")
    print(answer)

asyncio.run(main())
```

### Command line

```bash
# Index a folder
python rag_index.py "/path/to/documents" my_docs

# Index a single file
python rag_index.py "/path/to/a/paper.pdf" papers

# Query the knowledge graph
python rag_query.py "What is the relationship between JuXueLi and YuanYe HanNa?"

# Import multi-format sample documents (txt/md/pdf) to verify the knowledge-graph page
python rag_import_test.py
```

### Notes on the RAG pipeline

- `get_rag_anything()` builds a `RAGAnything` instance with the vendored LightRAG engine.
- Index output is written under `src/rag/graph_rag/<classify_folder>/output/`.
- `parse_method="auto"` lets the engine choose the best parser per file type (e.g. `mineru` for PDFs); `parser="mineru"` forces the PDF pipeline, and `backend="pipeline"` avoids VLM cold-load on CPU.

---

## 📝 Public API

| Function | Signature | Description |
| :------- | :-------- | :---------- |
| `folder_index` | `(input_folder_path: str, classify_folder: str) -> str` | Index all files under a folder into a category |
| `file_index` | `(input_file_path: str, classify_folder: str) -> None` | Index a single file into a category |
| `query` | `(question: str) -> str` | Ask a question against the knowledge graph (multi-hop) |

All three are exported from `scripts/__init__.py`.

---

## 🧪 Testing

```bash
pytest skills/builtin/core/multimodal_rag/tests/ -q
```

The test suite validates that the vendored `graph_rag` component initializes with its SNKV storage backends and that the vendored package loads correctly under the short canonical `graph_rag` import identity.

---

## 🗂️ Project Layout

```text
multimodal_rag/
├── SKILL.md                    # Skill manifest (name, description, usage example)
├── README.md                   # English docs (this file)
├── README.zh.md                # 中文
├── README.ko.md                # 한국어
├── README.ja.md                # 日本語
├── scripts/
│   ├── __init__.py             # Exports query, folder_index, file_index
│   ├── rag_index.py            # folder_index / file_index
│   ├── rag_query.py            # query
│   ├── rag_import_test.py      # Multi-format sample import
│   ├── rag_import_pdf_test.py  # Real PDF import via mineru parser
│   └── graph_rag/           # Fully vendored RAG engine
│       ├── vendored_raganything/  # Vendored RAG-Anything pipeline
│       ├── vendored_lightrag/  # Vendored LightRAG, storage fused with SNKV
│       └── ...
└── tests/
    └── test_rag_anything_vendored.py
```

---

## 📄 License

MIT — see the [project top-level license](../../../../LICENSE).
