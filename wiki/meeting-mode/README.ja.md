# ミーティングモード

ミーティングモードは Sherry の第 3 のプロダクトモードです。複数の登録済みエキスパートが同じトピックについて議論し、共通の結論に達し、構造化された計画をエキスパートチーム実行コンポーネントへ渡します。議論フェーズと実行フェーズは独立しており、計画だけを保存して後から実行できます。

## なぜミーティングが必要か

単一の agent は一つの視点からしか答えません。ミーティングでは複数のドメインエキスパートが同じ会話に入り、判断の依存関係が記録されます。フロントエンドの決定がバックエンドに影響し、それがデータベースに影響します。ユーザーは「ボス」であり、いつでも割り込めます。参加者同士は直接呼び合わないため、議論がデッドロックすることはありません。

## アーキテクチャ

```
User  ->  Host  ->  Selector  ->  Participants  ->  Conclusion
            |           |              |
         tools       one JSON       one statement
         allowlist   verdict        per turn
```

三つの役割は `agent/meeting/` にあります。`host` は調整のみを行い、そのツール面は調整用の allowlist に制限されます。`selector` は毎ターン一つの `MeetingTurnDecision` を返します。各 `participants` は共有履歴に基づいて一回発言します。`run_meeting` が元のループで、`run_meeting_with_interrupts` はステージ遷移、割り込み、フェーズ allow-list ゲートを追加します。

## ライフサイクル

ミーティングはホストがトピックを確認して始まり、ディスパッチングループに入ります。セレクターが次の発言者を指名し、その参加者が発言し、発言が追加されて保存されます。セレクターが次の発言者を返さなくなったとき、または 40 ラウンドの上限に達したとき、ホストが要約を作成し、終了確認へ進みます。各発言は speaker kind と message role とともに永続化されます。

## 割り込みとロールバック

`apply_interruption` はユーザーの割り込みを六種の一つに分類します。補足は計画を変えずに追加され、終了要求は終了確認へ進みます。訂正、スコープ変更、優先度変更は `apply_local_rollback` を呼びます。影響を受ける実行計画のフェーズと、それらに依存するすべてのフェーズが `needs_rediscussion` とマークされ、セレクターが次のターンで再計画します。フェーズ allow-list ゲートは、参加者が自分を選んだフェーズの外で発言することを防ぎます。

## 結論から実行へ

`build_conclusion` は TaskFlow 形式の計画を持つ `MeetingConclusion` を生成します。各ステップはタスク、登録済みエキスパート名、依存関係を持ちます。`execute_conclusion` は決定的な id `meeting-{meeting_id}` でフローを作成し、エキスパートチームの仕組みでそのステップをディスパッチします。二回目の実行要求は既存のフローを見つけ、重複を作成しません。`reusable` ヒントは TaskFlow ステップのネイティブ項目ではないため、会議の結論側に残されます。

## ストリーミング

会議ストリームは `/meetings/ws` の会議単位 WebSocket チャネルです。agent イベントは `MEETING_EVENT_PUSH` フック経由で送信され、このフックは fail-open です。WS サーバーのないプロセスでも会議は通常どおり動作します。フレームは発言者の変更、メッセージの差分と完了、thinking の差分、ask-user プロンプト、要約ドラフトをカバーします。Nuxt ページは各フレームをストアのフレームリデューサーへ渡し、mock モードではバックエンドなしでもページを操作できます。

## 永続化と復旧

会議、メッセージ、`meeting_compaction_segments` は共有の registry データベースにあります。長い議論は会議専用トリガーで summarization ミドルウェアの再利用可能なプリミティブを使って圧縮され、メイン agent の task-intent が再起動されることはありません。起動時に `auto_adjourn_at_boot` は `process_restart` 理由で生存中の会議をキャンセルします。メモリ上のループは再起動後も生き残れないからです。

## API

HTTP サーフェスは作成の `POST /meetings`、一覧の `GET /meetings`、読み取りの `GET /meetings/one` と `GET /meetings/messages`、キャンセルの `DELETE /meetings` に加え、`POST /meetings/run`、`/meetings/interrupt`、`/meetings/reply`、`/meetings/confirm-end`、`/meetings/execute`、`GET /meetings/conclusion` です。すべてのルートはクエリパラメータか JSON ボディを使い、パス補間は使いません。

## テストとゲート

`tests/agent/meeting/` はループ、分類、ロールバック、圧縮、結論、起動復旧をカバーし、`tests/server/trigger/http/test_meetings_api.py` がルートを、`tests/server/trigger/ws/test_meeting_ws.py` がフレーム送信をカバーします。フロントエンドテストはストアのリデューサーとページを固定します。静的ゲートは `ruff`、`basedpyright`、`lint-imports` で、ドキュメントゲートは parity、links、language です。
