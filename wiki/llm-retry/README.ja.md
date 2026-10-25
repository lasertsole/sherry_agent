# 🔁 LLM リトライ予算とカウント

[English](README.md) · [中文](README.zh.md) · **日本語** · [한국어](README.ko.md)

> Sherry がプロバイダーのリトライをどう数えるか：モデル呼び出しごとの物理試行列、独立した予算試行、予算を消費しない追加試行、ターンごとの fallback 切替上限、そして次のターンに持ち越さなくなったタイムアウト遮断。

## カウントモデル

ZCode の adapter は `attempt` を 1 回のモデルリクエストのローカル変数として扱います。Sherry も同じモデルです：`wrap_model_call` / `awrap_model_call` は毎回 `retry_count = 0` から始まり、新しいモデル呼び出しは新しい予算になります。

WS イベントは 4 つの数値を運ぶので、UI 側で推測する必要はありません：

| フィールド | 意味 |
| --- | --- |
| `attempt` | 追加試行を含む物理リクエスト番号 |
| `budget_attempt` | 物理試行から `extra_attempts` を引いたもの |
| `extra_attempts` | 認証更新 / 署名修復の試行。予算を消費しない |
| `max_attempts` | `1 + max_retries + extra_attempts` |

`retries = budget_attempt - 1`。既定値は `max_retries = 10` なので、通常のターンは `2/11`、……、`11/11` と表示されます。

## リセットされる条件

予算がリセットされるのはリクエスト経路が変わったときだけです。追加試行は減算で扱い、リセットではありません。

| イベント | 予算 | 追加 | 理由 |
| --- | --- | --- | --- |
| モデル呼び出し入口 | リセット | リセット | 各モデル呼び出しが独自の `retry_count` を持つ |
| fallback 切替 | リセット | リセット | 新しいモデル、新しいリクエスト、新しい予算 |
| マルチモーダル降格 | リセット | リセット | リクエストが skill 経路へ移る |
| コンテンツフィルター再バインド | リセット | リセット | リクエストが別モデルへ移る |
| 認証更新 | 変更なし | +1 | 追加試行。`max_retries` を消費してはならない |
| 署名修復 | 変更なし | +1 | 同じ規則 |
| 空補完 | +1 | 変更なし | 実際のリトライ。独自の上限 1 を持つ |
| 成功 | 破棄 | 破棄 | カウンターはその呼び出しのローカル |

テキストも tool call も usage もない空補完は、通常のリトライを 1 回消費し、さらに `empty_completion_retries = 1` で制限されます。これは ZCode の `canRetryEmptyCompletion` と同じで、`retryCount < 1` と `attempt < maxAttempts` の両方を確認します。

## fallback 切替

fallback 候補は新しいモデルリクエストを開始するため、リトライ予算はゼロから始まります。上限がないと、N 個の候補が `11 × N` 回の物理リクエストを発行し得ます。そこで Sherry は `llm_fallback_switches` をターンごとに記録し、`max_fallback_switches`（既定 `1`）に達したら切替を拒否します。`0` は既に固定された候補も無効にします。セッションの fallback インデックスは変わらないため、以降のターンも既に勝った候補から始まります。

## タイムアウト遮断の範囲

`llm_stale_streak` は連続する timeout 失敗を数えます。`stale_giveup_scope = "turn"`（既定）では `before_agent` がメイン agent のターン開始時にクリアします。遮断は `stale_giveup_threshold = 5` で現在のターンを中止しますが、次のターンには持ち越しません。`"session"` は以前のターン横断動作に戻します。ZCode に同等のターン横断遮断はなく、これは Sherry 独自の保護であり parity の主張ではありません。

## イベント、UI、台帳

scheduled / succeeded / exhausted のたびに `model_retry` イベントを 1 件記録し、セッションの socket に送ります：

```text
{"event":"model_retry","session_id":…,"content":{
  "status":"scheduled","attempt":2,"budget_attempt":2,
  "max_attempts":11,"retries":1,"extra_attempts":0,
  "fallback_switches":0,"reason":"rate_limit","delay_ms":1000}}
```

ストリーミング中はチャット行が `attempt/max_attempts` を表示します。台帳は各遷移を独立した行として保存します（イベント id に attempt と status を含む）。フロントエンドの表示はメモリ内だけなので履歴の再読み込みで消えますが、台帳は永続記録です。

## 未実装

- 認証更新と thinking 署名修復：`models/LLMs` にプロバイダーの更新口がまだないため、イベントフィールドだけ用意し、必ず失敗する偽の試行は行いません。
- off-peak キューのロールバック：ZCode のゲートウェイ契約がありません。
- `bounded` / `unbounded` / `single` の予算モード：現時点で必要とする本番呼び出し元がありません。

## コードマップ

- `agent/middlewares/llm_retry/core.py`：ループ、カウンター、fallback 上限、`before_agent`。
- `agent/middlewares/llm_retry/retry_events.py`：イベント payload と WebSocket 送信。
- `config/features/agent_side/llm_retry.py`：`LLM_RETRY` の既定値。
- `runtime/session/state_keys.py`：`LLM_FALLBACK_SWITCHES`、`LLM_STALE_STREAK`。
- `client/app/composables/use-chat-stream.ts`：`retryStatus` のマッピング。
- `client/app/pages/home/components/ChatBox.vue`：行内のリトライ表示。
- `tests/agent/middlewares/test_llm_retry.py`：カウント、fallback 上限、遮断範囲。
