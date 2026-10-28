# 🔁 ストリーム回復

[English](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> このページは API 契約ではなく機構を記録します。どの状態が権威か、再接続がどうそれを見つけるか、なぜ差分再生ではなくスナップショット水位を使うのか。

## 設計上の問題

ターンは長寿命の async generator です。出力は生成されるたびに全ライブ socket へ push され、クライアント別キューも再生ログもありません。視聴者は任意の chunk で消えることがあり、ターンは実行を続けなければなりません——すでにツール呼び出しを実行し token を消費している可能性があるからです。

要件は相反します：

- 再接続はターンを再実行してはならない（副作用とコストの重複）。
- 再接続はフレームを再生してはならない（他の視聴者が受信済みなら可視テキストが重複する）。
- 再接続のためにサーバーが視聴者ごとの全差分を保持してはならない（無界の状態）。
- 同じセッションの複数視聴者は、再生カーソルを共有せず同じテキストに収束しなければならない。

答えは**ある時点のスナップショット + ターン単位のシーケンス水位**で、永続化履歴と同じ 1 回のリクエストで届けます。

## 権威状態

`StreamTurn` が実行中ターンの権威テキストを持ちます。think scrubber を通った可視モデルテキストは `self.ai_text` に追加され、推論は `_has_reasoning` で別管理されます。ツール呼び出しは `ai_text` に含まれず、trajectory ledger に記録されます。

エンジンは `_run_frames` 開始時に `_ACTIVE_TURNS[session_id]` へ登録し、generator の `finally` で自分自身を削除します。識別チェックにより古いターンが新しいターンを削除できません。turn runner/queue がターンを直列化するため、1 セッションにつき最大 1 ターンがアクティブです。

`StreamTurn` は周囲の turn scope から `turn_id` を取得します。`active_turn_snapshot()` はエンジンから 4 フィールドを読みます：

- `ai_text`：これまでに生成された可視テキスト；
- `_has_reasoning`：推論チャネルが存在するか；
- `_frame_seq`：これまでに刻まれた最大フレームシーケンス；
- `turn_id`：テキストが属するターンの識別子。

読み取りはストリーム generator と同じイベントループで実行されるため、テキストとシーケンス番号は同じ一貫したカットで読まれ、ロックは不要です。エンジンが未登録なら実行中ターンはなく、スナップショット関数は `None` を返します。HTTP 履歴パスは表示用メタデータとして `turn_num`（次に永続化されるターン番号）を追加します。

## 永続化実行：クラッシュ後に何が残るか

Sherry の耐久性モデルは**「状態は永続 + sweeper が下支え + 新しい turn が再駆動」**であり、Temporal 流のアクティビティ再実行ではありません。クラッシュは再生できませんが、どの**状態**も嘘をつかないことを保証できれば、復旧には真実の材料があります。2026-10 に 5 つのサブシステム（HITL、チェックポインタ、TaskFlow、サブエージェントレジストリ、証跡台帳）を監査し、最初のバッチを修正しました：

| 欠陥 | 修正 |
| :--- | :--- |
| レジストリの終端判定が文字列リテラル（`COMPLETED` 等）を、値が `pending / running / interrupted / terminal` の列挙と比較——一致せず、保持上限が機能せずメモリが無制限に増加 | `ExecutionStatus.TERMINAL` と比較。上限は稼働中 run を残し古い終端 run を追い出す |
| セッションディレクトリのスイープが生の session id で checkpoint を照会。チェックポインタは `session_thread_id(session_id)`（INTEGER）を保存するため、checkpoint だけが残るフォルダが空と誤認され削除された | `pub.func` の単一の `session_thread_id()` マッピングをスイープで使用 |
| trajectory は「書き込み時に剪定」と記しつつ `prune_session` に呼び出し元がなく、しかも常に 0 の `MAX(seq)` で並べていたため任意の turn が残った | 書き込み時に接続し、セッションごとに 60 秒で間引いて実行、挿入順（rowid）で並べる |
| グローバルな keep-latest 剪定が `built_agent()`（= 毎ターン）で、`busy_timeout` の無い接続で走り、keep-set を削除前に確定していた（その間に他セッションがコミットした最新 checkpoint が失われ得る） | 起動時に 1 回（`server/service/checkpoint_maintenance.py`）、`BEGIN IMMEDIATE` 内で実行し、keep は最上位行を優先 |
| すべてのチェックポインタ接続に `busy_timeout` が無かった（WAL と違い接続単位の PRAGMA） | 単一ヘルパー（`agent/checkpointer/pragmas.py`、30 秒）に統一 |
| `delete_thread_history` に保護が無く、DB ロック時にメッセージストアを空にした後で purge が中断——半削除の最悪側 | 各手順を保護し、checkpoint 削除を最初に実行（再試行で必ず完了できる） |
| 終端 run の配信状態は 30 秒周期の flush でしかディスクに届かず、その窓でのクラッシュは SQLite を TERMINAL+PENDING に残す——再試行経路が無く、完了結果が永久に失われる | 配信状態の遷移（`delivered / failed / suspended / in_progress`）を即時永続化（`set_run_durable`） |
| auto-turn ドライバが空実装の `apply_hitl_pending` を継承し、headless turn の中断後もグラフが停止したままセッションは idle に見えた：ターン終了時の剪定がその checkpoint を消し、drain が新しいメッセージを停止中のグラフへ注入し得た | ドライバがフラグを設定。`submit_user_input` も checkpoint 内の interrupt を busy として扱う（フェイルオープンな探索）。これは再起動後の典型ケース |

監査の残項目（起動時のセッション横断 HITL 再構築、永続化タイムスタンプを `time.monotonic()` から実時間へ、再起動後の in-flight step 復帰、P3/P4 の証跡・可観測性バッチ）は計画で追跡しており、完了扱いにはしていません。


## フレームシーケンスと水位

`StreamTurn.run()` が出力する各フレームは yield 前にターン単位の単調 `_frame_seq` を刻まれます：

```text
_run_frames() produces a frame
  → run() increments _frame_seq
  → frame["seq"] = _frame_seq
  → frame["turn_id"] = self.turn_id
  → the frame is broadcast
```

`StreamDriver` も `turn_started`、`done`、`hitl_request`、`error`、`stopped` に `turn_id` を刻みます（chunk ループ外で構築されるフレーム）。したがって再接続クライアントは、ターン切り替えの後でも各フレームを所属ターンへルーティングできます。

水位は**ターン単位**です。スナップショットは読んだ時点の `seq` と `turn_id` を運び、フレームの `turn_id` が一致し `seq <= snapshot.seq` のときだけ破棄します。新しいターンは `seq = 1` から再開するため、他のターンの水位より数値が小さいだけで古いデータと誤認されることはありません。

## 再接続時の 1 往復

クライアントは再接続時に `resume_stream` を送りません。1 回の履歴リクエストを送ります：

```text
GET /get_history_by_turn_page?session_id=…&include_active_turn=1
  → {messages: [...persisted rows...],
     active_turn: {text, has_reasoning, seq, turn_id, turn_num} | null}
```

`include_active_turn=1` がなければエンドポイントは従来どおり行配列を返すため、既存呼び出しの契約は不変です。フラグ付きでは永続化履歴と実行中ターンのスナップショットが同時に届きます：切断中に完了したターンは `messages` に、実行中のターンは `active_turn` に入ります。

WS handler `_handle_resume_stream` と `stream_resume` 応答は旧クライアント向けフォールバックとして残りますが、現行クライアントはそのフレームを送りません。

## クライアントのスナップショットキュー

履歴リクエストが飛行中、socket は受信した全 WS フレームを適用せず保持します（`holdFrames`、最大 1000 フレーム、超過時は最古を破棄）。応答が届いたら：

1. 永続化メッセージをチャット一覧へマージ（切断中に完了したターンが即座に見える）；
2. `active_turn` を所属ターンの行へ適用：行に `turn_id` があればそれ、なければ `turn_num`、どちらもなければ新しい AI 行を作成；
3. socket がスナップショット水位（`seq` と `turn_id`）を記録；
4. 保持フレームを到着順に解放。同じターンで `seq <= 水位` のフレームは破棄し、それ以外は自身の `turn_id` で通常適用。

遅延・消失した応答で UI が固まることはありません：待機は 10 秒で上限。タイムアウト時は応答をチャットマージから除外し、水位なしでキューを解放し、socket の終端フレームまたは busy watchdog が収束させます。chunk をまだ受け取っていない送信は再開可能なので idle スナップショットで決済されず、chunk 受信済みの送信だけが切断中の終了として決済されます。

## 切断中のライフサイクル

`_release_socket` は socket を登録解除し保留中のツール引数を掃除できますが、ターンを**キャンセルしません**。ターンタスクは終了まで `_active_tasks` に残り、`_ACTIVE_TURNS` は切断経路ではなく generator 自身の `finally` で削除されます。最後の視聴者が離れてもターンは生存します。

フレームはちょうど 1 回ブロードキャストされ再生されません。再接続クライアントはスナップショットで権威テキストを取得し、登録後に生成されたフレームだけを受け取ります——両者の組み合わせに欠落はなく、再生ログも不要です。

## スナップショットが省略するもの

- 推論差分：スナップショットは `has_reasoning` のみを報告し、推論本文は含みません。
- インクリメンタルなツール入力 JSON と delta 単位の使用量テレメトリ。
- ツール呼び出しカードのストリームイベント。確定済みのツール呼び出しは trajectory ledger（`GET /sessions/:session_id/trajectory`）で照会できます。

この分割は意図的です。テキストだけが、永続記録から部分レンダリングを再構築できない状態です。ツール呼び出しには既に永続 ledger があり、ストリームフレームを再生すると既存記録を重複させます。

## 失敗時の意味論

- スナップショット後に届いた古いフレームはターン単位の水位で破棄され、可視テキストを重複させません。
- 履歴応答の欠落・タイムアウト時は水位なしで保持フレームを解放し、従来どおり適用します。watchdog は最後の保険です。
- 切断中に終了したターンは履歴行に現れます。スナップショットがアクティブターンなしを報告したとき、chunk 受信済みの送信は決済されます。chunk 未受信の送信は再送され（backend は `msg_id` で重複排除）、ターン開始まで pending を保ちます。
- socket ごとのターゲティングにより、1 視聴者の再接続が他視聴者のカーソルやストリームを変えることはありません。

## 却下した代替案

- **再接続時にターンを再実行する。** ツール副作用と token 消費を重複させ、回答を変え得ます。
- **全差分を再生する。** 視聴者ごとの履歴・カーソル・保持が必要で、`seq` 水位が解く競合が再導入されます。
- **ZCode の 7 イベントアンカープロトコル。** より細かい推論／ツール入力回復を与えますが、フロントエンドプロトコルと永続 anchor/tail-discard パイプラインが必要です。Sherry のスナップショットプロトコルはより少ない機構でユーザー可視の回復目標を満たし、細粒度イベント型は設計上未実装のままです。

## 軌跡パネル

台帳には UI 消費側もあります。SESSION スコープの右サイドバータブ
（`client/app/pages/home/components/TrajectoryPanel.vue`）が
`GET /sessions/:session_id/trajectory` を読み、ターンごとのタイムラインを表示します。
これは再接続プロトコルではなくデバッグビューです。開くと最新の 500 行ページを 2 回だけ
取得し（最大 1000 イベント、超えたら切り詰め表示）、更新はボタンで行います（ライブ購読
なし）。

イベントは `turn_id` でグループ化し、`(ts, seq, event_id)` で並べ替えます。`read()` は
`seq` 順であり、すべてのライフサイクルイベント（turn_start / turn_end / compact /
model_retry / invalid_tool_call）が `seq=0` のため、並べ替えないとツール行より前に
集まり、`turn_end` が先頭に来てしまいます。ツール行は名前/状態/時間と展開可能な
引数・結果 JSON、ライフサイクル行は種別バッジと展開可能な payload を表示します。
ヘッダーにはフィルター式検索（n/m カウント）、2 つの折りたたみスイッチ（ツール行は既定で
折りたたみ）、すべて展開/折りたたみ、ターンフィルター、更新があります。ツールカードの
履歴ボタンは `ChatBox.vue` 経由で同じ単一タブを開きます。

純粋関数（`groupTurns`、`summarizeTurns`、`formatDuration`、`previewOf`、
`eventMatches`）は `client/app/composables/use-trajectory.ts` にあり、bridge 呼び出しは
`bridge/session.ts` の `fetchTrajectory` です。

## コードマップ

- `server/service/stream_dispatch.py`：`StreamTurn`（`ai_text`、`_frame_seq`、`turn_id`、`_ACTIVE_TURNS`）と `active_turn_snapshot`。
- `server/service/stream_driver.py`：driver が構築する全フレームに `turn_id`。
- `server/trigger/http/messages.py`：`GET /get_history_by_turn_page` の `include_active_turn`。
- `server/trigger/ws/messages.py`：旧 `_handle_resume_stream` 経路と `_release_socket`。
- `runtime/session/ws_push.py`：ちょうど 1 回のブロードキャスト、再生ログなし。
- `client/app/composables/messages.ts`：`get_history_page_with_active_turn`。
- `client/app/composables/use-session-lifecycle.ts`：スナップショットを返す履歴マージ。
- `client/app/composables/use-chat-stream.ts`：`syncHistory`、スナップショット適用、ターンルーティング。
- `client/app/composables/bridge/agent-socket.ts`：フレーム保持／解放とスナップショット水位。
- `client/app/composables/bridge/agent-socket-frames.ts`：ターン単位の水位フィルタ。
- `tests/server/test_stream_resume.py`、`tests/server/service/test_stream_turn_id.py`、`tests/server/trigger/test_history_active_turn.py`、`client/app/composables/__tests__/agent-socket-resume.test.ts`：上記の不変条件。
