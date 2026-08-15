# EMA Cron — 予定タスクサービス

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

EMA AI Agent システム内で、定期的・一時的・cron 式ベースのエージェントタスクを予約・実行するための軽量なファイルベースの cron サービスモジュールです。

## 機能

- 3 種類のスケジュールタイプ: `at`（一時）、`every`（間隔）、`cron`（cron 式）
- ファイルベースの永続ストレージ（`jobs.json`）、外部変更時の自動リロード
- ミリ秒精度のタイマー駆動実行
- ジョブごとの実行履歴（直近 20 件を保持）
- 保護されたシステムジョブ（API 経由では削除不可）
- cron 式のタイムゾーンサポート
- 外部チャネルへのメッセージ配信設定（例: QQ、WhatsApp）

## モジュール構成

```
cron/
├── __init__.py    # 公開エクスポート: CronService, cron_service, types
├── core.py        # コア実装: CronService、ジョブ実行、タイマーループ
├── types.py       # データモデル: CronSchedule, CronPayload, CronJob など
├── jobs.json      # 永続ジョブストア（自動管理）
└── README.md      # このファイル
```

## 型リファレンス

### CronSchedule

ジョブを実行するタイミングを定義します。

| フィールド | 型   | 説明 |
|-----------|--------|-------------|
| `kind`    | `"at" \| "every" \| "cron"` | スケジュールタイプ |
| `at_ms`   | `int \| None` | "at" 用のミリ秒単位の Unix タイムスタンプ |
| `every_ms`| `int \| None` | "every" 用のミリ秒単位の間隔 |
| `expr`    | `str \| None` | "cron" 用の cron 式、例: `"0 9 * * *"` |
| `tz`      | `str \| None` | タイムゾーン、例: `"Asia/Shanghai"`。"cron" のみ |

### CronPayload

ジョブが実行されたときのアクションを定義します。

| フィールド | 型            | 説明 |
|-----------|-----------------|-------------|
| `kind`    | `"system_event" \| "agent_turn"` | ペイロードタイプ |
| `message` | `str`           | エージェントに送るプロンプトメッセージ |
| `deliver` | `bool`          | 結果を外部チャネルに配信するか |
| `channel` | `str \| None`   | チャネル名（例: `"whatsapp"`、`"qq"`） |
| `to`      | `str \| None`   | 受信者識別子 |

### CronJob

完全なジョブ定義です。

| フィールド          | 型            | 説明 |
|---------------------|-----------------|-------------|
| `id`                | `str`           | 一意のジョブ ID（自動生成） |
| `name`              | `str`           | 人間が読める名前 |
| `enabled`           | `bool`          | ジョブがアクティブか |
| `schedule`          | `CronSchedule`  | スケジュール定義 |
| `payload`           | `CronPayload`   | アクション定義 |
| `delete_after_run`  | `bool`          | 一時実行後に自動削除するか |

## 公開 API

### `CronService`（`cron_service` によるシングルトン）

| メソッド | 説明 |
|--------|-------------|
| `start()` | cron サービスを開始 |
| `stop()` | cron サービスを停止 |
| `list_jobs(include_disabled=False)` | すべてのジョブを一覧表示 |
| `add_job(name, schedule, message, ...)` | 新しいジョブを追加 |
| `register_system_job(job)` | 保護されたシステムジョブを登録 |
| `remove_job(job_id)` | ジョブを削除 |
| `enable_job(job_id, enabled=True)` | ジョブを有効/無効化 |
| `run_job(job_id, force=False)` | ジョブを手動トリガー |
| `get_job(job_id)` | ジョブの詳細を取得 |
| `status()` | サービスの状態を取得 |

## 使用例

```python
from cron import cron_service, CronSchedule

# サービスを開始
await cron_service.start()

# 一時ジョブ: 指定時間に実行
cron_service.add_job(
    name="morning_greeting",
    schedule=CronSchedule(kind="at", at_ms=1700000000000),
    message="Say good morning to the user",
    deliver=True,
    channel="qq",
    to="group_123456",
    delete_after_run=True,
)

# 間隔ジョブ: 30 分ごとに実行
cron_service.add_job(
    name="weather_update",
    schedule=CronSchedule(kind="every", every_ms=30 * 60 * 1000),
    message="Check today's weather and remind user to bring an umbrella if needed",
)

# Cron ジョブ: 上海時間で毎日午前 9 時に実行
cron_service.add_job(
    name="daily_digest",
    schedule=CronSchedule(kind="cron", expr="0 9 * * *", tz="Asia/Shanghai"),
    message="Summarize today's schedule and important events",
)

# すべてのジョブを一覧表示
jobs = cron_service.list_jobs()
for j in jobs:
    print(f"{j.name}: next run at {j.state.next_run_at_ms}")

# ジョブを手動トリガー
await cron_service.run_job("job_id_here", force=True)

# ジョブを削除
cron_service.remove_job("job_id_here")
```

## ジョブの永続性

すべてのジョブは `jobs.json` に永続化されます。ファイルはサービス開始時に自動ロードされ、外部変更が検出された場合（ファイルの更新時刻を比較することにより）自動リロードされます。`jobs.json` を直接編集してジョブを一括追加・変更できます — サービスは次の tick で変更を反映します。

## スケジュールのセマンティクス

| 種類 | 動作 |
|------|----------|
| `at` | 指定されたタイムスタンプで 1 回実行。実行後は無効化（`delete_after_run=True` の場合削除） |
| `every` | 各完了から固定の `every_ms` 間隔で再実行 |
| `cron` | `croniter` を使用し、指定されたタイムゾーンで cron 式から次の実行時刻を計算 |

## 依存関係

- `croniter` — cron 式のパース
- Python `zoneinfo` — タイムゾーンサポート

## 注意事項

- 一時（`at`）ジョブは実行後、デフォルトで**無効化**（削除ではない）されます。自動削除するには `delete_after_run=True` を設定してください。
- システムジョブ（`payload.kind == "system_event"`）は保護されており、`remove_job()` では削除できません。
- cron サービスは asyncio イベントループに依存します — `await cron_service.start()` を呼び出す時、アプリケーションがイベントループを実行していることを確認してください。
