# 🧠 LLM Wiki 图层

[**English**](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> `llm_wiki` 技能的设计说明：显式图层、多模态 ingest 路径与图算法查询如何与
> markdown 知识库并存，而不改变后者。

事实来源：`skills/builtin/llm_wiki/scripts/{core,ingest,search,graph,graph_query}.py`、
`skills/builtin/llm_wiki/SKILL.md` 与
`config/features/infra_side/skills_tooling.py`。

## markdown 仍是唯一事实来源

wiki 仍是一组互相链接的 markdown 文件。图是**派生**的：`scripts/graph.py` 写入
`src/data/wiki/.wiki_graph.db`（可丢弃、从 markdown 重建），包含三张表——
`wiki_nodes`、`wiki_edges`、`wiki_graph_meta`。`save_source`、`search_wiki`、
`init_wiki` 与 `lint_wiki` 的签名不变（L1）；图层是附加层。

| 边的来源 | `edge_type` | confidence |
|----------|-------------|------------|
| `[[Wikilink]]` | `wikilink` | `EXTRACTED` |
| `[[Target|INFERRED]]` | `wikilink` | `INFERRED` |
| `[inline link](...)` | `inline_link` | `EXTRACTED` |
| `` `code span` `` 提及 | `mention` | `EXTRACTED` |
| frontmatter `extracted_relations` | 该关系的 `type` | 该关系的 `confidence` |

`build_wiki_graph(force=False)` 按文件 `mtime` 增量：未变化的文件跳过，变化文件的
边被替换，删除文件会移除其节点与所有相邻边。`|EXTRACTED` 可显式书写，后缀是
可选的（普通 wikilink 即 EXTRACTED）。可选的 `extracted_entities` 增加节点；
`extracted_relations` 增加带类型的边。

## 多模态 ingest

`scripts/ingest.py::save_binary_source(content_bytes, content_type, ...)` 是
`save_source`（仅文本，保持不变）旁边的附加二进制路径：

| `content_type` | 抽取器 | `raw/` 分类 |
|----------------|--------|-------------|
| `pdf` | pypdf → pdftotext → mineru | `papers` |
| `image` / `png` / `jpg` | `image_to_text`（`itt`） | `assets` |
| `audio` / `mp3` / `wav` | `speech_to_text`（`stt`） | `transcripts` |
| `video` / `mp4` / `mov` | `video_text_to_text`（`vtt`） | `transcripts` |
| `docx` | 标准库 ZIP + `word/document.xml` | `articles` |
| `xlsx` | 标准库 ZIP + sharedStrings/sheet XML | `articles` |
| `md` / `txt` | 解码 | `articles` |

一次 auxiliary-LLM 语义抽取负责 entities/relations（fail-open——provider 失败则
留下空列表），随后写入带附加 frontmatter 的 markdown 源并刷新图。缺少 `pypdf` /
`python-docx` / `openpyxl` 不会让调用失败：标准库读取器与 pdftotext/mineru 链会
退化（L5）。

## 图查询

`scripts/graph_query.py` 暴露读取侧：

| 函数 | 返回 |
|------|------|
| `path(a, b, max_depth=5)` | 最短无向路径 + 每跳的边类型/confidence |
| `god_nodes(limit=10)` | 按度数排序的节点（纯 SQL，无可选依赖） |
| `explain(name)` | 邻居、方向、边类型、confidence 与来源文件 |
| `communities()` | networkx Louvain 社区；无 networkx 时为连通分量 |
| `query_graph(question)` | ASCII 词 + CJK 双字 → 节点召回 → 1 跳子图 |

`search_wiki(keyword)` 仍是 grep 路径，适合字面字符串；涉及关系的问题推荐
`query_graph`。分词不依赖第三方库（不需要 jieba）；wiki 图刻意不与 code_intel
或 graph_rag 合并——每套图针对自己的存储与查询语言优化。

## lint 图指标

`lint_wiki()` 保留原有报告，并追加一个 `graph` 块：连通性（`components`、
`largest_component`、`isolated_nodes`）、`EXTRACTED` / `INFERRED` 比例、
`low_confidence_pages`（推断边占比至少 `wiki_lint_low_confidence_ratio` 的页面）
以及 `god_nodes` 前 10。图区块是 fail-open 的：构建失败时报告
`available: false`，不会破坏原有十项检查（L7）。

## 测试

| 范围 | 测试 |
|------|------|
| 图构建、confidence 标签、增量重建 | `tests/skills/builtin/llm_wiki/test_graph_build.py` |
| path、god nodes、explain、communities、自然语言查询 | `tests/skills/builtin/llm_wiki/test_graph_query.py` |
| 二进制 ingest 与 office 读取器 | `tests/skills/builtin/llm_wiki/test_binary_ingest.py` |
| lint 图指标 | `tests/skills/builtin/llm_wiki/test_lint_graph.py` |
