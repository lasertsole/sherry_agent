[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

---

# Subagent システム

> 経験ナレッジグラフ連携を備えた階層型タスク分解・並列実行サブシステム。

## 概要

**Subagent システム**は AI Agent が複雑なタスクを分解し、バックグラウンドでサブタスクを並列実行し、メッセージバスを通じて結果を非同期に返すことを可能にします。**経験ナレッジグラフのクローズドループ**を備えています：ドラフト → 蒸留 → 取り込み → リコール → アセンブル。

コアレイヤー：

- **`SubagentManager`** — バックグラウンドのサブエージェントタスクのライフサイクルを管理するシングルトンオーケストレーター。
- **`Commander`** — タスクごとに作成される LangGraph エージェントで、ワークを計画・分解・Worker へ配信します。
- **Distiller** — タスク後の蒸留エンジンで、再利用可能な経験を抽出してナレッジグラフに書き込みます。
- **Draft ツール** — タスク実行中に重要な発見を記録するための Agent 呼び出し可能なツール。

## アーキテクチャ

```
ユーザー / メイン Agent
       │
       ▼
┌──────────────────────────────────────────────────────────────┐
│                     SubagentManager                          │
│  (シングルトン、ライフサイクルオーケストレーター)              │
│                                                              │
│  _run_subagent() フロー:                                    │
│    1. ナレッジグラフをリコール → Commander に AIMessage 注入  │
│    2. Commander がタスクを実行 (ツール: todo_writer, worker,  │
│       draft)                                                 │
│    3. 結果をバスに公開 (プラン C)                            │
│    4. 経験をナレッジグラフへ蒸留                              │
│    5. ランタイムレジスタをクリア                              │
└──────────────────────────────────────────────────────────────┘
       │ 作成
       ▼
┌──────────────────────────────────────────────────────────────┐
│                      Commander エージェント                  │
│  (LangGraph、タスク別インスタンス)                            │
│                                                              │
│  ツール:                                                     │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐                 │
│  │TodoWriter│  │  Worker  │  │  Draft   │                  │
│  │(todo.md  │  │(並列     │  │(発見を   │                  │
│  │ 書き込み) │  │ 配信)    │  │ 記録)    │                  │
│  └──────────┘  └────┬─────┘  └──────────┘                 │
│                      │                                       │
│  ミドルウェア:       │                                       │
│  ┌───────────────┐   │                                       │
│  │Summarization  │   │                                       │
│  ├───────────────┤   │                                       │
│  │TODOManager    │   │                                       │
│  │(注入+クリーン)│   │                                       │
│  ├───────────────┤   │                                       │
│  │ToolCallNorm   │   │                                       │
│  ├───────────────┤   │                                       │
│  │IterationBudget│   │                                       │
│  ├───────────────┤   │                                       │
│  │ToolGuardrails │   │                                       │
│  └───────────────┘   │                                       │
└──────────────────────┼──────────────────────────────────────┘
                        │ 配信
                        ▼
                ┌────────────────┐
                │ Worker エージェント│
                │ (codeact_agent)│
                │ Worker エージェント│
                │ ... (並列)      │
                └────────────────┘
                        │
                        ▼ タスク後
┌──────────────────────────────────────────────────────────────┐
│              経験ナレッジグラフ                               │
│                                                              │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐        │
│  │ Draft ツール │→ │  Distiller  │→ │  ナレッジ   │         │
│  │(タスク中に  │  │(補助 LLM    │  │  グラフ     │         │
│  │ メモ記録)   │  │ 抽出)       │  │(ノード/エッジ│         │
│  └─────────────┘  └─────────────┘  └──────┬──────┘        │
│                                              │ リコール       │
│  ┌───────────────────────────────────────────┘               │
│  │  次のタスク: リコール → アセンブル → AIMessage 注入       │
│  └───────────────────────────────────────────────────────────│
│                                                              │
│  DB ロール:                                                  │
│    default → 経験グラフ (戦略レベル)                         │
│    worker  → 経験グラフ (運用レベル)                         │
└──────────────────────────────────────────────────────────────┘
```

## モジュール構造

```
subagent/
├── __init__.py              # エクスポート: build_subagent_tool
├── base.py                  # SubagentManager — シングルトンオーケストレーター + 蒸留
├── core.py                  # @tool subagent_tool — 非同期スパウンインターフェース
├── type.py                  # SubAgentOutput — pydantic データモデル
├── draft.py                 # Draft @tool — 重要な発見の記録 + ヘルパー関数
├── distiller.py             # Distiller — タスク後の経験蒸留
├── commander/
│   ├── __init__.py          # エクスポート: build_commander
│   ├── core.py              # build_commander() — LangGraph エージェント作成
│   ├── tools/
│   │   ├── todo_writer.py   # TodoWriter — todo.md ファイル書き込み
│   │   └── worker/
│   │       ├── core.py      # Worker — 並列サブタスク配信
│   │       └── middlewares/
│   │           └── WorkerSummarization.py
│   └── middlewares/
│       └── core.py          # TODOManager — todo コンテキスト注入 + アーカイブ
├── templates/
│   └── subagent_announce.md # 結果通知用 Jinja2 テンプレート
├── README.md
└── README.zh.md
```

## 経験ナレッジグラフのクローズドループ

### フロー

```
1. タスク実行:  Commander/Worker が draft_tool を呼び出す → state_register_db
2. タスク完了:  bus.publish → distill_and_ingest → Register.clear_all
3. 蒸留:        auxiliary_llm がドラフト+結果からノード/エッジを抽出
4. 取り込み:    戦略 → ナレッジグラフ("default")、運用 → ナレッジグラフ("worker")
5. 次のタスク:  recall(task) → assemble_context → AIMessage 注入
```

### Draft ツール

`draft` は Commander・Worker・メイン Agent すべてが呼び出せる `@tool` 関数です：

```python
@tool
def draft(
    key_points: str,
    category: Literal["strategy", "obstacle", "tool_pattern", "insight"],
    session_id: Annotated[str, InjectedState("session_id")] = "",
) -> str
```

ヘルパー関数 (distiller が使用):
- `get_drafts(session_id)` — すべてのドラフトエントリを読み取る
- `append_drafts(session_id, drafts)` — Worker のドラフトを Commander セッションにマージ
- `clear_drafts(session_id)` — 蒸留後にドラフトエントリをクリア

### Distiller

`distill_and_ingest()` は各サブエージェントタスク後に実行されます (プラン C の順序):

1. **戦略蒸留** → `get_instance("default").ingest_experiences()` (Commander レベルのパターン)
2. **運用蒸留** → `get_instance("worker").ingest_experiences()` (Worker レベルの技法)

Worker のドラフトは蒸留前に Commander セッションにマージされます。

### ナレッジグラフ注入

`agent.ainvoke()` の前に、リコールされた経験が `AIMessage` として注入されます：

```python
messages = [HumanMessage(content=task)]
# ナレッジグラフからリコール
if recall_result["nodes"]:
    assembled = assemble_context(db, nodes, edges)
    messages.append(AIMessage(content=f"徊\n{system_prompt}\n\n{xml}\n徊"))
```

- **Commander**: 経験グラフからリコール (戦略レベル)
- **Worker**: 経験グラフからリコール (運用レベル)

## データモデル

### `SubAgentOutput`

```python
class SubAgentOutput(BaseModel):
    status: Literal["ok", "failed"]           # タスク成功/失敗
    finish_reason: str                        # 完了理由 (失敗時はエラー詳細を含む)
    result: str                               # 結果または結果保存パス
```

## SubagentManager ライフサイクル

### プラン C: 公開 → 蒸留 → クリア

Commander の実行完了後 (成功・タイムアウト・エラー):

```
1. 結果をバスに公開 (ユーザーは即座に通知を受ける)
2. distill_and_ingest() (ドラフトはまだ state_register_db にある)
3. Register.clear_all_register_sessions() (クリーンアップ、ドラフトも消去)
```

ユーザーが迅速に結果を受け取る一方、ドラフトは蒸留が終わるまで保持されることを保証します。

### スパウン → 実行 → 通知

```
spawn(task, session_id)
  │
  ├─ task_id 生成 (タイムスタンプベース)
  ├─ asyncio タスク作成 (_run_subagent)
  ├─ _running_tasks と _session_tasks で追跡
  ├─ _cleanup コールバックを登録
  └─ "started" メッセージを返す

_run_subagent(session_id, task_id, task, label)
  │
  ├─ Commander ナレッジグラフをリコール → AIMessage 注入で messages を構築
  ├─ Commander エージェントを構築
  ├─ agent.ainvoke({messages: [HumanMessage(task), AIMessage(knowledge)]})
  ├─ SubAgentOutput で通知テンプレートをレンダリング
  ├─ InboundMessage をバスに公開
  ├─ distill_and_ingest() → 経験をナレッジグラフに抽出
  └─ Register.clear_all_register_sessions()
```

### サービーモード

`start_service()` は `_consume_loop()` を起動し、これは:
1. バスからの `InboundMessage` を待機します。
2. キャラクターのペルソナを通じて結果を再パーソナライズします。
3. 登録された `_consumer` コールバックに転送します。

## Commander エージェント

### 構築

`build_commander()` は LangGraph エージェントを構築します:

| コンポーネント | 詳細 |
|-----------|---------|
| **システムプロンプト** | タスク分解、並列化、動的プラン調整、ドラフト記録 |
| **モデル** | `main_llm` (プロジェクト共有モデル) |
| **チェックポインター** | `InMemorySaver` |
| **ツール** | `todo_writer` + `worker` + `draft` |
| **ミドルウェア** | `SummarizationMiddleware` (15 でトリガー、8 保持) + `TODOManager` + `ToolCallNormalize` + `IterationBudget` + `ToolGuardrails` |
| **応答形式** | `SubAgentOutput` 構造化出力 |

## Commander ミドルウェア

### TODOManager (TodoInjector + TodoCleaner を置き換え)

- **`abefore_model`**: `todo/{task_id}.md` を読み込み、`[SYSTEM CONTEXT - TODO LIST UPDATE]` として注入します。
- **`aafter_agent`**: todo ファイルを `todo_archive/` にアーカイブするか、削除します。

### ToolCallNormalize

要約によるメッセージ削減後の孤立 tool_call を修正します。

### IterationBudget

タスクごとのエージェント反復回数を制限します。

### ToolGuardrails

ツール呼び出しが安全ルールを遵守するかを検証します。

## Worker エージェント

Worker は `codeact_agent` インスタンス (LangGraph エージェントではない) であり:

- **ツール**: `build_worker_tools()` (サブエージェント専用を除く全ツール、`draft` を含む)
- **ミドルウェア**: `WorkerSummarization` + `HeartbeatStaleness` + `IterationBudget`
- **応答形式**: `SubAgentOutput`
- **ナレッジグラフ注入**: 実行前に経験グラフからリコール
- **ドラフトマージ**: Worker のドラフトは `finally` ブロックで Commander セッションにマージ

## FAQ

### distiller はなぜナレッジグラフモジュールから分離されたのですか？

`distiller.py` は元々ナレッジグラフ抽出器の中にありましたが、`draft.py` (subagent レイヤー) を import するため逆依存関係が生じていました: ナレッジグラフ基盤 → subagent ビジネスロジック。distiller を `subagent/` に移すことで依存方向が一方向になります: `subagent/distiller` → ナレッジグラフ ✓

### なぜプラン C (公開 → 蒸留 → クリア) なのですか？

ユーザーは即座に結果を受け取るべきです。蒸留は `state_register_db` のドラフトデータを必要とし、`Register.clear_all` が先に実行されるとドラフトが失われます。プラン C は両方を保証します: 迅速な配信 + 完全な蒸留。

### 蒸留が失敗したらどうなりますか？

蒸留は `try/except` で包まれており、失敗時は警告ログのみ記録され、すでにユーザーに公開された結果には影響しません。

### Worker のドラフトはどのように収集されますか？

`_arun_task` の `finally` ブロックで、`get_drafts(worker_session_id)` により Worker ドラフトを読み取り、`append_drafts(commander_session_id, ...)` で Commander セッションにマージします。distiller は Commander セッションから一様に読み取ります。

## 技術スタック

| レイヤー | 技術 |
|-------|-----------|
| エージェントフレームワーク | LangGraph (`CompiledStateGraph`) + codeact_agent |
| LLM | `main_llm` (共有)、`auxiliary_llm` (蒸留) |
| チェックポインティング | `InMemorySaver` |
| ミドルウェア | `@before_model` / `@after_agent` デコレーター |
| ナレッジグラフ | 経験グラフ (SQLite + FTS5 + ベクトル検索 + PageRank) |
| 非同期 | `asyncio.create_task`, `asyncio.gather`, `asyncio.wait_for` |
| データ検証 | Pydantic v2 |
| テンプレーティング | カスタム `render_template_file()` (Jinja2 スタイル) |
| メッセージバス | プロジェクト内部 `MessageBus` / `InboundMessage` |
| 状態管理 | `state_register_db` (SQLite)、`state_register_mem` (インメモリ) |
