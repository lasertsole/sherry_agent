# Channels

[English](./README.md) | 中文版

---

本模块提供与各种聊天平台（如 Telegram、QQ、WhatsApp 等）集成的统一接口。它负责消息接收、发送以及 EMA AI 代理与不同消息平台之间的路由。

## 概述

Channels 模块采用插件化架构，可以轻松添加新的聊天平台集成。每个通道都实现为继承自 `BaseChannel` 的类，通过消息总线与系统进行通信。

## 主要功能

- **统一接口**：所有聊天平台共享通用的 `BaseChannel` 接口
- **插件系统**：支持内置通道和通过 entry points 注册的外部插件
- **消息路由**：自动路由入站和出站消息
- **访问控制**：可配置的发件人白名单 (`allow_from`)
- **异步处理**：基于 asyncio 构建，支持并发消息处理
- **自动重连**：通道可以在连接失败时自动重连

## 文件结构

```
channels/
├── __init__.py      # 包导出 (BaseChannel, channel_manager)
├── base.py          # BaseChannel 抽象类 - 定义通道接口
├── manager.py       # ChannelManager - 协调所有通道和消息路由
├── registry.py      # 通道注册发现 - 自动发现内置和插件通道
├── qq.py            # 使用 botpy SDK 的 QQ 通道实现
└── README.md        # English documentation
```

## 核心组件

### BaseChannel (base.py)

定义所有通道实现接口的抽象基类：

- `start()`：开始监听消息
- `stop()`：停止通道并清理资源
- `send(msg)`：通过通道发送消息
- `is_allowed(sender_id)`：检查发件人是否被允许
- `_handle_message()`：处理传入消息并发布到消息总线

### ChannelManager (manager.py)

协调所有已启用的通道：

- 从配置初始化通道
- 管理通道生命周期（启动/停止）
- 将出站消息路由到相应的通道
- 提供所有通道的状态信息

### Channel Registry (registry.py)

自动发现可用的通道：

- 使用 pkgutil 扫描内置通道模块
- 通过 entry points 加载外部插件
- 合并内置和外部通道

## 使用方法

### 配置

通道在 `channels.json` 中配置：

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

配置选项：
- `enabled`：启用/禁用通道
- `app_id`：聊天平台的应用 ID
- `secret`：应用密钥
- `allow_from`：允许的发件人 ID 列表（`"*"` 允许所有人）
- `msg_format`：消息格式（"plain" 或 "markdown"）

### 使用 Channel Manager

```python
from channels import channel_manager

# 获取特定通道
qq_channel = channel_manager.get_channel("qq")

# 获取所有已启用的通道
enabled = channel_manager.enabled_channels

# 获取通道状态
status = channel_manager.get_status()

# 获取消息总线
bus = channel_manager.get_bus()
```

### 实现新通道

```python
from channels.base import BaseChannel
from bus.queue import MessageBus
from bus.events import OutboundMessage
from typing import Any

class MyChannel(BaseChannel):
    name = "my_channel"
    display_name = "我的通道"

    async def start(self) -> None:
        # 连接到聊天平台
        pass

    async def stop(self) -> None:
        # 断开连接并清理
        pass

    async def send(self, msg: OutboundMessage) -> None:
        # 通过平台发送消息
        pass
```

## 内置通道

### QQ 通道 (qq.py)

使用 botpy SDK 实现的 QQ 机器人。支持：
- C2C（私聊）消息
- 群消息
- 文本和 markdown 消息格式
- 图片附件
- 自动重连

## 架构

```
┌─────────────────────────────────────────────────────────┐
│                    ChannelManager                        │
│  - 管理通道生命周期                                       │
│  - 路由消息                                              │
│  - 协调入站/出站消费者                                    │
└─────────────────────────────────────────────────────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
┌───────────────┐   ┌───────────────┐   ┌───────────────┐
│  QQ 通道      │   │  Telegram     │   │  WhatsApp     │
│   (qq.py)     │   │   通道         │   │   通道        │
└───────────────┘   └───────────────┘   └───────────────┘
        │                     │                     │
        └─────────────────────┼─────────────────────┘
                              ▼
                    ┌─────────────────┐
                    │   Message Bus   │
                    │  (bus.queue)    │
                    └─────────────────┘
```

消息流向：
- 入站：聊天平台 → 通道 → 消息总线 → AI 代理
- 出站：AI 代理 → 消息总线 → 通道 → 聊天平台