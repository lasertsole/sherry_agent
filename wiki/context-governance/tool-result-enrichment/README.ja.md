# 🔧 ツール結果エンリッチメントキュー

[English](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> [Context Governance](../README.ja.md)の一部: 登録ベースのキューが各ツール呼び出し後に `ToolMessage` の content にアドバイスヒントを追加します。[system-reminder カタログ](../system-reminder/README.ja.md)をモデル化していますが、ターンごとではなくツール呼び出しごとに発火します。

---

エンリッチメントミドルウェア(`ToolResultEnrichmentMiddleware`)は `wrap_tool_call` チェーンの**最外層**です — `ToolGuardrails` の前に登録され、他のすべてのミドルウェアが処理した最終結果を見ます。handler 返却後、登録されたすべてのインジェクターを実行し、`None` 以外の出力を収集して `ToolMessage.content` に注入します。

## JSON 認識デュアルモード注入

ミドルウェアは content タイプを自動検出し、注入方式を選択します:

| content タイプ | 注入方式 | 例 |
|---|---|---|
| 有効な JSON dict | JSON オブジェクト内に `"advisory"` フィールドを追加し、再シリアライズ | `read_file`: `{"content": "...", "truncated": false}` → `{"content": "...", "truncated": false, "advisory": "[Advisory: ...]"}` |
| プレーンテキスト | 末尾に `\n\n{suffix}` を追加 | `terminal`: `"Exit code: 0\noutput..."` → `"Exit code: 0\noutput...\n\n[Advisory: ...]"` |

**JSON フィールド注入が安全な理由**: `read_file` は `json.dumps({"content": ..., "truncated": bool, ...})` を返します。eviction / truncation パイプラインの 3 箇所(`eviction.py`, `target_truncation.py`)がこの content に `json.loads` を行いますが — `content` と `truncated` キーしか読みません。新しい `"advisory"` フィールドは無視されます。ツールレベルの宣言や除外セットは不要 — `try json.loads` 自動検出がすべてを処理します。

**error 結果はスキップ**: `status="error"` の ToolMessage(ToolGuardrails BLOCK/HALT, HITL 拒否など)はそのまま返されます — 失敗したツール呼び出しはヒントを受け取りません。

## 登録されたインジェクター

カタログは `ToolResultInjector` データクラスでインジェクターを登録します(`agent/middlewares/tool_result_enrichment/core.py`)。各々は `injector_id`, `builder(tool_name, tool_args, result, session_id) → str | None`, `priority`, オプションの `tools_filter`, `only_if_changed` を宣言します。

| インジェクター | 優先度 | `only_if_changed` | 対象 | 注入内容 |
|---|---|---|---|---|
| `delegation_hint` | 80 | `False` | すべてのツール | `read_calls >= warn_after`(7) または `>= block_after`(14) の時の委任圧力アドバイス。ゲートの `_PressureState` を直接読みます。 |
| `hook_context` | 60 | `True` | すべてのツール | `PostToolUse` フックの `additionalContext`(`UserHooksMiddleware`), `_dispatch` が stash しインジェクターが drain します。 |

**優先度ソート**: 優先度昇順で並べ替え — 低優先度が先に追加(テキスト上部)、高優先度が最後に追加(テキスト下部 — モデルの注意力に最も近い)。

## DelegationPressureGate 統合

ゲートの warn / escalated 指令は**唯一** `delegation_hint` インジェクターを通じて配信されます — `abefore_model` HumanMessage 注入はありません。`read_calls` が閾値に達すると、**すべての**後続ツール結果(read, write, すべてのツール)がアドバイスを持ちます:

- `read_calls >= 7`(`warn_after`): `[Advisory: N read/exploration calls this turn. Consider delegating via sessions_spawn...]`
- `read_calls >= 14`(`block_after`): `[Advisory: N read/exploration calls this turn — Delegate NOW via sessions_spawn...]`

成功した `sessions_spawn`(accepted/pending)は `_post_tool` で `read_calls` を 0 にリセット — spawn 結果自体はヒントを持ちません(インジェクターが 0 を読む → 閾値未満 → `None` 返却)。後続ツール結果も 7 回さらに read が蓄積するまでヒントなしです。

## UserHooksMiddleware 接続

`UserHooksMiddleware._dispatch` は各フックの stdout JSON から `additionalContext` をパースします(`HookOutcome.additional_context`)。Step 1(読み取り専用イベント)ではこのフィールドはパースされましたが破棄されました。エンリッチメントキューがこれを接続: `_dispatch` が `stash_hook_context(session_id, text)` でテキストを stash し、`hook_context` インジェクターが stash を drain して `[Hook context]: ...` としてツール結果に追加します。フックは結果をブロック、置換、書き換えできません — テキストの追加のみ可能です。

## system-reminder カタログとの比較

| | reminder カタログ | ツール結果エンリッチメント |
|---|---|---|
| 発火タイミング | 毎ターンの最初のモデル呼び出し(`before_model`) | 毎ツール呼び出しの返却(`wrap_tool_call`) |
| 運び手 | `HumanMessage`(独立メッセージ) | `ToolMessage.content`(フィールドまたは追加) |
| 優先度ソート | 予算割り当て(最低優先度を破棄) | 追加順(低優先度が先、高優先度が後) |
| 関係 | 直交 — 共存 | 直交 — 共存 |

ヒントはモデルが既に処理するツール結果に搭載されます — 独立メッセージを生成せず、注意力を奪いません。JSON ツール(`read_file`)のヒントはモデルが既にパースする JSON 内のフィールドであり、プレーンテキストツールのヒントは content 末尾の接尾辞です。どちらの場合もモデルの注意力はツールの実際の出力にあり、アドバイスは副次的に見えます。
