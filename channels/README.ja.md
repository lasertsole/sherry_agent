# Channels

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

---

このモジュールは、チャットプラットフォームと統合するための統一インターフェースを提供し、EMA AI エージェントと外部メッセージングプラットフォーム間のメッセージの受信、送信、ルーティングを処理します。プラットフォームアダプターはこのパッケージの外部にあるプラグインとして存在し、リポジトリには現在 1 つのアダプター（**QQ**：`plugins/channels/qq/`）のみがバンドルされています。

## 概要

channels モジュールはプラグインベースのアーキテクチャを実装しており、新しいチャットプラットフォーム統合を簡単に追加できます。各チャネルは `BaseChannel` を拡張するクラスとして実装され、`MessageBus`（`bus/core.py`）を介してシステムと通信します。`MessageBus` は 2 つの無制限の `asyncio.Queue`（インバウンドとアウトバウンド）を提供します。チャネルの実装はこのパッケージ内ではなく、`plugins/channels/` の下の個別のディレクトリとして存在します（`core.py` を含むフォルダをスキャンしてランタイム時に検出されます）。

## 主な機能

- **統一インターフェース**: すべてのチャットプラットフォームが共通の `BaseChannel` インターフェースを共有します
- **プラグインシステム**: 組み込みチャネル（`plugins/channels/` の下の `core.py` を含むフォルダ）と `entry_points(group="channels")` で登録された外部プラグインの両方をサポートします
- **メッセージルーティング**: `MessageBus` 上のインバウンド/アウトバウンド `asyncio.Queue`。アウトバウンドメッセージは `ChannelManager._dispatch_outbound()` によって一致するチャネルへルーティングされます
- **アクセス制御**: 設定可能な送信者ホワイトリスト（`allow_from`）。空のリストはすべての送信者を拒否し、`"*"` はすべて許可します
- **非同期処理**: asyncio 上に構築され、同時メッセージ処理をサポートします
- **安全な起動**: `_validate_allow_from()` は、設定されたチャネルの `allow_from` が空のリストの場合（全員を拒否することになる）に `SystemExit` で起動を中断します
- **依存関係の自動インストール**: `_ensure_deps()` が各プラグインの `requirements.txt` をインストールします（可能な場合は `uv`、フォールバックで `pip`）。インストールに失敗してもチャネルはスキップされず、登録されたまま `start()` 内で遅延リトライされます

## ファイル構造

```
channels/
├── __init__.py      # パッケージエクスポート (BaseChannel, channel_manager)
├── base.py          # BaseChannel 抽象基底クラス - チャネルインターフェースを定義
├── manager.py       # ChannelManager - ライフサイクル調整とメッセージルーティング
├── registry.py      # チャネル検出 - 組み込み・プラグインチャネルを自動検出
├── README.md        # English documentation
├── README.zh.md     # 简体中文文档
├── README.ja.md     # 日本語ドキュメント
└── README.ko.md     # 한국어 문서
```

> **注**: チャネル実装はこのパッケージには**保存されません**。ランタイム時に `plugins/channels/` からロードされます（`registry.py` 参照）。現在存在するのは `plugins/channels/qq/` のみです。

## コアコンポーネント

### BaseChannel (base.py)

すべてのチャネル実装のインターフェースを定義する抽象基底クラス:

- クラス属性: `name: str = "base"`、`display_name: str = "Base"`
- `__init__(self, config: Any, bus: MessageBus)`: `self.config` と `self.bus` を保存し、`self._running = False` を設定
- `async def start(self) -> None`: 抽象メソッド — チャネルを起動しメッセージのリッスンを開始（長時間実行：接続、リッスン、`_handle_message()` による転送）
- `async def stop(self) -> None`: 抽象メソッド — チャネルを停止しリソースをクリーンアップ
- `async def send(self, msg: OutboundMessage) -> None`: 抽象メソッド — このチャネルを通じてメッセージを送信
- `def is_allowed(self, sender_id: str) -> bool`: `config.allow_from` を確認 — 空のリストはすべて拒否（警告をログ出力）、`"*"` はすべて許可、それ以外は完全一致
- `async def _handle_message(self, sender_id: str, chat_id: str, content: str, media: list[str] | None = None, metadata: dict[str, Any] | None = None, session_id: str | None = None) -> None`: `is_allowed()` で権限を確認した後、`InboundMessage` を構築し `bus.publish_inbound()` で公開
- `@classmethod def default_config(cls) -> dict[str, Any]`: デフォルトは `{"enabled": False}` を返す。プラグインはオーバーライドして `config.json` を自動生成できます
- `@property is_running(self) -> bool`: チャネルが現在実行中かどうか

### ChannelManager (manager.py)

有効なすべてのチャネルを調整します。モジュールレベルのシングルトン `channel_manager` はインポート時に生成されます:

- `__init__(config=None, bus=None)`: 設定が渡されない場合は `plugins/channels/config.json` を読み込みます（ファイルが存在しない場合は初期化が早期リターン — チャネルもバスもなし）。バスが渡されない場合は `MessageBus()` を生成し、その後 `_init_channels()` を実行
- `_init_channels()`: `channels.registry.discover_all()` で検出されたチャネルのうち、設定セクションが `"enabled": true` のものをすべてインスタンス化。インスタンス化の例外はログに記録され、そのチャネルはスキップされます。最後に `_validate_allow_from()` を呼び出し
- `_validate_allow_from()`: いずれかのチャネルの `allow_from` が `[]` の場合に `SystemExit` を送出
- `start_service()`: `_dispatch_outbound()` ディスパッチャ、`_inbound_consume_loop()` / `_outbound_consume_loop()` コンシューマ、およびチャネルごとの `_start_channel()` タスクをスケジュールし、イベントループを永続的に実行します（有効なチャネルがない場合は警告して返る）
- `async stop_service()`: ディスパッチャタスクをキャンセルし、全チャネルの `stop()` を呼び出し、イベントループを停止
- `_dispatch_outbound()`: 1 秒の `asyncio.wait_for` タイムアウトで `bus.consume_outbound()` をポーリングし、各メッセージを `self._channels[msg.channel].send(msg)` へ転送
- `set_inbound_consumer(cb)` / `set_outbound_consumer(cb)`: `(msg, channel) -> Awaitable[None]` 形式のコールバックを登録
- `_inbound_consume_loop()` / `_outbound_consume_loop()`: バスから消費し、設定された各チャネルに対して 1 回ずつ登録済みコンシューマを呼び出す
- アクセサ: `get_channel(name)`、`get_status()`（チャネルごとの `enabled` / `running`）、`get_bus()`、`get_event_loop()`、`enabled_channels` プロパティ

### Channel Registry (registry.py)

利用可能なチャネルを自動検出します:

- `discover_channel_names() -> list[str]`: `plugins/channels/` のチャネル**ディレクトリ**（サブディレクトリ、単一ファイルではない）のうち `core.py` を含むフォルダをスキャンし、ソートされた名前を返す
- `load_channel_class(module_name, strict_deps=True) -> type[BaseChannel]`: `plugins/channels/<module_name>/core.py` を動的にインポートし、最初に見つかった `BaseChannel` サブクラスを返す。`strict_deps=False` の場合、依存関係のインストール失敗でも読み込みは中断されず、チャネルは登録されたまま `start()` がインストールをリトライ
- `_ensure_deps(plugin_dir, plugin_name) -> bool`: 存在する場合はプラグインの `requirements.txt` をインストール（`uv pip install` を優先、`python -m pip` にフォールバック、120 秒タイムアウト）
- `discover_plugins() -> dict[str, type[BaseChannel]]`: `entry_points(group="channels")` で登録された外部プラグインをロード
- `discover_all() -> dict[str, type[BaseChannel]]`: 組み込み（ディレクトリスキャン、`strict_deps=False` でロード）と外部チャネルをマージ — 組み込みが優先され、外部プラグインは組み込み名を上書きできません

> 単一ファイルのフラットなモジュール（例：`qq.py`）は意図的にサポートされていません。各チャネルは `core.py` を含むフォルダである必要があり、`__init__.py` は不要です。

## アダプター

### QQ（`plugins/channels/qq/`）

現在バンドルされている唯一のアダプターです。`QQChannel(BaseChannel)`（`name = "qq"`、`display_name = "QQ"`）は Tencent の `botpy` SDK 上に構築されています（`qq-botpy>=1.2.1`、プラグインの `requirements.txt` に記載）:

- WebSocket クライアント：`botpy.Client` のサブクラス、`Intents(public_messages=True, direct_message=True)`
- イベントハンドラ: `on_c2c_message_create`、`on_group_at_message_create`、`on_direct_message_create` — すべて `QQChannel._on_message()` へ転送
- メッセージ ID による重複排除（受信済み ID の `deque(maxlen=1000)`）
- グループメッセージ: `chat_id = group_openid`、送信者は `author.member_openid`。C2C メッセージ: `chat_id` はユーザーの openid。`chat_id` ごとにチャットタイプをキャッシュし、返信 API を選択
- 返信: `post_c2c_message()` / `post_group_message()`。ペイロードは `msg_format` に応じて `msg_type` 2（markdown）または 0（plain）で、受信 `message_id`（`msg.metadata["message_id"]` から取得）と増加する `msg_seq` を参照
- `app_id` または `secret` が未設定の場合、`start()` は接続せずに戻ります。エラー時は 5 秒ごとに自動再接続
- SDK は読み込み/起動時に `requirements.txt` から自動インストールされます。連続 3 回インストールに失敗すると、60 秒間（クールダウン）リトライを停止

**設定 — 2 つのファイル:**

ルートのトグルは `plugins/channels/config.json`（実際の内容）:

```json
{
  "qq": {
    "enabled": true,
    "allow_from": ["*"],
    "secret": "",
    "heartbeat": false,
    "cron": false
  }
}
```

認証情報は `plugins/channels/qq/config.json`（実際の内容）:

```json
{
    "app_id": "",
    "receiver": ""
}
```

チャネル構築時に、`QQChannel._merge_credentials()` がプラグインローカルファイルの `app_id` と `receiver` を設定セクションへマージします。実効的なフィールドは `QQConfig`（pydantic モデル）由来です:

- `enabled`: チャネルの有効/無効（`ChannelManager` が読み取り）
- `app_id`: QQ アプリケーション ID（プラグインローカルファイルから。`secret` とともに `start()` の接続に必須）
- `secret`: QQ アプリケーションシークレット（ルート設定）
- `allow_from`: 許可された送信者 ID。`"*"` はすべて許可。空のリストは `SystemExit` で起動を中断
- `msg_format`: `"plain"`（デフォルト）または `"markdown"`
- `receiver`: プロアクティブ（ハートビート）配信のデフォルト `chat_id`
- `heartbeat`: `true` の場合、ハートビートサービスがエージェントの出力をこのチャネルの `receiver` へ送信（`server/service/heartbeat.py` の `process_heartbeat_notify()` を参照）
- `cron`: デフォルト設定ファイルに存在しますが、channels パッケージは読み取りません

## 使用法

### Channel Manager の使用

```python
from channels import channel_manager

# 特定のチャネルを取得
qq_channel = channel_manager.get_channel("qq")

# 有効なすべてのチャネル名を取得
enabled = channel_manager.enabled_channels

# チャネルの状態を取得
status = channel_manager.get_status()

# メッセージバスを取得
bus = channel_manager.get_bus()

# イベントループを取得
loop = channel_manager.get_event_loop()
```

### 新しいチャネルの実装

`plugins/channels/` の下に、チャネル名にちなんだ新しい**ディレクトリ（フォルダ）**を作成します。例: `plugins/channels/my_channel/`。このフォルダには `BaseChannel` サブクラスを定義（または再エクスポート）する `core.py` を置きます。`__init__.py` は不要です:

```text
plugins/channels/
├── config.json              # チャネル設定
└── my_channel/              # ← チャネルフォルダ、チャネル名にちなんで命名
    └── core.py              #  BaseChannel サブクラスを定義 / 再エクスポート
```

```python
from typing import Any

from channels.base import BaseChannel
from type.bus import OutboundMessage


class MyChannel(BaseChannel):
    name = "my_channel"
    display_name = "My Channel"

    async def start(self) -> None:
        self._running = True
        # チャットプラットフォームに接続。受信メッセージごとに呼び出し:
        # await self._handle_message(sender_id, chat_id, content, ...)

    async def stop(self) -> None:
        self._running = False
        # 切断してクリーンアップ

    async def send(self, msg: OutboundMessage) -> None:
        # プラットフォームを通じて msg.content を msg.chat_id へ配信
        ...

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return {"enabled": False, "allow_from": ["*"]}
```

レジストリは `plugins/channels/` の下のチャネルディレクトリをスキャンし、各フォルダの `core.py` を動的にインポートして（最初に見つかった `BaseChannel` サブクラスを使用）チャネルを自動検出します。フォルダに `requirements.txt` がある場合、その依存関係は自動的にインストールされます。有効化するには `plugins/channels/config.json` に `"enabled": true` のセクションを追加します。

## アーキテクチャ

```
┌──────────────────────────────────────────────────────────┐
│                     ChannelManager                        │
│  - start_service() / stop_service()                      │
│  - _inbound_consume_loop() / _outbound_consume_loop()    │
│  - _dispatch_outbound()                                  │
│  - _validate_allow_from()                                │
└────────────────────────────┬─────────────────────────────┘
                             │ _init_channels() → discover_all()
              ┌──────────────▼───────────────┐
              │     channels/registry.py     │
              │  plugins/channels/*/core.py  │
              │  + entry_points("channels")  │
              └──────────────┬───────────────┘
                             │
                    ┌────────▼────────┐
                    │    QQChannel    │   ← plugins/channels/qq/
                    └────────┬────────┘
                             │
┌────────────────────────────▼─────────────────────────────┐
│                 MessageBus (bus/core.py)                 │
│   inbound:  asyncio.Queue[InboundMessage]                │
│   outbound: asyncio.Queue[OutboundMessage]               │
└────────────────────────────┬─────────────────────────────┘
                             │
             ┌───────────────▼────────────────┐
             │ server/trigger/channels/core.py│
             │  _process_inbound()            │
             │  → server.service.async_generate()
             └────────────────────────────────┘
```

### メッセージフロー

サーバー起動時、`server/__main__.py` が `server.trigger` をインポートし、さらに `server.trigger.channels` がインポートされます。このモジュールは `channel_manager` シングルトンにインバウンド/アウトバウンドコンシューマを登録し、`channel_manager.start_service()` を呼び出すデーモンスレッドを開始します。

**インバウンド（ユーザー → エージェント → 返信）:**

1. プラットフォームイベントが `botpy` WebSocket 経由で到着し、`QQChannel._on_message()` へディスパッチされる
2. 重複排除と解析の後、`_handle_message()` が `allow_from` を確認し、`bus.publish_inbound()` で `InboundMessage` を公開
3. `ChannelManager._inbound_consume_loop()` がこれを消費し、登録済みコンシューマ `_process_inbound()`（`server/trigger/channels/core.py`）を呼び出す：画像 URL は base64 に変換、チャネル名からセッション ID を導出して `relation_register.register_channel_chat()` で登録、エージェントの返信は `server.service.async_generate()` で生成
4. 返信は直接の `channel.send(OutboundMessage(...))` 呼び出しで配信される

**アウトバウンド（バス経路）:** `bus.publish_outbound()` を呼び出したメッセージは `_dispatch_outbound()` が拾い、`msg.channel` で指定されたチャネルへルーティングします（`_outbound_consume_loop()` のコンシューマ `_process_outbound()` はチャネルセッションの登録のみ行います）。

**プロアクティブ（ハートビート）:** チャネルセクションが `"heartbeat": true` で、そのプラグイン設定に `receiver` が定義されている場合、ハートビートサービス（`server/service/heartbeat.py`）が `channel.send()` でエージェントの出力をそのチャットへ配信します。
