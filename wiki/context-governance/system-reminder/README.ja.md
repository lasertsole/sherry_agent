# 🧱 安定したシステムプロンプトとリマインダー層

[English](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> [Context Governance](../README.ja.md) の一部：システムプロンプトが安定した内容だけを載せ、すべてのライブビューが各ターンの最初のモデル呼び出しで `<system-reminder>` HumanMessage として注入される理由。

---

システムプロンプトが載せるのは**安定**した内容だけです: persona ファイル、memory ブロック、スキル索引。すべての**ライブ**ビュー——プランモード、作業ディレクトリ、プロジェクト指示（`AGENTS.md`、フォールバック `CLAUDE.md`）、todo リスト、アクティブ作業ポインタ、TaskFlow ボード、継続コンテキスト、知識索引——は、各ターンの最初のモデル呼び出しで `<system-reminder source="…">` HumanMessage として注入されます（`DynamicReminderMiddleware` + `agent/middlewares/reminder_catalog/` のカタログ）。理由は 2 つ: 最初のトークンが毎ターン変わるプロンプトは、以降すべて（persona 含む）の prefix キャッシュを無効化する; キャッシュミス時しか再構築されないプロンプトは、次の再構築まで**古い** todo リストを見せる。

凍結されたメモリスナップショットには意図的な例外が 1 つあります。セッションのモデル切替は新しい provider キャッシュ名前空間に入り、思考モード切替は同一プレフィックスで `cache_read` が 0 になることを実測済みです——どちらもキャッシュは元々冷えます。`ThinkingControlMiddleware` の `before_model` フックは、そのターンの `(thinking key, profile fingerprint, mapping fingerprint)` を最後に適用した値と比較し（グラフは毎ターン再構築されるためプロセスレベルで保持）、変化があれば `system_prompt_injection` より前に `MemoryStore` のスナップショットを再読み込みし、両レジスタから `StateKey.SYSTEM_PROMPT` を削除します。切替後のターンの最初のモデル呼び出しは、セッション途中に書かれた `FACTS.md` / `MEMORY.md` / `USER.md` を見ます。初回ターン、変化なし、再読み込み失敗ではキャッシュを保持し、送信なしでは発火しません。

FACTS.md はグローバルなので、ディレクトリ切替でメモリ用のキャッシュプロンプトは無効化されません。同じターンの `project_instructions` リマインダーは新しいプロジェクトの `AGENTS.md`（フォールバック `CLAUDE.md`）を読み直し、`only_if_changed` により内容が変わるまで再注入しません。

| 関心事 | ルール |
|---|---|
| 何を注入するか | builder がテキストを返したソースごとに 1 件: 予算順は高→低、配信時は低→高に反転して最高優先度を末尾に置く。`origin=<ソース ID>`、`provenance="reminder_catalog"`、`internal=True` |
| 予算 | 呼び出しごとに全ソース合計で `REMINDER_CATALOG["total_budget_chars"]`（30 000）——超過時は最低優先度を丸ごと落とし、途中で切ることはしない |
| スロットル | ソースは `cooldown_turns`（TaskFlow ボード / boulder: 5）と `only_if_changed`（同一内容は繰り返さない）を宣言できる。管理状態は実際に注入されたソースだけ進む |
| 圧縮後 | 圧縮がカタログをリセットするため、次のターンに全ソースが live state から再注入される——リマインダー層は履歴の生存に依存しない |
| エスケープ | ペイロード内のネストした `<system-reminder>` タグ（todo テキストはユーザーの影響を受けうる）はエスケープされる。さもなければタグを早期に閉じてこの層を装える |
| ターン内の位置 | ターンの最初のモデル呼び出しのみ——TaskIntent と同じ「最後のメッセージが本物のユーザーメッセージ」判定。人間メッセージを走査するスキャナ（`split_into_turns`、HITL のオペレーター判定、要約ラベル）は `pub.func.message.system_reminder` 経由でリマインダーを飛ばす |

todo リストにはもう 1 つの防護があります: `PlanContinuationEnforcer`（`TodoContinuationEnforcer` から改名。監督する「プラン」= todos + TaskFlow flows）は `todowrite` を呼ばないターン数を数え、`TODOLIST_INFRA["todo_write_nudge_turns"]`（10）を超えると継続指令に `[REMINDER: TODO TRACKING]` を追記します——スナップショット方式の停滞検出が見逃す唯一の形（TaskFlow を進めると streak が消え、todo リストは腐る）です。

## 📋 登録ソース

カタログは 8 個のソースを登録します（`agent/middlewares/reminder_catalog/sources.py::register_default_sources`）。すべて同じチャネル（`CURRENT_TURN`）に乗り、すべて `only_if_changed=True` を持ちます——前回注入と**バイト同一**のペイロードは、クールダウンが過ぎていても再注入**されません**。カタログは**1 ターンにつき 1 回のみ、最初のモデル呼び出しで**解決されます（最後のメッセージが本物のユーザーメッセージのとき）。再開ターンがユーザーメッセージより前に既にブロックを持っていれば再注入をスキップします。

| ソース（`source_id`） | 優先度 | 予算（文字） | クールダウン | Transient | 注入内容 |
|---|---|---|---|---|
| `plan_mode_block` | 85 | 2 000 | 0 —— 毎ターン | No | プランモード状態 / 指示（エージェントがプランモード中か） |
| `todo_block` | 80 | 1 500 | 0 —— 毎ターン | No | エージェントの現在の todo / 作業プランリスト |
| `taskflow_status` | 75 | 2 000 | 5 ターン | No | TaskFlow ボード状態（ステア済みボード） |
| `boulder_status` | 70 | 1 000 | 5 ターン | No | Boulder / アクティブ作業ポインタ |
| `continuity_context` | 65 | 1 000 | 0 —— 毎ターン | No | ターン間の継続コンテキスト |
| `project_instructions` | 62 | 8 000 | 0 —— 毎ターン | Yes | プロジェクトの `AGENTS.md`（フォールバック `CLAUDE.md`） |
| `project_dir_notice` | 60 | 500 | 0 —— 毎ターン | No | 作業ディレクトリ通知 |
| `knowledge_index` | 50 | 1 000 | 0 —— 毎ターン | No | 知識索引 |

**頻度の読み方。** `cooldown_turns = 0` は*毎ターン対象*——前回注入から内容が変化すれば再注入されます; 内容がめったに変わらないソース（`project_instructions` など）は毎ターン対象でも注入はまれで、頻繁に変わるソース（`todo_block` など）はほとんどのターンで注入されます。`cooldown_turns = 5`（TaskFlow ボード、boulder）は*最大でも 5 ターンに 1 回*、かつ内容も変化したときのみ。変化していないボードは対象であっても 5 ターン沈黙します。スロットル管理状態は実際に注入されたソースだけ進み、抑制されたソースはクールダウンを「無駄に消費」しません。

**圧縮フィルタ。** 各 source は `compress_exclude`（デフォルト `True`）を宣言し、`DynamicReminderMiddleware` がそれをメッセージ metadata に刻み、`_filter_summary_messages` が要約 prompt を組み立てる前にそれらの reminder を落とします。live view は毎ターン再派生されるため、圧縮前のスナップショットはノイズです。`continuity_context` だけが `compress_exclude=False` を設定し、`[Continuity]` としてシリアライズされます。タグ導入前に注入された古い reminder はキーを持たず、従来の挙動を保ちます。実際のユーザーターンは `pub.func.message.classify.is_user_message`（`metadata.origin == "user"`、fail-closed）で判定されるため、注入された HumanMessage が要約で `[User]` とラベルされることはありません。

**ターン間の持続。** 非 transient のリマインダーはトランスクリプト内の `HumanMessage` であり、ミドルウェアの whole-list rewrite が後続ターンで `messages[:-1]` 経由で再送するため、圧縮で破棄されるまでコンテキストウィンドウに留まります（圧縮後の `reset_session` が変化検出ハッシュをクリアし、次ターンに live state から再注入します）。**Transient ソース**（`transient=True`）は例外です: 再解決されないターンでは、rewrite が `messages[:-1]` から古いリマインダーを剥がすため、`project_instructions` は内容が変化したターン（および圧縮後の最初のターン）にだけ現れます。モデルは一度読めば十分で、以後の全ターンを占有しません。

**注入ゲートチェーン。** ソースが特定のターンに注入されるには、以下のチェックを順番にすべて通過する必要があります（`core.py` の `resolve_injections` + `dynamic.py` のミドルウェアゲート）:

1. **カタログ有効** —— `REMINDER_CATALOG["enabled"]` が `True`; オフなら何も注入されません。
2. **ターンの最初のモデル呼び出し** —— ミドルウェアは最後のメッセージが本物の（注入されていない）`HumanMessage` のときのみ発火; ターン内のツールループ呼び出しと、既にブロックを持つ resumed ターンはスキップされます（`_already_injected_this_turn`）。
3. **クールダウン経過** —— `cooldown_turns > 0` かつソースが以前注入済みの場合、前回注入から少なくとも N ターン経過が必要; 未注入のソースは即座に通過します（turn 0 から冷却しません）。
4. **builder が内容を返す** —— builder が実行; `None`・空・例外はスキップ（fail-open——壊れた 1 ソースが層全体を殺すことはない）。
5. **内容が変化** —— `only_if_changed=True` の場合、内容の SHA-256 ハッシュを最後の注入ハッシュと比較; 同一内容はスキップ。
6. **総予算** —— 生き残った候補は優先度降順で並べ替え; 収まるまで最低優先度のソースを丸ごと落とします（`REMINDER_CATALOG["total_budget_chars"]` = 30 000）。落とされたソースは表示済みと**みなされず**、次ターンも対象のままです。

**優先度が予算順序。** `resolve_injections` は優先度降順で並べます; `REMINDER_CATALOG["total_budget_chars"]`（30 000）上限の下、最低優先度のソースを丸ごと落とし（途中で切ることはしない）、残りが収まるまで繰り返します。配信は保持セットを反転させ、最高優先度のビュー（プランモード、次に todo リスト）をブロック末尾、ターンのユーザーメッセージの直前に置きます——ライブ状態が先、リクエストが後。

**ツール結果エンリッチメント**は別の呼び出し単位のキューです: ツール返却後に `ToolMessage` へアドバイスを追加します(JSON ツールは `advisory` フィールド、プレーンテキストは末尾の接尾辞)。このターン単位のリマインダー層とは直交します — [ツール結果エンリッチメント](../tool-result-enrichment/README.ja.md) を参照。

**本カタログ外。** `TaskIntent` ステアリング、todo 継続指令、ワークスペース通知はそれぞれ独自の注入経路を保ち、ここには登録されません; カタログはオーバーレイで、移行は段階的です。
**DelegationPressureGate** (`agent/middlewares/delegation_pressure/core.py`) は warn / escalated 指令を `<system-reminder source="delegation_pressure">` として、標準の提醒 metadata(`provenance="reminder_catalog"`) 付きで注入しますが、カタログの `resolve_injections` ではなく独自の `abefore_model` 経路を使います。カタログはターンの最初のモデル呼び出し(最後が本物のユーザーメッセージのとき)にしか解決しませんが、delegation pressure はターン途中(7 / 14 回のツール呼び出し後、続くモデル呼び出し)に発火するため、first-call-only ゲートでは止まってしまいます。概念的には最高優先度です: 発火時カタログは no-op で、指令はメッセージリスト末尾、生成点に最も近い位置に追加されます。ツールは利用可能なままで、警告するだけでブロックしません。
