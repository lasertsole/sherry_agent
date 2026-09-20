# 🛡️ TodoList 強制レイヤー — E1–E7 とオーケストレーター規律

[English](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> [TodoList](../README.ja.md) の一部：オーケストレーターに計画・委譲・検証・誠実さを強制する 7 層（E1–E7）。

---

## 強制レイヤー E1–E7

### E1: システムプロンプト強制 — orchestrator doctrine

`AGENTS.md` に追加。ゼロコード、純テキスト：

```markdown
## Task Management & Orchestration (CRITICAL)

### Orchestrator Doctrine (MANDATORY)

YOU ARE AN ORCHESTRATOR — NEVER THE IMPLEMENTER.

- You DO NOT write code. You DO NOT edit product files.
- EVERY unit of implementation MUST be delegated to a spawned subagent.

### When to Create Todos (MANDATORY)

- Multi-step task (2+ steps) → ALWAYS create todos first
- Uncertain scope → ALWAYS (todos clarify thinking)

### Workflow (NON-NEGOTIABLE)

1. IMMEDIATELY on receiving request: todowrite to plan atomic steps.
2. For each checkbox: decompose into atomic sub-tasks for ONE worker.
3. DELEGATE every sub-task via task tool — route by category.
4. Before starting each step: Mark in_progress (only ONE at a time)
5. After subagent returns: verify via acceptance criteria, then mark completed.

### Transition Barrier (CRITICAL)

- Do NOT mark a todo as completed while its subagent is still running
- Do NOT mark a TaskFlow-linked todo completed while its step is not `done`

### Completion Contract (Sisyphus)

- DoneClaim → AdversarialVerify → FullyDone
- If verification fails, the task is NOT done — re-dispatch or fix.

**FAILURE TO FOLLOW ORCHESTRATOR DOCTRINE = INCOMPLETE WORK.**
```

### E2: ツール説明強制 — フォーマット + 委譲ルール

`build_todolist_tools()` で todowrite の説明をオーバーライドし、MANDATORY フォーマットルールを注入：タイトルに WHERE/WHY/HOW/RESULT をエンコード、原子粒度、同時に1つの in_progress のみ、subagent 返却前に completed マークしない。

### E3: 継続強制器 — 事後クロージャのコア ★★

turn 終了後に未完了 todo があれば、システムが自動的に継続メッセージを注入して LLM を引き戻します。モデルの自覚に依存しません。

**主要定数**：`_MAX_STAGNATION = 3`（連続3回変化なし → 停止）、`_BASE_COOLDOWN_S = 2.0`（基本バックオフ）、`_MAX_COOLDOWN_S = 60.0`（最大バックオフ）、`_FAILURE_RESET_WINDOW_S = 300`（5分間失敗なし → リセット）、`_MAX_RECOVERY_ATTEMPTS = 2`（リカバリモード上限）。

**ワークフロー**：

```
turn ends (no tool_call, agent loop exits)
  → Summarization.aafter_agent (rebuild system prompt with latest todos)
  → TodoContinuationEnforcer.aafter_agent
      → is_abort_error? → skip
      → get_todos_sync(session_id) → filter incomplete
      → check_stagnation: snapshot compare, N consecutive no-change?
          → should_enter_recovery? → RECOVERY_PROMPT
          → else: stop continuation
      → is_in_cooldown? → skip
      → build continuation prompt (full todo status + flow/step)
      → maybe_trigger_auto_turn(session_key, prompt)
          → detect_state() → idle? → fire-and-forget
          → _watch_user_takeover() 0.5s poll
      → user sends message → detect_state() busy → cancel → reset()
```

**ファイル**：`stagnation_tracker.py`（~90 行）+ `todo_continuation/core.py`（~110 行）+ `agent/core.py`（~2 行登録）。

### E4: 遷移バリア — 二重保険

**プロンプトレベル**（E1 AGENTS.md）：subagent 実行中 / TaskFlow step 未完了時に completed をマークしない。

**コードレベルハードブロック**（`service.py`）、2つのソース、どちらも todolist 内にスケジューラを構築しない：

1. **TaskFlow step ステータス**：todo が `flow_id`/`step_id` に関連 → `done` のみ `completed` を許可。`blocked`/`ready`/`dispatched` はすべてブロック。
2. **subagent registry liveness**：todo が `subagent_id` に関連 → `get_run_by_child_session_key` + `is_live_unended_run` で子セッションが実行中か判定。

```python
for todo in validated:
    if todo["status"] != "completed":
        continue
    # ソース1：TaskFlow step ステータス
    flow_id, step_id = todo.get("flow_id"), todo.get("step_id")
    if flow_id and step_id:
        step_status = _read_taskflow_step_status(flow_id, step_id)
        if step_status is not None and step_status != "done":
            raise TodoStoreError(
                f"Cannot mark todo completed: TaskFlow step {step_id} is '{step_status}'. "
                "Call taskflow_wait_all then taskflow_resume to inject the result first."
            )
    # ソース2：subagent liveness
    if todo.get("subagent_id") and _is_subagent_running(todo["subagent_id"]):
        raise TodoStoreError(
            f"Cannot mark todo completed: subagent {todo['subagent_id']} is still running."
        )
```

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

**実装**：`verifier.py`（~80 行）+ `subagent_completion_drain/core.py` 拡張（~15 行、subagent 完了後に Sisyphus 検証リマインダーを追記）。

### E6: 委譲ルーティング ★

**ライフサイクル**：

```
LLM creates todo (with category + delegation fields)
  → #a fan-out reminder (first time per session)
  → #b category tells LLM which subagent type to spawn
  → #c AGENTS.md guides how to delegate
  → Dependent steps: taskflow_run_task(depends_on=[...]) → blocked or dispatched
  → Ready steps: taskflow_dispatch(flow_id, step_ids) → get child_session_key
  → LLM updates todo's subagent_id + flow_id/step_id
  → #d-prompt: "Do NOT mark done before step done / subagent returned"
  → #d-code: update_todos() checks step status + is_live_unended_run() → hard block
  → taskflow_wait_all → taskflow_resume → E5 Sisyphus verify → LLM marks completed
```

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

**ループ防止設計**：E7b が E7a より優先、E7a はセッション毎1回（`_armed_sessions` Set。armed 済みなら短いリマインダーのみ）、E7b は `before_model` のみで動作（E3 の `after_agent` 継続は E7b をトリガーしません）、`_is_system_directive()` でシステム注入メッセージをフィルタ（E7 注入は `metadata={"origin":"task_intent","internal":true}` を持ち、ストアとフィルタが内部メッセージとして正しく識別し、実ユーザー要求とは決して扱わず、セッションタイトルやユーザー要求抽出にも入りません）、E3 はバックオフ冷却あり、圧縮後に re-arm。

**ファイル**：`agent/middlewares/task_intent/core.py`（~160 行）+ `agent/core.py`（~2 行登録）。
