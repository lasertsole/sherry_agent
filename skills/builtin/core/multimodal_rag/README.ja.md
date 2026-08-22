# 🌐 マルチモーダル RAG スキル

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> EMA 内蔵スキル：マルチモーダルファイル/フォルダをプライベートナレッジグラフにインデックスし、**マルチホップグラフ検索**で質問に回答します。

## ✨ 概要

`multimodal_rag` は [EMA AI Agent](https://github.com/your-repo/EMA_AI_agent)（[トップレベル README](../../../../README.md)）の内蔵コアスキルです。プライベートな自己ホスト型 RAG ナレッジベースを提供します。

- **マルチモーダル取り込み**：さまざまな形式の文書（TXT / Markdown / PDF など）を有効なパーサーでインデックスします。
- **ナレッジグラフ**：フラットなベクトルチャンクの代わりに、文書からエンティティ–関係グラフを構築します。
- **マルチホップ検索**：複数の文書/エンティティにまたがる推論が必要な複雑な質問に答えます。
- **ネイティブストレージ**：検索スタック全体は完全にベンダリングされています — RAG-Anything パイプラインと、ストレージ層が [SNKV バックエンドと完全に融合](scripts/graph_rag/vendored_lightrag/README.md)された LightRAG エンジンです。

---

## 🔧 インストール

このスキルは EMA エージェントに同梱されるため、個別インストールは不要です。RAG エンジン（`graph_rag`）は `scripts/graph_rag/` 配下に完全にベンダリングされ、RAG-Anything パイプラインの `vendored_raganything` と、ストレージが SNKV と融合された LightRAG エンジンの `vendored_lightrag` の 2 部構成です。両者とも `graph_rag/__init__.py` で短いインポート名に別名付けされるため、PIP からのインストールは不要です。残りのランタイム依存（SNKV、mineru パーサー、埋め込みモデル）は EMA プロジェクト環境から解決されます。

---

## ▶️ 使い方

### Python API

```python
import asyncio
from skills.builtin.core.multimodal_rag.scripts import folder_index, file_index, query

async def main():
    # 1) フォルダ全体をナレッジグラフのカテゴリとしてインデックス
    await folder_index("/path/to/documents", "my_docs")

    # 2) 単一ファイルをインデックス
    # await file_index("/path/to/a/paper.pdf", "papers")

    # 3) グラフに問い合わせ（マルチホップ検索）
    answer = await query("巨学力と源野漢娜の関係は何ですか？")
    print(answer)

asyncio.run(main())
```

### コマンドライン

```bash
# フォルダをインデックス
python rag_index.py "/path/to/documents" my_docs

# 単一ファイルをインデックス
python rag_index.py "/path/to/a/paper.pdf" papers

# ナレッジグラフに問い合わせ
python rag_query.py "巨学力と源野漢娜の関係は何ですか？"

# 多形式のサンプル文書（txt/md/pdf）を取り込み、ナレッジグラフページを検証
python rag_import_test.py
```

### RAG パイプラインの注意点

- `get_rag_anything()` はベンダリングされた LightRAG エンジンに基づく `RAGAnything` インスタンスを構築します。
- インデックス出力は `src/rag/graph_rag/<classify_folder>/output/` に書き込まれます。
- `parse_method="auto"` はエンジンがファイル種別ごとに最適なパーサー（例: PDF なら `mineru`）を選びます。`parser="mineru"` は PDF パイプラインを強制し、`backend="pipeline"` は CPU での VLM コールドロードを回避します。

---

## 📝 公開 API

| 関数 | シグネチャ | 説明 |
| :--- | :--- | :--- |
| `folder_index` | `(input_folder_path: str, classify_folder: str) -> str` | フォルダ内の全ファイルをカテゴリにインデックス |
| `file_index` | `(input_file_path: str, classify_folder: str) -> None` | 単一ファイルをカテゴリにインデックス |
| `query` | `(question: str) -> str` | ナレッジグラフに質問（マルチホップ） |

3 関数とも `scripts/__init__.py` からエクスポートされます。

---

## 🧪 テスト

```bash
pytest skills/builtin/core/multimodal_rag/tests/ -q
```

テストスイートは、ベンダリングされた `graph_rag` コンポーネントが SNKV ストレージバックエンドで正しく初期化され、ベンダーパッケージが短い標準インポート識別子 `graph_rag` で正しくロードされることを検証します。

---

## 🗂️ プロジェクト構成

```text
multimodal_rag/
├── SKILL.md                    # スキルマニフェスト（名前、説明、使用例）
├── README.md                   # 英語ドキュメント（このファイル）
├── README.zh.md                # 中文
├── README.ko.md                # 한국어
├── README.ja.md                # 日本語
├── scripts/
│   ├── __init__.py             # query, folder_index, file_index をエクスポート
│   ├── rag_index.py            # folder_index / file_index
│   ├── rag_query.py            # query
│   ├── rag_import_test.py      # 多形式サンプルの取り込み
│   ├── rag_import_pdf_test.py  # mineru パーサーによる実 PDF 取り込み
│   └── graph_rag/           # 完全にベンダリングされた RAG エンジン
│       ├── vendored_raganything/  # ベンダリングされた RAG-Anything パイプライン
│       ├── vendored_lightrag/  # ベンダリングされた LightRAG、SNKV と融合したストレージ
│       └── ...
└── tests/
    └── test_rag_anything_vendored.py
```

---

## 📄 ライセンス

MIT — [プロジェクト最上位ライセンス](../../../../LICENSE) を参照。
