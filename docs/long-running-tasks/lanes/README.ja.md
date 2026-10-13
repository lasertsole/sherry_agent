# 🚦 並行レーン — MAIN · SUBAGENT · NUDGE · NESTED

[English](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> [Long-Running Tasks](../README.ja.md) の一部：プロセスレベルの並行レーン（MAIN、SUBAGENT、NUDGE、NESTED）で、子エージェント、バックグラウンド nudge、ネスト返信ターンを制御します。

---

## 🚦 並行レーン

長時間実行される作業は無制限にファンアウトしません。切り離された子エージェント、バックグラウンドの nudge、ネストされた `sessions.send` の返信ターンは、まず**プロセスレベルのレーン**を通ります。レーンは `asyncio.Semaphore` と `active`/`queued` カウンタ（`runtime/lane/core.py`）で、上限超過の作業は拒否されず **FIFO でキューイング**されます。

| レーン | 制約対象 | 既定並行数 | 設定キー |
| :--- | :--- | :--- | :--- |
| `MAIN` | メインエージェントのターン（`server/service/input_queue_service.py::_run_executor`） | `min(16, max(8, CPU))`、`SUBAGENT + NUDGE` 以上へ切り上げ → 12–16 | `LANE_SYSTEM["main_max_concurrent"]` |
| `SUBAGENT` | 子エージェントの実行（`spawn/core.py`、`control/steer.py`） | `8` | `LANE_SYSTEM["subagent_max_concurrent"]` |
| `NUDGE` | メモリ nudge / 計画抽出 / 圧縮後 todo 更新（`agent/middlewares/summarization/nudges.py` の 3 箇所） | `4` | `LANE_SYSTEM["nudge_max_concurrent"]` |
| `NESTED` | `sessions_send` の返信ターン（直列） | `1` | `LANE_SYSTEM["nested_max_concurrent"]` |

### 設定と検証

レーン上限は `config/features/infra_side/lane_system.py` にあります：

```python
LANE_SYSTEM: LaneSystemConfig = {
    "main_max_concurrent": _resolve_main_concurrency(),   # 12–16
    "subagent_max_concurrent": 8,
    "nudge_max_concurrent": 4,
    "nested_max_concurrent": 1,
    "lane_wait_warn_ms": 5000,
    "lane_drain_timeout_seconds": 30.0,
}
```

`_resolve_main_concurrency()` は OpenClaw の CPU スケーリング——`min(16, max(8, CPU))`——を踏襲し、さらにハード不変条件 `SUBAGENT + NUDGE`（8 + 4 = 12）以上へ**切り上げ**るため、出荷時の既定値はどの CPU 数でも有効です。`validate_lane_config()` はサーバー起動時に `install_lane_lifecycle()`（`server/service/lane_lifecycle.py`）から一度だけ呼ばれ（import 時には決して呼ばれません）、`main_max_concurrent < subagent + nudge` またはいずれかの上限が `< 1` のとき例外を送出します。`lane_wait_warn_ms` は「スロット待ちが長すぎる」警告を制御し、`lane_drain_timeout_seconds` は `LaneManager.drain_all()` の既定タイムアウトです。`LaneManager.set_concurrency(lane, n)` は上限をホット更新します——実行中のスロットは permit を保持したまま、新しい acquire だけが新しい上限を見ます。

### キューイング意味論：`PENDING`

グローバル並行性は拒否カウンタではありません。`spawn_subagent_direct` は親単位のアドミッション（`validate_spawn_depth`、`validate_concurrent_children`）を引き続き適用しますが、グローバル上限を超える spawn は**受理**され `ExecutionStatus.PENDING` として登録されます：`started_at` は `None` のままで、その run はレーンスロットを保持しません。SUBAGENT レーンのラッパー（`_execute_subagent_with_lane`、`agent/tools/subagent/spawn/core.py`）がスロットを待ち、`mark_run_running()` で `PENDING → RUNNING` へ昇格し、その瞬間に `started_at` を刻みます——キュー待ち時間が実行時間として数えられることはありません。`validate_global_concurrent()` と `SubagentConfig.max_concurrent` は後方互換のためだけに残り、spawn パイプラインからは呼ばれません。

キュー内の子は依然としてアドミッションスロットを占有するため、レジストリの計数関数は `RUNNING + PENDING` をアクティブとして数え（`count_active_runs_for_session`、`count_active_descendant_runs`、`count_all_active_runs`）、`is_live_unended_run()` は `PENDING` を含みます。

### PENDING ライフサイクル

| 経路 | 挙動 |
| :--- | :--- |
| **Kill** | PENDING run は一覧・kill 可能（`list_killable_children`）；`cancel_task()` がレーン待機をキャンセルし、`CancelledError` は permit を消費も漏洩もせず `Lane.acquire()` から伝播します |
| **Steer** | 拒否——`steer_subagent_run()` は RUNNING/INTERRUPTED のみ受理；steer で再起動された run は PENDING としてレーンに再入し、自分のスロット内で昇格します |
| **Sweeper / 孤児回復** | `is_live_unended_run()` は PENDING を含むため、task を失った PENDING run は孤児です；`evaluate_recovery_gate()` は `"wedged"` と判定し、`_recovery_loop()` は `ended_reason="pending_orphaned"`（outcome `TIMEOUT`、error `"pending orphaned"`）で直接 `TERMINAL` に確定し、announce フローを実行します |
| **再起動 / 復元** | `restore_runs_from_disk()`（`registry/state.py`）は起動時に SQLite から復元した全 PENDING run を `TERMINAL` / `pending_orphaned` として確定します —— 静かに実行され、announce フローは実行しません（親セッションは前のプロセス生存期間のものです）；同一プロセス生存期間内で task を失った場合のみ sweeper の孤児回復に入ります |
| **Yield** | `sessions_yield` は PENDING の子をアクティブとして数え、`wake_yield_if_all_children_settled` は全部が終わって初めて親を起こします；yield タイムアウトはレーン待ちを含み、期限切れ時は正常に戻ります |
| **計数 / 一覧** | `control/list.py` と `runtime_tools.py` は PENDING の子を RUNNING/INTERRUPTED と一緒に表示します |

PENDING run のレーン task がまだ存在する間、sweeper スキャンはそれをスキップします（プロセスは単にキューイングしているだけです）；task の消失だけが孤児化の原因です —— 前のプロセスが遺した PENDING は起動時の復元で確定されるため、sweeper には届きません。

### Drain モードとシャットダウン

`Lane.acquire()` は待機前に drain チェックコールバックを参照します：subagent ゲートウェイが draining を報告している間、取得者は `RuntimeError` で拒否され、**permit を消費しません**。コールバックは起動時に `set_drain_check(is_gateway_draining)`（`install_lane_lifecycle`）で注入され、`runtime/lane` を上位 import から解放します。

終了時、同じ seam が drain モードを切り替え（`set_draining(True)`）、`atexit` 経由の**有界** drain を実行します——`asyncio.run(drain_all_lanes(timeout=0))` は最終的なレーン別カウンタを報告し、プロセス終了を決して遅らせません。リポジトリには非同期シャットダウンパスが存在しない（Robyn の `shutdown_handler` は SIGINT/SIGTERM で呼ばれない）ため、実行中のターンは待たずに OS へ委ねられます。

### 可観測性

`GET /lane-status`（`server/trigger/http/lane.py`）は 4 レーンすべてのライブスナップショットを返します：

```json
{"main": {"name": "main", "max_concurrent": 12, "active": 1, "queued": 0},
 "subagent": {"name": "subagent", "max_concurrent": 8, "active": 3, "queued": 2},
 "nudge": {"name": "nudge", "max_concurrent": 4, "active": 0, "queued": 0},
 "nested": {"name": "nested", "max_concurrent": 1, "active": 0, "queued": 0}}
```

### デッドロック防止

| シナリオ | メカニズム |
| :--- | :--- |
| メインターンが MAIN スロットを保持したまま子の結果を待つ | 子は SUBAGENT スロットだけを必要とします——レーンは独立したセマフォであり、レーン間の待機サイクルは存在しません（`test_filling_main_does_not_block_subagent`、spawn 経路: `test_subagent_runs_while_main_lane_slot_is_held`）；任意の `run_timeout_seconds > 0` がハングした子を追加で打ち切ります |
| Nudge が SUBAGENT スロットを待つ | nudge はサブエージェントを spawn せず（ツールセット制限）、NUDGE レーン自体も独立です |
| 実行中のレーンホット更新 | `set_concurrency` は新しい acquire にのみ影響します |
| `sessions_yield` が PENDING の子を待つ | yield タイムアウトはレーン待ちを含み、期限切れ時は正常に戻ります |
| Drain モード + レーン待機者 | `acquire()` は drain チェックで拒否し、permit を消費しません |
| PENDING run の kill | `CancelledError` は permit を解放せず `Lane.acquire()` を抜けます |
| PENDING run の steer | ステータスチェックで拒否；steer 可能なのは RUNNING/INTERRUPTED だけです |

### レーンファイルマップ

| ファイル | 役割 |
| :--- | :--- |
| `config/features/infra_side/lane_system.py` | `LaneSystemConfig` / `LANE_SYSTEM` + `validate_lane_config()` |
| `runtime/lane/core.py` | `LaneType`、`Lane`、`LaneManager`、`get_lane_manager()`、`lane_slot()`、`set_drain_check()` |
| `server/service/lane_lifecycle.py` | 起動時検証、drain ゲート登録、有界終了 drain |
| `server/trigger/http/lane.py` | `GET /lane-status` |
| `agent/tools/subagent/spawn/core.py` · `control/steer.py` | SUBAGENT レーンラッパー + PENDING → RUNNING 昇格 |
| `agent/middlewares/summarization/nudges.py` | NUDGE レーンの 3 呼び出し箇所 |
| `agent/tools/subagent/tools/sessions_send.py` | 返信ターンを包む NESTED レーン |
| `server/service/input_queue_service.py` | `_run_executor` を包む MAIN レーン |
| `agent/tools/subagent/orphan/recovery.py` | PENDING 孤児の `pending_orphaned` 確定 |
| `agent/tools/subagent/registry/state.py` | 再起動残留 PENDING の復元時 `pending_orphaned` 確定 |
| `tests/runtime/lane/` · `tests/server/service/test_main_lane.py` · `tests/agent/tools/subagent/test_{spawn_lane_integration,kill_pending,steer_lane,sweeper_pending,sessions_yield_pending,registry_restore}.py` · `tests/server/trigger/http/test_lane_api.py` | レーンテストスイート |

### 実装上のトレードオフ

実装時に記録した意図的なトレードオフが 3 点あります：

- **MAIN は不変条件まで切り上げ。** CPU スケーリング単独（`min(16, max(8, CPU))`）では CPU ≤ 8 のマシンで 8 となり、起動時に `validate_lane_config()` が失敗するため、既定値を `SUBAGENT + NUDGE`（12）まで切り上げ、4/8/12/16/64 コアのマシンいずれでも有効にしています。
- **`GET /lane-status` に `/api` プレフィックスは付きません。** 当初設計の `/api/lane-status` ルートは採用せず、リポジトリ既存のルーティング慣例に従っています：ハンドラは `server/trigger/http/lane.py` にあり、`/channels` や `/cron` と並んでいます。
- **終了 drain は `atexit` 経由。** リポジトリに非同期シャットダウン seam が存在しない（Robyn の `shutdown_handler` は SIGINT/SIGTERM では呼ばれない）ため、有界の `drain_all(timeout=0)` は最終的なレーン別カウンタを報告するだけで、終了を遅らせません。実行中のターンは OS に委ねられます。

