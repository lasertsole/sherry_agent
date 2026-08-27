# Future Subagent — Pythonサブエージェントシステム

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> 既存の `agent/tools/subagent/`（Commander/Worker パターン）と共存する、多層サブエージェントシステムの Python 実装です。7 つの実装フェーズすべて + robustness-plan-v3 拡張 + バグ修正 + OpenClaw 整合 + 深さ整合 + 配線修正が完了しています。203 のテストに合格。

## クイックナビゲーション

| ドキュメント | 目的 |
|----------|---------|
| [AGENTS.md](./AGENTS.md) | **エントリーポイント** — プロジェクト規約、現在の進行状況 |
| [architecture.md](./docs/architecture.md) | 全体アーキテクチャ、ディレクトリ構造、モジュール依存関係グラフ |
| [decisions.md](./docs/decisions.md) | 主要な技術決定記録（22 件） |
| [integration.md](./docs/integration.md) | 既存システムとの統合計画 |

---

## 実行原則

### 1. システム概要

サブエージェントシステムの核となる目的は、メインエージェントが複雑なタスクを並列サブタスクに分解し、独立した子エージェントにディスパッチし、完了時に結果を親エージェントへ確実に配信できるようにすることです。システム全体は次の 3 つのコアパイプラインで駆動されます。

```
┌──────────────────────────────────────────────────────────────────┐
│  Parent Agent (LangGraph CompiledStateGraph)                     │
│    │                                                             │
│    ├─ 1. sessions_spawn ──► Spawn Pipeline ──► Child Agent Async │
│    │                                                             │
│    ├─ 2. sessions_yield ──► Pause current turn, await children   │
│    │                                                             │
│    ├─ 3. sessions_send  ──► A2A Bidirectional (via EventBus)    │
│    │                                                             │
│    └─ 4. Child completes ──► Announce Pipeline ──► Deliver via  │
│                              EventBus + Registry lifecycle       │
└──────────────────────────────────────────────────────────────────┘
```

### 2. スポーンパイプライン — 子エージェント作成とディスパッチ

`spawn_subagent_direct()` がシステムのエントリーポイントです。LLM が `sessions_spawn` ツールを呼び出すと、次のフローが実行されます。

```
spawn_subagent_direct(task, requester_session_key, agent_id, mode, ...)
  │
  ├── 1. Validation Phase
  │     ├── Validate task is non-empty
  │     ├── Normalize task_name (replace non-alphanumeric with _, truncate to 64 chars)
  │     ├── Validate target_policy (agent_id in allow_agents whitelist)
  │     ├── Compute depth: parent_depth + 1, validate ≤ max_spawn_depth (default 3)
  │     ├── Validate concurrency: active children < max_children_per_agent (default 5)
  │     └── Validate runtime isolation (cross-runtime spawn blocked)
  │
  ├── 2. Role & Capability Resolution
  │     └── resolve_subagent_capabilities(depth, max_depth)
  │           ├── depth == 0       → MAIN,       control_scope=CHILDREN
  │           ├── 0 < depth < max  → ORCHESTRATOR, control_scope=CHILDREN
  │           └── depth >= max     → LEAF,        control_scope=NONE
  │
  ├── 3. Context Preparation
  │     ├── Thinking level override resolution (plan.py)
  │     ├── Attachment materialization to disk (attachments.py)
  │     │     with safety checks: path traversal, size limits, file count
  │     ├── Tool policy: DEFAULT_SUBAGENT_BLOCKED_TOOLS = [sessions_spawn,
  │     │   sessions_yield, skill_manage, memory]
  │     │   ORCHESTRATOR role auto-unblocks sessions_spawn and sessions_yield
  │     ├── Context mode: ISOLATED (empty context) or FORK (copy parent
  │     │       transcript via agent.aget_state() — Decision 9)
  │     ├── Thread binding: SESSION mode → bind_thread_for_subagent_spawn()
  │     │       creates channel thread + delivery_origin (Decision 11)
  │     ├── Runtime isolation: resolve_runtime_isolation() + cwd validation
  │     │       (Decision 15)
  │     ├── Origin routing: resolve_requester_origin_for_child()
  │     └── Scope resolution: resolve_least_privilege_scopes() by role
  │
  ├── 4. Run Registration
  │     ├── Generate child_session_key = "agent:{agent_id}:subagent:{uuid}"
  │     ├── Create SubagentRunRecord (UUID, execution=RUNNING, delivery=PENDING)
  │     ├── Store in memory dict + SQLite
  │     └── Register terminal generation (TerminalGenerationTracker)
  │
  ├── 5. Prompt Construction
  │     ├── build_subagent_system_prompt(role, task, ...)
  │     │   ├── 6-section structure: Your Role / Rules / Output Format /
  │     │   │     What You DON'T Do / Sub-Agent Spawning / Session Context
  │     │   ├── Anti-polling rule (no active status polling)
  │     │   ├── Truncation hint for output length
  │     │   ├── LEAF: structured output template from output_schema
  │     │   └── ORCHESTRATOR: "You MAY spawn further subagents via sessions_spawn."
  │     ├── Append attachment location hint to system prompt
  │     ├── Append structured output prompt from output_schema (swarm mode)
  │     └── build_subagent_initial_user_message(task, context)
  │           └── Structured envelope: [Subagent Context] / [Subagent Task] / [Subagent Additional Context]
  │
  ├── 6. Async Dispatch (Fire-and-Forget)
  │     └── asyncio.create_task(_execute_subagent(...))
  │
  └── 7. Immediate Return
        └── SpawnResult { status: "accepted", child_session_key, run_id }
```

#### 子エージェントの実行

`_execute_subagent()` は、子エージェントのライフサイクル全体を担うバックグラウンドの `asyncio.Task` です。

```
_execute_subagent(run, system_prompt, user_message, forked_messages, ...)
  │
  ├── 1. 子エージェントの構築
  │     ├── build_main_tools() を呼び出して全ツールを取得
  │     ├── tool_allow/tool_deny でフィルタ（拒否リストが優先）
  │     ├── LLM を構築: ORCHESTRATOR → build_main_llm()、LEAF → build_auxiliary_llm()
  │     ├── 独立した SQLite チェックポインターを作成
  │     └── 5つのミドルウェアを備えた create_agent():
  │           ├── Summarization(trigger=[fraction:0.5, messages:40, tokens:30000])
  │           ├── IterationBudget(60)     — 最大反復回数
  │           ├── ToolGuardrails()        — ツール安全性ガードレール
  │           ├── ToolCallNormalize()     — ツール呼び出し正規化
  │           └── HeartbeatStaleness()    — ハートビート監視
  │
  ├── 2. 実行
  │     ├── メッセージリストを組み立て: forked_messages + HumanMessage(user_message)
  │     └── await asyncio.wait_for(child_agent.ainvoke(...), timeout)
  │
  ├── 3. 結果抽出
  │     └── ainvoke が返した最後のメッセージから result_text を抽出
  │
  └── 4. Finally（成功・失敗にかかわらず常に実行）
        ├── TimeoutError → outcome = TIMEOUT
        ├── Exception   → outcome = ERROR
        ├── complete_run(run_id, outcome, result_text)  — Registry を更新
        │     └── result_text は 24000 文字に切り詰め
        └── run_subagent_announce_flow(updated_run)      — Announce をトリガー
```

### 3. Registry — 実行状態レジストリ

Registry はシステム全体の状態ハブであり、すべての子エージェントラン記録のライフサイクルを管理します。

#### ストレージアーキテクチャ

```
┌─────────────────────────────────────────────────┐
│  Memory Store (registry/memory.py)              │
│  threading.Lock で保護された dict[str, SubagentRunRecord]  │
│  ↓ 定期スナップショット                            │
│  SQLite (registry/store_sqlite.py)              │
│  agent/tools/subagent/data/subagent_registry.db │
│  Table: subagent_runs(run_id PK, data JSON)     │
└─────────────────────────────────────────────────┘
```

- メモリがプライマリストアで、すべての読み書きはインメモリ dict を直接対象にします
- SQLite は永続バックアップで、Sweeper が呼び出す `periodic_persist(interval=30s)` でスナップショットされます
- 起動時、`init_registry()` が SQLite から既存の記録を復元します
- 単一レコードの upsert/delete はリアルタイムで SQLite に同期されます

#### SubagentRunRecord の主要フィールド

| カテゴリ | フィールド | 説明 |
|----------|-------|-------------|
| **ID** | `run_id` | UUID、一意の識別子 |
| | `task_run_id` | steer/restart 全体で安定した ID |
| | `child_session_key` | `"agent:{agentId}:subagent:{uuid}"` |
| | `requester_session_key` | 親セッションキー |
| **スポーンパラメータ** | `spawn_mode` | RUN（ワンショット）/ SESSION（永続） |
| | `context_mode` | ISOLATED / FORK |
| | `depth` | ネスト深さ |
| | `role` | MAIN / ORCHESTRATOR / LEAF |
| **所有権** | `completion_owner_session_key` | 完了配信を所有するセッションキー |
| | `spawned_by` | スポーンを開始した ID |
| | `spawned_cwd` | スポーン時の作業ディレクトリ |
| **スコープ** | `scopes` | 付与された権限スコープ |
| | `inherited_tool_policy_version` | 継承されたツールポリシーのバージョン |
| **スキーマ** | `output_schema` | 構造化出力検証用の JSON Schema |
| **実行** | `execution.status` | RUNNING → INTERRUPTED → TERMINAL |
| | `execution.outcome` | OK / ERROR / TIMEOUT / UNKNOWN |
| **配信** | `delivery.status` | PENDING → IN_PROGRESS → DELIVERED |
| | `delivery.attempt_count` | 配信リトライ回数 |
| **添付** | `attachments_dir` | 添付ディレクトリの絶対パス |
| | `attachments_root_dir` | 安全なクリーンアップ検証用のルートディレクトリ |

### 4. 3つのコア状態機械

#### 1. ExecutionState — 実行状態機械

```
    RUNNING ──────────────────► INTERRUPTED
      │                            │
      │ (completed/error/timeout)  │ (resume)
      ▼                            │
    TERMINAL ◄─────────────────────┘
      ▲
      │ (restart)
      └────────────────────────────
```

- `RUNNING`: 子エージェントが実行中
- `INTERRUPTED`: yield/steer により一時停止
- `TERMINAL`: 最終状態（completed/error/timeout）、不可逆

#### 2. CompletionDeliveryState — 配信状態機械

```
    not_required ──(RUN mode skip)──► delivered

    pending ──► in_progress ──► delivered
                    │
                    ├──(failure)──► failed ──(retry)──► pending
                    │                               │
                    │     (retries exhausted + soft cap) │
                    │                               ▼
                    └──(soft cap exceeded)──► suspended ──► discarded
```

- `not_required`: SESSION モードでは配信不要
- `pending → in_progress → delivered`: 通常の配信パス
- `failed → pending`: 指数バックオフリトライ（1s、2s、4s、最大3回）
- `suspended → discarded`: 一時停止が制限を超えると破棄

#### 3. CleanupState — クリーンアップ状態機械

```
    registered ──► cleanup_handled ──► cleanup_completed_at
```

- `resolve_deferred_cleanup_decision()` がセッションを削除するか判定
- cleanup="delete" かつ配信が完了/破棄/不要 → 削除
- 配信が一時停止/失敗 → 保持
- 添付クリーンアップはシンボリックリンク回避保護付きの `safe_remove_attachments_dir()` を使用

### 5. Announce パイプライン — 結果通知と配信

子エージェントの実行が完了すると、Announce パイプラインが結果を親エージェントへ確実に配信します。

```
Child Agent execution completed
  │
  └──► run_subagent_announce_flow(run)
         │
         ├── 事前ガード
         │     ├── execution.status != TERMINAL → スキップ
         │     ├── completion.required == False → スキップ
         │     └── delivery.status == DELIVERED → スキップ（冪等性）
         │
          └──► deliver_subagent_announcement(run)
                │
                ├── 1. プロセス内冪等性チェック
                │     └── _is_already_delivered(run) → インメモリセットを確認
                │         key = "subagent_announce:{run_id}:gen:{generation}"
                │         セット上限 10K、満杯時は古いもの 5K を追い出し
                │
                ├── 2. ハードキャップチェック
                │     └── 保留中の子孫数 ≥ hard_cap(50) → 即 SUSPENDED
                │
                ├── 3. 子孫チェック
                │     └── リクエスターに保留中の子孫がある場合のみ wake を配信
                │
                ├── 4. IN_PROGRESS にマーク
                │
                ├── 5. リトライループ（最大3回）
                │     ├── _do_deliver(ctx)
                │     │     ├── InboundMessage を構築:
                │     │     │     channel = "system"
                │     │     │     sender_id = "subagent"
                │     │     │     chat_id = "direct"
                │     │     │     session_id = requester_session_key
                │     │     │     metadata.injected_event = "subagent_result"
                │     │     │     content = フォーマット済み結果（4Kで切り詰め）
                │     │     └── get_event_bus().publish_internal(msg)
                │     │     fire_delivery_target_hook() → リダイレクトを許可
                │     │
                │     ├── 成功 → DELIVERED をマーク + 冪等性キーを記録 → リターン
                │     ├── 一時的障害 → sleep [5s/10s/20s] → リトライ
                │     ├── 圧縮エラー → sleep [1s/2s/4s/8s] → リトライ
                │     └── 永続的障害 → リトライなし
               │
                ├── 6. リトライを使い切った
                │     ├── FAILED をマーク
                │     └── 保留数 ≥ soft_cap(25) → SUSPENDED をマーク
                │
                └── 7. クリーンアップ
                     └── cleanup="delete" → safe_remove_attachments_dir()
```

#### 配信メッセージフォーマット

```
**Subagent Result** [{label}]
Status: completed successfully / failed: {error} / timed out
Task: {task description}
Result:
{result_text, truncated at 4000 chars}
```

### 5.1 スウォーム/コレクトモード（v3)

スウォームシステムは、FIFO スケジューリングと並行性制御を備えたサブタスクの並行バッチ実行を可能にします。

```
configure_swarm_group(SwarmGroupConfig(group_id="g1", max_concurrent=3))
  │
  ├── reserve_swarm_run(group_id, task, requester)
  │     └── FIFO にエンキュー + state=RESERVED に設定
  │
  ├── activate_swarm_run(run_id)
  │     └── デキュー + state=ACTIVE に設定（max_concurrent を尊重）
  │
  ├── complete_swarm_run(run_id, outcome)
  │     └── state=COMPLETED/FAILED に設定 + 次の予約済みを自動アクティブ化
  │
  └── build_structured_output_prompt(output_schema)
        └── 構造化出力用の JSON スキーマプロンプトを生成

validate_structured_output(result_text, output_schema)
  │
  ├── result_text を JSON としてパース
  ├── 必須フィールドの存在をチェック
  ├── スキーマに対してフィールド型を検証
  └── (is_valid, error_message) を返す

SwarmGroupConfig フィールド: group_id, max_children_per_group (5), max_total_per_group (0=無制限), max_concurrent (3)

reserve_swarm_run(group_id, task, requester, launch_fingerprint=None)
  │
  ├── launch_fingerprint 指定 → _launch_fingerprints で冪等ヒットをチェック
  └── 新規ラン → FIFO にエンキュー + state=RESERVED に設定

_pump_lane(group_id)
  │
  ├── max_concurrent に対して利用可能なスロットをチェック
  ├── スロットが空くと予約済みランを自動アクティブ化
  └── アクティブ化時に _on_swarm_run_started コールバックをトリガー

onStartFailure 処理:
  │
  ├── ランを自動失敗させる（state=FAILED）
  └── 次のキューイング待ちの予約済みランを自動アクティブ化
```

### 5.2 配信デュアルパスルーティング（v3)

Announce 配信はリクエスターのタイプに応じてルーティングされるようになりました。

```
deliver_subagent_announcement(run)
  │
  ├── リクエスターがサブエージェント → _deliver_internal_injection()
  │     ├── metadata.internal = True
  │     ├── コンテンツ: "[Subagent Internal] {label}: {status}"
  │     └── ユーザー向け出力なし
  │
  └── リクエスターがユーザーセッション → _deliver_completion_message()
        ├── レビュー指示付きの完全マークダウン形式
        ├── コンテンツ: "**[Subagent Task]** [{label}]..."
        └── "請審閱以上子 Agent 執行結果，如需進一步操作請指示。"
```

### 5.3 ジェネレーションガード付きライフサイクルとキル調停（v3)

```
complete_subagent_run(run_id, outcome, expected_generation)
  │
  ├── TerminalGenerationTracker.is_callback_current()
  │     └── 期限切れのジェネレーションコールバックを拒否
  │
  ├── _arbitrate_kill_vs_completion(run, outcome)
  │     ├── kill_reconciliation なし → パススルー
  │     ├── Kill + 結果ありの Provider OK → Provider が勝つ
  │     └── Kill + その他の結果 → Kill が勝つ
  │
  ├── _should_suspend_pending_final_delivery()
  │     └── cleanup="keep" + complete + ok + expects + PENDING → 一時停止
  │
  └── _start_announce_cleanup_flow()
        ├── SettleWakeBatch: IDLE → COMPLETING → SETTLED → DONE
        └── ジェネレーションガード付きの遅延クリーンアップ
```

### 5.4 キルターゲット状態の解決と可視性（v3)

```
resolve_kill_target_state(run) → "killable" | "finalizing" | "terminal"

kill_subagent_run_with_cascade(run_id, cascade=True)
  │
  ├── ターゲット状態を解決
  │     ├── "terminal" → リターン（すでに完了）
  │     ├── "finalizing" → 1秒待って再チェック
  │     └── "killable" → キルを実行
  │
  ├── キル調停スナップショットを保存
  ├── タスクをキャンセル + セッションキューをクリア
  ├── cascade の場合: すべての子を再帰的にキル
  └── すべての子が確定したら親を wake

is_subagent_run_visible_to_session(run, session_key)
  ├── controller_session_key と一致 → 可視
  ├── requester_session_key と一致 → 可視
  └── それ以外 → 不可視
```

### 6. 深さと役割システム — 階層制御

サブエージェントシステムは多層ネストをサポートし、深さと役割を通じて再帰スポーン能力を制御します。

```
depth 0:  MAIN Agent
           ├── 子エージェントをスポーン可能
           └── control_scope = CHILDREN

depth 1:  ORCHESTRATOR（max_depth > 1 の場合）
           ├── 子エージェントのスポーンを継続可能
           └── control_scope = CHILDREN

depth 2:  ORCHESTRATOR（max_depth > 2 の場合）
           ├── 子エージェントのスポーンを継続可能
           └── control_scope = CHILDREN

depth N:  LEAF（depth == max_spawn_depth）
           ├── 子エージェントをスポーン不可
           └── control_scope = NONE
```

既定の `max_spawn_depth = 3` で、3 層ツリーを形成します: MAIN → ORCHESTRATOR → LEAF

**深さの計算**: `requester_session_key` から親の深さを抽出し、子の深さ = 親の深さ + 1。セッションキー形式 `"agent:{id}:subagent:{uuid}"` 内の `:subagent:` の出現回数が深さに等しくなります。

**ツールポリシーの結合**:
- LEAF ロールは `DEFAULT_SUBAGENT_BLOCKED_TOOLS`（`sessions_spawn`、`sessions_yield`、`skill_manage`、`memory`）によって完全に制限され、`sessions_spawn` を呼び出せません
- ORCHESTRATOR ロールは `sessions_spawn` と `sessions_yield` を自動的にブロック解除し、再帰スポーンを可能にします
- これにより、ネスト深さのハード制約を回避できないことが保証されます

### 7. 添付ファイルシステム

スポーンパイプラインは子エージェントへのファイル添付をサポートします。

```
materialize_subagent_attachments(attachments, child_workspace, ...)
  │
  ├── 1. 検証
  │     ├── ファイル名: パストラバーサルなし、制御文字（C0+DEL）なし、予約名なし、重複名なし
  │     ├── 数制限: スポーンあたり最大50ファイル
  │     ├── サイズ制限: ファイルあたり1MB、スポーンあたり合計5MB
  │     └── mount_path のサニタイズ: 英数字 + ._-/
  │
  ├── 2. 分離ディレクトリへの書き込み
  │     └── <childWorkspace>/.openclaw/attachments/<uuid>/
  │
  ├── 3. マニフェスト生成
  │     └── ファイル名、サイズ、SHA-256 ハッシュを含む .manifest.json
  │
  └── 4. システムプロンプト接尾辞を返す
        └── "Attachments: N file(s), M bytes. Available at: .openclaw/attachments/<uuid>"
```

### 8. バックグラウンドデーモン機構

#### Sweeper（レジストリスキャナー）

```
registry/sweeper.py — 60秒間隔ループ

各スイープで実行:
  1. recover_orphaned_runs()       — 孤児ランの回復
  2. finalize_suspended_deliveries() — 一時停止配信のリトライ/破棄
  3. persist_runs_to_disk()        — SQLite へのスナップショット
```

孤児の基準: `RUNNING` かつ `started_at` が None でない かつ 経過時間が階層化しきい値を超える（cron=2h、subagent=6h、interactive=24h）。Sweeper は詰まったラン（wedge）をスキップします。

#### Followup（タイムアウトチェッカー）

```
followup/core.py — sweeper_interval * 2 間隔ループ

各チェックで実行:
  1. すべてのランを反復
  2. run_timeout_seconds を超える RUNNING ランを見つける
  3. recover_orphaned_runs() を呼び出して強制回復
```

#### 孤児回復

```
orphan/recovery.py — run_id ごとの遅延スケジューリング

各孤児ランについて:
  1. delay_seconds（既定120秒）待機
  2. まだライブかつ未完了かチェック
  3. reconcile_orphaned_run() → TERMINAL + TIMEOUT にマーク
  4. run_subagent_announce_flow() をトリガー → 親エージェントへタイムアウト結果を配信
```

重複排除: 各 `run_id` は回復のために最大1回スケジュールされます。

### 9. LLM ツールインターフェース

#### sessions_spawn — 子エージェントの作成

| パラメータ | 型 | 既定 | 説明 |
|-----------|------|---------|-------------|
| `task` | str | 必須 | タスクの説明 |
| `task_name` | str\|None | None | 安定したエイリアス |
| `label` | str\|None | None | 表示ラベル |
| `agent_id` | str | "main" | ターゲットエージェント ID |
| `thinking` | str\|None | None | 思考モードのオーバーライド |
| `mode` | str | "run" | "run"（ワンショット）/ "session"（永続） |
| `cleanup` | str | "delete" | "delete" / "keep" |
| `context` | str | "isolated" | "isolated" / "fork" |
| `attachments` | list\|None | None | ファイル添付（name、content、encoding、mount_path） |

戻り値: `"Subagent spawned: status={status}, run_id={id}, session_key={key}"`

#### sessions_yield — 一時停止と待機

メインエージェントに現在のターンを終了し、子結果の到着を待つように通知します。これは**シグナルツール**です — スレッドをブロックせず、現在のターンを一時停止できることをフレームワークに通知します。

戻り値: `"Turn yielded. You will be resumed when subagent results arrive."`

#### sessions_send — 双方向通信

| パラメータ | 型 | 説明 |
|-----------|------|-------------|
| `target_session_key` | str | ターゲット子エージェントのセッションキー |
| `message` | str | メッセージの内容 |
| `max_turns` | int | 最大ラウンド数（既定1） |

`get_event_bus().publish_internal()` と `metadata.injected_event = "subagent_message"` を使用して対象メッセージを配信します。

#### agents_list — 利用可能なエージェントリスト

設定から `allow_agents` 許可リストを返します。

#### subagents_list — 子エージェントの状態リスト

現在のセッション配下のアクティブおよび直近の子エージェントを返します:

```
Subagents: total=5, active=3, recent=2

Active:
  - [abc12345] research (depth=1, role=leaf, model=gpt-4, runtime=30s, pending=0)
  - [def67890] analysis (depth=1, role=leaf, model=gpt-4, runtime=2.5m, pending=0)
  - [ghi11223] writer (depth=1, role=orchestrator, model=gpt-4, runtime=1.2h, pending=2)

Recent:
  - [jkl44556] lookup status=ok runtime=45s
  - [mno77889] verify status=timeout runtime=5.0m
```

#### sessions_kill — 子エージェントのキャンセル

| パラメータ | 型 | 既定 | 説明 |
|-----------|------|---------|-------------|
| `run_id` | str | 必須 | キルするラン ID |
| `reason` | str | "killed" | キルの理由 |

実行中の子エージェントをキャンセルします。キルできるのはコントローラーセッションのみです。カスケードキル（すべての子を再帰的にキル）をサポートします。キル調停は同時進行する完了と仲裁します。

`kill_all_controlled_subagent_runs(requester_session_key)` — セッションのキル可能な子をすべて1回でキルします。

#### sessions_steer — 子エージェントのステア/再起動

| パラメータ | 型 | 既定 | 説明 |
|-----------|------|---------|-------------|
| `run_id` | str | 必須 | ステアするラン ID |
| `new_instructions` | str | 必須 | 注入する新しい指示 |

実行中の子エージェントに新しい指示を注入します。ランの状態は `pause_reason="steer"` とともに INTERRUPTED に遷移し、`generation` が増加します。

### 10. フックプロトコル

フック機構により、外部コードが子エージェントのライフサイクルイベントをリッスンできます:

```python
from agent.tools.subagent.hooks.base import register_start_hook, register_stop_hook
from agent.tools.subagent.hooks.progress import register_spawned_hook, register_ended_hook, register_delivery_target_hook

async def on_start(event: SubagentStartEvent):
    print(f"Subagent started: {event.child_session_key}")

async def on_stop(event: SubagentStopEvent):
    print(f"Subagent stopped: {event.child_status}")

async def on_delivery_target(run, target_session_key):
    return None  # return a session_key to redirect, or None

register_start_hook(on_start)
register_stop_hook(on_stop)
register_delivery_target_hook(on_delivery_target)
```

| イベント | フィールド |
|-------|--------|
| `SubagentStartEvent` | `parent_session_key`, `child_session_key`, `child_role`, `child_goal` |
| `SubagentStopEvent` | `parent_session_key`, `child_session_key`, `child_role`, `child_status`, `child_summary`, `duration_ms` |

フックは登録順に順次実行され、例外は飲み込まれてフローを中断しません。

### 11. 旧サブエージェントシステムとの共存

このシステムは既存の `agent/tools/subagent/`（Commander/Worker パターン）と同位で共存します。

| 属性 | 旧 `agent/tools/subagent/` | 新 `agent/tools/subagent/` |
|----------|------------------------------|---------------------------|
| 言語 | TypeScript | Python |
| スポーンモデル | "Spawn Tool" パターン | "Async Direct" パターン |
| 通信 | 単方向戻り値 | 双方向（`sessions_send`） |
| 並行性 | 制限あり | 最大5並列 |
| 永続性 | メモリ | SQLite + メモリ |
| 知識グラフ | あり（draft→distill→ingest） | まだなし |
| 配信チャネル | MessageBus | EventBus（独自） |
| ミドルウェア | — | Summarization + IterationBudget + ToolGuardrails + ToolCallNormalize + HeartbeatStaleness |
| 柔軟性 | 低（硬直的なパターン） | 高（役割ベース） |

両ツールセットは `_MAIN_TOOLS_BUILDERS` に衝突なく共存しており、段階的なマイグレーションが可能です。

### 12. 主要設計決定

| 決定 | 選択 | 根拠 |
|----------|--------|-----------|
| 子エージェント実行 | `CompiledStateGraph.ainvoke()` | LangGraph インフラを再利用、ネイティブ非同期 |
| 配信チャネル | 独自の `EventBus.publish_internal()` | グローバル MessageBus から分離、独立した進化 |
| 永続性 | aiosqliteのみ（JSON フォールバックなし） | プロジェクト依存関係を再利用、SQLite はクロスプラットフォームで安定 |
| サンドボックス | ACP ポートなし | 同一プロセスで実行、ツール拒否リストで権限制御 |
| 利回り実装 | `asyncio.Event` + Registry コールバック | Python にはゲートウェイステアリングがなく、Event がそれに相当 |
| A2A 通信 | EventBus + セッションキールーティング | 既存のメッセージング機構を再利用 |
| 共存戦略 | 独立した新規モジュール、別ツールネームスペース | 既存機能を壊さずに段階的マイグレーション |
| フルフォークコンテキスト | チェックポインタの `agent.aget_state()` | 決定9: 外部 `parent_messages` パラメータは不要 |
| ブロックツール | `sessions_spawn`, `sessions_yield`, `skill_manage`, `memory` | 再帰スポーンと権限昇格を防止 |

---

## 設定

すべての設定は `SubagentConfig`（Pydantic モデル、シングルトン）を介して管理されます:

| パラメータ | 既定 | 説明 |
|-----------|---------|-------------|
| `max_spawn_depth` | 3 | 最大ネスト深さ |
| `max_children_per_agent` | 5 | エージェントあたりの最大同時子数 |
| `run_timeout_seconds` | 300.0 | 子エージェント実行のタイムアウト |
| `require_agent_id` | False | agent_id 必須かどうか |
| `allow_agents` | `["*"]` | 許可された agent_id 許可リスト |
| `default_cleanup` | "delete" | 既定のクリーンアップポリシー |
| `default_context_mode` | ISOLATED | 既定のコンテキストモード |
| `announce_retry_max` | 3 | 最大配信リトライ回数 |
| `announce_retry_delay_base_ms` | 1000 | 指数バックオフの基数（1s、2s、4s） |
| `delivery_suspend_soft_cap` | 25 | 配信一時停止のソフトしきい値 |
| `delivery_suspend_hard_cap` | 50 | 配信一時停止のハードしきい値 |
| `delivery_suspend_target` | 10 | 圧力削減のターゲット数 |
| `lifecycle_grace_period_seconds` | 15.0 | エラー/タイムアウト確定前の猶予期間 |
| `sweeper_interval_seconds` | 60 | スイーパーのスキャン間隔 |
| `orphan_recovery_delay_seconds` | 120 | 孤児回復の遅延 |
| `announce_expiry_ms` | 7,200,000 | 配信のソフト期限切れ（2h） |
| `announce_hard_expiry_ms` | 86,400,000 | 配信のハード期限切れ（24h） |
| `max_announce_retry_count` | 10 | 最大 announce リトライ回数 |
| `stale_unended_threshold_seconds` | 7200 | 未終了の実行が古くなるしきい値 |
| `recent_ended_window_seconds` | 1800 | 最近終了とした表示ウィンドウ |
| `steer_rate_limit_ms` | 2000 | ステアのレート制限 |
| `archive_after_minutes` | 1440 | 分単位の自動アーカイブ |
| `attachments_enabled` | True | 添付有効かどうか |
| `attachments_max_files` | 50 | スポーンあたりの最大ファイル数 |
| `attachments_max_file_bytes` | 1MB | 単一ファイルの最大サイズ |
| `attachments_max_total_bytes` | 5MB | 最大合計添付サイズ |

---

## プロジェクト状態

**7つのフェーズすべて完了（2026-07-15）。** robustness-plan-v3 拡張完了（2026-07-22）。**バグ修正 + OpenClaw 整合 + 深さ整合 + 配線修正完了（2026-07-23）。** 203のテストに合格。規約は [AGENTS.md](./AGENTS.md)、技術決定は [decisions.md](./docs/decisions.md) を参照してください。
