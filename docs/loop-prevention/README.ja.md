# 🔁 暴走ループ防止：ガード、ブレーカー、クラッシュゲーティング

[English](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> 常時稼働するエージェントが自分で立ち往生するのを防ぐ仕組み: モデル呼び出しとツール呼び出しに対するターン単位の病理ガード、バックグラウンドサービスへの指数バックオフ・ブレーカー、サブエージェント間の確実な完了通知の配送、起動時のプロセスレベル・クラッシュゲーティング、そしてすべてが失敗したときの REST / 手動エスケープハッチ。

暴走ループ(runaway loop)とは、システムが自力で抜け出せないあらゆる循環のことです。同じ文章を永遠に繰り返すモデル呼び出し、同じ失敗する引数で再試行され続けるツール呼び出し、虚空に向かって刻み続けるハートビートや cron ジョブ、親に届かない(あるいは二度届く)サブエージェントの完了通知、クラッシュして再起動した途端に同じクラッシュへ落ちるプロセス。このハーネスは無人で動くので(cron スケジュール、ハートビット起床、サブエージェント掃除、長いチャットターン)、すべてのループには、モデルに「いい子にしてね」と頼むプロンプトではなく、*システム自身が*強制する境界がなければなりません。

以下のすべてのガードには二つの設計ルールが貫かれています:

1. **劣化はするが、クラッシュはしない。** 保護機能がプロセスを落とすことはありません: バックグラウンドサービスは*停止*し、ターンは*安全に終了し*、起動ゲートはプロセスを HTTP 専用モードへ*縮小*します。
2. **必ずハッチを残す。** すべてのブレーカーには文書化された手動リセット(REST エンドポイント、状態ファイルの削除、プロセスの再起動)があります。

**一次情報:** `agent/middlewares/tool_guardrails.py`、`agent/middlewares/iteration_budget.py`、`agent/middlewares/output_repetition_guard.py`、`agent/stream_repetition_guard_wrapper.py`、`agent/middlewares/heartbeat_staleness.py`、`agent/middlewares/subagent_completion_drain.py`、`agent/tools/subagent/announce/delivery.py`、`agent/tools/subagent/announce/idempotency.py`、`runtime/periodic_backoff.py`、`runtime/crash_loop_breaker.py`、`skills/builtin/core/cron/scripts/base.py`、`skills/builtin/core/heartbeat/scripts/base.py`、`agent/tools/subagent/registry/sweeper.py`、`server/__main__.py`、`server/trigger/http/cron.py`、`server/trigger/__init__.py`、`server/trigger/channels/core.py`。

## 🎯 概要と脅威モデル

| ループの高さ | 見た目 | 防御 |
|---|---|---|
| **テキストデスループ** (モデル呼び出し 1 回) | 同じ文 / 同じ文字列が永遠にストリーミングされる | `OutputRepetitionGuard` (ワーカー) + `RepetitionGuardWrapper` (メインエージェントのストリーム) |
| **ツール病理ループ** (ターン 1 回) | 同じ失敗するツール呼び出し、ピンポンペア、引数の改変 | `ToolGuardrails`: 5 種類の病理 → WARN → BLOCK → HALT、リカバリモード付き |
| **無限ターン** | モデル/ツール呼び出しが止まらない | `IterationBudget` (メイン 90 / ワーカー 60、合算呼び出し) |
| **スタックしたターン** | 数分間まったく進まない (ハングしたツール、挟まったループ) | `HeartbeatStaleness` ウォッチドッグ → `HeartbeatTimeoutError` |
| **バックグラウンドサービスループ** | ハートビート / スイーパー / cron ティックが永遠に失敗 | `PeriodicBackoff` (枯渇 = サービス停止) / cron 降格 → 自動無効化 |
| **完了通知の消失または重複** | サブエージェントは終わったのに親が通知を受け取れない、または二度受け取る | 完了ドレイン (1 回だけ注入) + announce 再試行ラダー + 冪等キー |
| **クラッシュ再起動ループ** | プロセスが起動時にクラッシュし、スーパーバイザが同じクラッシュへ再起動 | `CrashLoopBreaker` → HTTP 専用モード |
| **それ以外のすべて** | ブレーカーがジョブを無効化したか、起動ゲートが作動した | Cron REST `failure-state` / `reset-failures`、状態ファイルのリセット |

防御は高さごとに層状になっており、一つの層をすり抜けた失敗は次の層に引っかかります:

1. **ターンレベル**: ターン単位のガードが一つの会話ターンを拘束します (テキスト、ツール、反復回数、ステイルネス)。
2. **回復の幕間**: `ToolGuardrails` の BLOCK は HALT に格上げされる前に、*管理された再試行ウィンドウ*を一度だけ得ます。
3. **バックグラウンドレベル**: 周期サービスは指数的にバックオフし、連続 5 回の失敗で*停止*します。
4. **プロセスレベル**: クラッシュ再起動ループは起動ブレーカーを作動させ、最小限の HTTP 専用モードで起動します。
5. **運用**: REST エンドポイントと文書化された手動リセット。

## ⚙️ 実装とアーキテクチャ

### ターンレベル: `ToolGuardrails`、ツール呼び出し病理検知

メインエージェント、ワーカーエージェント、nudge サブエージェントで有効です。すべてのツール呼び出し記録は評価*より前に*ターン単位状態(`state_register_mem` の `tool_guardrail_state`)へ追加されるため、しきい値は自己包含的です。「2 回の後」とは現在の呼び出しが 2 回目であることを意味し、5 回目の同一無進行呼び出し自体が np=5 に達してブロックされます。格上げラダー: ALLOW → WARN (トランスクリプトに nudge を追記) → BLOCK (説明メッセージ付きで呼び出しを拒否) → HALT (ターミナルメッセージとともにターンが安全に終了、反復予算と同じパターン)。

| 病理 | 信号 | WARN 後 | BLOCK 後 | hard-stop モード |
|---|---|---|---|---|
| 同一失敗の反復 | 同じツール + 同じ (ハッシュ化した) 引数が失敗し続ける | 2 | 5 | 5 で HALT |
| 同一ツール失敗ストーム | 同じツールが失敗し続ける、引数は変わってもよい | 3 | 8 | 8 で HALT |
| 冪等無進行 | 冪等ツールで同一の (ハッシュ化した) 結果 | 2 | 5 | 5 で HALT |
| ピンポン | 途切れない読み取り専用 A → B → A → B の往復 | 4 | 6 | 6 で HALT |
| 引数改変 | 同じ冪等ツールが引数バリアントを巡回 | 3 変種 | 5 変種 | 5 で HALT |

重要な詳細:

- **リカバリモード** (`recovery_mode_enabled=True` がデフォルト): 最初の BLOCK でターンが死ぬことはありません。ターンはリカバリ状態に入り、*precheck* 経路がブロックされたツールを解放するので、再試行は新鮮に評価されます。それ以降の BLOCK ごとに違反カウンタが増え、カウンタが `recovery_max_violations`(デフォルト 1)を超えると HALT に格上げされます。要するに、即席の壁ではなく管理された再試行ウィンドウです。旧来の厳格な挙動にするには `recovery_mode_enabled=False` を、すべての BLOCK しきい値を HALT に変えるには `hard_stop_enabled=True` を設定してください(表参照)。
- **ピンポンペア**は隣接する 2 つの呼び出しのツール名をハッシュし、*連続する 2 つ*の呼び出しが両方とも成功した冪等呼び出しである間(両方の記録が結果ハッシュを持つ間)だけ累積します。エラーが一度でも出たり、成功した非冪等(変異)呼び出しが一度でもあると、累積済みのすべてのペア連続記録がゼロに戻ります。結果の内容は比較しません: 途切れない読み取り専用の往復は、それ自体がループ信号として扱われます。非冪等ツールの成功も同様に引数改変状態をリセットします。
- ガード状態は厳密に**ターン範囲**です: `before_agent` がリセットするので、新しいターンはクリーンに始まります。

### ターンレベル: `IterationBudget`

ターンごとにモデル呼び出しとツール呼び出しを**合算**して数えます。メインエージェント 90、ワーカーエージェント 60 (ベースデフォルト 50)。使い果たされたモデル呼び出しはターミナル AIMessage を返し、使い果たされたツール呼び出しはエラー ToolMessage を返すので、モデルはループの途中で死ぬことなく締めくくれます。内部の完了通知ターンは免除されます(反復回数を消費しません)。カウンタ(`iteration_budget` / `iteration_budget_used`)は毎ターン リセットされます。

### ターンレベル: テキストデスループ、`OutputRepetitionGuard` + `RepetitionGuardWrapper`

**呼び出し間検知** (`OutputRepetitionGuard`、ワーカーパイプラインのミドルウェア):

- 内容は正規化され(NFKC → 空白除去 → 句読点除去)、先頭/末尾 500 文字によるデュアル `head|tail` MD5 としてハッシュ化されるため、長い出力のどちらの端でも反復を捉えます。セッションごとに 30 ハッシュのローリング履歴を保持します。
- 連続 2 回の同一出力で WARN (nudge を追記)、3 回で HALT (ターミナルメッセージ、halt フラグはターン中 sticky)。呼び出し間マッチングは内容 1 文字だけで成立するので、連続する呼び出し間で繰り返される一つの短い文だけでも有効なデスループ信号になります。
- 出力ごとの**内部検知**: 重複セグメント率 > 0.6 (句読点/改行で分割、最少 6 セグメント)、文字連続ラン ≥ 8、2〜10 文字の短い句が連続 ≥ 5 回の反復。20 文字未満の内容は誤検知を避けるためスキップします。内部警告はラベルごとにセッションごと最大 1 回発火します。
- **推論は独立して追跡**: `reasoning_content` / `reasoning` / `reasoning_text` kwargs とインラインの `<think>` / `<thinking>` / `<reasoning>` ラッパーは、別々の履歴と warned フラグに供給されます。
- このミドルウェアは正確に 6 つのセッション状態キー(`SESSION_STATE_KEYS`)を所有し、サブエージェント由来のエージェントが解体されるときに解放されます。ミドルウェア間の状態リークはありません。

**ストリーム層** (`RepetitionGuardWrapper`、メインエージェントをラップ):

- チャンクがクライアントに届く*前に*、ストリームの途中で内部反復を切り取ります: 警告を 1 つ注入した後、その呼び出しのストリームの残りを抑制します。
- HALT ショートサーキット: ターンに halt が記録されると、以降のモデル呼び出しは halt メッセージを直接返します。
- **ファントムストリームガード** (オプトイン、本番では有効): 新しい dict 入力の呼び出しが進行中のランを置き換えるとき、更新前のモデルテキストを破棄します。

### ターンレベル: `HeartbeatStaleness`、スタックしたターンのウォッチドッグ

ターンごとに 1 分タイマー(`timer_call_register`)を登録し、`heartbeat_iter` / `heartbeat_tool` カウンタを前回の観測値と比較します。進行があれば stale カウンタはリセットされます。アイドル状態のエージェントで **7** サイクル(約 7 分)、ツール実行中は **20** サイクル(約 20 分)進行がないと killed フラグが立ち、次のエージェントループ突入時に `HeartbeatTimeoutError` を投げてターンを安全に終わらせます。メインとワーカーの両エージェントに登録され、ターン単位状態は `before_agent` でリセット、タイマーは `after_agent` で解除されます。

### パイプラインレベル: サブエージェント完了ドレイン + announce 再試行

**注入ドレイン** (`agent/middlewares/subagent_completion_drain.py`): セッションの `SteeringQueue` を再水和して排出する `before_model` ミドルウェアで、次のモデル呼び出しの直前に待機中の完了キャリアを注入します。SQLite 行はドレイン時に `CONSUMED` と印付かれるため、チェックポイント再生(HITL 再開)が同じ完了を再注入することは決してありません。このミドルウェアは完全に fail-open です: すべての失敗はログに記録して飲み込まれ、親ターンは注入なしで続行します。これが「すでに終わった子を親が永遠に待つ」ループを塞ぎます。

**配送再試行 + 冪等性** (`agent/tools/subagent/announce/delivery.py`、`idempotency.py`): ビジー状態のセッションでの完了通知は、固定ラダーで一時的失敗を再試行します(5s / 10s / 20s、最大 `announce_retry_max=3`; コンパクションエラーは 1s / 2s / 4s / 8s)。永続的失敗は再試行されません。すべての配送は `subagent_announce:{run_id}:gen:{generation}` をキーとして有界なインメモリ冪等セットに記録されるため、再試行された announce が二重注入することはできません。再試行の枯渇 → run は FAILED; ソフト再試行上限 → SUSPENDED; `max_announce_retry_count`(10)回の再試行に達した run、または 24 時間の年齢上限を超えた run は破棄されます。スイーパーの孤児回収と合わせて、サブエージェントライフサイクルの配送側に境界が引かれます。

### バックグラウンドレベル: `PeriodicBackoff`、ブレーカー一つ、サービス三つ

`runtime/periodic_backoff.py` は純粋な状態機械です(スレッドなし、I/O なし):

- `record_failure()`: `consecutive_failures += 1`; `current_interval = min(base × factor^n, max_interval)`; `consecutive_failures >= max_consecutive_failures` で枯渇。
- `record_success()`: 完全リセット。デフォルト: `factor=2.0`、`max_interval=7200s`、`max_consecutive_failures=5`。

| サービス | 基本間隔 | 失敗間隔 | 枯渇時 |
|---|---|---|---|
| ハートビート (`skills/builtin/core/heartbeat/scripts/base.py`) | 1800s (`HeartbeatConfig.interval_s` と一致) | 3600s → 7200s → 7200s → 7200s | CRITICAL ログ ("paused ... manual recovery required"); ループは return するので、サービスは止まるがプロセスは生き続ける |
| サブエージェントスイーパー (`agent/tools/subagent/registry/sweeper.py`) | 60s (`sweeper_interval_seconds`) | 120s → 240s → 480s → 960s | CRITICAL ログ; `_running=False` がスイープタスクを終了させる |
| cron ジョブブレーカー (下記) | ジョブごと、基本 5s | 降格 → 無効化 | ジョブ自動無効化 |

知っておくべき意味論:

- ハートビートは tick の*内部で*成功を記録し(tick は自身のエラーを飲み込む)、本当の tick 失敗だけがカウントされます。一時停止後も `trigger_now()` は機能します: 手動のつつきは眠っているループを迂回します。
- `stop_sweeper()` はバックオフオブジェクトを破棄し(`_backoff=None`)、手動で再開したスイープは新鮮な状態で始まります。バックオフオブジェクトは遅延生成(`_get_backoff`)で、import 時には作られません。
- 本番ではスイーパーは `server/trigger/channels/core.py` の `_schedule_sweeper` が起動し、コルーチンをメインイベントループへ乗せます(`run_coroutine_threadsafe`); この配線は `tests/unit/server/test_sweeper_wiring.py` がカバーします。
- バックオフ状態は Python オブジェクトの中にあります: プロセスを再起動すればハートビートとスイーパーのブレーカーはリセットされます。

### バックグラウンドレベル: cron ジョブ失敗ブレーカー

`skills/builtin/core/cron/scripts/base.py` のジョブごとの状態機械(`CronJobFailureState`、メモリ専用。`enabled` フラグを除き `cron_jobs.json` には書き込まれない):

| 連続失敗 | 効果 |
|---|---|
| 1-4 | ジョブは普段どおり失敗: ステータスを error にマーク、WS ベル通知 |
| ≥ 5 (降格) | バックオフウィンドウ内はトリガーをスキップ: 最後の失敗から `min(5000ms × 2^(n-5), 300000ms)` |
| ≥ 10 | `enabled=False` を永続化; 最善努力の通知をジョブの payload チャンネルへ |

- **記録してから再スロー:** 失敗をまず記録し、それから例外をそのまま再スローするので、ステータス/エラー報告は無傷のまま保たれます。
- **一回きりの `at` ジョブは免除** (二度発火し得ないため、単一の失敗はループではない)。
- 成功は状態を完全にリセットします。手動の `enable_job` はこれを消し、REST `reset-failures` エンドポイントは*ブレーカー自身が*無効化した場合にだけ再有効化するので、オペレータによる無効化は保持されます。

### プロセスレベル: `CrashLoopBreaker` + 起動ゲーティング

`runtime/crash_loop_breaker.py` はブートジャーナルを `src/data/boot_lifecycle.json` に永続化します(キー: `{ts, clean, reason}` エントリを持つ `boots`、reason は 200 文字上限; `last_exit_clean` は一回限りのマーカー):

| パラメータ | 値 | 意味 |
|---|---|---|
| `TRIP_THRESHOLD` | 3 | 作動に必要な不潔なブート回数 |
| `WINDOW_S` | 300 | 5 分ウィンドウの中で |
| `RETENTION_S` | 3600 | ブート記録は 1 時間後に刈り込み |

起動シーケンス (`server/__main__.py`)、順に:

1. `was_last_exit_clean()` が `record_boot(clean=..., reason="startup")` がマーカーを消費する**前に**、一回限りのマーカーを読みます。
2. `atexit.register(mark_clean_exit)`: *グレースフル*なシャットダウンは次のブートをクリーンと印付けます。これが自己修復です。一度きれいに終了すれば、古い不潔な記録は 5 分ウィンドウから自然に消えていきます。
3. 作動したら (5 分以内に 3 回以上の不潔なブート): `SHERRY_HTTP_ONLY=1` を設定、CRITICAL ログ、**HTTP 専用モード**で起動:
   - `init_agent_core()` は依然として走るので、チャットは動き続けます。
   - キュレータと cron のバックグラウンド初期化はスキップされ、`server/trigger/__init__` はチャンネルマネージャとサブエージェントの import をスキップするので、ハートビートサービスとスイーパーも決して始まりません。
   - HTTP/WS ルートと cron REST API は生きたままです。

手動リセット: `src/data/boot_lifecycle.json` を削除するか、単に一度きれいに終了してウィンドウの自然減衰に任せるだけです。

### 層のマトリクス: どの層が何を捕まえるか

| 層 | メカニズム | 捕まえるもの |
|---|---|---|
| ミドルウェア (グラフ内、ターンごと) | `ToolGuardrails`、`IterationBudget`、`OutputRepetitionGuard` / `RepetitionGuardWrapper`、`HeartbeatStaleness`、`SubagentCompletionDrain` | ツール病理、無限ターン、テキストデスループ、スタックしたターン、欠落した完了注入 |
| プロセス (バックグラウンドサービス) | `PeriodicBackoff` (ハートビート、スイーパー)、cron 失敗ブレーカー、announce 再試行ラダー + 冪等性 | サービス再試行ストーム、失敗するスケジュールジョブ、重複する完了配送 |
| 起動 (プロセスライフサイクル) | `CrashLoopBreaker`、`server/__main__` ゲーティング、`trigger.__init__` の早期終了 | クラッシュ再起動ループ |
| インフラ / 運用 | cron REST ハッチ、HTTP 専用 env、状態ファイル削除 | オペレータの介入が要る、動けなくなったブレーカー状態 |

## 📊 優先順位マトリクス

| ガード | 高さ | 状態の住処 | リセットのタイミング |
|---|---|---|---|
| `ToolGuardrails` | ターン (ツール呼び出し) | `state_register_mem` (`tool_guardrail_state`) | 毎ターン (`before_agent`) |
| `IterationBudget` | ターン (呼び出し回数) | `state_register_mem` | 毎ターン |
| `OutputRepetitionGuard` | ターン + セッション (テキスト) | 6 つのセッションキー | halt フラグはターンごと、ハッシュ履歴はセッションごと (サブエージェント解体で解放) |
| `RepetitionGuardWrapper` | ストリーム呼び出し (テキスト) | in-flight + halt キー | モデル呼び出しごと |
| `HeartbeatStaleness` | ターン (実時間) | `heartbeat_*` キー + 1 分タイマー | 毎ターン |
| `SubagentCompletionDrain` | ターン (注入) | SteeringQueue 行 (SQLite) | ドレイン時に行を CONSUMED とマーク |
| Announce 再試行 + 冪等性 | ラン (配送) | インメモリ冪等セット + run 記録 | 成功 / 再試行上限 / 24 時間失効 |
| `PeriodicBackoff` (ハートビート / スイーパー) | サービス (ティック) | Python オブジェクト | 成功 / プロセス再起動 / `stop_sweeper` |
| cron 失敗ブレーカー | ジョブ (トリガー) | インメモリ `CronJobFailureState` | 成功 / `reset-failures` / 手動 `enable_job` |
| `CrashLoopBreaker` | プロセス (ブート) | `src/data/boot_lifecycle.json` | クリーン終了による減衰 / ファイル削除 |

一つのターンの中で、ターンガードは互いに直交し並行して発動します: `OutputRepetitionGuard` / `RepetitionGuardWrapper` は*テキスト*を、`ToolGuardrails` は*ツール呼び出し*を、`IterationBudget` は*回数*を、`HeartbeatStaleness` は*実時間*を守ります。最初に作動したものがターンを終わらせ、互いを妨げません。そのすべてが見逃したら、バックグラウンドブレーカーが*次の*トリガーを拘束し、起動ブレーカーが*次の*プロセスを拘束します。

## 🛠️ 設定と使い方

- **すべてのしきい値はコードのデフォルト値です** (dataclass / コンストラクタパラメータ)。意図的に環境変数は用意していません。注目点として、`config/schema.py` の `max_tool_iterations = 40` はミドルウェアには*消費されず*(予算は明示的に渡されます: 90 / 60)、`HeartbeatConfig.interval_s = 1800` はハートビートサービスのデフォルトと一致しますが、サービスはデフォルト値で構築されます。
- `TOOL_CALL_TIMEOUT_MINUTES` (`.env.example` でデフォルト 5) は現在**ドキュメント専用**です: これを消費するコードはありません。実際に有効なツール別の上限は定数です(web 検索 15s、ターミナル 30s、python REPL 30s)。これをループ境界として当てにしないでください。
- ワーカーはミドルウェアとして `OutputRepetitionGuard` を受け取り、メインエージェントは `RepetitionGuardWrapper` でラップされます(ミドルウェアフックは生のストリームチャンクを見えません)。
- `ToolGuardrails` のノブ: `warnings_enabled` (デフォルト True)、`hard_stop_enabled` (デフォルト False、BLOCK はブロックのまま)、`recovery_mode_enabled` (デフォルト True)、`recovery_max_violations` (デフォルト 1)。
- Announce 配送のノブ (サブエージェント announce 設定): 5s / 10s / 20s の一時的遅延を伴う `announce_retry_max=3`、さらに `max_announce_retry_count=10` と 24 時間の run 失効。

手動回復のチートシート:

| 状況 | 対処 |
|---|---|
| cron ジョブがブレーカーで自動無効化された | `POST /cron/reset-failures {"id": ...}` (ブレーカーが無効化したジョブだけを再有効化) |
| cron ジョブのブレーカー状態を調べる | `POST /cron/failure-state {"id": ...}` (未知のジョブ → 404、失敗したことのないジョブ → ゼロの状態) |
| ハートビートが一時停止中 (tick 5 回失敗) | プロセスを再起動; `trigger_now` は依然として一回きりの tick を発火できる |
| スイーパーが停止中 (バックオフ枯渇) | プロセスを再起動; 新しいスイープは新しいバックオフで始まる |
| 起動ゲートが作動済み (HTTP 専用) | 一度きれいに終了するか、`src/data/boot_lifecycle.json` を削除する |

## 🧪 テスト

| スイート | カバー範囲 |
|---|---|
| `tests/unit/middlewares/test_tool_guardrails.py` | 病理検知、格上げラダー、リカバリモード |
| `tests/unit/runtime/test_periodic_backoff.py` | 間隔の計算、枯渇、成功リセット |
| `tests/unit/runtime/test_crash_loop_breaker.py` | 作動ウィンドウ / 保持、クリーンマーカー、壊れた状態 |
| `tests/unit/cron/test_cron_failure_breaker.py` | 降格 → 無効化、リセットの意味論 |
| `tests/unit/heartbeat/test_heartbeat_backoff.py` | サービスバックオフ配線、枯渇時の一時停止 |
| `tests/unit/subagent/test_sweeper_backoff.py` | スイーパーバックオフ配線、ループ停止 |
| `tests/unit/server/test_sweeper_wiring.py` | スイーパー起動配線 |
| `tests/unit/server/test_crash_gating.py` | 起動ゲーティング、HTTP 専用モード |
| `tests/unit/server/test_cron_api.py` | Cron REST、failure-state / reset-failures を含む |

## ⚠️ 正直さと限界

- **ターンガードは設計上、ターン範囲です**: 新しいターンは新しいガード状態で始まります。ターンをまたぐ反復の検知は `OutputRepetitionGuard` の領域(セッション範囲の履歴)であって、ツールガードレイルの領域ではありません。
- **インメモリのブレーカー状態は再起動を生き延びません**: ガードレイル/反復/反復予算の状態はもともとターンまたはセッション範囲であり、cron 失敗カウンタはプロセス再起動で失われます(永続化された `enabled` フラグは失われません)。ハートビート/スイーパーのバックオフは Python オブジェクトに住んでいます。したがって再起動は常にリセットであり、ときには気前のよすぎるリセットです。
- **枯渇したサービスは再起動まで止まったままです**: 一時停止したハートビートや停止したスイーパーには、実行時に再武装する API がありません("manual recovery required" は文字どおりの意味です)。プロセス自体は提供を続けます。
- **クラッシュゲートはウィンドウベースです**: 5 分より離れて間隔が空いたクラッシュループは決して作動せず、状態ファイルを削除するスーパーバイザはそれをリセットします。このファイルはブレーカーの記憶であり、手動のエスケープハッチでもあります。
- **`hard_stop_enabled` はデフォルトで False です**: 厳格モードでは、同一ツールの失敗と hard-stop 変換された BLOCK だけが HALT に達し、他の病理は BLOCK で止まります(リカバリモードの影響を受けます)。
- **内容の正規化は両刃の剣です**: 空白/句読点の除去はハッシュをフォーマットノイズに強くしますが、毎回*言い換えながら*ループするモデルはハッシュベースの検知を回避します。内部のセグメント/連続ラン検知器が部分的にカバーします。完全に言い換えられたループは範囲外です。
- **リカバリモードはモデルに失敗の余地を与えます**: しつこい病理は HALT の前に、管理された再試行を一度支払います。即席の壁を望むオペレータは `recovery_mode_enabled=False` を設定すべきです。
- **`TOOL_CALL_TIMEOUT_MINUTES` は宣言されているのに読まれていません**: `.env.example` に存在し(ルート README にも記載されています)、今日これを消費するコードはありません。上に列挙したツール別定数が本当の境界です。
- **HTTP 専用モードは縮小されたフットプリントであって、ロックダウンではありません**: チャット、HTTP/WS ルート、cron REST は設計上、生きたままです。目的は*クラッシュループ*を断ち切ることであり、プロセスを空気遮断することではありません。
