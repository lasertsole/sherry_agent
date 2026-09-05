# EMA Cron — 予定タスクサービス

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

EMA AI Agent システム向けの軽量なファイルベース cron サービスです。一回限りのタスク、固定間隔タスク、cron 式タスクをスケジュール・実行します。ジョブはプロジェクトルートの `cron_jobs.json` に永続化され、専用のバックグラウンドサービスによって実行され、その結果はメッセージバスを通じて有効なチャネルへ配信されます。

## 機能

- 3 種類のスケジュールタイプ: `at`（一回限り）、`every`（固定間隔）、`cron`（cron 式、`croniter` 使用）
- `cron_jobs.json`（プロジェクトルート）によるファイル永続化、外部変更時の自動リロード
- 専用のバックグラウンドサービススレッド（独自の asyncio イベントループを持つ）; 自己再武装するタイマーが最も早い実行時刻のジョブに合わせて正確に起床
- ジョブ実行は専用エージェントを起動（メイン LLM + システムプロンプト + Python REPL / ファイル読み込み / ファイル書き込みツール）
- 結果は `MessageBus` のインバウンドキューを通じてチャネルへ配信され、ブラウザ UI にはベストエフォートで WebSocket `notification` イベントを送信
- ジョブごとの実行ログを JSON Lines 形式で `logs/output/cron/<job_id>.log` に追記
- 保護されたシステムジョブ（`payload.kind == "system_event"`）は削除不可
- cron 式のタイムゾーンサポート（`zoneinfo` による IANA タイムゾーン名）
- REST API（Robyn）: `GET/POST/PUT/DELETE /cron`、`POST /cron/trigger`、`POST /cron/enable`、`POST /cron/failure-state`、`POST /cron/reset-failures` — デスクトップクライアント向け

## モジュール構成

```
skills/builtin/core/cron/
├── __init__.py
├── SKILL.md             # エージェントスキル定義（add / list / remove / set_context のレシピ）
└── scripts/
    ├── __init__.py      # 公開エクスポート: CronService, cron_service, Cron, cron, types
    ├── base.py          # CronService シングルトン、cron_jobs.json 入出力、タイマーループ、ジョブ実行
    ├── core.py          # Cron ファサード（エージェント向け）: add_job / list_jobs / remove_job / set_context
    ├── types.py         # データモデル: CronSchedule, CronPayload, CronRunRecord, CronJobState, CronJob, CronStore
    └── README.md        # このファイル
```

このスキルディレクトリ外の関連コード:

- [`server/trigger/http/cron.py`](../../../../../server/trigger/http/cron.py) — `cron_service` をラップする REST エンドポイント
- [`../SKILL.md`](../SKILL.md) — エージェントによるスキルスクリプトの呼び出し方法
- `cron_jobs.json` — プロジェクトルートのジョブストア（`config.ROOT_DIR / "cron_jobs.json"`）
- `logs/output/cron/` — ジョブごとの実行ログ

## 動作の仕組み

1. **サービス起動**: サービスのエントリポイントが `skills.builtin.core.cron.scripts.base` の `init()` を呼び出し、実行コールバックを接続して `cron-service` という名前のデーモンスレッド（`_start_cron_service_thread`）を起動します。このスレッドは専用の asyncio イベントループを作成し、`cron_service.start()` を実行した後ループし続けます。cron スクリプトのインポートに副作用はありません。`CronService.add_job()` / `register_system_job()` も、サービスが未起動の場合は呼び出し元のイベントループ上で遅延起動します。
2. **タイマーループ**: `_arm_timer()` は有効なジョブの中で最も早い `nextRunAtMs` までの `asyncio` スリープを 1 回スケジュールします。その後 `_on_timer()` がストアを再ロード（外部変更を取得）し、`nextRunAtMs <= now` の有効なジョブをすべて実行して、ストアを保存し、タイマーを再武装します。
3. **実行**（`_execute_job`）: `set_on_job` で登録されたコールバック（`_on_cron_job`）がジョブを実行します。ジョブの `lastStatus` / `lastError` を記録し、WS 通知を送信し、実行ログを 1 行追記します。一回限り（`at`）のジョブはその後削除（`deleteAfterRun` 時）または無効化され、定期ジョブは次回実行時刻を再計算します。

**結果の配信**（`base.py` の `_on_cron_job`）:

1. `create_agent(system_prompt=build_system_prompt(), model=build_main_llm(), tools=[build_python_repl_tool(), build_read_file_tool(), build_write_file_tool()])` で新しいエージェントを構築し、ジョブの `payload.message` を `HumanMessage` として実行します。
2. エージェントの最終メッセージを `InboundMessage(channel=payload.channel, sender_id="cron tool", chat_id=payload.to, content=result)` としてメッセージバスにパブリッシュします。
3. チャネルのインバウンドコンシューマ（`server/trigger/channels/core.py`）が有効なチャネルごとにそのメッセージを処理し、生成された返信を `channel.send(OutboundMessage(...))` で設定された `chat_id` へ配信します。
4. それとは別に、`_push_cron_notification` がセッション `default`（`CRON_WS_SESSION_ID`）の WebSocket へ `{"event": "notification", "content": "cron: <job name> [<status>]"}` を送信し、ブラウザ UI の通知ベルをリアルタイムに更新します。ベストエフォート: 失敗はログに記録されるだけでフローを中断しません。

> 注意: `deliver` フィールドはジョブに保存され API でも公開されますが、現在の実行パス（`_on_cron_job`）はその値に関係なく結果をバスへパブリッシュします。メッセージが実際にユーザーに届くかどうかは、有効なチャネルに依存します（`plugins/channels/config.json` を参照）。

## ジョブストア（`cron_jobs.json`）

ジョブはプロジェクトルートの `cron_jobs.json` に永続化されます。ファイルはサービス起動時にロードされ、更新時刻（mtime）が変わるたびに自動的に再ロードされます。ファイルを直接編集してジョブを一括追加・変更でき、変更は次のタイマーティックで反映されます。

ディスク上のフィールドは camelCase です（`base.py` の `_save_store` / `_load_store`）。最上位は `version`（int）と `jobs`（配列）です。ジョブの例:

```json
{
  "version": 1,
  "jobs": [
    {
      "id": "a1b2c3d4",
      "name": "daily_digest",
      "enabled": true,
      "schedule": {
        "kind": "cron",
        "atMs": null,
        "everyMs": null,
        "expr": "0 9 * * *",
        "tz": "Asia/Shanghai"
      },
      "payload": {
        "kind": "agent_turn",
        "message": "Summarize today's schedule and important events",
        "deliver": false,
        "channel": null,
        "to": null
      },
      "state": {
        "nextRunAtMs": 1756000000000,
        "lastRunAtMs": null,
        "lastStatus": null,
        "lastError": null
      },
      "createdAtMs": 1755000000000,
      "updatedAtMs": 1755000000000,
      "deleteAfterRun": false
    }
  ]
}
```

| フィールド | 型 | 説明 |
|-----------|-----|------|
| `id` | `str` | 一意なジョブ ID（`uuid4` の先頭 8 文字） |
| `name` | `str` | 人間が読める名前 |
| `enabled` | `bool` | 有効かどうか（デフォルト `true`） |
| `schedule` | `object` | 実行タイミング: 下記参照 |
| `payload` | `object` | 実行内容: 下記参照 |
| `state` | `object` | 実行時状態: 下記参照 |
| `createdAtMs` | `int` | 作成タイムスタンプ（ミリ秒） |
| `updatedAtMs` | `int` | 最終更新タイムスタンプ（ミリ秒） |
| `deleteAfterRun` | `bool` | 一回限りの実行後にジョブを削除するか（デフォルト `false`） |

**`schedule`**

| フィールド | 型 | 説明 |
|-----------|-----|------|
| `kind` | `"at" \| "every" \| "cron"` | スケジュールタイプ |
| `atMs` | `int \| null` | Unix タイムスタンプ（ミリ秒） — `kind: "at"` 用 |
| `everyMs` | `int \| null` | 間隔（ミリ秒） — `kind: "every"` 用 |
| `expr` | `str \| null` | cron 式、例: `"0 9 * * *"` — `kind: "cron"` 用 |
| `tz` | `str \| null` | IANA タイムゾーン、例: `"Asia/Shanghai"` — `kind: "cron"` とのみ併用可能 |

**`payload`**

| フィールド | 型 | 説明 |
|-----------|-----|------|
| `kind` | `"agent_turn" \| "system_event"` | ペイロードタイプ（デフォルト `"agent_turn"`; サービスや API から追加されるジョブは常に `agent_turn`） |
| `message` | `str` | エージェントへ送るプロンプトメッセージ |
| `deliver` | `bool` | 配信フラグ（デフォルト `false`; 上記の注意を参照 — 現在の実行パスでは参照されません） |
| `channel` | `str \| null` | チャネル名、例: `"qq"` |
| `to` | `str \| null` | 受信者識別子（`chat_id` として使用） |

**`state`**

| フィールド | 型 | 説明 |
|-----------|-----|------|
| `nextRunAtMs` | `int \| null` | 次回の予定実行時刻（ミリ秒）; 無効・期限切れジョブは `null` |
| `lastRunAtMs` | `int \| null` | 最終実行の開始時刻（ミリ秒） |
| `lastStatus` | `"ok" \| "error" \| "skipped" \| null` | 最終実行の結果 |
| `lastError` | `str \| null` | 最後のエラーメッセージ |

Python 側の対応モデル（`types.py`）は snake_case を使用します（`at_ms`、`every_ms`、`next_run_at_ms`、`last_run_at_ms`、`last_status`、`last_error`、`created_at_ms`、`updated_at_ms`、`delete_after_run`）。`CronRunRecord` はエクスポートされていますが現在未使用です。

## 公開 API

### エージェントスキルコマンド（`Cron` ファサード、`core.py` の `cron` シングルトン）

以下は [`../SKILL.md`](../SKILL.md) を通じてエージェントに公開されるコマンドです。`from skills.builtin.core.cron.scripts import cron` として使用します:

| コマンド | 説明 |
|---------|------|
| `cron.set_context(channel, chat_id)` | セッションコンテキストを設定（両方必須・非空）。以降に追加するジョブの配信先になります |
| `cron.add_job(name=None, message, every_seconds=None, cron_expr=None, tz=None, at=None, deliver=True)` | ジョブを追加。`every_seconds` / `cron_expr` / `at`（ISO 日時）のいずれか 1 つが必須。事前の `set_context` が必要。`tz` は `cron_expr` とのみ併用可（デフォルト `"UTC"`）; タイムゾーン情報のない `at` は UTC として扱われ、`at` ジョブには `delete_after_run=True` が設定されます。`name` のデフォルトは `message` の先頭 30 文字 |
| `cron.list_jobs()` | 人間が読める形式の一覧: スケジュール時刻、システムジョブの用途と保護フラグ、前回/次回の実行時刻 |
| `cron.remove_job(job_id)` | ジョブを削除。保護されたシステムジョブには丁寧なエラーメッセージを返します |

### `CronService`（Python API、`base.py` の `cron_service` シングルトン）

| メソッド | 説明 |
|---------|------|
| `await start()` | ストアをロードし、次回実行時刻を再計算して保存し、タイマーを武装 |
| `stop()` | サービスを停止しタイマータスクをキャンセル |
| `set_on_job(callback)` | 非同期実行コールバックを登録（`init()` によって `_on_cron_job` に接続） |
| `list_jobs(include_disabled=False)` | 次回実行時刻でソートしてジョブを一覧表示; `include_disabled=True` の場合のみ無効なジョブを含む |
| `add_job(name, schedule, message, deliver=False, channel=None, to=None, delete_after_run=False)` | ジョブを追加（`payload.kind` は常に `"agent_turn"`）; サービスを自動起動; `CronJob` を返す |
| `register_system_job(job)` | `id` をキーにシステムジョブを冪等に（再）登録（現在リポジトリ内に呼び出し元なし） |
| `remove_job(job_id)` | `"removed"`、`"protected"`（`payload.kind == "system_event"`）、`"not_found"` のいずれかを返す |
| `enable_job(job_id, enabled=True)` | 有効/無効化; `nextRunAtMs` を再計算またはクリア |
| `await run_job(job_id, force=False)` | 即時実行; 無効なジョブは `force=True` がない限りスキップ |
| `get_job(job_id)` | ID でジョブを取得、なければ `None` |
| `status()` | `{"enabled": bool, "jobs": int, "next_wake_at_ms": int \| None}` を返す |

### HTTP REST API（`server/trigger/http/cron.py`、バックエンド `http://127.0.0.1:8080`）

| エンドポイント | 説明 |
|--------------|------|
| `GET /cron?include_disabled=false` | ジョブ一覧（camelCase JSON） |
| `POST /cron` | 作成: `{"name", "message", "schedule": {"kind", "atMs"/"everyMs"/"expr"/"tz"}, "deliver", "channel", "to", "delete_after_run"}` |
| `PUT /cron` | 更新: 削除 + 再追加として適用され、`id` と `createdAtMs` は保持される |
| `POST /cron/trigger` | 即時実行: `{"id", "force"}`（無効かつ `force` なしの場合は 400） |
| `POST /cron/enable` | 有効/無効化: `{"id", "enabled"}` |
| `POST /cron/failure-state` | 失敗ブレーカー状態の照会: `{"id"}` → `{consecutive_failures, last_error, degraded_since, backoff_ms}`; 未知のジョブ → `404`、失敗したことのないジョブ → ゼロの状態 |
| `POST /cron/reset-failures` | 失敗ブレーカー状態のリセット: `{"id"}`; ブレーカー自身が無効化したジョブだけを再有効化（オペレータによる無効化は保持） |
| `DELETE /cron` | 削除: `{"id"}`; 保護されたシステムジョブは `403` |

## 失敗ブレーカー

すべての定期ジョブはジョブごとの失敗ブレーカー（`base.py` の `CronJobFailureState`）で保護されています。連続失敗は降格を経て自動無効化へ段階的にエスカレートし、完全に壊れたジョブが永遠に発火し続けるのを防ぎます。ブレーカー状態はメモリ専用です。`cron_jobs.json` のスキーマはそのままで、ブレーカーが書き戻すのはジョブの既存 `enabled` フラグだけです。

| 連続失敗 | 効果 |
|----------|------|
| 1-4 | ジョブは普段どおり失敗: ステータスを error にマーク、WS ベル通知 |
| ≥ 5 (降格) | バックオフウィンドウ内はトリガーをスキップ: 最後の失敗から `min(5000ms × 2^(n-5), 300000ms)`（n = 連続失敗回数） |
| ≥ 10 | `enabled=False` をジョブストアに永続化; 最善努力の通知をジョブの payload チャンネルへ |

- **記録してから再スロー:** 失敗をまず記録し、それから例外をそのまま再スローするので、`lastStatus` / `lastError` の報告と WS ベルは無傷のまま保たれます。
- **一回きりの `at` ジョブは免除** (二度発火し得ないため、単一の失敗はループではない)。
- 成功は状態を完全にリセットし、手動の `enable_job` はこれを消します。`POST /cron/reset-failures` はブレーカー自身が無効化したジョブだけを再有効化するので、オペレータによる無効化は保持されます。
- メモリ上の失敗カウンタはプロセス再起動で失われます（永続化された `enabled` フラグは残ります）。

## 使用例

エージェントスキルスクリプト（[`../SKILL.md`](../SKILL.md) 参照）:

```python
from loguru import logger
from skills.builtin.core.cron.scripts import cron

# ジョブを追加する前に、セッションコンテキストを一度設定する必要があります
cron.set_context(channel="qq", chat_id="group_123456")

# cron 式ジョブ: 毎日上海時間 9 時
res = cron.add_job(
    name="daily_digest",
    message="Summarize today's schedule and important events",
    cron_expr="0 9 * * *",
    tz="Asia/Shanghai",
)
logger.info(res)

# 固定間隔ジョブ: 30 分ごと
res = cron.add_job(
    message="Check today's weather and remind user to bring an umbrella if needed",
    every_seconds=30 * 60,
)

# 一回限りのジョブ: 明示的な ISO 日時
res = cron.add_job(message="Say good morning to the user", at="2026-02-12T10:30:00")

logger.info(cron.list_jobs())
# cron.remove_job("a1b2c3d4")
```

Python API:

```python
from skills.builtin.core.cron.scripts import cron_service, CronSchedule

# サービスは初回使用時に自動起動します。明示的な start は任意です
await cron_service.start()

job = cron_service.add_job(
    name="weather_update",
    schedule=CronSchedule(kind="every", every_ms=30 * 60 * 1000),
    message="Check today's weather and remind user to bring an umbrella if needed",
)

jobs = cron_service.list_jobs()
print([j.name for j in jobs])

await cron_service.run_job(job.id, force=True)   # 手動トリガー
cron_service.remove_job(job.id)                   # "removed" | "protected" | "not_found"
```

HTTP:

```bash
curl http://127.0.0.1:8080/cron
curl -X POST http://127.0.0.1:8080/cron -H "Content-Type: application/json" \
  -d '{"name": "daily_digest", "message": "Summarize today", "schedule": {"kind": "cron", "expr": "0 9 * * *", "tz": "Asia/Shanghai"}}'
curl -X POST http://127.0.0.1:8080/cron/trigger -H "Content-Type: application/json" -d '{"id": "a1b2c3d4", "force": true}'
```

## スケジューリングの意味論

| 種類 | 動作 |
|------|------|
| `at` | `atMs` に指定された時刻に 1 回だけ発火。計算時点でタイムスタンプが過去の場合、`nextRunAtMs` は `null` になりジョブは実行されません。実行後は削除（`deleteAfterRun=true`）または無効化（`enabled=false`、`nextRunAtMs=null`）されます |
| `every` | 次回実行 = 現在時刻 + `everyMs`。実行のたびに再計算されます |
| `cron` | `croniter` が式から次回実行時刻を計算。基準時刻は `tz` 指定があればそのタイムゾーンで、なければシステムのローカルタイムゾーンで評価されます |

バリデーション: `tz` は `kind: "cron"` の場合のみ指定可能。不明な IANA タイムゾーン名は拒否されます（`ValueError`）。サービス層とファサード層の両方で同様です。

## 保護されたシステムジョブ

`payload.kind == "system_event"` のジョブは保護されています: `CronService.remove_job()` は削除を拒否し（`"protected"`、HTTP `DELETE /cron` は `403`）、スキル層はさらに `dream` という名前のジョブを認識し、長期記憶のための Dream 記憶統合ジョブとして説明します。`add_job`（Python・スキル・HTTP いずれも）で追加されるジョブは常に `agent_turn` です。`system_event` ジョブは `register_system_job()` または `cron_jobs.json` の直接編集によってのみ作成されます。

## 依存関係

- `croniter>=6.2.2` — cron 式のパース
- Python `zoneinfo` — タイムゾーンサポート
- `config/` に cron 固有の設定項目は存在せず、cron 関連の環境変数もありません

## 注意事項

- 実行履歴: 実行のたびに `logs/output/cron/<job_id>.log` へ 1 行の JSON が追記されます（`timestamp`、`job_id`、`job_name`、`start_time`、`end_time`、`duration_ms`、`status`、`error`、`message`）。メモリ上の実行履歴はありません（`CronRunRecord` は未使用のレガシーです）。
- `cron_jobs.json` への外部変更はファイルの更新時刻で検出され、次のタイマーティックで反映されます。ストアは実行のたびに再保存されます。
- サービスは `cron-service` デーモンスレッド上の独立したイベントループで動作し、メインサーバーのループとは独立しています。`run_job()` と `start()` は実行中のイベントループから await する必要があります。
- WebSocket 通知はセッション `"default"`（ブラウザクライアントのセッション）宛てのため、クライアント接続中のみデスクトップ通知が届きます。

▶️ 詳細：[docs/harness/loop-prevention/README.md](../../../../../docs/harness/loop-prevention/README.md) · [中文](../../../../../docs/harness/loop-prevention/README.zh.md) · [한국어](../../../../../docs/harness/loop-prevention/README.ko.md) · [日本語](../../../../../docs/harness/loop-prevention/README.ja.md)
