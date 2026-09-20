# 🧠 Summarization 内部 — 切り詰め、圧縮、要約、ガード

[English](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> [Summarization](../README.ja.md) の一部：内部トラック — 予算切り詰めと TTL、圧縮パイプライン、メディア・オフロード、LLM 要約チェーン、静的フォールバック、出力ペア、スラッシング防止マトリクス、システムプロンプト更新、登録箇所。

---

## ✂️ 切り詰めトラック：予算切り詰めとTTLモジュール

`_run_budget_truncation`（:659）の中では 2 つの切り詰めレイヤがこの順で走ります:

**ステップ 1 —— ツール呼び出し引数**（`pub/func/message/tool_args_truncate.py`）: JSON 直列化が `MIN_ARGS_CHARS_TO_TRUNCATE (500)` 文字を超えるすべての `AIMessage.tool_calls[].args` —— そのツールが `PROTECTED_TOOLS` に含まれない場合 —— は、`MAX_TOOL_ARGS_CHARS (2_000)` 文字で封印される `{"_truncated_args": "head…[args truncated, omitted N chars]…tail"}` に置き換えられます（先頭 30% / 末尾 30%、ツール結果と同じ比率）。これで `args` は dict のまま（LangChain の `ToolCall.args` 型）で、すべてのプロバイダアダプタに対して JSON 直列化可能を保ち、モデルは引数が切られたことを見て取れます。直近 `TRUNCATABLE_RECENT_SKIP (6)` 件のメッセージはスキップされ、置き換えられた `AIMessage` は `model_copy` クローンです —— tool_call_ids は決して触られないため、AIMessage↔ToolMessage ペアリングは完全に保たれます。

**ステップ 2 —— ツール結果**: `pub/func/message/tool_result_ttl.py` は、切り詰めトラックが使うその場での切り詰めを提供します。設計不変量（構造を支えるもの）:

- **その場でのみ** —— このモジュールはメッセージを削除・並べ替え・pop しません; `msg.content`（または content リストブロック）を変更してインデックスを返すだけです。これがプロバイダ API と `ToolCallNormalize` が依存する tool-call/`ToolMessage` ペアリングを守ります。
- **プレースホルダは空にしない** —— 切り詰められた結果は常に空でない内容を保持します: `ToolCallNormalize.before_model` は**空の `ToolMessage` をドロップ**してトランスクリプトを浄化するため、空のプレースホルダは静かにペアリングを壊します。
- **先頭 30% / 末尾 30% 保持**（`CONTENT_HEAD_RATIO` / `CONTENT_TAIL_RATIO`）に省略マーカーを付けます。

ミドルウェアが実際に消費するもの: `truncate_tool_args`（ステップ 1、引数）と **`truncate_to_budget`**（ステップ 2、ツール結果）。ルーターの候補リストに駆動され、`_run_budget_truncation`（:659）が予算（`usable × TRUNCATE_BUDGET_RATIO`）を満たすまで候補を切り詰めます。ステップ 1 はミューテートせず新しい `AIMessage` を返すため、この関数は最終リストを返し、すべての呼び出し元はそのリストを `request.override` に**必ず**渡さなければなりません。

**read_file の結果は復元可能なまま**: `pub/func/message/target_truncation.py` の先頭+末尾クリップ（非 LLM 戦略 `_run_non_llm_strategies` が実行）は、各 `ToolMessage` を `tool_call_id` で AIMessage のツール呼び出しに引き戻します; ツールが `read_file` で `args.file_path` がある場合、切り落とされた中間は匿名マーカーではなく復元通知に置き換わります。通知は同じ先頭 30% / 末尾 30% の比率を保ち、元の `file_path` を明記し、`Use offset=<N> to continue reading: read_file(file_path='<path>', offset=<N>, limit=500)` を示します。`N` は**先頭に完全には残っていない最初の行の絶対（1-based）ファイル行番号** —— したがって `offset=100` で読んだページは先頭が実際に止まった位置から再開し、途中で切れた行は再読され、決してスキップされません。offset を導出できない場合（ペイロードが read_file の JSON 結果でない場合）、通知は `offset=1` からの再読を求めます —— 推測した offset は決して出しません。その他のツールは匿名の `...[truncated N chars]...` マーカーをバイト単位で保ちます。

TTL レジストリ本体（`record_first_seen` / `select_expired` / `truncate_expired`、`PRUNE_TTL_SECONDS = 300`、`TTL_REGISTRY_MAX_ENTRIES = 512`、`tool_call_id` キー、再起動で揮発）は、現在**テストスイートだけが使用**しています —— ミドルウェアには年齢ベースの有効期限ロジックは接続されていません（「[正直な限界](../README.ja.md#%EF%B8%8F-正直な限界)」参照）。

## 🔁 圧縮トラック：`_apply_compression` の内部

`_apply_compression`（:1688; 非同期ツイン :1760）は次の順で実行されます:

1. **復帰コンテキストの捕捉**（`_capture_recovery_context`、:1577）: 最後のユーザー要求（≤ 800 文字）とファイル操作ラチェット —— `read`/`write` 系ツール呼び出しからパスを抽出し（:415）、前回分の集合とマージ（読んだものは記憶され、変更されたファイルが読み取り専用に格下げされることはない）。
2. **非 LLM 戦略**（`_run_non_llm_strategies`、:1493）: `重複排除 → プルーン → ターゲット切り詰め → ツール引数切り詰め`（詳細は [切り詰めトラック](#-切り詰めトラック予算切り詰めとttlモジュール)）。これらはタダです —— モデル呼び出しなし。
3. **LLM を使うかの判定**:

   ```
   if tokens_after_non_llm > budget × 2  OR  skip_llm  OR  nothing was reduced:
       summarize [0:cutoff] and rebuild   → strategy "llm_summary" / "fallback"
   else:
       keep as-is                          → strategy "non_llm_sufficient"
   ```

   非 LLM での縮小が最初の機会を得ます; 履歴がまだ保持予算の 2 倍を超えるとき（またはガバナが LLM 要約を無効化しているとき、または非 LLM 戦略が何も減らせなかったとき）にのみ、補助 LLM に金を払います。
4. **アグレッシブ・バックストップ**（`_aggressive_truncate`、:1539）: 結果が*それでも*大きすぎる場合、`AGGRESSIVE_TRUNCATE_CHARS (1 000)` 文字を超えるすべての `ToolMessage` がマーカー付きでハードカットされます —— 同じ上限を超えるすべてのツール呼び出し引数 JSON も同様です（先頭のみ、`PROTECTED_TOOLS` は免除、`{"_truncated_args": ...}` に置き換え）。
5. **要約の自己切り詰め**（`_truncate_summary_messages`、:1630）: `SUMMARY_TOTAL_MAX_CHARS (16 000)` 文字を超える既存の要約メッセージ（`lc_source == "summarization"`）は、先頭 30% / 末尾 30% に再切り詰めされます（`_truncate_content`、:1622）。
6. **復帰コンテキストの注入**（`_inject_recovery_context`、:1595）: 捕捉したファイル操作ラチェットが要約の `## Relevant Files` セクションに書き込まれ、チェックポイントが常に最新の読み取り/変更ファイルマップを運ぶようにします。
7. **帳簿記録**（`_record_compression`、:1277）し、最後に `request.override(messages=..., system_message=...)`。

### 💾 圧縮時 nudge

メッセージ永続化は圧縮パスとは別に動作します: `MessagePersistenceMiddleware`（`agent/middlewares/message_persistence/`）が新しい human/AI メッセージをモデル呼び出しの各境界で、ツール結果を返却時に MesMemory へフラッシュし、永続ウォーターマーク `persisted_message_ids` で write-once を保証します。compact は圧縮と下記 nudge のスケジュールだけを行います。トリガー意味論は `agent/middlewares/README.md` を参照してください。

**圧縮時 nudge**（`agent/middlewares/summarization/nudges.py::schedule_compression_nudges`）: メモリレビュー（`_nudge_memory`）は圧縮のたびにディスパッチされます; プラン抽出は同じ時点で `_detect_todo_all_complete` を評価します。どちらも NUDGE レーン上で fire-and-forget でディスパッチされ、モデル呼び出しをブロックしません。nudge ロックが保持されている間、圧縮はディスパッチを完全にスキップします（キューイングなし）。単発の `nudge_plan_extraction_fired` フラグは完了サイクルごとに 1 回の抽出を保証します —— そのため、一度も圧縮しないセッションはプラン抽出を発火しません。

**カットポイント選択**（`_determine_cutoff`、:1310）: 履歴をターンに分割し、**最新から逆方向**に歩きながら保持予算 `clamp(window × 0.25, 2 000, 15 000)`（`_calculate_preserve_budget`、:565）に照らして累積します; 丸ごと入らないターンはターン途中で割られることがあります。`_adjust_for_orphan_pairs`（:1340）がカットポイントを逆に歩き、`ToolMessage` が `AIMessage` のツール呼び出しから分離する状態がなくなるまで調整します。最終ターン比率ゲートが発火しない限り（最後のユーザーターン ≥ 全トークンの `LAST_TURN_RATIO_THRESHOLD (0.5)` —— `_check_last_turn_ratio`、wrap 入口 :1968/:2054 で呼び出し）、カットポイントが最後の `HumanMessage` を超えることはありません。

すべての失敗モードは fail-open です: `_apply_compression` が例外を投げてもログに記録されるだけで、元のリクエストがそのまま進行します —— 壊れた圧縮がターンを壊すことはありません。

## 🖼️ 圧縮時メディア・オフロード（インライン・メディア → 参照）

圧縮が base64 ペイロードを補助要約モデルに渡してはなりません。要約されるプレフィックス（`current_messages[:cutoff]`）を直列化する前に、`offload_inline_media`（`agent/middlewares/summarization/media_offload.py`）が**その範囲だけ**のすべてのインライン・メディアブロックを書き換えます:

- `data:` URL / base64 ペイロード（`image_url` / `audio_url` / `video_url`、裸の `base64` フィールド、`audio_bytes` / `video_bytes`、または Anthropic 形式の `source.data`）をデコードし、`SESSIONS_DIR/<session_id>/media/{sha256(raw)[:16]}{ext}` に書き込みます; 拡張子はマジックバイトから `media_handlers._infer_extension` で導出します;
- 同一バイトは一度だけ書き込みます —— 内容ハッシュのファイル名が単一回および複数回の圧縮をまたいで重複を排除します;
- ブロックはテキストポインタ `[evicted to: <path>]` に置き換わります。`pub/func/message/eviction.py` が出すのと同じマーカーなので、`_collect_evicted_refs` が拾い、`_finalize_summary_doc` がパスを `SummaryDoc.evicted_refs` に運びます（*Evicted References* として描画）;
- デコードまたは書き込みに失敗したブロックは `<media error="failed_to_offload" />` になります —— メディアの失敗が圧縮を壊すことはありません。

保持ウィンドウ（`current_messages[cutoff:]`）は決して触りません: メディアはそのターンが実際に要約されるまで残ります。両方の圧縮経路が nudge スケジュールとメモリフラッシュの前にプレフィックススライスに対してオフロードを呼びます —— `_apply_compression_under_lock`（同期）と `_aapply_compression_under_lock`（非同期）。要約プロンプト（`_SUMMARY_JSON_RULES` / `_SUMMARY_TEMPLATE`）は、ポインタが存在すること、`evicted_refs` に逐語で持ち越すこと、視覚/音声/動画の詳細を捏造しないこと、ペイロードはパスから取得できることをモデルに伝えます。

メディアファイルはセッションツリー内にあるため、`clear_session()` が `evicted/` と `plans/` とともに削除します。

## 📝 LLM要約：プロンプト、チェイニング、フォールバック

`_create_summary` / `_acreate_summary`（:1410 / :1435）:

1. **直列化**（`_serialize_for_summary`、:258）: 各メッセージがタグ付きの 1 行になります —— `[User]:`（≤ 2 000 文字）、`[Assistant]:`（≤ 2 000 文字）、`[Assistant tool call]: name(args: > 500 chars → head 300 + tail 150 + omission marker)`、`[Tool result|Tool error] (id):`（> 2 000 文字 → 1 800 文字保持 + 省略マーカー）。
2. **前のチェックポイントのチェイニング**（`_extract_previous_doc` / `_extract_previous_summary`）: `additional_kwargs["lc_source"] == "summarization"` を持つ最新の `AIMessage` を探し、まず構造化 `summary_doc` ペイロードとして読み取ります（`<prior-summary>` 用に Markdown へ再レンダリング）。ペイロードのないメッセージ —— レガシーセッション、または free-form にフォールバックした回 —— は従来どおり `<summary>…</summary>` 本体から解析します。前回の Doc が存在すれば、プロンプトは `conversation + <prior-summary-json> + _SUMMARY_PROMPT_UPDATE_STRUCTURED` になります。レガシー Markdown は `<prior-summary>` 経由でそのまま注入され、アップグレード後の最初の圧縮ラウンドで新しい `SummaryDoc` が出力されます（移行不要）。
3. **構造化出力**（`summary_doc.py::SummaryDoc`）: 補助モデルは `with_structured_output(SummaryDoc, method="json_mode")` でラップされます。設定済みの `glm-5.3-flash` エンドポイントは関数呼び出しスキーマを無視するため（デフォルト method は自由テキストを返し Pydantic パーサーに拒否されます —— 実測済み）、`json_mode` が主ティアです。解析/検証の失敗は生呼び出し + `json_repair`（`_sync_json_repair_doc` / `_async_json_repair_doc`）へ降格し、それも失敗すればレガシー free-form Markdown プロンプトが最後の LLM ティア、静的フォールバックが最終ガードです。
4. **呼び出し**は補助モデルに対し `config={"metadata": {"lc_source": "summarization"}}` 付きで行われ、下流のツールチェーンが要約呼び出しを識別できるようにします。
5. **ガードレール:** free-form 応答が空または極端に短い場合は決定論的要約へフォールバックし、例外も同様です。失敗時に LLM が最後の言葉を持つことはありません。

**レンダリング（Doc → Markdown）。** `render_summary_markdown` は純粋かつ決定論的（同じ Doc → 同じバイト、プレフィックスキャッシュ安全）です。従来のセクション骨格を出力し、配列が非空の場合のみ *Active Plan Notes* / *Evicted References* を追加します:

| `SummaryDoc` フィールド | レンダリングされるセクション | コード層 cap |
| :--- | :--- | :--- |
| `latest_user_request` | `## Latest Unresolved User Request` | 逐字、上限なし |
| `goal` | `## Goal` | — |
| `constraints` | `## Constraints & Preferences` | — |
| `completed` | `### Completed` | `completed[-5:]` |
| `in_progress` / `blocked` | `### In Progress` / `### Blocked` | — |
| `key_decisions` | `## Key Decisions` | `key_decisions[-5:]` |
| `next_steps` | `## Next Steps` | — |
| `critical_context` | `## Critical Context` | `critical_context[-3:]` |
| `relevant_files` | `## Relevant Files` | — |
| `active_plan_notes` | `## Active Plan Notes`（非空時のみ） | `active_plan_notes[-20:]` |
| `evicted_refs` | `## Evicted References`（非空時のみ） | `evicted_refs[-20:]` |

cap は `cap_summary_doc` の配列スライス（チェーンに保存される形）で、レンダラーは表示時に再度スライスし `"(N earlier items omitted for brevity)"` を追記します。`evicted_refs` はコードが維持します: `_collect_evicted_refs` が圧縮範囲内の `[evicted to: <path>]` マーカーと人間メッセージの `lc_evicted_to` タグを走査し、`_finalize_summary_doc` が前回 Doc のエントリと新規エントリを順序どおり重複排除してマージします。`_inject_recovery_context` はファイル操作ラチェットを、レンダリング済み `## Relevant Files` セクションと保存 Doc の `relevant_files` フィールドの両方へ書き戻します。

### 🗂️ Active Plan Notes と計画コンテキスト注入

圧縮は計画を認識します。補助モデル呼び出しの前に、`_get_plan_context_sync(session_id)`（`core.py`、TaskFlow ブロックの隣）が、このセッションが実行中の計画を権威ブロックとしてレンダリングします:

- 計画のアクティブ判定は `agent/tools/todolist/knowledge/ownership.py` の 2 つの生の関連ソースを再利用します — セッションの `plan_ref` state キーと、このセッションのいずれかの todo の `plan_ref`（`plan_context.py::resolve_active_plan`）。**boulder ソースは意図的に除外**されます。
- 注入内容は**計画ファイルの相対パス + 計画名 + 未完了 todo の要約**で、計画本文は決して注入しません（モデルはパスを `read_file` できます）。todos がすべて `completed` / `cancelled` のセッションは**アクティブではありません**。
- このブロックは両方のプロンプト経路（初回要約とチェーン更新、構造化とレガシー free-form）に注入されます。

`SummaryDoc.active_plan_notes` はモデルではなくパイプラインが所有するため、計画スコープの教訓は計画がアクティブな間ずっと保持されます:

- チェーン更新では、前回 Doc のエントリを**逐語・順序どおりに継承**し、モデルは新しい一行の教訓（`症状 -> 回避策`）を追記することだけができます。
- 配列は `active_plan_notes[-20:]` で上限; チェーン上のレンダリングは `(N earlier items omitted for brevity)` を追記し、保存ペイロードは切り詰めた末尾を保持します。
- 計画が完了すると（todos がすべて `completed` / `cancelled`）、リゾルバは「アクティブな計画なし」を返し、レンダリングから `## Active Plan Notes` セクションが消え、次回 Doc の配列はクリアされます。`plan_ref` がまったく無いセッションも同様に配列を持ちません。

**最新ユーザーリクエストの逐語 + 退避ポインタ。** *Latest Unresolved User Request* セクションは決して切り詰めません: `latest_user_request` は逐語で提示され（旧テンプレートの `max 800 chars` 指示も削除）、シリアライズされる `<conversation>` は **state** から来ます — 退避された人間メッセージは state に全文と `lc_evicted_to` タグを保持し、モデルビューだけがプレビューです。最新ユーザーリクエスト自体が退避されている場合、パイプラインがその `[evicted to: <path>]` ポインタをフィールドに追記します（コード所有、`_latest_human_eviction_ref`）。これにより要約チェーンは常にディスク上の全文への道を保ちます。内部注入（`metadata.internal`）がこのアンカーを奪うことはありません。

**チェイニング要約のフィルタリング**（`_filter_summary_messages`）: 前のチェックポイントが存在する場合、その Human/AI ペアは直列化された `<conversation>` 入力から除去されます —— 抽出済みの前回要約は `<prior-summary>`（または `<prior-summary-json>`）経由でのみ注入され、古い要約テキストはプロンプト内にちょうど 1 回だけ現れます。ペアは**両方**が `additional_kwargs={"lc_source": "summarization"}` を持ち、これにより両方とも MesMemory に入りません（`MessagePersistenceMiddleware._is_persistable` + `HumanMessageRowBuilder`）: このペアは圧縮の内部成果物であり、会話履歴ではありません。フィルタは `_extract_previous_doc` / `_extract_previous_summary` の**後**に走り（チェイニングは旧要約を引き続き参照できます）、フィルタ後のリストが直列化・プロンプト構築・2 つの静的フォールバック分岐（LLM 失敗/短すぎる応答）に使われます —— `_apply_compression_under_lock` / `_aapply_compression_under_lock` の `skip_llm` パスも同様です。opencode-dev の `hidden` セットと deepagents の `_filter_summary_messages` に整合します。

レガシープロンプトテンプレート（`_SUMMARY_TEMPLATE`）は free-form フォールバックの骨格を引き続き固定します —— *Latest Unresolved User Request / Goal / Constraints & Preferences / Progress（Completed ≤ 5 · In Progress · Blocked）/ Key Decisions ≤ 5 / Next Steps / Critical Context ≤ 3 / Relevant Files* —— 「空でもすべてのセクションを保持する」ことと秘密保持ルール（"NEVER include API keys, tokens, passwords, secrets"）を要求します。構造化パスは Markdown 骨格を `_SUMMARY_JSON_RULES`（JSON フィールド一覧 + 同じ秘密保持ルール）に置き換えます。フィールドの意味は Pydantic `SummaryDoc` モデル自体に定義されています。

**ユーザー要求の送信元の正同定。** 永続化された各 `human` 行は `origin` を持ちます（トランスポート入口で刻印）: WS/チャネルのユーザー入力は `"user"`、オーケストレーターのステアリング注入は `"task_intent"`、完了キャリアは `"subagent_completion"`、Cron 配信ターンは `"cron"`（`ai`/`tool` 行は `NULL` のまま；origin タグ導入前の行はレガシー user メッセージとして読み取られます）。*Latest Unresolved User Request* の送信元はこの列で正同定されます: ユーザー送信元のみ（`origin = 'user'`、またはレガシー `NULL`）がユーザー要求であり、内部注入（`task_intent` / `subagent_completion` / `cron`）が要求として引用されることはありません。routing plan の複数形 `unresolved_user_requests[]` リストも同じ正フィルタを使います。

## 🧱 静的フォールバック（LLMを使わない要約）

`_build_static_fallback_summary`（:296）はモデル呼び出しゼロで同じセクション骨格を生成します:

- 最後のユーザー要求 → *Latest Unresolved User Request*; 最初の要求 → *Goal*;
- 決定キーワード（`decided`、`choosing`、`because`、`therefore`）を含む AI テキスト → *Key Decisions*、なければ *Completed*;
- すべてのツール呼び出し → *Completed*; パス風トークン（`/` または `\` を含む、または `.py`/`.md`/`.js`/`.ts`/`.json` で終わる）→ *Relevant Files*（≤ 10、`http` リンク除外）;
- エラーの `ToolMessage` → *Blocked* と *Critical Context*。

`skip_llm` が有効なときはそのまま使用され、短い/失敗した LLM 要約のセーフティネットにもなります。

## 📦 出力：要約メッセージペア

`_build_new_messages`（:1464）は要約テキストを包み、正確に 2 つのメッセージを出力します:

```
[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted …
Respond ONLY to the latest user message that appears AFTER this summary.

<summary>
…summary Markdown…
</summary>

--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---
```

- **HumanMessage** `"What did we do so far?"` —— 役割交代を維持する中立的な質問; AI 半分と同じ `lc_source` マーカーを持ちます。
- **AIMessage**、`additional_kwargs={"lc_source": "summarization"}` 付き —— このマーカーを後続のターンは (a) 前のチェックポイントを発見してチェーンし、(b) チェイニング再要約の入力からペアを除去し、プルーンがチェックポイントで停止するようにし、(c) テストが置き換え後の要約がモデル視点から丸ごと呑み込めることを検証するために使います。
- このペアが MesMemory に入ることはありません: `MessagePersistenceMiddleware._is_persistable` が `lc_source="summarization"` の両半分をスキップします（human 行ビルダーも同じゲートを持ちます）。
- AIMessage は構造化ドキュメント本体も保持します: `additional_kwargs["summary_doc"]`（cap 後の `SummaryDoc`、JSON 直列化可能な素の dict —— チェーンキャリア。`<prior-summary-json>` として再入力されます）。free-form フォールバックの回はこのペイロードを持たず、`_extract_previous_summary` が `<summary>` 本体の解析へフォールバックします。
- 合計コンテンツは `SUMMARY_TOTAL_MAX_CHARS (16 000)` で封印され、先頭/末尾 30/30 保持。

## 🛡️ スラッシング防止マトリクスと劣化リカバリ

状態はセッションスコープの `state_register_mem` の **13 個**の `summarization_*` キー（:92–107）にあります。`_reset_turn_state`（:1849）はターン開始時にそのうち **11 個**をリセットします; `summarization_last_user_question`、`summarization_cooldown_rounds` は意図的にターンごとにはリセットされ**ません**。

| ガード | キー | 閾値 | 効果 |
| :---- | :-- | :-------- | :----- |
| ターンクールダウン | `summarization_cooldown_rounds` | `COMPACTION_COOLDOWN_ROUNDS = 3` | 実際の compact のたびに武装（:694）; **すべての**モデル呼び出しが減算（:832）; T1 compact ルート、T2 主動、T3 を封鎖 —— T4/T5 強制リングは決して封鎖しない |
| ターンあたりの圧縮数 | `summarization_turn_attempts` | `MAX_COMPRESS_ATTEMPTS_PER_TURN = 3` | :694 が増加; T2 主動 + T3 を抑圧（強制リングは免除） |
| オーバーフロー再試行（T4/T5 共有） | `summarization_overflow_retries` | `MAX_OVERFLOW_RETRIES = 3` | 両エラークラスが共有しターンごとにリセット; 成功した強制ステップのたびに増加; 枯渇 → 元のプロバイダエラーが伝播 |
| セッション総圧縮数 | `summarization_compression_count` | `MAX_TOTAL_COMPRESSION_ATTEMPTS = 5` | `_should_skip_compression`（:1255）が True を返す —— 主動圧縮は完全停止 |
| 連続無効回数 | `summarization_compression_ineffective` | `INEFFECTIVE_THRESHOLD = 2` | `skip_llm` を設定 —— 非 LLM 戦略のみ |
| 有効性判定 | （`_record_compression`、:1277） | メッセージ数減少**または**トークン削減 ≥ `MIN_EFFECTIVENESS_PCT (0.05)` | 成功した非 LLM 戦略（`dedup`/`prune`/`truncate`/`fallback`/`aggressive`）が `skip_llm` を再び解除 |
| 劣化リカバリ予算 | `summarization_recovery_attempts` | `MAX_RECOVERY_ATTEMPTS = 2` | 劣化モニタが発動する強制リカバリの上限 |

**劣化モニタ**（`_monitor_degradation`、:1661）: この呼び出しで実際に圧縮が起きたときのみ参照されます（`_compaction_just_happened` フラグ）。モデルの応答にテキストがなければカウンタが増え、`DEGRADATION_NO_TEXT_THRESHOLD (3)` 回連続の空応答 —— かつ `summarization_recovery_attempts < 2` の間 —— で `force_recovery` を設定し、無効連続記録とセッション圧縮カウントをクリアします。空でない応答はすべてカウンタをリセットします。これは「圧縮 → モデルが混乱 → 空の出力 → 再圧縮」の病的ループを捉えます。相互作用に注意: force フラグは wrap 入口（:1973）で `_should_skip_compression` **より先に**読まれ、スキップゲートはカウンタをリセットして進むことでそのフラグを消費します（:1256–1261）—— リカバリ圧縮は正確に一度だけ実行されます。

## 🔄 システムプロンプト更新

メインエージェントのみ（`need_update_system_prompt=True`）: 圧縮後、ミドルウェアはシステムプロンプトを再構築して `system_prompt` 状態キーに書き込み、次のモデル呼び出しがペルソナファイル / 長期記憶を現時点のまま見るようにします。2 つの配送経路: 圧縮直後の `request.override(system_message=SystemMessage(...))`、および —— T1 の compact が既に起きたがアンチスラッシングゲートが 2 回目を封鎖したとき —— 再構築されたプロンプトはゲート経路でも配送されます（:1993–2005）。`@dynamic_prompt` システムプロンプトミドルウェアを持たないチェーン（サブエージェント / nudge パイプライン）はこのミドルウェアの配送に依存するためです。ゲート経路は、リクエストの現在の system message と**内容が異なる場合にのみ**注入します: 内容が一致していれば override も新しい `SystemMessage` も作らず（再注入しません）。

## 📌 登録箇所

```python
# agent/core.py:152 — メインエージェント（Summarization は最後のミドルウェア:
# 最も内側の wrap レイヤ、LLM に最も近い）
Summarization(
    need_update_system_prompt=True,
    model=auxiliary_llm,
    main_llm_context_window=main_llm_max_tokens,
    trigger=[("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO))],
    keep=("messages", 10),
)

# agent/tools/subagent/spawn/core.py:755 — ワーカーエージェント（最初のミドルウェア）
Summarization(
    model=auxiliary_llm,
    main_llm_context_window=main_llm_max_tokens,
    trigger=[
        ("messages", 40),
        ("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO)),
    ],
    keep=("messages", 10),
)
```
