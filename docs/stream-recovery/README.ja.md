# 🔁 ストリーム復元

[**English**](README.md) · [中文](README.zh.md) · **日本語** · [한국어](README.ko.md)

> ターン実行中の WebSocket 切断で、クライアントがターンを再実行したり、他のビューアが既に受け取ったフレームを再送したりしてはならない。Sherry はテキストスナップショットとターンごとのシーケンス水位で実行中のターンを復元する。

## 適用範囲

- **ターンが少なくとも 1 つの chunk を生成した後**に切断したビューアは、再接続時に `resume_stream` で実行中ターンのスナップショットを要求する。
- **最初の chunk より前**に切断したビューアは送信待ちの payload を再送する。バックエンドは `client_msg_id` で重複排除するため、ターンが二重に開始されることはない。
- 最後のビューアが切断してもターン自体は実行を続ける。socket の解放はターンをキャンセルしない。
- このプロトコルはスナップショットベースであり、ZCode の 7 イベントアンカープロトコルではない。中核目標——再実行しない、再送しない、重複しない、テキスト末尾はサーバーが正——は満たしている。

## ワイヤプロトコル

1. クライアントは agent socket で `{"type": "resume_stream", "session_id": ..., "content": {}}` を送る。
2. サーバーは `active_turn_snapshot(session_id)` を呼ぶ。実行中のターンがあれば、エンジンの `ai_text`、`has_reasoning`、現在のターンごとの `seq` を返す。
3. サーバーは**その socket にだけ** `stream_resume` フレームを返し、`status` は `ok`、`ended`、`gone`、`unknown` のいずれかになる。
4. `ok` はスナップショットを運ぶ。`ended` は実行中ターンがないが履歴はある状態（クライアントは再取得）、`gone` はセッション削除済みまたは未永続化、`unknown` は読み取り失敗時の fail-open 結果。
5. サーバーはスナップショットをブロードキャストせず、過去のフレームも再送しない。ライブフレームは生成時に一度だけブロードキャストされる。

## クライアント契約

- クライアントはまずバッファ済み delta を flush する。スナップショットは既にそれらを含んでいる。
- そのターンの AI 行はスナップショットのテキストで**置き換え**られ、追記はされない。
- 各ライブフレームはターンごとの `seq` を持ち、スナップショットは読み取り時の `seq` を運ぶ。復元したクライアントはその水位以下のフレームを破棄し、スナップショット読み取り後に在途フレームが届く競合を閉じる。
- `stream:resumed` はビジネス層が消費するバスイベント。水位は chat store ではなく socket のフレームコンテキストに保存される。

## 対象外

- reasoning delta は復元しない。スナップショットは `has_reasoning` を報告するだけ。
- 増分 tool-input JSON と delta ごとの usage テレメトリはスナップショットに含まれない。
- 切断前にコミット済みのツール呼び出しは、ターンごとの trajectory ledger（`GET /sessions/:session_id/trajectory`）で引き続き参照できるが、ストリームイベントとして再送はしない。
- ZCode の完全な 7 イベントアンカープロトコルは実装せず、予定もない。スナップショットプロトコルがユーザーに見える復元目標をより少ない仕組みで満たす。

## コードマップ

- `server/service/stream_dispatch.py`: `active_turn_snapshot` とアクティブターンレジストリ。
- `server/trigger/ws/messages.py`: `_handle_resume_stream`、socket ごとの応答。
- `runtime/session/ws_push.py`: ライブフレーム用の `broadcast_to_session`。再送ログはない。
- `client/app/composables/bridge/agent-socket.ts`: 切断検出と `resume_stream` 要求。
- `client/app/composables/bridge/agent-socket-frames.ts`: `stream_resume` ルーティングとスナップショット `seq` 水位。
- `client/app/composables/use-chat-stream.ts`: テキスト置換と `ended` / `unknown` の再取得判断。

## テスト

- `tests/server/test_stream_resume.py`: ブロードキャストが正確に 1 回、死んだ socket の除去、スナップショットは要求者だけ、session を越えないこと、水位契約。
- `client/app/composables/__tests__/agent-socket-resume.test.ts`: 再接続要求と `ok` / `ended` / `gone` の処理。
- `client/app/composables/__tests__/agent-socket-frames.test.ts`: `stream_resume` フレームのルーティングとスナップショット適用。

## 失敗モード

- スナップショット読み取りが失敗すると、サーバーは `unknown` を返す。クライアントは古いテキストを黙って残さず、履歴を再取得する。
- アクティブターンも永続履歴もない場合、応答は `gone` で、クライアントは何もしない。
- スナップショット `seq` 以下のフレームがスナップショット後に届いた場合、クライアントはそれを破棄する。したがって重複配信で可視テキストが重複することはない。
- スナップショットは要求した socket だけに届く。同じ session の他のビューアはライブフレームを続け、スナップショットを再送として見ることはない。
