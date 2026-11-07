# 🧠 メモリと継続性

[English](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> [Long-Running Tasks](../README.ja.md) の一部：二層メモリシステム、要約 ↔ TaskFlow 連携、サブエージェントメモリ還流、ツール出力の一行要約、セッション継続性、TaskFlow 自動再開。

---

## 🧠 階層メモリ

2 つの層は、*どのように*モデルへ届くかで区別されます：

| 層 | ストア | 場所 | プロンプトに入るか？ |
| :--- | :--- | :--- | :--- |
| **L1 —— 精錬メモリ** | `memory_entries` 行（`memory` エージェントノート、`user` プロファイル、`facts` システムの落とし穴） | MesMemory SQLite（`mes_memory.db`）の `memory_entries` テーブル | はい——凍結スナップショットとして常に注入 |
| **L2 —— 生の履歴** | `mes_memory.db`（SQLite、WAL、FTS5） | `src/store/mes_memory/mes_memory.db` | いいえ——`context_engine` / `message_search` が取得 |

エントリは `memory_entries` の**1 行の SQLite レコード**です。`§` 区切りはレガシーインポートと `GET/PUT /memory` 互換パスにのみ残ります。エントリは複数行にわたれ、YAML frontmatter も箇条書きプレフィックスもありません。

層 1 は `MemoryStore`（`agent/tools/memory.py::load_from_store`）が管理します：ターゲットごとの文字数上限は `2200`（memory）、`1375`（user）、`1375`（facts）、プロンプトインジェクションと認証情報漏洩を拒否する注入スキャン（`_MEMORY_THREAT_PATTERNS`）、完全一致の重複排除を備え、同時実行の安全はファイルロックやアトミック書き換えではなく SQLite WAL が担います。行は `origin_session`、`usage_count`、`last_used` を持ち、レンダリングブロックは由来が分かる場合のみ `[origin: …, created: …, used: Nx]` を追加します。`memory(action="read")` は使用回数を増やします。ライブ行は即座に変更される一方、プロンプトは `load_from_store()` で取得された**凍結スナップショット**を使い、セッション中のプレフィックスキャッシュを安定させます。旧 `MEMORY.md`/`USER.md`/`FACTS.md` は起動時に一度インポートされ（`context_engine/store/memory_migration.py`）、`*.md.bak` に改名されます。既存行が 1 つでもあればインポートはスキップされます。

`memory` ツールは `scope="main_only"` とタグ付けされているため、サブエージェントには決して見えません。

**グラフ状態チェックポイントストア。** セッションの LangGraph 状態は `src/checkpoints/sqlite.db` にも永続化され、上記 2 層とは別です。グローバルな keep-latest 剪定は起動時に 1 回だけ実行され（`server/service/checkpoint_maintenance.py`）、`built_agent()` 呼び出しごとには行いません（毎ターン共有 DB に競合書き込みを作るため）。スレッド単位の剪定は child 解放と session purge で続きます。`auto_vacuum=0` では DELETE はページを解放するだけなので、保守処理が `PRAGMA freelist_count × page_size` を読み、解放領域が `_VACUUM_THRESHOLD_BYTES`（10 MB、`agent/checkpointer/thread_safe_checkpointer.py`）を超えるときだけ `VACUUM` します——フェイルオープン：VACUUM エラーはログのみで、剪定結果は有効です。

## 🔎 意味検索と整理

どちらもオプトインで、すべて fail-open です。`semantic_recall_enabled`（記憶設定の「意味検索」）を有効にすると、システムプロンプトにはコンパクトな索引（`MemoryStore.format_index_for_system_prompt`）だけが入り、毎ターン `memory_recall` リマインダー（priority 88、cooldown 3）が現在のリクエストに関連するエントリを注入します。`context_engine/store/memory_recall.py` は bge-m3 で `memory_entries.embedding` 索引を自己修復し、bge-reranker で cosine 候補を再ランクし、`semantic_recall_char_budget` で切り詰め、現在の `context_epoch` ベースラインより古いエントリを除外し、`usage_count` を加算します。`memory(action="recall")` と `POST /memory/search` は同じ経路を使い、スイッチがオフの検索は部分文字列にフォールバックします。

`POST /memory/consolidate` は非同期タスクを開始します（`GET /memory/consolidate/:task_id` で照会）。まず決定的な `prune_unused` 老化を実行し、アクティブエントリが `consolidation_min_entries`（10）以上ある場合だけ、メインモデルが memory ツール経由で重複統合・矛盾解消・誤った target の修正・簡潔化を行います。圧縮はメモリレビューの後に同じパスを実行し（15 turns）、セッション削除は 2 回の salvage の後に実行します（5 turns）。失敗はログのみで、圧縮や削除をブロックしません。

## 🔗 要約 ↔ TaskFlow 連携

圧縮が LLM プロンプトを組み立てるとき、`_get_taskflow_context_sync(session_id)`（`agent/middlewares/summarization/core.py:122`）がこのセッションのアクティブな flow を描画し、要約プロンプトの**最後**の部分として追記します（`_build_summary_prompt`、`summarization/summary_generation.py:554`）：

```python
taskflow_ctx = _get_taskflow_context_sync(session_id)
if taskflow_ctx:
    parts.append(taskflow_ctx)
```

このブロックは `## Current TaskFlow State (authoritative)` を見出しとし（`summarization/core.py:137`）、セッションが所有する最大 3 つの flow（ストア読み取りは SQL 層で `session_id` にスコープされ、Python 側の再フィルタはありません）について、flow id/ステータス、説明、`done/total` 進捗とステータス内訳、最後の 2 つの完了ステップ、最初の 2 つの保留ステップ、待機理由を列挙します。DAG ヘルパー `step_status` と `steps_summary` を再利用し、完全にフェイルオープンです（`except Exception → ""`）。決定論的フォールバック要約（`_build_static_fallback_summary`）はこのブロックを**含みません**；これは LLM プロンプト専用の追加です。

## 🧠 サブエージェントメモリ還流

`SubagentCompletionDrainMiddleware`（`agent/middlewares/subagent_completion_drain/core.py`）は、キューに入ったサブエージェント完了メッセージの親ターン側の取り込み点です：`before_model` でセッションの `SteeringQueue` を再水和して排出し、再構築された完了キャリアメッセージを注入します。**排出が非空のとき**、共有メモリを親のインメモリビューと照合します：

```python
# subagent_completion_drain/core.py:93-117
def _backflow_shared_memory() -> None:
    from agent.tools.memory import memory_store
    memory_store.load_from_store()
```

親と子は SQLite 上の**単一のプロセス全体 `MemoryStore`** を共有するため、子の書き込みはすでに永続化されています。ドリフトしうるのは親のインメモリビュー——ライブ行と、システムプロンプトの構築に使った**凍結スナップショット**——です。再読込は必須かつ十分です：SQLite が唯一の真実であり、書き戻しはなく、再読込が並行書き手を上書きすることもありません（以前の load → persist 照合は上書きしえました）。

排出と同様、還流も**フェイルオープン**です——メモリ I/O の失敗はログに記録されて握りつぶされ、完了キャリアは親ターンへ届きます。排出は親に完了の扱いを厳格に保たせます：セッションに合格した証跡がないとき、キャリアの後ろに必須検証のゲートメッセージを追記し、完了は `DoneClaim` であって検証済み結果ではないことを親に思い出させます（todo を完了にする前に `todoread` で検証し、受け入れ基準に照らし、古い状態を調査します）。

## ✂️ ツール出力の要約

非 LLM 剪定では、大きすぎる古い `ToolMessage` の内容は通常マーカーへクリアされます。`pub/func/message/tool_output_prune.py` は裸のマーカー `_PRUNE_MARKER = "[Old tool result content cleared]"`（`tool_output_prune.py:22`）を**一行のツール固有要約**へ置き換え、結果に何が含まれていたかの手がかりをモデルに残します：

```python
# _TOOL_SUMMARY_TEMPLATES (tool_output_prune.py:43-65)
"read"/"read_file"   -> "[read] read file, {len} chars, {lines} lines"
"write"/"write_file" -> "[write] wrote file, {len} chars"
"edit"/"edit_file"   -> "[edit] edited file, {len} chars"
"grep"               -> "[grep] search done, {lines} matches"
"glob"               -> "[glob] matched {n} files"
"bash"/"shell"       -> "[bash] exit_code={code}, output {len} chars"
"taskflow_flow"      -> "[taskflow_flow] {len} chars"
"taskflow_step"      -> "[taskflow_step] {len} chars"
"taskflow_inspect"   -> "[taskflow_inspect] {len} chars"
"memory"             -> "[memory] {first 80 chars}..."
default              -> "[tool] output {len} chars, first 100: ..."
```

`prune_tool_outputs(messages, protect_tokens=…, min_reduction_tokens=…, protected_tools=None, estimator=None)`（`tool_output_prune.py:104`）は新しい順から古い順へメッセージを走査し、最初の要約メッセージで停止し、最新の `prune_protect_tokens`（40 000）を保護し、保護対象ツール（`{"memory", "skill_view", "skill_list"}`）をスキップし、解放トークンが `prune_min_reduction_tokens`（5 000）に達した場合にのみ確定します。置換されたメッセージは `additional_kwargs["status"] = "compacted"` と `["original_length"]` を持つ `model_copy` クローンです。要約は 200 文字に制限され、テンプレート例外はマーカーへフォールバックします。呼び出し元は `Summarization._run_non_llm_strategies` です（`summarization/compression.py:314`）。

## 🔄 セッション継続性

セッションがクリアされるとき、`context_engine/session_continuity.py` が終了状態を永続化し、次のセッションが継続性を提示できるようにします。`server/service/session_cleanup_service.py::purge_session` は削除の前に `auto_save_on_session_end(session_id)` を**ステップ 0** として呼びます（同関数のステップ 0）。この関数は：

1. `runtime.session.relation_register` 経由で `channel_id`/`chat_id` を解決します（`_get_channel_chat_for_session`、`session_continuity.py:167`）。
2. 直近 3 ターンを読み、最後の AI 返信を `_MAX_SUMMARY_CHARS = 500` に切り詰めます（`session_continuity.py:28`）。
3. そのセッションのアクティブ flow id を収集します。
4. `save_session_end_state(...)` を `src/data/session_continuity/{safe-key}.json` に書き込みます（`session_continuity.py:25`）。フィールドは `last_session_id`、`ended_at`、`ended_ts`、`summary`、`taskflow_ids` です。

メッセージストアの削除後、`clear_session` はそのセッションの**計画ストア**もパージします——`agent.tools.todolist.registry.store_sqlite.delete_todos_by_session(session_id)` と `agent.tools.taskflow.registry.store_sqlite.delete_flows_by_session(session_id)`——そのため、クリアされたセッションに todo やタスクフローの残骸は残りません。削除はベストエフォートで（失敗はログに記録され、残りのパージをブロックしません）、`session_id` が一致する行だけが削除され、分離前のタスクフロー行（`session_id = ''`）は決して一致しません。

次のセッションは `build_continuity_prompt(session_id)`（`session_continuity.py:80`）でこれを読みます。これは `workspace/prompt_builder.py:169` の `_build_continuity_block` から呼ばれ、完全なプロンプトを構築するときに注入されます（`prompt_builder.py:289-295`）：

```
## Last Session (continuity)
Last conversation ended with: <要約 ≤ 500 文字>
Related tasks: <最大 3 つの flow id>
If the user says 'continue' or doesn't specify a new task, refer to the above context first.
```

セッションが自分自身の状態を受け取ることはありません（`last_session_id == session_id → ""`）。検索には **channel id と chat id の両方**が必要なため、チャネルバインディングのない純粋な WebSocket セッションには継続性ブロックがありません。ストレージはデータベースではなく、ファイルシステム上の JSON（`channel:chat` をキーとし、退化時は `session_id`）です。

## 🧹 セッション削除時のメモリサルベージ

メインセッションを破棄する前に、`purge_session` は独立した memory review（`background_reviews.salvage_session_memory`）もスケジュールする。圧縮後の checkpoint transcript、モデル、システムプロンプト、ツール面をスナップショットし、通常の圧縮カットを**読み取り専用**で計算して、スケジュール後すぐに戻る——削除はモデルを待たない。第 1 波は圧縮が破棄したはずのプレフィックスを、キャプチャした（cache-warm の）モデル/プロンプト/ツール面で review する。メモリエントリ（由来は派生 wave キーではなく削除対象セッションの id）を書いた後、第 2 波は memory を再読み込みし、システムプロンプトを非永続的に再構築して、`memory` のみのツール面で保持尾部を review する。両波は BACKGROUND_REVIEW レーン、派生セッションキー（`<id>::session-end-salvage:1|2`）、checkpointer なしで走る。`background_review_memory` metadata ゲートが削除後の `knowledge`/plan ツールを拒否する。すべての失敗は fail-open で、プロセス終了は未完了の波を放棄しうる。サイドチャット/サブエージェントの子は対象外。上の continuity は次の会話のために最後の AI 返信を保存し、このサルベージは永続 memory 行を書く。

## ♻️ TaskFlow 自動再開

アクティブな flow はシステムプロンプトへ再浮上し、新しいセッションが未完了の作業を引き継げるようにします。3 つの独立した読み取りが同じレシピを使います——セッション単位の `get_active_flows_sync(session_id)` を `PromptDataProvider.get_active_flows(session_id)` 経由で取得します：

| 読み取り | 場所 | 目的 |
| :--- | :--- | :--- |
| `_build_taskflow_block` | `workspace/prompt_builder.py:145` | 毎ターンの reminder ブロックの `## Pending TaskFlows` |
| `_get_taskflow_context_sync` | `agent/middlewares/summarization/core.py:122` | 圧縮要約プロンプトの TaskFlow ブロック |
| `_get_active_taskflow_ids_sync` | `context_engine/session_continuity.py:215` | 永続化された継続性状態の `taskflow_ids` |

`creator_session_key` は flow 作成時に `requester_session_key(session_id)` = `f"agent:main:session:{session_id}"` として刻まれます（`taskflow_create.py:38`、`_shared.py:21`）。`get_active_flows_sync()`（`store_sqlite.py:466`）は `running` と `waiting` の flow だけをリビジョン順で返し、イベントループを必要としない stdlib `sqlite3` パスを使います；失敗時は `[]` を返します。

システムプロンプトブロック（`prompt_builder.py:140`）は次の形です：

```
## Pending TaskFlows
- [running] flow-1: "<説明>" | 2/5 steps done | next: step-3 "<タスク>"
Use taskflow_inspect(action="summary") to inspect a flow and continue execution.
```

最大 3 つの flow に制限され、ファイルフィルタ付きでプロンプトを構築するとき（`selected_file_names is not None`）は抑制されます。すべての読み取りはフェイルオープンです。

