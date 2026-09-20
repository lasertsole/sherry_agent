# ⏳ 長時間タスク：TaskFlow、予算、締め切り、記憶、継続性

[English](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> エージェントが単一ターンを超えて生き続ける作業をどう実行するか：永続化された SQLite DAG エンジン（`taskflow_*`、14 ツール）が、依存関係を持つステップを会話ターンをまたいで追跡し、各ステップを分離された子エージェントへディスパッチし、オプトインのポリシーに従って失敗/死亡ステップを再ディスパッチし、ステップの受け入れ基準をオーケストレータが検証できるようエコーし、予算に対してトークン/コスト消費を集計し、バックグラウンド sweeper が期限切れやアイドル状態の flow を失効させ、セッションごとに分離された flow ボードを公開し（すべての読み取りは SQL 層で所有セッションをフィルタし、子エージェントが taskflow/todolist/knowledge を受け取ることはありません）、2 層メモリシステム、圧縮前メモリフラッシュ、要約と TaskFlow の橋渡し、ツール出力の一行要約、セッション間の継続性、サブエージェント完了時のメモリ還流、そしてアクティブな flow のシステムプロンプトへの自動再注入を通じて、コンテキストを先へ引き継ぎます。

一次情報：`agent/tools/taskflow/**`、`agent/tools/memory.py`、`agent/middlewares/summarization/memory_flush.py`、`agent/middlewares/summarization/core.py`（TaskFlow コンテキストブロック）、`agent/middlewares/subagent_completion_drain/core.py`（メモリ還流）、`agent/middlewares/task_intent/core.py`、`agent/middlewares/todo_continuation/core.py`、`context_engine/session_continuity.py`、`workspace/prompt_builder.py`、`pub/func/message/tool_output_prune.py`、`agent/tools/subagent/registry/sweeper.py`、`agent/wrapper/**`、`config/features/**`。以下の定数、シグネチャ、行番号はすべてこのコードと突き合わせて検証済みです。

## 目次

- [概要](#-概要)
- [TaskFlow エンジン](engine/README.ja.md)
  - [TaskFlow DAG エンジン](engine/README.ja.md#-taskflow-dag-エンジン)
  - [ステップ再試行ポリシー](engine/README.ja.md#-ステップ再試行ポリシー)
  - [トークン / コスト予算](engine/README.ja.md#-トークン--コスト予算)
  - [タスク締め切り](engine/README.ja.md#-タスク締め切り)
  - [結果検証](engine/README.ja.md#-結果検証)
  - [進捗レポート](engine/README.ja.md#-進捗レポート)
  - [アイドル検出](engine/README.ja.md#-アイドル検出)
  - [セッションボードと分離](engine/README.ja.md#-セッションボードと分離)
- [メモリと継続性](memory/README.ja.md)
  - [階層メモリ](memory/README.ja.md#-階層メモリ)
  - [圧縮前メモリフラッシュ](memory/README.ja.md#-圧縮前メモリフラッシュ)
  - [要約 ↔ TaskFlow 連携](memory/README.ja.md#-要約--taskflow-連携)
  - [サブエージェントメモリ還流](memory/README.ja.md#-サブエージェントメモリ還流)
  - [ツール出力の要約](memory/README.ja.md#-ツール出力の要約)
  - [セッション継続性](memory/README.ja.md#-セッション継続性)
  - [TaskFlow 自動再開](memory/README.ja.md#-taskflow-自動再開)
- [並行レーン](lanes/README.ja.md)
- [設定レジストリ](#-設定レジストリ)
- [アーキテクチャ図](#-アーキテクチャ図)
- [API リファレンス](#-api-リファレンス)
- [テスト](#-テスト)
- [既知の制限](#-既知の制限)

## 🎯 概要

長時間タスクスタックは、メインエージェントがマルチターンの作業を**永続的な flow** へ分解し、そのステップ同士が依存し合えるようにし、各準備完了ステップを子エージェントへディスパッチし、プロセス再起動を生き延びられるようにします。6 つのサブシステムが協調します：

| # | サブシステム | 入口 | 永続化先 |
| :- | :--- | :--- | :--- |
| 1 | **TaskFlow DAG エンジン** | `agent/tools/taskflow/` | `data/taskflow_registry.db`（SQLite、WAL） |
| 2 | **トークン / コスト予算** | `taskflow_budget`、`taskflow_resume` | `task_flows.total_tokens` / `total_cost` / `token_budget` |
| 3 | **締め切り** | `taskflow_create(deadline_hours=…)` + sweeper | `task_flows.deadline_ts` |
| 4 | **アイドル検出** | sweeper `_scan_stale_waiting_taskflows` | `wait_json` の古いマーカー |
| 5 | **圧縮前フラッシュ** | `agent/middlewares/summarization/memory_flush.py` | `workspace/memory/MEMORY.md` |
| 6 | **継続性 / 自動再開** | `context_engine/session_continuity.py`、`workspace/prompt_builder.py` | `src/data/session_continuity/*.json` + プロンプトブロック |

全体を貫く設計契約は**エラーをテキストとして返す**ことです：ツールは業務エラーをモデルへ投げず、`Error:` で始まる人間可読な文字列を返します。すべてのバックグラウンドフックは**フェイルオープン**です——レジストリが使えなくても sweeper がクラッシュしても、「長時間タスクのコンテキストなし」へ退化するだけで、ターンを壊すことはありません。

## ⚙️ 設定レジストリ

すべての調整値は `config/features/` 配下にあり、これは**オブジェクト単位 `TypedDict` パッケージ**です——単一の巨大モジュールではありません。3 つの部分に分かれます：

| 部分 | 内容 |
| :--- | :--- |
| `config/features/agent_side/` | **19** 個のエージェント側設定モジュール（ミドルウェア、ツール、LLM クライアント、メモリ、TaskFlow） |
| `config/features/infra_side/` | **19** 個のインフラ側設定モジュール（サーバー、キュー、スキル、コンテキストエンジン、ランタイム、モデル価格） |
| `config/features/_env.py` | 唯一の共有環境ヘルパー |

各モジュールは `class XxxConfig(TypedDict)` とモジュールレベルの定数 `XXX: XxxConfig = {…}` を定義します。環境対応モジュールはビルダー `def _build_xxx(env: Mapping[str, str] | None = None) -> XxxConfig` を定義し、`env or os.environ` を読んでインポート時に定数を具体化します。環境ヘルパーは `_env_int(name, default, env)`（`config/features/_env.py:9`）で、`1/true/yes/on` と `0/false/no/off/""` を受け付け、決して例外を投げません。

レジストリは現在 **39 個の feature オブジェクト**を保持します——エージェント側 20 + インフラ側 19——各パッケージの `__init__.py` を通じて再エクスポートされ、`config/features/__init__.py` が集約するため、消費側は片方の半分またはレジストリ全体を 1 か所からインポートできます。消費側コードは定数をインポートして直接インデックスします（例：`ITERATION_BUDGET["default_max_iterations"]`）。`get_feature`/`load_feature` アクセサは存在しません。`config/__init__.py:38-39` は `GATEWAY` から `API_HOST`/`API_PORT` を導出します。

本文書に最も関係する定数：

| 設定オブジェクト | フィールド | 値 |
| :--- | :--- | :--- |
| `TASKFLOW_INFRA`（`agent_side/taskflow_infra.py`） | `busy_timeout_ms` | 5000 |
| | `init_wait_timeout_s` | 10.0 |
| | `persist_max_attempts` | 3 |
| | `wait_all_min_poll_interval_seconds` | 0.05 |
| | `wait_all_default_timeout_seconds` | 300.0 |
| | `wait_all_default_poll_interval_seconds` | 0.5 |
| | `waiting_timeout_hours` | 24 |
| `MODEL_PRICING`（`infra_side/model_pricing.py`） | `model_pricing_per_m_tokens` | `glm-5` / `deepseek-chat` / `kimi-latest` / `_default` |
| | `budget_warn_threshold` | 0.80 |
| `MEMORY_FLUSH`（`agent_side/memory_flush.py`） | `enabled` | `MEMORY_FLUSH_ENABLED`（既定 1） |
| | `model` | `MEMORY_FLUSH_MODEL`（既定 ""） |
| | `soft_threshold_tokens` | 8000 |
| | `force_flush_chars` | 50000 |
| | `output_max_tokens` | 2048 |
| | `timeout_seconds` | 30 |
| `SUMMARIZATION`（`agent_side/summarization.py`） | `prune_protect_tokens` | 40000 |
| | `prune_min_reduction_tokens` | 5000 |
| | `protected_tools` | `{"memory", "skill_view", "skill_list"}` |
| `MES_MEMORY`（`infra_side/mes_memory.py`） | `busy_timeout_s` / `connect_attempts` | 10.0 / 5 |

## 🏗️ アーキテクチャ図

```
                    ┌────────────────────────────────────────────────────────┐
                    │              agent/wrapper/ registry                   │
                    │  apply_graph_wrappers() → innermost-first chain:       │
                    │  RepetitionGuardWrapper → ContextLimitGuardWrapper     │
                    └───────────────────────────┬────────────────────────────┘
                                                │ wraps the compiled graph
                          ┌─────────────────────▼────────────────────────┐
                          │                MAIN AGENT                     │
                          │  create_agent + middleware chain             │
                          └───────────────┬──────────────────────────────┘
                                          │
        ┌─────────────────────────────────┼─────────────────────────────────────────┐
        ▼                                 ▼                                         ▼
┌───────────────────┐          ┌──────────────────────┐                 ┌────────────────────────┐
│ taskflow_* tools  │          │  memory tool         │                 │ prompt_builder         │
│ (14, main_only)   │          │  add/replace/remove  │                 │ build_system_prompt    │
└────────┬──────────┘          └──────────┬───────────┘                 └───────────┬────────────┘
         │                                │                                         │
         ▼                                ▼                                         ▼
┌───────────────────┐          ┌──────────────────────┐                 ┌────────────────────────┐
│ TaskFlow store    │          │ MemoryStore (L1)     │                 │ ─ MEMORY/USER snapshot │
│ task_flows (WAL)  │          │  agent/tools/        │                 │ ─ Pending TaskFlows    │
│ state_json DAG    │          │  memory.py           │                 │ ─ Last Session         │
│ + retry/validation│          │                      │                 │                        │
└────────┬──────────┘          └──────────────────────┘                 └────────────────────────┘
         │ dispatch_child()                                                       ▲
         ▼                                                                        │ continuity json
┌───────────────────┐     announce/settle     ┌────────────────────────┐           │
│ child subagent    │ ──────────────────────▶ │ taskflow_resume        │           │
│ sessions          │                         │ (+ token_usage budget) │           │
└───────────────────┘                         └───────────┬────────────┘           │
         │                                                │                        │
         │ drain                                          │                        │
         ▼                                                │                        │
┌──────────────────────────────┐                          │                        │
│ SubagentCompletionDrain      │                          │                        │
│ _backflow_shared_memory      │                          │                        │
└──────────────────────────────┘                          │                        │
         ┌────────────────────────────────────────────────┘                        │
         ▼                                                                         │
┌──────────────────────────────┐    every sweep    ┌───────────────────────────┐   │
│ SUBAGENT SWEEPER             │◀─────────────────▶│ Summarization middleware  │   │
│ _expire_overdue_taskflows    │                   │ prune → memory_flush →    │   │
│ _scan_stale_waiting_taskflows│                   │ summary (+TaskFlow)       │   │
└──────────────────────────────┘                   │                           │   │
                                                   └───────────┬───────────────┘   │
                                                               │ clear_session      │
                                                               ▼                    │
                                                   ┌───────────────────────────┐    │
                                                   │ session_continuity JSON   │────┘
                                                   └───────────────────────────┘
```

コンパイル済みグラフは **`agent/wrapper/`** パッケージがラップし、同パッケージがガードを所有します。`agent.wrapper.registry` はプロセス全体の、順序付きでプラグ可能なチェーン（`register_graph_wrapper`、`unregister_graph_wrapper`、`apply_graph_wrappers`、`reset_graph_wrappers`）を公開し、`GraphWrapperFactory` エントリは**最内優先**で適用されます；既定エントリは `RepetitionGuardWrapper(phantom_stream_guard=True)`、次に `ContextLimitGuardWrapper(context_window=main_llm_max_tokens)` です。ストリーム重複ガードは `agent/wrapper/repetition_guard.py`、コンテキストウィンドウガードは `agent/wrapper/context_limit.py` にあります。**メモリ還流**は `agent/middlewares/subagent_completion_drain/core.py` の `SubagentCompletionDrainMiddleware` が実行します。

## 📚 API リファレンス

### TaskFlow ツール

| ツール | シグネチャ | 戻り値 |
| :--- | :--- | :--- |
| `taskflow_create` | `(flow_id, description="", initial_state=None, session_id, deadline_hours=None)` | 作成した id/ステータス/リビジョン（+ 締め切り） |
| `taskflow_run_task` | `(flow_id, task, label=None, expected_revision=None, depends_on=None, validation_criteria=None, retry_policy=None, session_id)` | ディスパッチ済みステップ、または未充足依存付き `blocked` |
| `taskflow_dispatch` | `(flow_id, step_ids, expected_revision=None, session_id)` | ディスパッチ済み step id + リビジョン |
| `taskflow_wait_all` | `(flow_id, timeout_seconds=300.0, poll_interval_seconds=0.5, session_id)` | ステップごとの確定レポート（完全または部分；ポリシー付きステップを自動再試行） |
| `taskflow_resume` | `(flow_id, child_session_key="", result="", expected_revision=None, token_usage=None, validation_criteria=None, session_id)` | 再開後ステータス、アンロック済みステップ、ステップ状態カウント、基準エコー、再試行ノート |
| `taskflow_set_waiting` | `(flow_id, wait_reason="", expected_revision=None, session_id)` | waiting ステータス + リビジョン |
| `taskflow_summary` | `(flow_id, session_id)` | 待機/締め切り状態を含む完全な flow 状態 |
| `taskflow_progress` | `(flow_id, session_id)` | 完了率、内訳、次のステップ、残り推定 |
| `taskflow_budget` | `(flow_id, action="query", token_budget=None, expected_revision=None, session_id)` | 予算レポート、または設定確認 |
| `taskflow_list` | `(status_filter="active", session_id)` | このセッションのボード（`active` / `all` / ステータス名） |
| `taskflow_finish` | `(flow_id, summary="", expected_revision=None, session_id)` | 終端 `done` |
| `taskflow_fail` | `(flow_id, reason="", expected_revision=None, session_id)` | 終端 `failed` |
| `taskflow_cancel` | `(flow_id, reason="", expected_revision=None, session_id)` | 終端 `cancelled` |

### memory ツールのアクション

| アクション | シグネチャ | 戻り値 |
| :--- | :--- | :--- |
| `add` / `replace` / `remove` | `memory(action, target="memory"\|"user", content, old_text)` | JSON 成功/エラー |

### 主要な関数と定数

| シンボル | 場所 | 役割 |
| :--- | :--- | :--- |
| `TaskFlowStatus` / `StepStatus` | `agent/tools/taskflow/config.py:11,21` | ライフサイクル / DAG 列挙 |
| `update_flow` | `agent/tools/taskflow/registry/store_sqlite.py` | 楽観的ロック付きセッションスコープ変更 |
| `get_active_flows_sync` | `agent/tools/taskflow/registry/store_sqlite.py` | セッションスコープのアクティブ flow 読み取り（SQL `session_id` フィルタ） |
| `get_overdue_flows` / `get_waiting_flows` | `store_sqlite.py` | sweeper クエリ（意図的にセッション横断） |
| `deps_satisfied` / `unlock_dependents` | `agent/tools/taskflow/tools/_shared.py:99,137` | DAG 遷移 |
| `update_flow_with_conflict_retry` | `_shared.py:191` | 生成済みの子を失わない永続化 |
| `_expire_overdue_taskflows` | `agent/tools/subagent/registry/sweeper.py:123` | 締め切りの執行 |
| `_scan_stale_waiting_taskflows` | `sweeper.py:154` | アイドル検出マーカー |
| `_get_taskflow_context_sync` | `agent/middlewares/summarization/core.py:262` | 要約連携 |
| `_build_taskflow_block` | `workspace/prompt_builder.py:112` | 自動再開プロンプトブロック |
| `prune_tool_outputs` | `pub/func/message/tool_output_prune.py:103` | ツール出力の一行要約 |
| `auto_save_on_session_end` | `context_engine/session_continuity.py:117` | 継続性保存フック |
| `should_flush` / `run_memory_flush` | `agent/middlewares/summarization/memory_flush.py:43,65` | 圧縮前フラッシュ |
| `append_entries` | `agent/tools/memory.py:281` | MEMORY.md への一括追記 |
| `get_all_flows_sync` | `agent/tools/taskflow/registry/store_sqlite.py` | セッションボード読み取り（SQL `session_id` フィルタ） |
| `classify_failure` / `should_retry_failure` | `agent/tools/taskflow/tools/_retry.py:56,103` | 失敗分類 |
| `plan_settled_retries` / `persist_retry_actions` | `agent/tools/taskflow/tools/_retry.py:199,254` | wait_all 再試行の計画/永続化 |
| `_backflow_shared_memory` | `agent/middlewares/subagent_completion_drain/core.py:68` | メモリ還流の照合 |
| `apply_graph_wrappers` | `agent/wrapper/registry.py:69` | プラグ可能なグラフラッパーチェーン |

## 🧪 テスト

TaskFlow スイートは `tests/agent/tools/taskflow/` にあります（17 個の `unit` テストファイル + 共有 `conftest.py`）：

| テストファイル | カバー内容 |
| :--- | :--- |
| `test_store_sqlite.py` | CRUD、リビジョン増分、楽観的並行性競合、WAL、同期アクセサ、active/waiting/terminal フィルタ |
| `test_step_graph.py` | `deps_satisfied`、`mark_step_done`、`unlock_dependents`、旧ステータス導出、自己依存ガード |
| `test_summary_dag.py` | `taskflow_summary` の DAG 描画 |
| `test_resume_dag.py` | 再開で done 化、後続アンロック、部分完了、冪等な空操作 |
| `test_taskflow_tools.py` | 再起動を跨ぐ create→run→resume→finish、競合、終端遷移 |
| `test_taskflow_dispatch.py` | 一括ディスパッチ、オール・オア・ナッシング検証、バッチ途中失敗の永続化 |
| `test_dag_e2e.py` | プロセス再起動を跨ぐ完全並列 DAG flow |
| `test_conflict_persistence.py` | 競合後の生成済み子の永続化、再試行枯渇 |
| `test_taskflow_wait_all.py` | flow スコープ待機、タイムアウト部分レポート、無関係な生存子 |
| `test_run_task_dag.py` | ブロック登録、充足後ディスパッチ、未知依存エラー |
| `test_taskflow_progress.py` | 完了率/内訳/次のステップ/残り推定/waiting |
| `test_token_budget.py` | トークン集計、コスト計算、予算照会/設定/警告/超過 |
| `test_deadline.py` | `deadline_hours`、要約描画、sweeper 失効 |
| `test_idle_detection.py` | active/stale 待機状態、sweeper マーカー、生存子スキップ |
| `test_retry_policy.py` | ポリシー検証、失敗分類、再ディスパッチ、枯渇 |
| `test_validation.py` | 基準の保存、再開エコー、上書き |
| `test_taskflow_list.py` | セッションボード描画、ステータスフィルタ、最終活動タイムスタンプ |

横断スイート：`tests/agent/middlewares/test_memory_flush.py`（フラッシュ閾値と `append_entries`）、`tests/agent/middlewares/test_lt5_memory_backflow.py`（完了排出時のメモリ照合）、`tests/agent/middlewares/test_subagent_completion_drain_reminder.py`（完了キャリア検証リマインダー）、`tests/context_engine/test_session_continuity.py`（継続性の保存/プロンプト）、`tests/agent/middlewares/test_todo_continuation.py`（ターン終了時の継続）、`tests/pub/func/message/test_tool_output_prune.py`（一行要約）、`tests/workspace/test_prompt_builder_taskflow.py`（保留 flow のプロンプト注入）。

標準の uv/pytest ツールでこの領域だけを実行：

```bash
uv run pytest tests/agent/tools/taskflow -q
uv run pytest tests/agent/middlewares/test_memory_flush.py tests/context_engine/test_session_continuity.py -q
uv run pytest tests/pub/func/message/test_tool_output_prune.py -q
```

完全なプロセス分離スイートには `uv run python tests/run_tests_split.py` を使います（Group A が `unit` ファイル、Group B が `module`/`integration` ファイルを実行）。

## ⚠️ 既知の制限

- **`done` は成功ではない。** ステップの `done` は「結果が注入された」ことだけを意味し、`failed`/`skipped` のステップ状態は存在しません。子がエラーを報告しても `taskflow_resume` はステップを `done` にして後続をアンロックします。失敗を認識するステップ遷移は意図的に先送りされています。
- **`taskflow_wait_all` は設計上 flow スコープ。** 指定 flow のディスパッチ済みステップに記録された子だけを待ち、未知/クリーンアップ済みの run は確定とみなします。「すべてのアクティブ flow を待つ」グローバルプリミティブはありません。
- **アイドル検出は助言的。** sweeper は `stale_detected_at` / `stale_child_session_key` を `wait_json` に刻みますが、古い `waiting` flow を自動で失敗させることはありません。人間かモデルがマーカーに基づいて行動する必要があります。
- **圧縮前メモリフラッシュは潜在状態。** `Summarization` の本番インスタンス（メイン/サブ）は `memory_store` / `llm_factory` を渡さないため、呼び出し箇所が配線するまでフラッシュは実行されません。コードは実装・テスト済みですが現在は不活性です。
- **継続性はチャネル依存。** `build_continuity_prompt` は channel id と chat id の両方を必要とするため、チャネルバインディングのないセッションは継続性ブロックを受け取りません。ストレージはディスク上のキー別 JSON であり、データベースではありません。
- **アクティブ flow スキャンが 3 重複。** `prompt_builder._build_taskflow_block`、`summarization._get_taskflow_context_sync`、`session_continuity._get_active_taskflow_ids_sync` が同じクエリを独立実装しています；同期を保つ必要があります。
- **レジストリ規模は 39。** 設定レジストリは 39 個の feature オブジェクト（エージェント側 20 + インフラ側 19）を保持します；インフラ側の契約テストはそのうち 18 個（GATEWAY + 17 のデータ駆動ケース）をカバーし、`MODEL_PRICING` を省いています。
- **パッケージ再エクスポートの欠落。** `agent/tools/taskflow/__init__.py` は 11 個の名前しか再エクスポートしません；`taskflow_dispatch` と `taskflow_wait_all` は `build_taskflow_tools()` 経由で到達できますが、パッケージ `__all__` から漏れています。
- **TaskFlow ブロックは LLM プロンプト専用。** LLM 失敗時に使われる決定論的フォールバック要約は `## Current TaskFlow State` を含みません。
- **トークン会計は呼び出し側提供。** コストは `taskflow_resume` が `token_usage` 辞書を受け取ったときだけ計算されます；無しで注入されたステップはゼロトークン・ゼロコストに貢献します。
- **結果検証は助言的。** `validation_criteria` は保存され結果と共にエコーされますが、ツールが強制することはありません；合否はオーケストレータ自身が判断する必要があります。基準未達でステップを失敗させられる自動ゲートはありません。
- **再試行分類はテキストベース。** `classify_failure` は結果テキストに対する部分文字列ヒューリスティックです：パターン表の外の言い回しの失敗（または否定フレーズに隠れた真の失敗）は再試行を引き起こさず、空の `retry_on` は分類されたすべての失敗を再試行します。`taskflow_wait_all` は結果テキストの無い死亡した子を分類できないため、予算が残る限り常に再試行予算を消費します。
- **`taskflow_list` はセッションスコープ。** グローバルなセッション横断ボードは存在しません：すべての読み取りが所有 `session_id` で SQL フィルタされるため、あるセッションが別のセッションの flow を列挙することはできません。分離前の行（`session_id = ''`）はセッション読み取りからは見えませんが、sweeper のセッション横断締め切り/アイドルスキャンは解決します。
- **Knowledge の保存は計画名ではなく計画アイデンティティをキーとします。** アクセスは所有権チェック（`ownership.is_plan_associated()`：セッションの `plan_ref`、todo の `plan_ref`、または `session_ids` に当該セッションを含む boulder work）で制御され、保存ディレクトリは正規化された計画パスから導出されます——`workspace/knowledge/plans/<plan_key>/`、`plan_key = sha1(リポジトリルート相対パス)[:12]`、各ディレクトリの `meta.json` に可読な `plan_name` / `plan_ref` を記録します。**計画ファイルが異なる**同名計画は 2 セッション間で**物理的に隔離**され（各自の key ディレクトリへ書き込み）、boulder `session_ids` で**1 つの計画ファイル**を協業するセッションは同じパスに解決され 1 つのディレクトリを共有します。計画ファイルが解決できないセッションはフォールバックアイデンティティ `session-<sha1(session_id)[:8]>` に書き込みます（全 id ハッシュにより先頭 8 文字が同じセッション id も衝突しません）。レガシーの名前キー・ディレクトリは読み取り可能なまま；書き込みは常に key ディレクトリへ。`clear_session` はセッション私有のアイデンティティディレクトリを削除し、他セッションと共有された計画は保持します。サブエージェント境界は絶対的のままです——`knowledge` は `main_only` です。
