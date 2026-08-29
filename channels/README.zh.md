# Channels

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

---

本模块提供与聊天平台集成的统一接口，负责 EMA AI 代理与外部消息平台之间的消息接收、发送和路由。平台适配器以插件形式存在于本包之外——仓库当前只内置一个适配器：**QQ**（`plugins/channels/qq/`）。

## 概述

Channels 模块采用插件化架构，可以轻松添加新的聊天平台集成。每个通道都实现为继承自 `BaseChannel` 的类，通过 `MessageBus`（`bus/core.py`）与系统通信——它提供两条无界的 `asyncio.Queue`（入站与出站）。通道实现以独立目录（文件夹）形式存放在 `plugins/channels/` 目录下（运行时扫描含 `core.py` 的目录自动发现），而非本包内。

## 主要功能

- **统一接口**：所有聊天平台共享通用的 `BaseChannel` 接口
- **插件系统**：内置通道（`plugins/channels/` 下含 `core.py` 的文件夹）和通过 `entry_points(group="channels")` 注册的外部插件
- **消息路由**：`MessageBus` 上的入站/出站 `asyncio.Queue`；出站消息由 `ChannelManager._dispatch_outbound()` 路由到匹配的通道
- **访问控制**：可配置的发件人白名单 (`allow_from`)；空列表拒绝所有发件人，`"*"` 允许所有
- **异步处理**：基于 asyncio 构建，支持并发消息处理
- **安全启动**：`_validate_allow_from()` 在某个已配置通道的 `allow_from` 为空列表时（将拒绝所有人）以 `SystemExit` 中止启动
- **依赖自动安装**：`_ensure_deps()` 安装各插件的 `requirements.txt`（优先使用 `uv`，回退到 `pip`）；安装失败不会跳过该通道——通道仍会注册，并在 `start()` 中延迟重试

## 文件结构

```
channels/
├── __init__.py      # 包导出 (BaseChannel, channel_manager)
├── base.py          # BaseChannel 抽象基类 - 定义通道接口
├── manager.py       # ChannelManager - 生命周期协调与消息路由
├── registry.py      # 通道发现 - 自动发现内置和插件通道
├── README.md        # English documentation
├── README.zh.md     # 简体中文文档
├── README.ja.md     # 日本語ドキュメント
└── README.ko.md     # 한국어 문서
```

> **注意**：通道实现**不**存储在本包中，而是在运行时从 `plugins/channels/` 加载（见 `registry.py`）。当前只有 `plugins/channels/qq/` 一个通道。

## 核心组件

### BaseChannel (base.py)

定义所有通道实现接口的抽象基类：

- 类属性：`name: str = "base"`、`display_name: str = "Base"`
- `__init__(self, config: Any, bus: MessageBus)`：保存 `self.config` 和 `self.bus`，并设置 `self._running = False`
- `async def start(self) -> None`：抽象方法——启动通道并开始监听消息（长时间运行：连接、监听、经 `_handle_message()` 转发）
- `async def stop(self) -> None`：抽象方法——停止通道并清理资源
- `async def send(self, msg: OutboundMessage) -> None`：抽象方法——通过本通道发送消息
- `def is_allowed(self, sender_id: str) -> bool`：检查 `config.allow_from`——空列表拒绝所有（记录警告日志），`"*"` 允许所有，否则精确字符串匹配
- `async def _handle_message(self, sender_id: str, chat_id: str, content: str, media: list[str] | None = None, metadata: dict[str, Any] | None = None, session_id: str | None = None) -> None`：通过 `is_allowed()` 做权限检查，然后构造 `InboundMessage` 并以 `bus.publish_inbound()` 发布
- `@classmethod def default_config(cls) -> dict[str, Any]`：默认返回 `{"enabled": False}`；插件可覆写以自动填充 `config.json`
- `@property is_running(self) -> bool`：通道当前是否正在运行

### ChannelManager (manager.py)

协调所有已启用的通道。模块级单例 `channel_manager` 在导入时创建：

- `__init__(config=None, bus=None)`：未传入配置时加载 `plugins/channels/config.json`（若文件不存在则初始化提前返回——没有通道也没有总线）；未传入总线时创建 `MessageBus()`；随后执行 `_init_channels()`
- `_init_channels()`：实例化所有已发现且配置段中 `"enabled": true` 的通道（经 `channels.registry.discover_all()`）；实例化异常会被记录并跳过该通道；最后调用 `_validate_allow_from()`
- `_validate_allow_from()`：任何通道的 `allow_from` 等于 `[]` 时抛出 `SystemExit`
- `start_service()`：调度 `_dispatch_outbound()` 分发器、`_inbound_consume_loop()` / `_outbound_consume_loop()` 消费者以及每个通道的 `_start_channel()` 任务，然后永久运行事件循环（未启用任何通道时警告并返回）
- `async stop_service()`：取消分发器任务，对每个通道调用 `stop()`，并停止事件循环
- `_dispatch_outbound()`：以 1 秒的 `asyncio.wait_for` 超时轮询 `bus.consume_outbound()`，并将每条消息转发给 `self._channels[msg.channel].send(msg)`
- `set_inbound_consumer(cb)` / `set_outbound_consumer(cb)`：注册形如 `(msg, channel) -> Awaitable[None]` 的回调
- `_inbound_consume_loop()` / `_outbound_consume_loop()`：从总线消费消息，并对每个已配置的通道调用一次已注册的消费者
- 访问器：`get_channel(name)`、`get_status()`（每个通道的 `enabled` / `running`）、`get_bus()`、`get_event_loop()`、`enabled_channels` 属性

### Channel Registry (registry.py)

自动发现可用的通道：

- `discover_channel_names() -> list[str]`：扫描 `plugins/channels/` 下的通道**目录**（子目录，而非单文件），仅识别含 `core.py` 的文件夹；返回排序后的名称
- `load_channel_class(module_name, strict_deps=True) -> type[BaseChannel]`：动态导入 `plugins/channels/<module_name>/core.py` 并返回找到的第一个 `BaseChannel` 子类。`strict_deps=False` 时，依赖安装失败不会中断加载——通道仍会注册，并由 `start()` 重试安装
- `_ensure_deps(plugin_dir, plugin_name) -> bool`：如存在则安装插件的 `requirements.txt`（优先 `uv pip install`，回退 `python -m pip`，120 秒超时）
- `discover_plugins() -> dict[str, type[BaseChannel]]`：通过 `entry_points(group="channels")` 加载外部插件
- `discover_all() -> dict[str, type[BaseChannel]]`：合并内置（目录扫描，以 `strict_deps=False` 加载）与外部通道——内置优先，外部插件不能覆盖内置名称

> 单文件平铺模块（如 `qq.py`）不受支持；每个通道必须是一个含 `core.py` 的文件夹，且无需 `__init__.py`。

## 适配器

### QQ（`plugins/channels/qq/`）

当前唯一的内置适配器。`QQChannel(BaseChannel)`（`name = "qq"`，`display_name = "QQ"`）基于腾讯 `botpy` SDK 构建（`qq-botpy>=1.2.1`，列于插件的 `requirements.txt`）：

- WebSocket 客户端：`botpy.Client` 子类，`Intents(public_messages=True, direct_message=True)`
- 事件处理器：`on_c2c_message_create`、`on_group_at_message_create`、`on_direct_message_create`——全部转发到 `QQChannel._on_message()`
- 按消息 ID 去重（`deque(maxlen=1000)` 保存已见 ID）
- 群消息：`chat_id = group_openid`，发送者为 `author.member_openid`；C2C 消息：`chat_id` 为用户 openid；每个 `chat_id` 的会话类型会被缓存以选择回复 API
- 回复：`post_c2c_message()` / `post_group_message()`；载荷为 `msg_type` 2（markdown）或 0（plain），由 `msg_format` 决定，并引用入站 `message_id`（取自 `msg.metadata["message_id"]`）与递增的 `msg_seq`
- 缺少 `app_id` 或 `secret` 时 `start()` 直接返回不连接；出错时每 5 秒自动重连
- SDK 在加载/启动时从 `requirements.txt` 自动安装；连续 3 次安装失败后，60 秒内（冷却期）不再重试

**配置——两个文件：**

根配置开关在 `plugins/channels/config.json`（实际内容）：

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

凭据在 `plugins/channels/qq/config.json`（实际内容）：

```json
{
    "app_id": "",
    "receiver": ""
}
```

构造通道时，`QQChannel._merge_credentials()` 会把插件本地文件中的 `app_id` 和 `receiver` 合并进配置段。有效字段来自 `QQConfig`（pydantic 模型）：

- `enabled`：启用/禁用通道（由 `ChannelManager` 读取）
- `app_id`：QQ 应用 ID（来自插件本地文件；与 `secret` 一起为 `start()` 连接所必需）
- `secret`：QQ 应用密钥（根配置）
- `allow_from`：允许的发件人 ID；`"*"` 允许所有；空列表会以 `SystemExit` 中止启动
- `msg_format`：`"plain"`（默认）或 `"markdown"`
- `receiver`：主动（心跳）投递的默认 `chat_id`
- `heartbeat`：为 `true` 时，心跳服务将代理输出发送到该通道的 `receiver`（见 `server/service/heartbeat.py` 的 `process_heartbeat_notify()`）
- `cron`：存在于默认配置文件中，但 channels 包不会读取它

## 使用方法

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

在 `plugins/channels/` 下创建一个以通道名称命名的新**目录（文件夹）**，例如 `plugins/channels/my_channel/`。该目录包含一个定义（或重新导出）`BaseChannel` 子类的 `core.py`；无需 `__init__.py`：

```text
plugins/channels/
├── config.json              # 通道配置
└── my_channel/              # ← 通道目录，以通道名称命名
    └── core.py              #  定义 / 重新导出 BaseChannel 子类
```

```python
from typing import Any

from channels.base import BaseChannel
from type.bus import OutboundMessage


class MyChannel(BaseChannel):
    name = "my_channel"
    display_name = "我的通道"

    async def start(self) -> None:
        self._running = True
        # 连接到聊天平台；每收到一条入站消息调用：
        # await self._handle_message(sender_id, chat_id, content, ...)

    async def stop(self) -> None:
        self._running = False
        # 断开连接并清理

    async def send(self, msg: OutboundMessage) -> None:
        # 通过平台将 msg.content 送达 msg.chat_id
        ...

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return {"enabled": False, "allow_from": ["*"]}
```

注册中心会扫描 `plugins/channels/` 下的通道目录并动态导入每个目录的 `core.py`（使用找到的第一个 `BaseChannel` 子类）来自动发现通道。若目录中存在 `requirements.txt`，其依赖会被自动安装。在 `plugins/channels/config.json` 中添加 `"enabled": true` 的配置段即可启用。

## 架构

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

### 消息流

服务器启动时，`server/__main__.py` 导入 `server.trigger`，后者导入 `server.trigger.channels`。该模块在 `channel_manager` 单例上注册入站/出站消费者，并启动一个守护线程调用 `channel_manager.start_service()`。

**入站（用户 → 代理 → 回复）：**

1. 平台事件经 `botpy` WebSocket 到达并分发给 `QQChannel._on_message()`
2. 去重与解析后，`_handle_message()` 检查 `allow_from` 并通过 `bus.publish_inbound()` 发布 `InboundMessage`
3. `ChannelManager._inbound_consume_loop()` 消费该消息并调用已注册的消费者 `_process_inbound()`（`server/trigger/channels/core.py`）：图片 URL 转为 base64，由通道名派生会话 ID 并经 `relation_register.register_channel_chat()` 注册，代理回复通过 `server.service.async_generate()` 生成
4. 回复通过直接调用 `channel.send(OutboundMessage(...))` 送达

**出站（总线路径）：** 任何调用 `bus.publish_outbound()` 的消息都会被 `_dispatch_outbound()` 接收，并路由到 `msg.channel` 指定的通道（`_outbound_consume_loop()` 的消费者 `_process_outbound()` 仅注册通道会话）。

**主动推送（心跳）：** 当通道配置段为 `"heartbeat": true` 且其插件配置定义了 `receiver` 时，心跳服务（`server/service/heartbeat.py`）通过 `channel.send()` 将代理输出投递到该会话。
