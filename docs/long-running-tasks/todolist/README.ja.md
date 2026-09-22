# TaskFlow 上の計画・規律レイヤー

[English](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> セッションスコープの軽量な計画・規律レイヤーで、既存の TaskFlow システム上に構築されています。これは**タスクエンジンではなく**、**DAG ではなく**、**スケジューラを実装しません**。セッションレベルの計画は `todowrite` / `todoread` で管理され（システムプロンプト注入により圧縮に対して免疫）、クロスセッションで永続的かつサブエージェントをディスパッチする実行は **TaskFlow** が担当します（`depends_on`、`blocked/ready/dispatched/done`、`taskflow_dispatch`、`taskflow_wait_all`）。2つのレイヤーは `flow_id` / `step_id` でリンクされ、単一の状態には唯一の権威ソースがあり、レイヤー間で複製されません。

---

## 目次

- [TaskFlow との関係](#taskflow-との関係)
- [設計哲学とコア原則](#設計哲学とコア原則)
- [アーキテクチャ概要](#アーキテクチャ概要)
- [データレイヤー](data/README.ja.md)
  - [計画ファイル (workspace/sessions/<session_id>/plans/*.md)](data/README.ja.md#計画ファイル-workspacesessionssession_idplansmd)
  - [Boulder 状態 (src/data/boulder.json)](data/README.ja.md#boulder-状態-srcdataboulderjson)
  - [エビデンス台帳 (src/data/evidence-ledger.jsonl)](data/README.ja.md#エビデンス台帳-srcdataevidence-ledgerjsonl)
  - [todos.db — セッションレベル TODO ストレージ](data/README.ja.md#todosdb--セッションレベル-todo-ストレージ)
- [サービスレイヤー](#サービスレイヤー)
- [ツールレイヤー](#ツールレイヤー)
- [オーケストレーション実行レイヤー](#オーケストレーション実行レイヤー)
- [圧縮保護レイヤー](#圧縮保護レイヤー)
- [強制レイヤー E1–E7](enforcement/README.ja.md)
  - [E1: システムプロンプト強制 — orchestrator doctrine](enforcement/README.ja.md#e1-システムプロンプト強制--orchestrator-doctrine)
  - [E2: ツール説明強制 — フォーマット + 委譲ルール](enforcement/README.ja.md#e2-ツール説明強制--フォーマット--委譲ルール)
  - [E3: 継続強制器 — 事後クロージャのコア ★★](enforcement/README.ja.md#e3-継続強制器--事後クロージャのコア-)
  - [E4: 遷移バリア — 二重保険](enforcement/README.ja.md#e4-遷移バリア--二重保険)
  - [E5: Sisyphus 完了契約 ★★](enforcement/README.ja.md#e5-sisyphus-完了契約-)
  - [E6: 委譲ルーティング ★](enforcement/README.ja.md#e6-委譲ルーティング-)
  - [E7: 意図認識とガイダンス — 事前トリガー ★★](enforcement/README.ja.md#e7-意図認識とガイダンス--事前トリガー-)
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
| 計画ファイルの進捗（チェックボックス） | `workspace/sessions/<session_id>/plans/*.md`     | orchestrator が `- [ ]` → `- [x]` に編集                       |
| 実行エビデンス                         | `src/data/evidence-ledger.jsonl`   | `EvidenceLedger` が追記                                        |
| アクティブワーク状態                   | `src/data/boulder.json`   | 計画のアクティベーション/復元                                  |

### 関連付け（唯一の真実のソース）

todo は TaskFlow を指す2つのオプションフィールドを保持できます：

- `flow_id`：リンクされた TaskFlow flow id。
- `step_id`：リンクされた TaskFlow step id（例：`step-2`）、その step の DAG ステータスを再読取するため。

**非ミラーリング原則**：todo レイヤーは `depends_on` をコピーせず、ウェーブを保存せず、step ステータスをキャッシュしません。DAG ステータスが必要な時は読み取り専用の `taskflow_summary(flow_id)` を呼び出し、`blocked/ready/dispatched/done` を UI/プロンプトにマッピングします。

---

## 設計哲学とコア原則

HTN（階層型タスクネットワーク）パラダイムを採用：計画ファイル → チェックボックス → 原子サブタスク → subagent worker への委譲 → 対抗的検証。

```
Plan (workspace/sessions/<session_id>/plans/*.md)
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

```
Before (message arrives)    During                     After
┌──────────┐  ┌────────────────────┐  ┌──────────────────────┐
│ E7 Intent │  │ E6 Delegation ★    │  │ E3 Continuation ★★  │
│ Recognizer│  │ #a fan-out         │  │ idle + incomplete    │
│ ★★       │  │ #b category route  │  │ todo + backoff +     │
│ before_   │  │ #c delegation cmd  │  │ stagnation + abort   │
│ model     │  │ #d barrier         │  │ + recovery mode      │
│ inject    │  └────────────────────┘  └──────────────────────┘
└──────────┘  ┌────────────────────┐  ┌──────────────────────┐
┌──────────┐  │ E1 System prompt   │  │ E5 Sisyphus verify ★★│
│ E2 Tool  │  │ orchestrator       │  │ DoneClaim →          │
│ desc      │  │ doctrine           │  │ AdversarialVerify →  │
│ MANDATORY │  │ + hook notice      │  │ FullyDone             │
│ format    │  └────────────────────┘  └──────────────────────┘
└──────────┘                            ┌──────────────────────┐
                                          │ E4 Transition barrier│
                                          │ TaskFlow step status+│
                                          │ subagent alive → block│
                                          └──────────────────────┘
```

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

**Layer 5 の DAG 機能は本層では実装されていません** — TaskFlow（`agent/tools/taskflow/tools/*`）が提供します。本層は呼び出すだけで、再構築しません。

---

## サービスレイヤー

### TodoService

```python
class TodoService:
    @staticmethod
    async def update_todos(session_id: str, todos: list[dict]) -> list[dict]:
        validated = _validate_todos(todos)
        # E4：遷移バリア — TaskFlow step 未 done / subagent 実行中は completed をブロック
        for todo in validated:
            if todo["status"] == "completed":
                _assert_transition_allowed(todo)
        await store.replace_all(session_id, validated)
        latest = await store.get_todos(session_id)
        await _push_todo_update(session_id, latest)
        return latest

    @staticmethod
    async def get_flow_progress(session_id: str, flow_id: str) -> dict:
        """TaskFlow の DAG ステータスを todo ビューにマッピング（frontier を再計算しない）。
        DAG スケジューリングは TaskFlow が所有；本メソッドは読み取り専用の taskflow_summary を呼ぶだけ。"""
        from agent.tools.taskflow.tools.taskflow_summary import taskflow_summary
        linked = await store.get_todos_by_flow(session_id, flow_id)
        summary_text = await taskflow_summary.ainvoke({"flow_id": flow_id})
        return {"todos": linked, "taskflow_summary": summary_text}
```

### EvidenceLedger

`agent/tools/todolist/evidence_ledger.py` は append-only の JSONL 台帳（`src/data/evidence-ledger.jsonl`、1 行 1 JSON オブジェクト）です。`append` + `read_all` がストレージ契約のすべてであり、セッションスコープのビューは同じ共有ファイルを読みます：

```python
class EvidenceLedger:
    LEDGER_PATH = "src/data/evidence-ledger.jsonl"

    @classmethod
    def append(cls, entry: dict) -> None: ...          # 1 行の JSON + UTC タイムスタンプ
    @classmethod
    def read_all(cls) -> list[dict]: ...
    @classmethod
    def for_session(cls, session_key: str) -> SessionEvidenceLedger: ...
    @classmethod
    def read_for_session(cls, session_key: str) -> list[dict]: ...
    @classmethod
    def mark_stale_for_path(cls, file_path: str, session_key: str | None = None) -> int: ...
```

staleness は保存ではなく導出されます：`mark_stale_for_path` は `{"event": "stale", "file_path": …}` 行を追記し、読み取り側は、後続の stale イベントがその `command` に含まれるパスを名指しした場合にのみ、その evidence 行を stale とみなします。`agent/tools/todolist/evidence_recorder.py` がこれをツールに配線します（フェイルオープン、例外は握りつぶし）：`terminal` / `python_repl` は認識した検証コマンドの行を追記し（`EVIDENCE_LEDGER["auto_record"]`、既定 `True`）、`write_file` / `patch_file` は編集されたパスの stale イベントを追記します（`EVIDENCE_LEDGER["auto_stale"]`、既定 `True`）。`agent/tools/taskflow/evidence_collector.py` は判定器と `taskflow_finish` の evidence ゲートに表示する要約を描画します。

### WS プッシュ

```python
async def _push_todo_update(session_id: str, todos: list[dict]) -> None:
    from runtime import relation_register
    ws = relation_register.get_websocket_by_session_id(session_id)
    if ws:
        await ws.send_text(json.dumps({
            "event": "todo_updated", "session_id": session_id,
            "content": {"todos": todos}
        }))
```

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

```python
def build_todolist_tools() -> list[BaseTool]:
    for t in _TODOLIST_TOOLS:
        t.handle_tool_error = True
        t.metadata = {"scope": "main_only"}
    todowrite.description += _TODOWRITE_FORMAT_RULES  # E2 フォーマットルール
    return list(_TODOLIST_TOOLS)
```

`build_todolist_tools()` で todowrite の説明をオーバーライドし、MANDATORY フォーマットルールを注入：

- 各 todo タイトルは WHERE、WHY、HOW、EXPECTED RESULT をエンコード
- 原子粒度（1-3 ツール呼び出しで完了）
- 同時に1つの in_progress のみ
- subagent 返却前に completed をマークしない

### SKILL.md

todolist をいつ使うか（3+ ステップの複雑な作業）、利用可能ツール、ステータス/優先度/委譲フィールド、DAG フィールド（TaskFlow に委譲）、ルールを定義します。

### knowledge — 計画アイデンティティ分離

`knowledge` ツール（`agent/tools/todolist/knowledge/`）は計画名ではなく**計画アイデンティティ**をキーとします。関連は 3 つのソース（`ownership.association_plan_refs()`）から取得します：セッションの `plan_ref` 状態キー、セッションの todo の `plan_ref`（SQL で `session_id` をフィルタ）、`src/data/boulder.json` のうち `plan_name` が一致し `session_ids` に当該セッションを含む work。`identity.resolve_plan_identity()` が名前を正規化された計画パスへ解決し、保存ディレクトリ `workspace/knowledge/plans/<plan_key>/` を導出します（`plan_key = sha1(リポジトリルート相対の計画パス)[:12]`）。各ディレクトリの `meta.json` に可読な `plan_name` / `plan_ref` を記録します。結果：

- 計画ファイルが異なる同名計画は**物理的に隔離**され——各自の key ディレクトリへ書き込み、相互に上書きしません；
- boulder `session_ids` で**1 つの計画ファイル**を共有する全セッションは同じパスに解決されるため、**複数セッションの協業は維持**されます（1 ディレクトリを共有）；
- 計画ファイルが解決できないセッションはフォールバックアイデンティティ `session-<sha1(session_id)[:8]>` に書き込みます——全 id ハッシュにより先頭 8 文字が同じセッション id も互いに隔離されます；
- 名前が複数パスに一致する場合はセッション自身の `plan_ref` を優先；それでも曖昧な場合（自セッションのフォールバック名でない）は診断可能なエラーで拒否し、別の場所へ黙って書き込みません。

`list` は関連付けられた計画のみ（可読名 + key）を返し、`read` / `write` の他セッション・曖昧計画へのアクセスは拒否されます（沈黙の空結果にはしません）。レガシーの名前キー・ディレクトリ `workspace/knowledge/plans/<plan-name>/` は key ディレクトリが現れるまで読み取り可能；書き込みは常に key ディレクトリへ。`clear_session` はセッション私有のアイデンティティディレクトリを削除し、boulder `session_ids` で他セッションと共有された計画は保持します；レガシー・ディレクトリは決して削除しません。boulder ファイルの欠落/破損、空の `session_id`、未知の計画はいずれも安全に拒否されます——例外もクロスセッション読み取りもありません。

---

## オーケストレーション実行レイヤー

### 5フェーズフロー

```
Phase 1: 計画の選択 → src/data/boulder.json を読取、workspace/sessions/<session_id>/plans/*.md をリスト、マッチまたは復元
Phase 2: Boulder 状態の作成/更新 → boulder.json に書き込み、フェーズ/タスクを todos として登録
Phase 3: 次のチェックボックスを実行（スケジューリングはすべて TaskFlow に委譲）
  → 最初の未チェックの checkbox を見つける
  → 原子サブタスクに分解
  → taskflow_run_task(flow_id, task, depends_on=[...]) で登録
  → blocked はディスパッチしない；ready は taskflow_dispatch で一括ディスパッチ
  → taskflow_wait_all → taskflow_resume（結果注入、後続アンロック）
  → DELEGATE EVERYTHING via delegation router (E6)
Phase 4: 検証とエビデンス記録 → 5 gates → src/data/evidence-ledger.jsonl
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
| 順序依存チャネル → ウェーブ直列 | C が A と B の完了を必要        | 前提 step の `done` 後、`depends_on` + `resume` アンロックで実行 |
| 重複チャネル → team             | 同じモジュール/契約、並行が高速 | 直列化または手動調整                              |

---

## 圧縮保護レイヤー

`build_system_prompt()` はコンテキスト圧縮後に `Summarization` ミドルウェアで再呼び出しされます。ここで現在の todos + boulder 状態を注入することで、圧縮に対して天然で免疫を持ちます。

### 注入コンテンツ

| Block                      | データソース      | コンテキストコスト | 説明                                                      |
| -------------------------- | ----------------- | ------------------ | --------------------------------------------------------- |
| `_build_todo_block()`      | todos.db          | ~10行              | 現在の todo リスト + ステータス + TaskFlow flow/step 関連 |
| `_build_boulder_block()`   | src/data/boulder.json | ~5行               | アクティブワーク状態                                      |
| `_build_knowledge_block()` | workspace/knowledge/plans/&lt;plan_key&gt;/ | ~20行              | key_failures + key_successes + reusable_patterns（アイデンティティ解決） |

### 実装

```python
def _build_todo_block(session_id: str) -> str:
    from agent.tools.todolist.registry.store_sqlite import get_todos_sync
    todos = get_todos_sync(session_id)
    if not todos:
        return ""
    lines = ["## Current Todo List"]
    for t in todos:
        icon = {"pending": "○", "in_progress": "◐", "completed": "●", "cancelled": "✕"}
        tag_parts = [t.get("category", "quick")]
        if t.get("delegation", "self") != "self":
            tag_parts.append(t["delegation"])
        if t.get("flow_id"):
            tag_parts.append(f"flow:{t['flow_id']}")
        if t.get("step_id"):
            tag_parts.append(t["step_id"])
        lines.append(f"- [{icon.get(t['status'], '○')}] ({', '.join(tag_parts)}) {t['content']} ({t['priority']})")
    lines.append("Update todos via todowrite. Pass the COMPLETE list each time.")
    lines.append("Dependency scheduling is owned by TaskFlow; declare depends_on via taskflow_run_task.")
    lines.append("Your todo list is tracked by the continuation system. Incomplete todos will trigger automatic continuation.")
    lines.append("Completion is verified by the Sisyphus contract — unverified claims will be rejected.")
    return "\n".join(lines)
```

### omo の5層防御より軽量な理由

| omo の5層                           | 本アプローチ                                            |
| ----------------------------------- | ------------------------------------------------------- |
| Prune 保護リスト                    | 不要（todos はシステムプロンプト内）                    |
| 圧縮前スナップショット + 圧縮後復元 | 不要（システムプロンプトは毎回 DB から再構築）          |
| 8セグメント圧縮コンテキスト注入     | 不要（todos は会話履歴にない）                          |
| 60s 圧縮保護ウィンドウ              | 不要（保護すべき継続注入器がない）                      |
| 継続強制器                          | 必要、after_agent ミドルウェアに簡略化（E3）            |
| 知識サマリー注入                    | 新規 `_build_knowledge_block()`（約25行）               |
| **合計 ~600+ 行**                   | **~65行（プロンプト注入）+ ~130行（継続ミドルウェア）** |

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

### WS メッセージ形式

```json
{
  "event": "todo_updated",
  "session_id": "xxx",
  "content": {
    "todos": [
      {
        "content": "Implement login",
        "status": "completed",
        "priority": "high",
        "flow_id": "login-flow",
        "step_id": "step-1",
        "taskflow_status": "done"
      }
    ]
  }
}
```

`taskflow_status` は `taskflow_summary(flow_id)` で再読取した step ステータスであり、フロントエンドはウェーブ/依存を再計算しません。
