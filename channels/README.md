# Channels

[中文版](./README.zh.md) | English

---

This module provides a unified interface for integrating with various chat platforms (Telegram, QQ, WhatsApp, etc.). It handles message receiving, sending, and routing between the EMA AI agent and different messaging platforms.

## Overview

The channels module implements a plugin-based architecture that allows easy addition of new chat platform integrations. Each channel is implemented as a class that extends `BaseChannel` and communicates with the system through a message bus.

## Main Features

- **Unified Interface**: All chat platforms share a common `BaseChannel` interface
- **Plugin System**: Support for both built-in channels and external plugins via entry points
- **Message Routing**: Automatic routing of inbound and outbound messages
- **Access Control**: Configurable whitelist for permitted senders (`allow_from`)
- **Asynchronous**: Built on asyncio for concurrent message handling
- **Auto-recovery**: Channels can automatically reconnect on connection failures

## File Structure

```
channels/
├── __init__.py      # Package exports (BaseChannel, channel_manager)
├── base.py          # BaseChannel abstract class - defines channel interface
├── manager.py       # ChannelManager - coordinates all channels and message routing
├── registry.py      # Channel discovery - auto-discovers built-in and plugin channels
├── qq.py            # QQ channel implementation using botpy SDK
└── README.zh.md     # 中文文档
```

## Core Components

### BaseChannel (base.py)

Abstract base class that defines the interface for all channel implementations:

- `start()`: Start listening for messages
- `stop()`: Stop the channel and clean up resources
- `send(msg)`: Send a message through the channel
- `is_allowed(sender_id)`: Check if a sender is permitted
- `_handle_message()`: Process incoming messages and publish to bus

### ChannelManager (manager.py)

Coordinates all enabled channels:

- Initializes channels from configuration
- Manages channel lifecycle (start/stop)
- Routes outbound messages to appropriate channels
- Provides status information for all channels

### Channel Registry (registry.py)

Auto-discovers available channels:

- Scans built-in channel modules using pkgutil
- Loads external plugins via entry points
- Merges built-in and external channels

## Usage

### Configuration

Channels are configured in `channels.json`:

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
- `allow_from`: List of allowed sender IDs (`"*"` allows all)
- `msg_format`: Message format ("plain" or "markdown")

### Using Channel Manager

```python
from channels import channel_manager

# Get a specific channel
qq_channel = channel_manager.get_channel("qq")

# Get all enabled channels
enabled = channel_manager.enabled_channels

# Get channel status
status = channel_manager.get_status()

# Get the message bus
bus = channel_manager.get_bus()
```

### Implementing a New Channel

```python
from channels.base import BaseChannel
from bus.core import MessageBus
from type.bus import OutboundMessage
from typing import Any


class MyChannel(BaseChannel):
    name = "my_channel"
    display_name = "My Channel"

    async def start(self) -> None:
        # Connect to the chat platform
        pass

    async def stop(self) -> None:
        # Disconnect and clean up
        pass

    async def send(self, msg: OutboundMessage) -> None:
        # Send message through the platform
        pass
```

## Built-in Channels

### QQ Channel (qq.py)

Implements QQ bot using the botpy SDK. Supports:
- C2C (private) messages
- Group messages
- Text and markdown message formats
- Image attachments
- Automatic reconnection

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    ChannelManager                        │
│  - Manages channel lifecycle                            │
│  - Routes messages                                       │
│  - Coordinates inbound/outbound consumers               │
└─────────────────────────────────────────────────────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
┌───────────────┐   ┌───────────────┐   ┌───────────────┐
│  QQ Channel   │   │  Telegram     │   │  WhatsApp     │
│   (qq.py)     │   │   Channel     │   │   Channel     │
└───────────────┘   └───────────────┘   └───────────────┘
        │                     │                     │
        └─────────────────────┼─────────────────────┘
                              ▼
                    ┌─────────────────┐
                    │   Message Bus   │
                    │  (bus.queue)    │
                    └─────────────────┘
```

Messages flow from chat platforms → Channel → Message Bus → AI Agent (inbound)
Messages flow from AI Agent → Message Bus → Channel → Chat Platforms (outbound)