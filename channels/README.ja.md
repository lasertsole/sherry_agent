# Channels

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

---

このモジュールは、さまざまなチャットプラットフォーム（Telegram、QQ、WhatsApp など）と統合するための統一インターフェースを提供します。EMA AI エージェントとさまざまなメッセージングプラットフォームの間でのメッセージの受信、送信、ルーティングを処理します。

## 概要

channels モジュールはプラグインベースのアーキテクチャを実装しており、新しいチャットプラットフォーム統合を簡単に追加できます。各チャネルは `BaseChannel` を拡張するクラスとして実装され、メッセージバスを介してシステムと通信します。チャネルの実装はこのパッケージ内ではなく、`plugins/channels/` の下の個別のディレクトリとして存在します（`core.py` を含むフォルダをスキャンしてランタイム時に検出されます）。

## 主な機能

- **統一インターフェース**: すべてのチャットプラットフォームが共通の `BaseChannel` インターフェースを共有します
- **プラグインシステム**: 組み込みチャネル（`plugins/channels/` の下、`core.py` を含むフォルダをスキャンして検出）と entry points による外部プラグインの両方をサポートします
- **メッセージルーティング**: インバウンドおよびアウトバウンドメッセージのルーティングを自動処理します
- **アクセス制御**: 許可された送信者のための設定可能なホワイトリスト（`allow_from`）。空のリストはすべてを拒否します
- **非同期処理**: asyncio 上に構築され、同時メッセージ処理をサポートします
- **安全な起動**: `_validate_allow_from()` が、すべての送信者を拒否してしまう誤設定チャネルを防止します

## ファイル構造

```
channels/
├── __init__.py      # パッケージエクスポート (BaseChannel, channel_manager)
├── base.py          # BaseChannel 抽象クラス - チャネルインターフェースを定義
├── manager.py       # ChannelManager - すべてのチャネルとメッセージルーティングを調整
├── registry.py      # チャネル検出 - 組み込み・プラグインチャネルを自動検出
└── README.zh.md     # 中文文档
```

> **注**: チャネル実装（例：QQ、Telegram、WhatsApp）はこのパッケージには**保存されません**。ランタイム時に `plugins/channels/` からロードされます（registry.py 参照）。

## コアコンポーネント

### BaseChannel (base.py)

すべてのチャネル実装のインターフェースを定義する抽象基底クラス:

- `start()`: メッセージのリッスンを開始（抽象メソッド）
- `stop()`: チャネルを停止しリソースをクリーンアップ（抽象メソッド）
- `send(msg)`: チャネルを通じてメッセージを送信（抽象メソッド）
- `is_allowed(sender_id)`: 送信者が許可されているかを確認 — 空の `allow_from` はすべて拒否、`"*"` はすべて許可
- `_handle_message(sender_id, chat_id, content, ...)`: 権限を確認し `InboundMessage` をバスに公開
- `default_config()`: クラスメソッド — オンボーディング用のデフォルト設定 dict を返す（プラグインでオーバーライド）
- `is_running`: プロパティ — チャネルが現在実行中かを確認

### ChannelManager (manager.py)

有効なすべてのチャネルを調整します:

- 設定からチャネルを初期化（`plugins/channels/config.json`）
- チャネルのライフサイクルを管理（`start_service()` / `stop_service()`）
- `_dispatch_outbound()` を介してアウトバウンドメッセージを適切なチャネルにルーティング
- インバウンド/アウトバウンドのコンシューマーループを実行（`_inbound_consume_loop`, `_outbound_consume_loop`）
- すべてのチャネルの状態情報を提供
- 起動時に `allow_from` を検証し、誤設定チャネルを防止
- モジュールレベルのシングルトンを作成: `channel_manager`

### Channel Registry (registry.py)

利用可能なチャネルを自動検出します:

- `discover_channel_names()`: pkgutil を使わずに `plugins/channels/` のチャネル**ディレクトリ**（サブディレクトリ、単一ファイルではありません）をスキャンし、`core.py` を含むフォルダのみ認識
- `load_channel_class(module_name)`: チャネルディレクトリの `core.py` を動的にインポートし、最初の `BaseChannel` サブクラスを見つける
- `discover_plugins()`: `entry_points(group="channels")` で登録された外部プラグインをロード
- `discover_all()`: 組み込みチャネルと外部チャネルをマージ（組み込みが優先 — 外部は組み込み名を上書きできません）

## 使用法

### 設定

チャネルは `plugins/channels/config.json` で設定されます:

```json
{
  "qq": {
    "enabled": true,
    "app_id": "your_app_id",
    "secret": "your_secret",
    "allow_from": ["*"],
    "msg_format": "plain"
  }
}
```

設定オプション:
- `enabled`: チャネルの有効/無効
- `app_id`: チャットプラットフォームのアプリケーション ID
- `secret`: アプリケーション秘密鍵
- `allow_from`: 許可された送信者 ID のリスト（`"*"` はすべて許可、空のリストは起動エラーを発生）
- `msg_format`: メッセージ形式（"plain" または "markdown"）

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

`plugins/channels/` の下に、チャネル名にちなんだ新しい**ディレクトリ（フォルダ）**を作成します。
例: `plugins/channels/my_channel/`。このフォルダには `core.py` のみが含まれます。
実際の `BaseChannel` サブクラスはここに定義するか、ここから再エクスポート
する必要があります。`__init__.py` は不要です:

```text
plugins/channels/
├── config.json              # チャネル設定
└── my_channel/              # ← チャネルフォルダ、チャネル名にちなんで命名
    └── core.py              #  BaseChannel サブクラスを定義 / 再エクスポート
```

```python
from channels.base import BaseChannel
from bus import MessageBus
from type.bus import OutboundMessage
from typing import Any


class MyChannel(BaseChannel):
    name = "my_channel"
    display_name = "My Channel"

    async def start(self) -> None:
        self._running = True
        # チャットプラットフォームに接続

    async def stop(self) -> None:
        self._running = False
        # 切断してクリーンアップ

    async def send(self, msg: OutboundMessage) -> None:
        # プラットフォームを通じてメッセージを送信
        pass

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return {"enabled": False, "allow_from": ["*"]}
```

レジストリは `plugins/channels/` の下のチャネルディレクトリをスキャンしてチャネルを自動的に検出し、各フォルダの `core.py` を動的にインポートしてロードします。

## アーキテクチャ

```
┌─────────────────────────────────────────────────────────┐
│                    ChannelManager                        │
│  - チャネルのライフサイクルを管理                         │
│  - dispatch ループでメッセージをルーティング              │
│  - インバウンド/アウトバウンドのコンシューマー調整         │
│  - 起動時に allow_from を検証                            │
└─────────────────────────────────────────────────────────┘
                               │
         ┌─────────────────────┼─────────────────────┐
         ▼                     ▼                     ▼
┌───────────────┐   ┌───────────────┐   ┌───────────────┐
│  QQ チャネル  │   │  Telegram     │   │  WhatsApp     │
│  (プラグイン) │   │  チャネル     │   │  チャネル     │
│  plugins/     │   │  (プラグイン) │   │  (プラグイン) │
│  channels/    │   │               │   │               │
│  qq/          │   │               │   │               │
└───────────────┘   └───────────────┘   └───────────────┘
         │                     │                     │
         └─────────────────────┼─────────────────────┘
                               ▼
                     ┌─────────────────┐
                     │   Message Bus   │
                     │  (bus モジュール) │
                     └─────────────────┘
```

メッセージフロー:
- インバウンド: チャットプラットフォーム → チャネル → メッセージバス → AI エージェント
- アウトバウンド: AI エージェント → メッセージバス → チャネル → チャットプラットフォーム
