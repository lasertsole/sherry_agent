# ライブ配信の安全フィルター

> 入出力のキーワード ルール、任意の LLM 審査、そして「送信前に走査する」ストリーム切断。

## 🎯 概要

この機能の以前は、モデルと視聴者の間でテキストを絞り込む仕組みがありませんでした。
フィルターはスイッチ（`sherry.jsonc` の `safety_filter.enabled`、既定はオフ）で
三つの層を有効にします。

1. **入力キーワード ルール** —— `block` に一致したらプロバイダを呼ばずに拒否します
   （`agent/middlewares/safety_filter/middlewares.py::InputSafetyFilter`）。
2. **LLM 審査（任意）** —— 設定した審査モデルが判定します。マスター スイッチ、`off` 以外の
   戦略、モデル名の三つが揃ったときだけ実行されます。
3. **出力キーワード ルール** —— 完成した回答は全体置換または部分置換され
   （`OutputSafetyFilter`）、ストリーム回答は
   `agent/middlewares/safety_filter/stream_guard.py` がチャンクごとに洗浄します。

ルールは利用者が管理します。アプリは**語リストを同梱しません**（語リストの選定は法務判断であり、
本プロジェクトが代行するものではありません）。ルールはワークスペース内の
`safety_filter.keyword_file` で指定したファイルにあります。

## ⚙️ 設定

`sherry.jsonc` を**ライブで読み込み**ます（mtime キャッシュ、再起動不要）。

- `safety_filter.enabled` —— マスター スイッチ。
- `safety_filter.llm_moderation_mode` —— `off` / `on_keyword_hit` / `always`。
- `safety_filter.llm_moderation_model` —— 審査モデル名。空欄なら戦略にかかわらず LLM 審査は動きません。
- `safety_filter.keyword_file` —— ワークスペース内のルール ファイル名。

設定画面は **システム設定 → ライブ安全**（`SafetyFilterPanel.vue`）です。マスター スイッチ、
LLM スイッチ（モデル名を入力するまで無効）、戦略の選択、テスト走査付きのルール エディタ、
最近のフィルター記録を表示します。

## 🧩 三つの層

- ルール形式: `{"rules": [{"pattern": "…", "type": "exact|contains|regex",
  "action": "block|replace|warn", "scope": "input|output|both",
  "replacement": "…"}]}`。保存前にルール数、pattern 長、置換テキスト長、正規表現の妥当性を検証します。
- `block` は拒否／全体置換、`replace` は一致部分の置換、`warn` は報告のみ。
  入力側でユーザーの本文を書き換えることはありません。
- 判定ごとにイベントを一件記録し（上限付きリング、新しい順）、`safety_filter` WS フレームを
  送信します。クライアントは toast を表示します（block はエラー レベル）。
- LLM 審査は**失敗時は通す**: タイムアウト、リクエスト失敗、解釈できない回答はそのまま
  ターンを続行し警告を残します。プロバイダ自身の整合性は依然として働きます。

## 🚂 ストリーム洗浄

`StreamTextGuard` は末尾 `stream_buffer_window` 文字（既定 20）を保持し、
走査済みのテキストだけを送り出します。

- バッファ内のどこかで `block` に一致したらラッチし、バッファを捨てて置換メッセージを一度だけ送ります。
- 解放点より内側で終わる `replace` 一致は置換してから送り、解放点が一致を分割する場合は解放点をその開始位置まで戻します。
- ウィンドウより長いパターンは末尾の flush でしか完了しないため、切断前に見えるのは最大 `len(match) - 1` 文字です。

ストリーム回答は `OutputSafetyFilter` を通りません。洗浄はフレーム生成の前に行われるため、
クライアントと保存された行は一致します。

## 🌐 HTTP API

| Method | Path | Body / query |
| :--- | :--- | :--- |
| `GET` | `/safety-filter/rules` | — |
| `PUT` | `/safety-filter/rules` | `{rules: [...]}` |
| `POST` | `/safety-filter/test` | `{text, scope}` |
| `GET` | `/safety-filter/log` | `?session_id=<sid>&limit=50` |

スイッチと審査モデルは `sherry.jsonc` のキーで、`PUT /sherry-config` から書き込みます
（専用エンドポイントはありません）。

## 🧪 テスト

`tests/agent/middlewares/safety_filter/test_rules.py`（検証、スコープ、ホット リロード、リテラル置換）、
`test_middlewares.py`（プロバイダを呼ばない入力拒否、warn/replace の通過、LLM のゲートと失敗時の通過、出力置換）、
`test_stream_guard.py`（保持ウィンドウ、ラッチ、送信前置換、末尾走査）。設定画面は
`client/app/pages/home/components/__tests__/SafetyFilterPanel.integration.test.ts` で固定しています。
