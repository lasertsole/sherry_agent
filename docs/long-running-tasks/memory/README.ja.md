# 🧠 メモリと継続性

[English](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> [Long-Running Tasks](../README.ja.md) の一部：二層メモリシステム、圧縮前メモリフラッシュ、要約 ↔ TaskFlow 連携、サブエージェントメモリ還流、ツール出力の一行要約、セッション継続性、TaskFlow 自動再開。

---

## 🧠 階層メモリ

2 つの層は、*どのように*モデルへ届くかで区別されます：

| 層 | ストア | 場所 | プロンプトに入るか？ |
| :--- | :--- | :--- | :--- |
| **L1 —— 精錬メモリ** | `MEMORY.md`（エージェントのノート）+ `USER.md`（ユーザープロファイル） | `workspace/memory/`（`MEMORY_DIR`） | はい——凍結スナップショットとして常に注入 |
| **L2 —— 生の履歴** | `mes_memory.db`（SQLite、WAL、FTS5） | `src/store/mes_memory/mes_memory.db` | いいえ——`context_engine` / `message_search` が取得 |

ファイルは**単独行の区切り文字 `§` で区切られたプレーンテキストのエントリ**です——`ENTRY_DELIMITER = "\n§\n"`（`agent/tools/memory.py:53`）。YAML frontmatter も箇条書きプレフィックスもありません。エントリは複数行にわたれます。

層 1 は `MemoryStore` が管理します（`memory.py:104`）：ファイルごとの文字数上限は `2200`（memory）と `1375`（user）、注入スキャン（`_MEMORY_THREAT_PATTERNS`、`memory.py:68`）がプロンプトインジェクションと認証情報漏洩を拒否し、クロスプラットフォームのファイルロック、アトミック書き込み、完全一致の重複排除を備えます。ライブのエントリは即座に変更される一方、プロンプトは `load_from_disk()` で取得された**凍結スナップショット**を使い、セッション中のプレフィックスキャッシュを安定させます。

`memory` ツールは `scope="main_only"` とタグ付けされているため、サブエージェントには決して見えません。

**グラフ状態チェックポイントストア。** セッションの LangGraph 状態は `src/checkpoints/sqlite.db` にも永続化され、上記 2 層とは別です：`built_agent()` の呼び出しごとにスレッドごとの最新チェックポイントへ剪定され（`ThreadSafeAsyncSqliteSaver.aclean_old_checkpoints`、`agent/core.py:227`）、`auto_vacuum=0` では DELETE はページを解放するだけでファイルを縮めないため、同じ呼び出しが剪定直後に `PRAGMA freelist_count × page_size` を読み、解放された領域が `_VACUUM_THRESHOLD_BYTES`（10 MB、`agent/checkpointer/thread_safe_checkpointer.py`）を超える場合にのみ `VACUUM` を実行します——フェイルオープン：VACUUM のエラーはログに記録されるだけで、剪定結果はそのまま有効です。

## 🔥 圧縮前メモリフラッシュ

要約ミドルウェアが古いメッセージを破棄する前に、`agent/middlewares/summarization/memory_flush.py` は安価なモデルへ、永続的な事実を `MEMORY.md` に保存する最後の機会を与えます。トリガーは `should_flush(discarded_messages, estimated_tokens)`（`memory_flush.py:43`）：

```python
if not MEMORY_FLUSH["enabled"]:
    return False
total_chars = sum(len(_msg_to_text(m)) for m in discarded_messages)
if total_chars >= MEMORY_FLUSH["force_flush_chars"]:   # 50_000
    return True
return estimated_tokens >= MEMORY_FLUSH["soft_threshold_tokens"]   # 8_000
```

発火すると、`run_memory_flush`（非同期）/ `run_memory_flush_sync` が注入されたファクトリでモデルを構築し、単一のプレーンテキスト抽出プロンプト（`_FLUSH_PROMPT`、`memory_flush.py:19`）を使います。出力は `§` で区切られた `Environment / Project / Decision / User / Tool` の事実リストです。空の結果やリテラル `(none)` はスキップされます。抽出テキストは `MemoryStore.append_entries(new_entries)`（`memory.py:281`）へ渡され、`§` で分割し、各候補を注入スキャンし、既存集合と重複排除し、追記し、2200 文字を超える間は最古のエントリを追い出し、最後に一度のアトミック書き込みを行います。`append_entries` は常に `MEMORY.md` を対象にします。すべての失敗経路は `False` を返して握りつぶされます——フラッシュが圧縮をブロックすることは決してありません。

⚠️ **配線状況。** `Summarization.__init__` は `memory_store` / `llm_factory` を受け取り（どちらも既定 `None`、`summarization/core.py:259-260`）、両方が設定されている場合にのみ、`_apply_compression`（`summarization/compression.py:138`）と `_aapply_compression`（`summarization/compression.py:221`）の中でフラッシュを呼びます。現在の本番インスタンス——メインエージェント `agent/core.py:204` とサブエージェント `agent/tools/subagent/spawn/core.py:909`——はこれらを渡して**いません**。したがってフラッシュは実装・テスト済みですが、呼び出し箇所がストアと `factory(model=…, max_tokens=…, timeout=…)` の形のファクトリを提供するまで潜在状態にあります。

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
    memory_store.load_from_disk()
    for target in ("memory", "user"):
        memory_store.save_to_disk(target)
```

親と子は**単一のプロセス全体 `MemoryStore`** を共有するため、子の書き込みはすでにファイル可視です。ドリフトしうるのは親のインメモリビュー——ライブエントリと、システムプロンプトの構築に使った**凍結スナップショット**——であり、これはプロセス外の書き手が `MEMORY.md` / `USER.md` を更新したときに起こります。**先に再読込**する順序が要です：古いインメモリ一覧を再読込前に永続化すると並行書き手を上書きしてしまうため、照合はターゲットごとに load → persist でなければなりません。

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
"taskflow_summary"   -> "[taskflow_summary] {len} chars"
"taskflow_run_task"  -> "[taskflow_run_task] dispatched, {len} chars"
"taskflow_resume"    -> "[taskflow_resume] injected result, {len} chars"
"memory"             -> "[memory] {first 80 chars}..."
default              -> "[tool] output {len} chars, first 100: ..."
```

`prune_tool_outputs(messages, protect_tokens=…, min_reduction_tokens=…, protected_tools=None, estimator=None)`（`tool_output_prune.py:104`）は新しい順から古い順へメッセージを走査し、最初の要約メッセージで停止し、最新の `prune_protect_tokens`（40 000）を保護し、保護対象ツール（`{"memory", "skill_view", "skill_list"}`）をスキップし、解放トークンが `prune_min_reduction_tokens`（5 000）に達した場合にのみ確定します。置換されたメッセージは `additional_kwargs["status"] = "compacted"` と `["original_length"]` を持つ `model_copy` クローンです。要約は 200 文字に制限され、テンプレート例外はマーカーへフォールバックします。呼び出し元は `Summarization._run_non_llm_strategies` です（`summarization/compression.py:314`）。

## 🔄 セッション継続性

セッションがクリアされるとき、`context_engine/session_continuity.py` が終了状態を永続化し、次のセッションが継続性を提示できるようにします。`server/DAO/messages.py::clear_session` は削除の前に `auto_save_on_session_end(session_id)` を**ステップ 0** として呼びます（`server/DAO/messages.py:27-33`）。この関数は：

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

## ♻️ TaskFlow 自動再開

アクティブな flow はシステムプロンプトへ再浮上し、新しいセッションが未完了の作業を引き継げるようにします。3 つの独立した読み取りが同じレシピを使います——セッション単位の `get_active_flows_sync(session_id)` を `PromptDataProvider.get_active_flows(session_id)` 経由で取得します：

| 読み取り | 場所 | 目的 |
| :--- | :--- | :--- |
| `_build_taskflow_block` | `workspace/prompt_builder.py:145` | システムプロンプトの `## Pending TaskFlows` |
| `_get_taskflow_context_sync` | `agent/middlewares/summarization/core.py:122` | 圧縮要約プロンプトの TaskFlow ブロック |
| `_get_active_taskflow_ids_sync` | `context_engine/session_continuity.py:215` | 永続化された継続性状態の `taskflow_ids` |

`creator_session_key` は flow 作成時に `requester_session_key(session_id)` = `f"agent:main:session:{session_id}"` として刻まれます（`taskflow_create.py:38`、`_shared.py:21`）。`get_active_flows_sync()`（`store_sqlite.py:466`）は `running` と `waiting` の flow だけをリビジョン順で返し、イベントループを必要としない stdlib `sqlite3` パスを使います；失敗時は `[]` を返します。

システムプロンプトブロック（`prompt_builder.py:140`）は次の形です：

```
## Pending TaskFlows
- [running] flow-1: "<説明>" | 2/5 steps done | next: step-3 "<タスク>"
Use taskflow_summary to inspect a flow and continue execution.
```

最大 3 つの flow に制限され、ファイルフィルタ付きでプロンプトを構築するとき（`selected_file_names is not None`）は抑制されます。すべての読み取りはフェイルオープンです。

