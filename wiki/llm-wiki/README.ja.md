# 🧠 LLM Wiki グラフ層

[**English**](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> `llm_wiki` スキルの設計資料：明示的グラフ、マルチモーダル ingest、グラフ
> アルゴリズムのクエリが、markdown ナレッジベースを変えずにどう共存するか。

情報源：`skills/builtin/llm_wiki/scripts/{core,ingest,search,graph,graph_query}.py`、
`skills/builtin/llm_wiki/SKILL.md`、
`config/features/infra_side/skills_tooling.py`。

## markdown が唯一の情報源

wiki は相互リンクされた markdown ファイルの集合のままです。グラフは**派生**
です：`scripts/graph.py` が `src/data/wiki/.wiki_graph.db`（破棄可能、markdown
から再構築）に `wiki_nodes`、`wiki_edges`、`wiki_graph_meta` の 3 テーブルを
書きます。`save_source`、`search_wiki`、`init_wiki`、`lint_wiki` のシグネチャは
不変（L1）で、グラフは追加レイヤーです。

| 辺の来源 | `edge_type` | confidence |
|----------|-------------|------------|
| `[[Wikilink]]` | `wikilink` | `EXTRACTED` |
| `[[Target|INFERRED]]` | `wikilink` | `INFERRED` |
| `[inline link](...)` | `inline_link` | `EXTRACTED` |
| `` `code span` `` の言及 | `mention` | `EXTRACTED` |
| frontmatter `extracted_relations` | その関係の `type` | その関係の `confidence` |

`build_wiki_graph(force=False)` はファイル `mtime` で増分更新します：未変更
ファイルはスキップ、変更ファイルの辺は置換、削除ファイルはノードと接する全辺を
削除します。`|EXTRACTED` は明示可能で、接尾辞は任意です（通常の wikilink は
EXTRACTED）。任意の `extracted_entities` はノードを追加し、
`extracted_relations` は型付き辺を追加します。

## マルチモーダル ingest

`scripts/ingest.py::save_binary_source(content_bytes, content_type, ...)` は
`save_source`（テキストのみ、不変）の隣の追加バイナリ経路です：

| `content_type` | 抽出器 | `raw/` カテゴリ |
|----------------|--------|-----------------|
| `pdf` | pypdf → pdftotext → mineru | `papers` |
| `image` / `png` / `jpg` | `image_to_text`（`itt`） | `assets` |
| `audio` / `mp3` / `wav` | `speech_to_text`（`stt`） | `transcripts` |
| `video` / `mp4` / `mov` | `video_text_to_text`（`vtt`） | `transcripts` |
| `docx` | 標準ライブラリ ZIP + `word/document.xml` | `articles` |
| `xlsx` | 標準ライブラリ ZIP + sharedStrings/sheet XML | `articles` |
| `md` / `txt` | デコード | `articles` |

auxiliary-LLM のセマンティック抽出が entities/relations を取り（fail-open——
provider 障害時は空リスト）、追加 frontmatter 付きの markdown ソースを書き、
グラフを更新します。`pypdf` / `python-docx` / `openpyxl` が無くても呼び出しは
失敗しません：標準ライブラリのリーダーと pdftotext/mineru の連鎖が退化します
（L5）。

## グラフクエリ

`scripts/graph_query.py` が読み取り側を公開します：

| 関数 | 返すもの |
|------|----------|
| `path(a, b, max_depth=5)` | 最短の無向パス + 各ホップの辺種別/confidence |
| `god_nodes(limit=10)` | 次数順のノード（純 SQL、任意依存なし） |
| `explain(name)` | 隣接ノード、方向、辺種別、confidence、来源ファイル |
| `communities()` | networkx Louvain クラスタ。networkx 無しなら連結成分 |
| `query_graph(question)` | ASCII 語 + CJK バイグラム → ノード召回 → 1 ホップ部分グラフ |

`search_wiki(keyword)` は grep 経路のままで、リテラル文字列向けです。関係を問う
質問には `query_graph` を推奨します。トークン化は依存なし（jieba 不要）で、
wiki グラフは code_intel / graph_rag と意図的に統合しません——各グラフは自分の
ストレージとクエリ言語に最適化されています。

## lint のグラフ指標

`lint_wiki()` は元のレポートを保ち、`graph` ブロックを追加します：連結性
（`components`、`largest_component`、`isolated_nodes`）、`EXTRACTED` /
`INFERRED` の比率、`low_confidence_pages`（推定辺が
`wiki_lint_low_confidence_ratio` 以上のページ）、`god_nodes` 上位 10。グラフ節は
fail-open で、構築失敗時は `available: false` を報告し、元の 10 チェックを壊し
ません（L7）。

## テスト

| 範囲 | テスト |
|------|--------|
| グラフ構築、confidence タグ、増分再構築 | `tests/skills/builtin/llm_wiki/test_graph_build.py` |
| path、god nodes、explain、communities、自然言語クエリ | `tests/skills/builtin/llm_wiki/test_graph_query.py` |
| バイナリ ingest と office リーダー | `tests/skills/builtin/llm_wiki/test_binary_ingest.py` |
| lint のグラフ指標 | `tests/skills/builtin/llm_wiki/test_lint_graph.py` |
