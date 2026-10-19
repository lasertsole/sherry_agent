# 🧵 ツール並列安全

[**English**](README.md) · [中文](README.zh.md) · **日本語** · [한국어](README.ko.md)

> LangGraph の `ToolNode` は 1 回のモデル応答に含まれるすべてのツール呼び出しを同時に開始する。Sherry はバッチ対応ノードと 2 層のリソースロックで置き換え、同じファイルを共有する呼び出しは順序付け、独立した呼び出しは並列のままにする。

## 問題

モデルは 1 回の応答で複数の tool call を返せる。ToolNode は `asyncio.gather` でまとめて実行する。独立した読み取りには正しいが、2 つの書き込み、読み取りと書き込みの競合、2 つの agent の shell 競合には危険である。全か無かの規則（書き込みが 1 つでもあればバッチ全体を直列化）は安全だが、無関係なファイルの並列性を捨てる。必要なのはブール値ではなくリソースキーである。

## 静的分類

`agent/tools/parallel_safety.py` が判定する：

- `READ_ONLY_TOOLS` は副作用のないツールの一覧。
- ツール metadata は `concurrent_safe`（明示的に安全）または `side_effect_scope`（`none`、`workspace`、`system`、`session`、`user_interaction` など）を宣言できる。
- 未知のツールと metadata 欠落は不安全。
- `destructive` はすべての許可宣言を上書きする。
- `idempotent` は意図的に**使わない**：`memory` は冪等だがセッション状態を書き、`sessions_spawn` は非冪等だが並列実行できる。

## バッチ計画と順序

各 ToolNode バッチは `plan_batch` がグループに分ける：

- `read_file`、`write_file`、`patch_file` は canonical `file_path` でグループ化されたファイル操作。
- 書き込みを含むグループはモデルの `tool_calls` 順に直列、読み取りのみのグループは並列。
- 異なる path は独立。
- ファイル以外の不安全な呼び出し（例：`terminal`）はバッチ全体を barrier にする。対象リソースを引数から推測できないため。
- 解決できない file path も barrier に戻す。

順序規則はモデルの呼び出し順である：`read_file(A)` が `write_file(A)` より前なら古い内容を読み、書き込みに license を与える。逆なら先に書き、後続の読み取りは新しい内容を見る。同じ path の 2 つの書き込みは順に実行され、最後が勝つ。

## ファイルロック

バッチ計画は**同じバッチ内**の順序だけを担う。ファイル層は並行する agent とプロセスを保護する：

- `agent/tools/pub_base/path_lock.py` は `path_read_lock`（共有）と `path_write_lock`（独占、書き手優先）を提供。
- `agent/tools/pub_base/file_lock.py` は共有 `flock`（`LOCK_SH`）を追加し、独占 `LOCK_EX` を維持。
- `read_file` は `file_read_lock`、`write_file` と `patch_file` は `file_write_lock` を使う。
- 固定順序はプロセス内ゲート、次にプロセス間 `flock`。

同じ path の 2 つの読み取りは重なり、読み取りと書き込みは決して重ならず、異なる path は競合しない。バッチ/agent をまたぐ順序はロック取得順であり、モデルの呼び出し順ではない。

## リソースロック

`agent/middlewares/tool_resource_lock.py` は path では表せない 2 つのリソースを扱う：

- `side_effect_scope: "system"`（`terminal`、`python_repl`、`execute_code`、`ptc`）はプロセス全体のロックを取る。`TOOL_PARALLEL["system_lock"]` はすべての shell 作業を直列化するため既定で OFF。
- `side_effect_scope: "user_interaction"`（`question`、`exit_plan_mode`）はセッションごとのロックを取る。`interaction_lock` は既定で ON で、1 つの HITL チャネルが同時に 1 つのプロンプトだけを扱う。
- `workspace`、`session`、`network` は再ロックしない。ファイルロック、memory store、SQLite store、browser manager が既に所有している。

## 導入とスコープ

`create_agent` に `tool_node` パラメータはない。`agent/core.py` がインポート時に `TOOL_PARALLEL["enabled"]` に従って `langchain.agents.factory.ToolNode` を一度 patch するため、main、subagent、補助 agent のグラフがすべて `SafeToolNode` を使う。子エージェントのミドルウェアチェーンも `ToolResourceLockMiddleware` を持つ。`TOOL_PARALLEL["enforce"] = False` はネイティブ並列のまま計画をログし、`True` で適用する。互換性テストが依存する `ToolNode` 私有メソッドを pin するため、LangGraph アップグレードは静かに壊れず明示的に失敗する。

## 制限

- `terminal`、`python_repl`、`execute_code`、`ptc` はバッチ全体の barrier のまま。コマンド文字列からファイル対象を確実に解析できない。
- 複数 agent 間の順序は保証しない。相互排他だけを保証する。`system_lock` を有効にした場合が明示的な例外。
- バッチ計画は canonical path を使う。解決できない場合は推測せず barrier に戻す。
- ファイルロックはローカルファイルシステムの `flock`。NFS のセマンティクスは対象外。

## コードマップ

- `agent/safe_tool_node.py`：`SafeToolNode`、`_afunc` / `_func`、factory への導入。
- `agent/tools/parallel_safety.py`：分類、`file_resource_key`、`plan_batch`。
- `agent/tools/pub_base/path_lock.py`：プロセス内読み書きゲート。
- `agent/tools/pub_base/file_lock.py`：`file_read_lock` / `file_write_lock`、`flock_path(shared=...)`。
- `agent/middlewares/tool_resource_lock.py`：プロセス全体とセッション単位のリソースロック。
- `agent/tools/file_tools/read_file.py`：読み取りロックの取得箇所。
- `tests/agent/test_safe_tool_node.py`、`tests/agent/tools/test_parallel_safety.py`、`tests/agent/tools/file_tools/test_file_rw_lock.py`、`tests/agent/middlewares/test_tool_resource_lock.py`：上記の挙動と不変条件。
