# Channels

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

---

This module provides a unified interface for integrating with chat platforms. It handles message receiving, sending, and routing between the EMA AI agent and external messaging platforms. Platform adapters are plugins that live outside this package — the repository currently ships exactly one adapter: **QQ** (`plugins/channels/qq/`).

## Overview

The channels module implements a plugin-based architecture that allows easy addition of new chat platform integrations. Each channel is implemented as a class that extends `BaseChannel` and communicates with the system through the `MessageBus` (`bus/core.py`), which exposes two unbounded `asyncio.Queue`s (inbound and outbound). Channel implementations live as separate folders (directories) under `plugins/channels/` (discovered at runtime by scanning for a `core.py`), not within this package.

## Main Features

- **Unified Interface**: All chat platforms share a common `BaseChannel` interface
- **Plugin System**: Built-in channels (folders under `plugins/channels/` containing a `core.py`) and external plugins registered via `entry_points(group="channels")`
- **Message Routing**: Inbound and outbound `asyncio.Queue`s on the `MessageBus`; outbound messages are routed to the matching channel by `ChannelManager._dispatch_outbound()`
- **Access Control**: Configurable sender whitelist (`allow_from`); an empty list denies all senders, `"*"` allows all
- **Asynchronous**: Built on asyncio for concurrent message handling
- **Safe Startup**: `_validate_allow_from()` aborts with `SystemExit` if a configured channel has an empty `allow_from` (would deny everyone)
- **Dependency Auto-Install**: `_ensure_deps()` installs each plugin's `requirements.txt` (via `uv` when available, `pip` fallback); a failed install does not skip the channel — it is registered anyway and retried lazily in `start()`

## File Structure

```
channels/
├── __init__.py      # Package exports (BaseChannel, channel_manager)
├── base.py          # BaseChannel abstract base class — defines the channel interface
├── manager.py       # ChannelManager — lifecycle coordination and message routing
├── registry.py      # Channel discovery — auto-discovers built-in and plugin channels
├── README.md        # English documentation
├── README.zh.md     # 简体中文文档
├── README.ja.md     # 日本語ドキュメント
└── README.ko.md     # 한국어 문서
```

> **Note:** Channel implementations are **not** stored in this package. They are loaded at runtime from `plugins/channels/` (see `registry.py`). Currently only `plugins/channels/qq/` exists.

## Core Components

### BaseChannel (base.py)

Abstract base class defining the interface for all channel implementations:

- Class attributes: `name: str = "base"`, `display_name: str = "Base"`
- `__init__(self, config: Any, bus: MessageBus)`: stores `self.config` and `self.bus`, sets `self._running = False`
- `async def start(self) -> None`: abstract — start the channel and begin listening for messages (long-running: connect, listen, forward via `_handle_message()`)
- `async def stop(self) -> None`: abstract — stop the channel and clean up resources
- `async def send(self, msg: OutboundMessage) -> None`: abstract — send a message through this channel
- `def is_allowed(self, sender_id: str) -> bool`: checks `config.allow_from` — empty list denies all (logs a warning), `"*"` allows all, otherwise exact string match
- `async def _handle_message(self, sender_id: str, chat_id: str, content: str, media: list[str] | None = None, metadata: dict[str, Any] | None = None, session_id: str | None = None) -> None`: permission check via `is_allowed()`, then builds an `InboundMessage` and publishes it with `bus.publish_inbound()`
- `@classmethod def default_config(cls) -> dict[str, Any]`: returns `{"enabled": False}` by default; plugins override to auto-populate `config.json`
- `@property is_running(self) -> bool`: whether the channel is currently running

### ChannelManager (manager.py)

Coordinates all enabled channels. A module-level singleton `channel_manager` is created at import time:

- `__init__(config=None, bus=None)`: loads `plugins/channels/config.json` when no config is passed (if the file does not exist, initialization returns early — no channels and no bus); creates a `MessageBus()` when no bus is passed; then runs `_init_channels()`
- `_init_channels()`: instantiates every discovered channel (via `channels.registry.discover_all()`) whose config section has `"enabled": true`; instantiation errors are logged and the channel is skipped; then calls `_validate_allow_from()`
- `_validate_allow_from()`: raises `SystemExit` if any channel's `allow_from` equals `[]`
- `start_service()`: schedules the `_dispatch_outbound()` dispatcher, the `_inbound_consume_loop()` / `_outbound_consume_loop()` consumers, and a `_start_channel()` task per channel, then runs the event loop forever (warns and returns when no channels are enabled)
- `async stop_service()`: cancels the dispatcher task, calls `stop()` on every channel, stops the event loop
- `_dispatch_outbound()`: polls `bus.consume_outbound()` with a 1-second `asyncio.wait_for` timeout and forwards each message to `self._channels[msg.channel].send(msg)`
- `set_inbound_consumer(cb)` / `set_outbound_consumer(cb)`: register callbacks of shape `(msg, channel) -> Awaitable[None]`
- `_inbound_consume_loop()` / `_outbound_consume_loop()`: consume from the bus and invoke the registered consumer once per configured channel
- Accessors: `get_channel(name)`, `get_status()` (per-channel `enabled` / `running`), `get_bus()`, `get_event_loop()`, `enabled_channels` property

### Channel Registry (registry.py)

Auto-discovers available channels:

- `discover_channel_names() -> list[str]`: scans `plugins/channels/` for channel **folders** (subdirectories, not single files) that contain a `core.py`; returns sorted names
- `load_channel_class(module_name, strict_deps=True) -> type[BaseChannel]`: dynamically imports `plugins/channels/<module_name>/core.py` and returns the first `BaseChannel` subclass found. With `strict_deps=False`, a failed dependency install does not abort the load — the channel stays registered and `start()` retries the install
- `_ensure_deps(plugin_dir, plugin_name) -> bool`: installs the plugin's `requirements.txt` if present (`uv pip install` preferred, `python -m pip` fallback, 120 s timeout)
- `discover_plugins() -> dict[str, type[BaseChannel]]`: loads external plugins registered via `entry_points(group="channels")`
- `discover_all() -> dict[str, type[BaseChannel]]`: merges built-in (directory scan, loaded with `strict_deps=False`) and external channels — built-ins take priority, an external plugin cannot shadow a built-in name

> Flat single-file modules (e.g. `qq.py`) are intentionally not supported; each channel must be a folder with a `core.py`, and no `__init__.py` is required.

## Adapters

### QQ (`plugins/channels/qq/`)

The only adapter bundled today. `QQChannel(BaseChannel)` (`name = "qq"`, `display_name = "QQ"`) is built on Tencent's `botpy` SDK (`qq-botpy>=1.2.1`, listed in the plugin's `requirements.txt`):

- WebSocket client: a `botpy.Client` subclass with `Intents(public_messages=True, direct_message=True)`
- Event handlers: `on_c2c_message_create`, `on_group_at_message_create`, `on_direct_message_create` — all forward to `QQChannel._on_message()`
- Incoming messages are deduplicated by message ID (a `deque(maxlen=1000)` of seen IDs)
- Group messages: `chat_id = group_openid`, sender = `author.member_openid`; C2C messages: `chat_id` = the user's openid; the chat type is cached per `chat_id` to choose the reply API
- Replies: `post_c2c_message()` / `post_group_message()`; payload is `msg_type` 2 (markdown) or 0 (plain) according to `msg_format`, and references the inbound `message_id` (taken from `msg.metadata["message_id"]`) with an incrementing `msg_seq`
- `start()` returns without connecting if `app_id` or `secret` is missing; the connection auto-reconnects every 5 seconds on error
- The SDK is auto-installed from `requirements.txt` at load/start time; after 3 consecutive install failures, retries are suppressed for 60 seconds (cooldown)

**Configuration — two files:**

Root toggles in `plugins/channels/config.json` (actual content):

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

Credentials in `plugins/channels/qq/config.json` (actual content):

```json
{
    "app_id": "",
    "receiver": ""
}
```

`QQChannel._merge_credentials()` merges `app_id` and `receiver` from the plugin-local file into the config section when the channel is constructed. The effective fields come from `QQConfig` (a pydantic model):

- `enabled`: enable/disable the channel (read by `ChannelManager`)
- `app_id`: QQ application ID (from the plugin-local file; required, together with `secret`, for `start()` to connect)
- `secret`: QQ application secret (root config)
- `allow_from`: allowed sender IDs; `"*"` allows all; an empty list aborts startup with `SystemExit`
- `msg_format`: `"plain"` (default) or `"markdown"`
- `receiver`: default `chat_id` for proactive (heartbeat) delivery
- `heartbeat`: when `true`, the heartbeat service sends agent output to the channel's `receiver` (see `process_heartbeat_notify()` in `server/service/heartbeat.py`)
- `cron`: present in the default config file, but not read by the channels package

## Usage

### Using Channel Manager

```python
from channels import channel_manager

# Get a specific channel
qq_channel = channel_manager.get_channel("qq")

# Get all enabled channel names
enabled = channel_manager.enabled_channels

# Get channel status
status = channel_manager.get_status()

# Get the message bus
bus = channel_manager.get_bus()

# Get the event loop
loop = channel_manager.get_event_loop()
```

### Implementing a New Channel

Create a new **folder** (directory) under `plugins/channels/` named after the channel, e.g. `plugins/channels/my_channel/`. The folder contains a `core.py` that defines (or re-exports) the `BaseChannel` subclass; no `__init__.py` is required:

```text
plugins/channels/
├── config.json              # Channel configuration
└── my_channel/              # ← Channel folder, named after the channel
    └── core.py              #  Defines / re-exports the BaseChannel subclass
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
        # Connect to the chat platform; for each inbound message call:
        # await self._handle_message(sender_id, chat_id, content, ...)

    async def stop(self) -> None:
        self._running = False
        # Disconnect and clean up

    async def send(self, msg: OutboundMessage) -> None:
        # Deliver msg.content to msg.chat_id through the platform
        ...

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return {"enabled": False, "allow_from": ["*"]}
```

The registry discovers the channel by scanning the channel folders under `plugins/channels/` and dynamically importing each folder's `core.py` (the first `BaseChannel` subclass found is used). If the folder has a `requirements.txt`, its dependencies are installed automatically. Add a section with `"enabled": true` to `plugins/channels/config.json` to activate it.

## Architecture

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

### Message Flow

At server boot, `server/__main__.py` imports `server.trigger`, which imports `server.trigger.channels`. That module registers the inbound/outbound consumers on the `channel_manager` singleton and starts a daemon thread that calls `channel_manager.start_service()`.

**Inbound (user → agent → reply):**

1. A platform event arrives over the `botpy` WebSocket and is dispatched to `QQChannel._on_message()`
2. After dedup and parsing, `_handle_message()` checks `allow_from` and publishes an `InboundMessage` via `bus.publish_inbound()`
3. `ChannelManager._inbound_consume_loop()` consumes it and invokes the registered consumer `_process_inbound()` (`server/trigger/channels/core.py`): image URLs are converted to base64, a session ID is derived from the channel name and registered via `relation_register.register_channel_chat()`, and the agent reply is generated through `server.service.async_generate()`
4. The reply is delivered with a direct `channel.send(OutboundMessage(...))` call

**Outbound (bus path):** anything that calls `bus.publish_outbound()` is picked up by `_dispatch_outbound()` and routed to the channel named in `msg.channel` (the `_outbound_consume_loop()` consumer `_process_outbound()` only registers the channel session).

**Proactive (heartbeat):** when a channel section has `"heartbeat": true` and its plugin config defines a `receiver`, the heartbeat service (`server/service/heartbeat.py`) delivers agent output to that chat via `channel.send()`.
