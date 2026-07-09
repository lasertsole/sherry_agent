# Channels

[中文版](./README.zh.md) | English

---

This module provides a unified interface for integrating with various chat platforms (Telegram, QQ, WhatsApp, etc.). It handles message receiving, sending, and routing between the EMA AI agent and different messaging platforms.

## Overview

The channels module implements a plugin-based architecture that allows easy addition of new chat platform integrations. Each channel is implemented as a class that extends `BaseChannel` and communicates with the system through a message bus. Channel implementations live as separate Python files under `plugins/channels/` (discovered at runtime via pkgutil), not within this package.

## Main Features

- **Unified Interface**: All chat platforms share a common `BaseChannel` interface
- **Plugin System**: Support for both built-in channels (under `plugins/channels/`, discovered via pkgutil) and external plugins via entry points
- **Message Routing**: Automatic routing of inbound and outbound messages
- **Access Control**: Configurable whitelist for permitted senders (`allow_from`); empty list denies all
- **Asynchronous**: Built on asyncio for concurrent message handling
- **Safe Startup**: `_validate_allow_from()` guards against misconfigured channels that would deny all senders

## File Structure

```
channels/
├── __init__.py      # Package exports (BaseChannel, channel_manager)
├── base.py          # BaseChannel abstract class — defines channel interface
├── manager.py       # ChannelManager — coordinates all channels and message routing
├── registry.py      # Channel discovery — auto-discovers built-in and plugin channels
└── README.zh.md     # 中文文档
```

> **Note:** Channel implementations (e.g., QQ, Telegram, WhatsApp) are **not** stored in this package. They are loaded at runtime from `plugins/channels/` (see registry.py).

## Core Components

### BaseChannel (base.py)

Abstract base class that defines the interface for all channel implementations:

- `start()`: Start listening for messages (abstract)
- `stop()`: Stop the channel and clean up resources (abstract)
- `send(msg)`: Send a message through the channel (abstract)
- `is_allowed(sender_id)`: Check if a sender is permitted — empty `allow_from` denies all, `"*"` allows all
- `_handle_message(sender_id, chat_id, content, ...)`: Check permissions and publish `InboundMessage` to the bus
- `default_config()`: Class method — return default config dict for onboarding (override in plugins)
- `is_running`: Property — check if the channel is currently running

### ChannelManager (manager.py)

Coordinates all enabled channels:

- Initializes channels from configuration (`plugins/channels/config.json`)
- Manages channel lifecycle (`start_service()` / `stop_service()`)
- Routes outbound messages to appropriate channels via `_dispatch_outbound()`
- Runs inbound/outbound consumer loops (`_inbound_consume_loop`, `_outbound_consume_loop`)
- Provides status information for all channels
- Validates `allow_from` on startup to prevent misconfigured channels
- Creates a module-level singleton: `channel_manager`

### Channel Registry (registry.py)

Auto-discovers available channels:

- `discover_channel_names()`: Scans `plugins/channels/` using pkgutil for `.py` modules
- `load_channel_class(module_name)`: Dynamically imports a channel module and finds the first `BaseChannel` subclass
- `discover_plugins()`: Loads external plugins registered via `entry_points(group="channels")`
- `discover_all()`: Merges built-in and external channels (built-in takes priority — external cannot shadow built-in names)

## Usage

### Configuration

Channels are configured in `plugins/channels/config.json`:

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

Configuration options:
- `enabled`: Enable/disable the channel
- `app_id`: Application ID for the chat platform
- `secret`: Application secret
- `allow_from`: List of allowed sender IDs (`"*"` allows all; empty list causes a startup error)
- `msg_format`: Message format ("plain" or "markdown")

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

Create a new `.py` file under `plugins/channels/`:

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
        # Connect to the chat platform

    async def stop(self) -> None:
        self._running = False
        # Disconnect and clean up

    async def send(self, msg: OutboundMessage) -> None:
        # Send message through the platform
        pass

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return {"enabled": False, "allow_from": ["*"]}
```

The registry will automatically discover and load the channel by scanning `plugins/channels/`.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    ChannelManager                        │
│  - Manages channel lifecycle                            │
│  - Routes messages via dispatch loop                    │
│  - Coordinates inbound/outbound consumers               │
│  - Validates allow_from on startup                      │
└─────────────────────────────────────────────────────────┘
                               │
         ┌─────────────────────┼─────────────────────┐
         ▼                     ▼                     ▼
┌───────────────┐   ┌───────────────┐   ┌───────────────┐
│  QQ Channel   │   │  Telegram     │   │  WhatsApp     │
│  (plugin)     │   │  Channel      │   │  Channel      │
│  plugins/     │   │  (plugin)     │   │  (plugin)     │
│  channels/    │   │               │   │               │
│  qq.py        │   │               │   │               │
└───────────────┘   └───────────────┘   └───────────────┘
         │                     │                     │
         └─────────────────────┼─────────────────────┘
                               ▼
                     ┌─────────────────┐
                     │   Message Bus   │
                     │  (bus module)   │
                     └─────────────────┘
```

Messages flow from chat platforms → Channel → Message Bus → AI Agent (inbound)
Messages flow from AI Agent → Message Bus → Channel → Chat Platforms (outbound)
