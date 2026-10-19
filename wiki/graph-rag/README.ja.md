# GraphRAG パスクエリ

`multimodal_rag` スキルの LightRAG エンジンはエンティティと関係をベクトル類似度
で召回し、マルチホップの結合は LLM に委ねます。このページでは、その召回の上に
追加した明示的パス層、同時に入れた召回しきい値の調整、そしてスパース召回に
ついての調査結論（変更前後の実測値付き）を記録します。

## 欠けていたもの

`vendored_lightrag` にはパスアルゴリズムがありませんでした：
`query_nodes_by_vector` と関係召回はそれぞれ 1 回のベクトル検索
（既定 `top_k=40`）を実行して返すだけです。橋渡しエンティティが top-k の外に
落ちると、グラフに経路が存在しても「A と B の関係は？」を探索で答えることは
できません。いわゆる「マルチホップ」は候補集合上で LLM が推論する確率的事象で
した。

エンティティベクトルは純粋に稠密（`BGE-M3`、1024 次元）です。vendored エンジン
にはスパース経路も BM25 フォールバックもありません。

## パス層

`skills/builtin/core/multimodal_rag/scripts/graph_rag/path_search.py` はスキル
側にあります（vendored ツリーには `operate.py` のディスパッチフックだけを
追加）：

- **端点解決** — コストの低い順に 3 段階：グラフノードの完全一致名 → グラフ
  ラベルの包含一致（`Kepler-7 launch pad` → `Kepler-7 Launch Pad`）→
  `compute_mdhash_id(..., prefix="ent-")` をキーにしたエンティティベクトル召回。
- **BFS** — 解決した端点間の全単純パスを最大 `max_depth=3` ホップまで列挙し、
  `(hops, -average edge weight)` で並べて `top_k_paths=10` に切り詰めます。
  パス内でノードは繰り返しません。全体の `max_visit_nodes` 予算と層ごとの
  `max_frontier` 上限が稠密グラフでの計算量を抑えます。
- **Beam フォールバック** — BFS が何も見つけないときだけ実行：双方向で
  embedding に導かれる beam（`beam_width=5`、`beam_max_hops=5`）が、各隣接
  ノードを「反対側端点へのコサイン類似度 + `0.2 × edge weight`」で採点し、最初の
  合流ノードで停止します。ベクトルは
  `SNKVVectorStorage.get_vectors_by_ids` から取得します。
- **整形** — LLM プロンプトには明示的なチェーン、各ノード・各エッジの説明、
  通過したエッジの元 chunk を渡します。構造化ペイロード（`entities` /
  `relationships` / `chunks` / `references`）は通常の召回データに
  `path_info` メタデータを添えて統合されます。

利用入口は 2 つです：

- `mode="path"` はパス検索だけを実行し、パスが無い場合は `hybrid` 召回に
  フォールバックするため、空のコンテキストで答えることはありません。
- 関係性を問う形の `mix` / `hybrid` クエリ（英・中・日・韓のパターン；
  `is_relationship_query`）は、パス節をコンテキストに自動で統合します。

数値はすべて
`config/features/agent_side/graph_rag_path.py::GRAPH_RAG_PATH` にあります。

## 召回しきい値

LightRAG の既定値はほぼ何でも通していました：`TOP_K=40`、`CHUNK_TOP_K=20`、
`COSINE_THRESHOLD=0.2`、そして `MIN_RERANK_SCORE` は無効。調整後の既定値は
`TOP_K=20`、`CHUNK_TOP_K=12`、`COSINE_THRESHOLD=0.4`、
`MIN_RERANK_SCORE=0.5` で、さらにグラフ規模の上限
`MAX_SOURCE_IDS_PER_ENTITY=50`、`MAX_SOURCE_IDS_PER_RELATION=50`、
`SOURCE_IDS_LIMIT_METHOD=FIFO`、`RELATED_CHUNK_NUMBER=5` を設定します。

これらは vendored パッケージのインポート**より前**に
`graph_rag/__init__.py` で設定します：`QueryParam` と LightRAG dataclass は
インポート時に環境変数をクラス既定値へ読み込むため、旧配置（`base.py` 自身の
LightRAG インポート後）では実際には一度も効いていませんでした。

## スパース召回：実装しなかった理由

計画のスパース経路は embedding 層で塞がっています。
`models/embed_model/text/core.py::CustomEmbedding` は稠密ベクトルを 1 本だけ
返します——ローカルは llama.cpp `create_embedding`、リモートは OpenAI 互換の
`/embeddings` エンドポイントです。`SNKVVectorStorage` は単一の HNSW コサイン
インデックスしか持たず、`EmbeddingFunc` も `ndarray` を 1 つ返すだけです。
BGE-M3 のネイティブなスパース出力には `FlagEmbedding`（PyTorch と数百 MiB）
またはスパース対応エンドポイントが必要で、どちらもデスクトップ配布には不適切
です。したがって希少な ID・バージョン文字列の召回は未解決のままで、SQLite
FTS5/BM25 が二重スパースインデックスではなく将来の選択肢として記録されて
います。

## 実測効果

`evals/graph_rag/multihop_bench.py` は、似た名前の妨害エンティティを含む
20 文書のコーパス（102 ノード / 106 エッジ）を索引し、6 問を評価します：
最短経路が 2-5 ホップの関係質問 4 問と、単一ホップの対照質問 2 問です。両方の
実行は**同じ**索引をクエリします。

| 指標（マルチホップ質問） | 変更前 `mix` | 変更後 `mix` | 変更後 `mode="path"` |
| :----------------------- | :----------- | :----------- | :------------------- |
| コンテキスト橋渡し召回   | 1.000        | 1.000        | 1.000                |
| 回答の橋渡し召回         | 0.875        | 1.000        | 1.000                |
| 全橋渡しを含む回答       | 0.750        | 1.000        | 1.000                |
| コンテキスト総文字数     | 155,720      | 87,351       | 23,834               |
| 単一ホップ対照の正解率   | 1.000        | 1.000        | 1.000                |

コンテキストは `mix` で `44%`、`mode="path"` で `85%` 縮小し、回答の橋渡し
召回は `0.875` から `1.000` に上がりました。同じグラフでのパス遅延は 1 クエリ
あたり `2-13 ms`（BFS `2-5 ms`、beam `11-13 ms`）で、BFS `500 ms`、beam `2 s`
の目標を大きく下回ります。beam は 3 ホップ BFS では見つからない 4・5 ホップの
チェーンを見つけたため、フォールバックとして既定で有効にしています。

結論：変更前のパイプラインより優れているため、GraphRAG の計画文書 2 件は削除
しました。

## テスト

```bash
uv run --no-sync pytest tests/skills/builtin/core/multimodal_rag/test_path_search.py \
  tests/skills/builtin/core/multimodal_rag/test_path_query_integration.py -q
uv run --no-sync python evals/graph_rag/multihop_bench.py --modes mix,path
uv run --group evals python evals/evals.py graph_rag
```

単体テストはホップ保証、循環防止、パス上限、ラベルとベクトルによる端点解決、
beam フォールバックを固定します。統合テストは実 SNKV ストレージ上で LLM を
呼ばずに `mode="path"` と `mix` 自動統合を実行します。RAGAS スイートは単一
ホップ回答品質の回帰ゲートです。
