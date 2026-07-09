# Channels

[English](./README.md) | 中文版

---

本模块提供与各种聊天平台（如 Telegram、QQ、WhatsApp 等）集成的统一接口。它负责消息接收、发送以及 EMA AI 代理与不同消息平台之间的路由。

## 概述

Channels 模块采用插件化架构，可以轻松添加新的聊天平台集成。每个通道都实现为继承自 `BaseChannel` 的类，通过消息总线与系统进行通信。通道实现以独立 Python 文件形式存放在 `plugins/channels/` 目录下（运行时通过 pkgutil 自动发现），而非本包内。

## 主要功能

- **统一接口**：所有聊天平台共享通用的 `BaseChannel` 接口
- **插件系统**：支持内置通道（`plugins/channels/` 下，通过 pkgutil 发现）和通过 entry points 注册的外部插件
- **消息路由**：自动路由入站和出站消息
- **访问控制**：可配置的发件人白名单 (`allow_from`)；空列表拒绝所有
- **异步处理**：基于 asyncio 构建，支持并发消息处理
- **安全启动**：`_validate_allow_from()` 防止误配置导致所有用户被拒绝

## 文件结构

```
channels/
├── __init__.py      # 包导出 (BaseChannel, channel_manager)
├── base.py          # BaseChannel 抽象类 - 定义通道接口
├── manager.py       # ChannelManager - 协调所有通道和消息路由
├── registry.py      # 通道注册发现 - 自动发现内置和插件通道
└── README.md        # English documentation
```

> **注意**：通道实现（如 QQ、Telegram、WhatsApp）**不**存储在本包中。它们在运行时从 `plugins/channels/` 加载（见 registry.py）。

## 核心组件

### BaseChannel (base.py)

定义所有通道实现接口的抽象基类：

- `start()`：开始监听消息（抽象方法）
- `stop()`：停止通道并清理资源（抽象方法）
- `send(msg)`：通过通道发送消息（抽象方法）
- `is_allowed(sender_id)`：检查发件人是否被允许 — 空 `allow_from` 拒绝所有，`"*"` 允许所有
- `_handle_message(sender_id, chat_id, content, ...)`：检查权限并将 `InboundMessage` 发布到消息总线
- `default_config()`：类方法 — 返回默认配置字典供引导使用（插件中可覆写）
- `is_running`：属性 — 检查通道是否正在运行

### ChannelManager (manager.py)

协调所有已启用的通道：

- 从配置初始化通道（`plugins/channels/config.json`）
- 管理通道生命周期（`start_service()` / `stop_service()`）
- 通过 `_dispatch_outbound()` 将出站消息路由到相应的通道
- 运行入站/出站消费者循环（`_inbound_consume_loop`、`_outbound_consume_loop`）
- 提供所有通道的状态信息
- 启动时验证 `allow_from` 以防止误配置
- 创建模块级单例：`channel_manager`

### Channel Registry (registry.py)

自动发现可用的通道：

- `discover_channel_names()`：使用 pkgutil 扫描 `plugins/channels/` 下的 `.py` 模块
- `load_channel_class(module_name)`：动态导入通道模块并查找第一个 `BaseChannel` 子类
- `discover_plugins()`：通过 `entry_points(group="channels")` 加载外部插件
- `discover_all()`：合并内置和外部通道（内置优先 — 外部不能覆盖内置名称）

## 使用方法

### 配置

通道在 `plugins/channels/config.json` 中配置：

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
- `allow_from`：允许的发件人 ID 列表（`"*"` 允许所有人；空列表会导致启动错误）
- `msg_format`：消息格式（"plain" 或 "markdown"）

### 使用 Channel Manager

```python
from channels import channel_manager

# 获取特定通道
qq_channel = channel_manager.get_channel("qq")

# 获取所有已启用的通道名称
enabled = channel_manager.enabled_channels

# 获取通道状态
status = channel_manager.get_status()

# 获取消息总线
bus = channel_manager.get_bus()

# 获取事件循环
loop = channel_manager.get_event_loop()
```

### 实现新通道

在 `plugins/channels/` 下创建新的 `.py` 文件：

```python
from channels.base import BaseChannel
from bus import MessageBus
from type.bus import OutboundMessage
from typing import Any


class MyChannel(BaseChannel):
    name = "my_channel"
    display_name = "我的通道"

    async def start(self) -> None:
        self._running = True
        # 连接到聊天平台

    async def stop(self) -> None:
        self._running = False
        # 断开连接并清理

    async def send(self, msg: OutboundMessage) -> None:
        # 通过平台发送消息
        pass

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return {"enabled": False, "allow_from": ["*"]}
```

注册中心会自动发现并加载该通道。

## 架构

```
┌─────────────────────────────────────────────────────────┐
│                    ChannelManager                        │
│  - 管理通道生命周期                                       │
│  - 通过 dispatch 循环路由消息                              │
│  - 协调入站/出站消费者                                    │
│  - 启动时验证 allow_from                                 │
└─────────────────────────────────────────────────────────┘
                               │
         ┌─────────────────────┼─────────────────────┐
         ▼                     ▼                     ▼
┌───────────────┐   ┌───────────────┐   ┌───────────────┐
│  QQ 通道      │   │  Telegram     │   │  WhatsApp     │
│  (插件)       │   │  通道 (插件)   │   │  通道 (插件)   │
│  plugins/     │   │               │   │               │
│  channels/    │   │               │   │               │
│  qq.py        │   │               │   │               │
└───────────────┘   └───────────────┘   └───────────────┘
         │                     │                     │
         └─────────────────────┼─────────────────────┘
                               ▼
                     ┌─────────────────┐
                     │   Message Bus   │
                     │  (bus 模块)     │
                     └─────────────────┘
```

消息流向：
- 入站：聊天平台 → 通道 → 消息总线 → AI 代理
- 出站：AI 代理 → 消息总线 → 通道 → 聊天平台
