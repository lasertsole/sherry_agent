# Session Token 予算

> セッション単位の token 上限、ターンを跨ぐ使用量、推定コスト。

## 🎯 概要

セッションは任意の token **上限**（`StateKey.TOKEN_BUDGET_CAP`）を持てます。
`TOKEN_BUDGET_USED >= cap` になると、`TokenBudget`
（`agent/middlewares/token_budget/core.py`）が次の model call を終端
`AIMessage` で短絡し、provider へは到達しません。ターンは要約依頼で終わり、
操作者は上限を上げるかカウンタをリセットできます。

ミドルウェアは `IterationBudget` の隣にあり、常時オン（無効化不可）です。
上限がなければ完全な pass-through です。token 数は provider の
`usage_metadata` に由来し正確、コストは推定値です。

## ⚙️ 設定

- `PUT /sessions/budget` で上限を設定（`null` で解除、`0` は無制限）。ターン
  実行中の変更は `TOKEN_BUDGET_CAP_PENDING` に PARK され、ターン境界で
  promote されます（thinking/model コントロールと同じ）。
- `GET /sessions/budget` は `{cap, session_cap, default_cap, inherited, used, cost, limited, pending}`
  を返します。`cap` は次のターンに実際に適用される上限（継承した既定値を含む）、
  `session_cap` はセッション自身の選択（`null` は未設定、`0` は明示的な無制限）、
  `inherited` は `default_cap` を継承しているかどうかです。
- 上限を設定していないセッションは `sherry.jsonc` の `TOKEN_BUDGET_DEFAULT_CAP`
  を継承します（0 は無制限で、出荷時の既定値）。セッション自身の値が常に優先される
  ため、`0` にすると明示的な無制限となり、正の既定値で復活することはありません。
  `null` は継承に戻します。この値はプロセスごとに 1 回だけ読み込みます
  （ファイルは再起動後に反映）。モデル呼び出しごとの経路にあるためです。
- `POST /sessions/budget/reset` は `TOKEN_BUDGET_USED` /
  `TOKEN_BUDGET_COST` / `TOKEN_BUDGET_LIMITED` をクリアし、上限は保持。
- `TOKEN_BUDGET_USED` / `TOKEN_BUDGET_COST` はターンを跨いで累積し、mem + DB
  の両方へ書き込まれます。ターン単位の iteration budget ではリセットされません。
  `clear_session` は他の session state と一緒に削除します。
- `budget_warning` は `MODEL_PRICING["budget_warn_threshold"]`（0.80）で 1 回、
  `budget_limited` は上限初到達時に 1 回、WS へ best-effort で送られます。

- **Token 予算** はセッション単位の右パネル タブ（`TokenBudgetPanel.vue`、右パネル →「現在のセッション」）で、その会話の上限を編集し使用量をリセットします。入口はコンテキスト指標のポップオーバー下部です。上限がアプリ設定の継承かこのセッション専用かを表示し、上限欄を空にして保存すると無制限になります。タブのペイロードにセッションがない場合のみ空状態を表示します。
## 🧮 価格の解決

価格表は `.models` でユーザーが設定します：

```jsonc
"MODEL_PRICING_TABLE": "{\"glm-5\": {\"input\": 0.5, \"output\": 1.5}, \"_default\": {\"input\": 0.5, \"output\": 1.5}}"
```

値は JSON OBJECT を JSON 文字列にしたもので、フラットな `.models` の
key/value エディタでも扱えます。既存ファイルに表がなければ import 時に追加
されます（テンプレートが既定値を提供）。

`TokenBudget._cost_delta` は 4 段階でコストを解決します：

1. `usage_metadata` / `response_metadata` の provider 直返 cost
   （`cost` / `total_cost` / `estimated_cost`、または `prompt_cost` +
   `completion_cost`）をそのまま使用；
2. なければ `config/features/infra_side/model_pricing.py::resolve_model_pricing`
   がユーザー表（完全一致、次に `/` 以降の裸名）を参照；
3. 次にローカル litellm キャッシュ `src/data/litellm_prices.json`（gitignored。
   `uv run --no-sync python scripts/sync_litellm_prices.py` で更新）；
4. 最後に表の `_default`（表自体が使えない場合は `input $0.5/M` /
   `output $1.5/M`）。

上限は token のみで判定し、この推定値には依存しません。

## 🌐 HTTP API

| Method | Path | Body / query |
| :--- | :--- | :--- |
| `GET` | `/sessions/budget` | `?session_id=<sid>` |
| `PUT` | `/sessions/budget` | `{session_id, cap: int \| null}` |
| `POST` | `/sessions/budget/reset` | `{session_id}` |

## 🧪 テスト

`tests/agent/middlewares/test_token_budget.py`（短絡・累積・provider cost・
warning/limited イベント）、`tests/server/test_token_budget_settings.py`
（idle 書き込み、busy park/promote、reset、latch 解除）、
`tests/config/test_model_pricing.py`（表読み込み、litellm/default 優先度、
provider プレフィックス名）、
`tests/server/trigger/http/test_token_budget_api.py`（HTTP 上限設定 → ミドル
ウェア累積 → 短絡 → GET 報告 → reset のエンドツーエンド経路）。
