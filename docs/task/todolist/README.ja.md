# TaskFlow 上の計画・規律レイヤー

[English](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> セッションスコープの軽量な計画・規律レイヤーで、既存の TaskFlow システム上に構築されています。これは**タスクエンジンではなく**、**DAG ではなく**、**スケジューラを実装しません**。セッションレベルの計画は `todowrite` / `todoread` で管理され（システムプロンプト注入により圧縮に対して免疫）、クロスセッションで永続的かつサブエージェントをディスパッチする実行は **TaskFlow** が担当します（`depends_on`、`blocked/ready/dispatched/done`、`taskflow_dispatch`、`taskflow_wait_all`）。2つのレイヤーは `flow_id` / `step_id` でリンクされ、単一の状態には唯一の権威ソースがあり、レイヤー間で複製されません。

---

## 目次

- [TaskFlow との関係](#taskflow-との関係)
- [設計哲学とコア原則](#設計哲学とコア原則)
- [アーキテクチャ概要](#アーキテクチャ概要)
- [データレイヤー](#データレイヤー)
- [サービスレイヤー](#サービスレイヤー)
- [ツールレイヤー](#ツールレイヤー)
- [オーケストレーション実行レイヤー](#オーケストレーション実行レイヤー)
- [圧縮保護レイヤー](#圧縮保護レイヤー)
- [強制レイヤー E1–E7](#強制レイヤー-e1e7)
- [フロントエンド](#フロントエンド)

---

## TaskFlow との関係

### 2層の役割分担

| 次元             | セッション計画/チェックリスト                                    | 実行フロー（DAG）                                                                           |
| ---------------- | ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| 所有者           | 計画・規律レイヤー（本文書）                                     | TaskFlow（既存システム）                                                                    |
| スコープ         | 単一セッション、軽量                                             | クロスセッション、永続的                                                                    |
| 状態             | todo の content/status/priority/category/delegation              | step の depends_on/status/child_session_key/results                                         |
| 永続化           | `todos.db`（セッションレベル）                                   | `taskflow_registry.db` + flow `state_json`                                                  |
| 注入方式         | システムプロンプトブロック（毎回圧縮後に再構築、天然で圧縮免疫） | ツール戻り値テキスト（`taskflow_summary` で再読取）                                         |
| 主な責務         | 計画、可視性、規律強制（E1–E7）                                  | 依存関係充足、ブロック/アンロック、並行ディスパッチ、有界待機、結果注入                     |
| スケジューリング | なし                                                             | あり（`taskflow_run_task` / `taskflow_dispatch` / `taskflow_wait_all` / `taskflow_resume`） |

一言：**計画・規律レイヤーは「明確に考える、追跡する、完了させる」を担当し、TaskFlow は「実際に実行する、クロスセッション、依存関係を管理する」を担当します。**

### 境界：何がどこに存在するか

| 関心事                                 | 権威ソース            | 説明                                                           |
| -------------------------------------- | --------------------- | -------------------------------------------------------------- |
| todo が完了したか                      | `todos.db`            | `todowrite` のみが todo ステータスを変更可能                   |
| step が完了したか / 依存が充足したか   | TaskFlow `state_json` | `taskflow_resume` のみが step を `done` にして後続をアンロック |
| 子セッションが実行中か                 | subagent registry     | `get_run_by_child_session_key` + `is_live_unended_run`         |
| 計画ファイルの進捗（チェックボックス） | `.omo/plans/*.md`     | orchestrator が `- [ ]` → `- [x]` に編集                       |
| 実行エビデンス                         | `.omo/ledger.jsonl`   | `EvidenceLedger` が追記                                        |
| アクティブワーク状態                   | `.omo/boulder.json`   | 計画のアクティベーション/復元                                  |

### 関連付け（唯一の真実のソース）

todo は TaskFlow を指す2つのオプションフィールドを保持できます：

- `flow_id`：リンクされた TaskFlow flow id。
- `step_id`：リンクされた TaskFlow step id（例：`step-2`）、その step の DAG ステータスを再読取するため。

**非ミラーリング原則**：todo レイヤーは `depends_on` をコピーせず、ウェーブを保存せず、step ステータスをキャッシュしません。DAG ステータスが必要な時は読み取り専用の `taskflow_summary(flow_id)` を呼び出し、`blocked/ready/dispatched/done` を UI/プロンプトにマッピングします。

---

## 設計哲学とコア原則

HTN（階層型タスクネットワーク）パラダイムを採用：計画ファイル → チェックボックス → 原子サブタスク → subagent worker への委譲 → 対抗的検証。

```
Plan (.omo/plans/*.md)
  └─ Wave 0: [Checkbox A] [Checkbox B]          ← 並行、依存なし
  └─ Wave 1: [Checkbox C] (depends on A, B)      ← Wave 0 完了待ち
  └─ Wave 2: [Final Verification Wave]            ← グローバル完了処理

各 Checkbox → 原子サブタスクに分解 → subagent worker に委譲
  └─ Worker が DoneClaim を返す
       └─ AdversarialVerify（独立検証）
            └─ FullyDone → チェックボックス完了
```

ここでの「ウェーブ」は**計画ファイル内の記述単位**であり、todolist のスケジューリングデータ構造ではありません。「次に何をディスパッチできるか」は TaskFlow の `depends_on` + `blocked/ready` ステータスで決定されます。

**コア原則**：YOU ARE AN ORCHESTRATOR — NEVER THE IMPLEMENTER. コードを書かない、プロダクトファイルを編集しない、すべての実装単位をサブエージェントに委譲する。

モデルの自覚に依存せず、7層の強制でクローズドループを形成します：

- モデルが計画しない → E7 がガイダンスを注入
- モデルが怠けて止まる → E3 が引き戻す
- モデルが虚偽完了する → E5 がブロック
- モデルが自分でコードを書く → E1 doctrine が牽制
- モデルが早めに完了マークする → E4 がハードブロック

---

## アーキテクチャ概要

```
Layer 9  │ UI コンポーネント  │ TodoDock.vue + TodoItem.vue (PrimeVue)、TaskFlow flow ごとにグループ化
Layer 8  │ フロントエンド状態  │ useTodoList.ts（モジュールレベルシングルトン）+ flow ごとにグループ化
Layer 7  │ リアルタイム通信    │ WS: todo_updated プッシュ + todo_refresh 再接続時再送
Layer 6  │ 圧縮保護 ★        │ build_system_prompt が現在の todos + boulder 状態を注入
Layer 5  │ DAG 実行 ★        │ TaskFlow が提供: depends_on + blocked/ready/dispatched/done（本層は不実装）
Layer 4  │ オーケストレーション ★ │ ulw-execute: plan→checkbox→sub-task→worker→verify、TaskFlow を呼び出し
Layer 3  │ ツール            │ todowrite + todoread（DAG は taskflow_* ツールファミリーが実行）
Layer 2  │ サービス           │ TodoService + EvidenceLedger（DAG 状態クエリは TaskFlow に委譲）
Layer 1  │ データストレージ   │ todos.db (WAL) + boulder.json + ledger.jsonl + plans/*.md + taskflow_registry.db
─────────│──────────────────│
E1       │ システムプロンプト  │ AGENTS.md: MANDATORY orchestrator doctrine
E2       │ ツール説明          │ todowrite docstring フォーマットルール
E3       │ 継続強制 ★★       │ after_agent ミドルウェア: idle+未完了→自動継続（バックオフ/停滞/abort/リカバリ）
E4       │ 遷移バリア ★       │ TaskFlow step 未 done / subagent 実行中 → completed をブロック
E5       │ Sisyphus 検証 ★★  │ DoneClaim → AdversarialVerify → FullyDone（5 gates）
E6       │ 委譲ルーティング ★ │ #a fan-out + #b category ルート + #c 委譲コマンド + #d バリア
E7       │ 意図認識 ★★       │ before_model: arming(計画なし+タスク意図→注入) + plan-active(計画あり→reminder)
```

**Layer 5 の DAG 機能は本層では実装されていません** — TaskFlow が提供します。本層は呼び出すだけで、再構築しません。

---

## データレイヤー

### 計画ファイル (.omo/plans/*.md)

チェックボックス形式の Markdown で、完全な HTN 分解を定義します：

```markdown
# <Plan Name>

## Goal

<詳細な目標：計画名、パス、終端状態、デリバリーモード、検証方法>

## Context

<プロジェクト背景、制約、既知の情報>

## TODOs

### Wave 0: <Wave description>

- [ ] [WHERE] [HOW] to [WHY] - expect [RESULT]
  - Recommended task executor category: quick
  - Verification: <exact command + assertion>
  - Files in scope: <path1, path2>

### Wave 1: <Wave description> (depends on Wave 0)

- [ ] [WHERE] [HOW] to [WHY] - expect [RESULT]
  - Depends on: Wave 0
  - Verification: <exact command + assertion>

## Final Verification Wave

- [ ] Run full test suite + typecheck + lint
- [ ] Manual QA: <exact command, exact observable, exact assertion>
- [ ] Cleanup receipts: <list of resources to tear down>
```

### Boulder 状態 (.omo/boulder.json)

永続的なワーク状態。`session_id` に `sherry:` プレフィックスを使用：

```json
{
  "schema_version": 2,
  "active_work_id": "<work-id>",
  "works": {
    "<work-id>": {
      "work_id": "<work-id>",
      "active_plan": ".omo/plans/<plan-name>.md",
      "session_ids": ["sherry:<session_id>"],
      "status": "active"
    }
  }
}
```

### エビデンス台帳 (.omo/ledger.jsonl)

1行1 JSON オブジェクトで、各チェックボックスの実行エビデンスを記録します。

### todos.db — セッションレベル TODO ストレージ

```sql
CREATE TABLE IF NOT EXISTS todos (
    session_id   TEXT    NOT NULL,
    content      TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'pending',
    priority     TEXT    NOT NULL DEFAULT 'medium',
    position     INTEGER NOT NULL,
    category     TEXT    NOT NULL DEFAULT 'quick',
    delegation   TEXT    NOT NULL DEFAULT 'self',
    subagent_id  TEXT    DEFAULT NULL,
    plan_ref     TEXT    DEFAULT NULL,
    flow_id      TEXT    DEFAULT NULL,
    step_id      TEXT    DEFAULT NULL,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (session_id, position)
);
```

> **設計境界**：`todos.db` は `depends_on` / ウェーブ / step ステータスを保持しません。依存グラフ、`blocked/ready/dispatched/done` とアンロックロジックはすべて TaskFlow が提供します。todo は `flow_id`/`step_id` で対応する flow step を指すだけで、DAG ステータスは `taskflow_summary(flow_id)` で再読取します。

CRUD インターフェース：`replace_all`（全量置換）、`get_todos`（position 順）、`get_todos_sync`（同期パス、プロンプト注入用）、`get_todos_by_flow`（flow 関連の todo 取得）。

---

## サービスレイヤー

### TodoService

- `update_todos`：全量置換 + E4 遷移バリア検証 + WS プッシュ。
- `get_todos`：DB から読取。
- `get_flow_progress`：TaskFlow の DAG ステータスを todo ビューにマッピング（frontier を再計算しない）。読み取り専用の `taskflow_summary` を呼び出して step の status/depends_on を取得。

### EvidenceLedger

```python
class EvidenceLedger:
    LEDGER_PATH = ".omo/ledger.jsonl"

    @classmethod
    def append(cls, entry: dict) -> None:
        entry["timestamp"] = datetime.utcnow().isoformat()
        with open(cls.LEDGER_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    @classmethod
    def read_all(cls) -> list[dict]:
        ...
```

### WS プッシュ

`relation_register` の直接送信パターンを再利用し、`todo_updated` イベントをプッシュします。

---

## ツールレイヤー

### todowrite — 全量置換、書き込み即読み

```python
@tool("todowrite")
async def todowrite(todos: list[dict], session_id: Annotated[str, InjectedState("session_id")] = "") -> str:
    """Update the todo list for the current session (full replacement).
    Pass the COMPLETE list every time.
    DAG scheduling is NOT done here. Declare dependencies with
    taskflow_run_task(flow_id, task, depends_on=[...])."""
    result = await service.update_todos(session_id, todos)
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if session_id not in _reminded_sessions:
        _reminded_sessions.add(session_id)
        output += _FANOUT_REMINDER  # E6a: 初回呼び出し時に fan-out リマインダーを追加
    return output
```

### todoread — 明示的読み取り

```python
@tool("todoread")
async def todoread(session_id: Annotated[str, InjectedState("session_id")] = "") -> str:
    """Read the current todo list from database. Use when unsure of current state."""
    todos = await service.get_todos(session_id)
    return json.dumps(todos, ensure_ascii=False, indent=2) if todos else "No todos found."
```

### ツール登録（E2 注入ポイント）

`build_todolist_tools()` で todowrite の説明をオーバーライドし、MANDATORY フォーマットルールを注入：

- 各 todo タイトルは WHERE、WHY、HOW、EXPECTED RESULT をエンコード
- 原子粒度（1-3 ツール呼び出しで完了）
- 同時に1つの in_progress のみ
- subagent 返却前に completed をマークしない

### SKILL.md

todolist をいつ使うか（3+ ステップの複雑な作業）、利用可能ツール、ステータス/優先度/委譲フィールド、DAG フィールド（TaskFlow に委譲）、ルールを定義します。

---

## オーケストレーション実行レイヤー

### 5フェーズフロー

```
Phase 1: 計画の選択 → .omo/boulder.json を読取、.omo/plans/*.md をリスト、マッチまたは復元
Phase 2: Boulder 状態の作成/更新 → boulder.json に書き込み、フェーズ/タスクを todos として登録
Phase 3: 次のチェックボックスを実行（スケジューリングはすべて TaskFlow に委譲）
  → 最初の未チェックの checkbox を見つける
  → 原子サブタスクに分解
  → taskflow_run_task(flow_id, task, depends_on=[...]) で登録
  → blocked はディスパッチしない；ready は taskflow_dispatch で一括ディスパッチ
  → taskflow_wait_all → taskflow_resume（結果注入、後続アンロック）
  → DELEGATE EVERYTHING via delegation router (E6)
Phase 4: 検証とエビデンス記録 → 5 gates → .omo/ledger.jsonl
Phase 5: 進捗マーク → checkbox - [ ] → - [x]、継続するか尋ねない
```

### 計画 → TaskFlow のマッピング

1. `taskflow_create(flow_id, description, initial_state)` — flow 作成。
2. `taskflow_run_task(flow_id, task, depends_on=[...])` — step 登録：依存なし → `dispatched`；依存未充足 → `blocked`（ディスパッチしない）；未知の依存 id → エラー、状態変更なし。
3. `taskflow_resume(flow_id, child_session_key, result)` — 結果注入：step が `done` になり、`blocked` 依存が `ready` にアンロック。**resume は自動ディスパッチしない。**
4. `taskflow_dispatch(flow_id, step_ids)` — ready step を一括並行ディスパッチ。
5. `taskflow_wait_all(flow_id, timeout, poll_interval)` — ディスパッチした子セッションの完了を待機。
6. `taskflow_finish(flow_id, summary)` — 完了；`taskflow_summary` でいつでも再読取可能。

### step 状態機械（TaskFlow 権威定義）

```
blocked（依存がすべて done ではない；run_task は登録のみ、spawn なし）
  → ready（依存充足、ディスパッチ待ち；taskflow_resume でアンロック）
  → dispatched（detached 子セッション spawn、child_session_key 永続化）
  → done（taskflow_resume で結果注入済み）
```

### HTN → TaskFlow 機能マッピング

| 計画レイヤー概念（HTN）    | TaskFlow 機能                                                               |
| -------------------------- | --------------------------------------------------------------------------- |
| チェックボックス間の依存   | step の `depends_on`（step-id リスト）                                      |
| ウェーブ                   | 明示的なウェーブなし；`ready` ステータス = "現在のウェーブ"                 |
| ノードステータス           | `status ∈ {blocked, ready, dispatched, done}`（`state_json` 内）            |
| Frontier（実行可能項目）   | `ready` ステータス + `taskflow_dispatch` が `deps_satisfied` を検証         |
| 依存未充足時のブロック     | `taskflow_run_task(..., depends_on=[...])` は `blocked` で登録、spawn なし  |
| 完了による後続のアンロック | `taskflow_resume` が step を `done` に + `unlock_dependents`                |
| 並行ディスパッチ           | `taskflow_dispatch(flow_id, step_ids)` で ready step を一括ディスパッチ     |
| 並行子セッションの待機     | `taskflow_wait_all(...)` で flow スコープの有界ポーリング                   |
| DAG ステータスの再読取     | `taskflow_summary(flow_id)` で各 step の status/depends_on + カウントを表示 |

### 並行デリバリーチャネル決定

| トポロジー                      | 条件                            | 戦略                                              |
| ------------------------------- | ------------------------------- | ------------------------------------------------- |
| 独立チャネル → 並行 workers     | 分離ファイル、共有契約なし      | 一括並行 spawn burst（`taskflow_dispatch`）       |
| 順序依存チャネル → ウェーブ直列 | C が A と B の完了を必要        | 前提 step の `done` + `resume` アンロック後に実行 |
| 重複チャネル → team             | 同じモジュール/契約、並行が高速 | 直列化または手動調整                              |

---

## 圧縮保護レイヤー

`build_system_prompt()` はコンテキスト圧縮後に `Summarization` ミドルウェアで再呼び出しされます。ここで現在の todos + boulder 状態を注入することで、圧縮に対して天然で免疫を持ちます。

### 注入コンテンツ

| Block                      | データソース      | コンテキストコスト | 説明                                                      |
| -------------------------- | ----------------- | ------------------ | --------------------------------------------------------- |
| `_build_todo_block()`      | todos.db          | ~10行              | 現在の todo リスト + ステータス + TaskFlow flow/step 関連 |
| `_build_boulder_block()`   | .omo/boulder.json | ~5行               | アクティブワーク状態                                      |
| `_build_knowledge_block()` | .omo/knowledge/   | ~20行              | key_failures + key_successes + reusable_patterns          |

### omo の5層防御より軽量な理由

| omo の5層                           | 本アプローチ                                            |
| ----------------------------------- | ------------------------------------------------------- |
| Prune 保護リスト                    | 不要（todos はシステムプロンプト内）                    |
| 圧縮前スナップショット + 圧縮後復元 | 不要（システムプロンプトは毎回 DB から再構築）          |
| 8セグメント圧縮コンテキスト注入     | 不要（todos は会話履歴にない）                          |
| 60s 圧縮保護ウィンドウ              | 不要（保護すべき継続注入器がない）                      |
| 継続強制器                          | 必要、after_agent ミドルウェアに簡略化（E3）            |
| **合計 ~600+ 行**                   | **~65行（プロンプト注入）+ ~130行（継続ミドルウェア）** |

---

## 強制レイヤー E1–E7

### E1: システムプロンプト強制 — orchestrator doctrine

`AGENTS.md` に追加。ゼロコード、純テキスト。核心：

- YOU ARE AN ORCHESTRATOR — NEVER THE IMPLEMENTER
- 複数ステップタスク（2+ 歩）→ 必ず先に todos を作成
- 各ステップで in_progress をマーク → subagent 返却後検証 → completed マーク
- 遷移バリア：subagent 実行中 / TaskFlow step 未完了時に completed をマークしない
- Sisyphus 契約：DoneClaim → AdversarialVerify → FullyDone

### E2: ツール説明強制 — フォーマット + 委譲ルール

`build_todolist_tools()` で todowrite の説明をオーバーライドし、MANDATORY フォーマットルールを注入：タイトルに WHERE/WHY/HOW/RESULT をエンコード、原子粒度、同時に1つの in_progress のみ、subagent 返却前に completed マークしない。

### E3: 継続強制器 — 事後クロージャのコア ★★

turn 終了後に未完了 todo があれば、システムが自動的に継続メッセージを注入して LLM を引き戻します。モデルの自覚に依存しません。

**主要定数**：`_MAX_STAGNATION = 3`（連続3回変化なし → 停止）、`_BASE_COOLDOWN_S = 2.0`（基本バックオフ）、`_MAX_COOLDOWN_S = 60.0`（最大バックオフ）、`_FAILURE_RESET_WINDOW_S = 300`（5分間失敗なし → リセット）、`_MAX_RECOVERY_ATTEMPTS = 2`（リカバリモード上限）。

**ワークフロー**：turn 終了 → todos 読取 → 未完了フィルタ → 停滞検出 → バックオフ冷却 → 継続プロンプト構築 → `maybe_trigger_auto_turn()` で注入。ユーザーがメッセージ送信 → キャンセル → reset()。

### E4: 遷移バリア — 二重保険

**プロンプトレベル**（E1 AGENTS.md）：subagent 実行中 / TaskFlow step 未完了時に completed をマークしない。

**コードレベルハードブロック**（`service.py`）、2つのソース、どちらも todolist 内にスケジューラを構築しない：

1. **TaskFlow step ステータス**：todo が `flow_id`/`step_id` に関連 → `done` のみ `completed` を許可。`blocked`/`ready`/`dispatched` はすべてブロック。
2. **subagent registry liveness**：todo が `subagent_id` に関連 → `get_run_by_child_session_key` + `is_live_unended_run` で子セッションが実行中か判定。

### E5: Sisyphus 完了契約 ★★

三段階完了検証で「虚偽完了」に対する最終防線：

```
Worker 返却 → DoneClaim → AdversarialVerify（5 gates）→ FullyDone / NOT done
  Gate 1: Plan reread（計画再読取、验收基準確認）
  Gate 2: Automated verification（検証コマンド実行）
  Gate 3: Manual QA（人工または agent が確認）
  Gate 4: Adversarial QA（stale state / dirty worktree / leftover resources チェック）
  Gate 5: Cleanup（一時リソースのクリーンアップ）
```

### E6: 委譲ルーティング ★

**ライフサイクル**：LLM が todo 作成（category + delegation）→ #a fan-out reminder → #b category で subagent タイプを指定 → #c AGENTS.md が委譲方法を指導 → 依存ステップは taskflow_run_task(depends_on) → ready ステップは taskflow_dispatch → LLM が subagent_id + flow_id/step_id を更新 → #d 遷移バリア → taskflow_wait_all → taskflow_resume → E5 検証 → completed マーク。

**Category ルーティング表**：

| Category     | ルート先                         | 説明                                   |
| ------------ | -------------------------------- | -------------------------------------- |
| `quick`      | subagent spawn (default model)   | 機械的、単一ファイル、ボイラープレート |
| `deep`       | subagent spawn (reasoning model) | 複雑なデバッグ、研究集約型             |
| `ultrabrain` | subagent spawn (最強 model)      | 真に困難な論理問題                     |
| `visual`     | subagent spawn                   | フロントエンド、UI/UX                  |
| `git`        | subagent spawn                   | git 操作                               |
| `writing`    | subagent spawn                   | ドキュメント                           |

### E7: 意図認識とガイダンス — 事前トリガー ★★

デュアルモード設計で「モデルが自発的に計画ツールを呼ばない」問題を解決：

| シナリオ                            | モード     | 挙動                                         |
| ----------------------------------- | ---------- | -------------------------------------------- |
| 初回タスクリクエスト、計画なし      | E7a        | 完全なガイダンスプロンプトを注入             |
| arm 済み + 新規タスクリクエスト     | E7a 軽量   | 短いリマインダーを注入                       |
| アクティブ計画 + ユーザーメッセージ | E7b        | plan-active reminder を追加                  |
| アクティブ計画 + turn 終了          | (E3)       | E7 は不干渉                                  |
| 非タスク（質問/雑談）               | スキップ   | 注入しない                                   |
| 圧縮後 + 新規タスク                 | E7a 再 arm | armed フラグをクリア、完全ガイダンスを再注入 |

**意図検出**：軽量ヒューリスティック、LLM 呼び出しなし（ゼロ遅延、ゼロコスト）— 質問パターン → 非タスク、雑談パターン → 非タスク、タスクキーワード → タスク、長文（>100文字）非質問 → タスクの可能性。

**ループ防止設計**：E7b が E7a より優先、E7a はセッション毎1回、E7b は before_model のみ、`_is_system_directive()` でシステム注入メッセージをフィルタ、E3 はバックオフ冷却あり、圧縮後に re-arm。

---

## フロントエンド

### フロントエンド状態レイヤー (useTodoList.ts)

モジュールレベルシングルトン、WebSocket 駆動、TaskFlow flow ごとにグループ化：

```typescript
interface Todo {
  content: string;
  status: "pending" | "in_progress" | "completed" | "cancelled";
  priority: "high" | "medium" | "low";
  category?: "quick" | "deep" | "ultrabrain" | "visual" | "git" | "writing";
  delegation?: "self" | "subagent";
  flow_id?: string | null;
  step_id?: string | null;
  taskflow_status?: "blocked" | "ready" | "dispatched" | "done" | null;
}
```

WS イベント：`todo_updated`（変更時プッシュ）+ `todo_refresh`（再接続時再送）。

### UI コンポーネント

- **TodoDock.vue**：`flow_id` ごとにグループ化して表示、進捗カウント、折りたたみ可能。
- **TodoItem.vue**：Checkbox (PrimeVue)、category バッジ、委譲アイコン、in_progress パルスドット。

`taskflow_status` は `taskflow_summary(flow_id)` で再読取した step ステータスであり、フロントエンドはウェーブ/依存を再計算しません。
