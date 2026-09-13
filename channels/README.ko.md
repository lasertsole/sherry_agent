# Channels

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

---

이 모듈은 채팅 플랫폼과 통합하기 위한 통합 인터페이스를 제공하며, EMA AI 에이전트와 외부 메시징 플랫폼 간의 메시지 수신, 전송 및 라우팅을 처리합니다. 플랫폼 어댑터는 이 패키지 외부에 플러그인으로 존재하며, 저장소에는 현재 하나의 어댑터(**QQ**: `plugins/channels/qq/`)만 포함되어 있습니다.

## 개요

channels 모듈은 새로운 채팅 플랫폼 통합을 쉽게 추가할 수 있는 플러그인 기반 아키텍처를 구현합니다. 각 채널은 `BaseChannel`을 확장하는 클래스로 구현되며, `MessageBus`(`bus/core.py`)를 통해 시스템과 통신합니다. `MessageBus`는 두 개의 무제한 `asyncio.Queue`(인바운드와 아웃바운드)를 제공합니다. 채널 구현은 이 패키지 내부가 아니라 `plugins/channels/` 아래의 별도 디렉터리로 존재하며(`core.py`가 포함된 폴더를 검색하여 런타임 시 발견), 이 패키지 내부에는 없습니다.

## 주요 기능

- **통합 인터페이스**: 모든 채팅 플랫폼이 공통 `BaseChannel` 인터페이스를 공유합니다
- **플러그인 시스템**: 내장 채널(`plugins/channels/` 아래 `core.py`가 포함된 폴더)과 `entry_points(group="channels")`로 등록된 외부 플러그인을 모두 지원합니다
- **메시지 라우팅**: `MessageBus`의 인바운드/아웃바운드 `asyncio.Queue`. 아웃바운드 메시지는 `ChannelManager._dispatch_outbound()`가 일치하는 채널로 라우팅합니다
- **접근 제어**: 구성 가능한 발신자 화이트리스트(`allow_from`). 빈 목록은 모든 발신자를 거부하며, `"*"`는 모두 허용합니다
- **비동기 처리**: asyncio 기반으로 동시 메시지 처리를 지원합니다
- **안전한 시작**: `_validate_allow_from()`는 구성된 채널의 `allow_from`가 빈 목록일 때(모두를 거부하게 됨) `SystemExit`로 시작을 중단합니다
- **의존성 자동 설치**: `_ensure_deps()`가 각 플러그인의 `requirements.txt`를 설치합니다(가능하면 `uv`, 폴백으로 `pip`). 설치가 실패해도 채널은 건너뛰어지지 않고, 등록된 상태로 `start()`에서 지연 재시도됩니다

## 파일 구조

```
channels/
├── __init__.py      # 패키지 내보내기 (BaseChannel, channel_manager)
├── base.py          # BaseChannel 추상 기본 클래스 - 채널 인터페이스 정의
├── manager.py       # ChannelManager - 수명주기 조정 및 메시지 라우팅
├── registry.py      # 채널 발견 - 내장 및 플러그인 채널 자동 발견
├── README.md        # English documentation
├── README.zh.md     # 简体中文文档
├── README.ja.md     # 日本語ドキュメント
└── README.ko.md     # 한국어 문서
```

> **참고**: 채널 구현은 이 패키지에 **저장되지 않습니다.** 런타임 시 `plugins/channels/`에서 로드됩니다 (`registry.py` 참조). 현재 존재하는 채널은 `plugins/channels/qq/` 하나뿐입니다.

## 핵심 컴포넌트

### BaseChannel (base.py)

모든 채널 구현의 인터페이스를 정의하는 추상 기본 클래스:

- 클래스 속성: `name: str = "base"`, `display_name: str = "Base"`
- `__init__(self, config: Any, bus: MessageBus)`: `self.config`와 `self.bus`를 저장하고 `self._running = False`로 설정
- `async def start(self) -> None`: 추상 메서드 — 채널을 시작하고 메시지 수신을 시작 (장기 실행: 연결, 수신, `_handle_message()`로 전달)
- `async def stop(self) -> None`: 추상 메서드 — 채널을 중지하고 리소스를 정리
- `async def send(self, msg: OutboundMessage) -> None`: 추상 메서드 — 이 채널을 통해 메시지 전송
- `def is_allowed(self, sender_id: str) -> bool`: `config.allow_from` 확인 — 빈 목록은 모두 거부(경고 로그 기록), `"*"`는 모두 허용, 그 외에는 정확한 문자열 일치
- `async def _handle_message(self, sender_id: str, chat_id: str, content: str, media: list[str] | None = None, metadata: dict[str, Any] | None = None, session_id: str | None = None) -> None`: `is_allowed()`로 권한을 확인한 후 `InboundMessage`를 구성하고 `bus.publish_inbound()`로 게시
- `@classmethod def default_config(cls) -> dict[str, Any]`: 기본적으로 `{"enabled": False}` 반환. 플러그인이 오버라이드하여 `config.json`을 자동 채울 수 있음
- `@property is_running(self) -> bool`: 채널이 현재 실행 중인지 여부

### ChannelManager (manager.py)

활성화된 모든 채널을 조정합니다. 모듈 수준 싱글턴 `channel_manager`는 임포트 시 생성됩니다:

- `__init__(config=None, bus=None)`: 설정이 전달되지 않으면 `plugins/channels/config.json`을 로드합니다(파일이 없으면 초기화가 조기 반환 — 채널도 버스도 없음). 버스가 전달되지 않으면 `MessageBus()`를 생성한 뒤 `_init_channels()` 실행
- `_init_channels()`: `channels.registry.discover_all()`로 발견된 채널 중 설정 섹션이 `"enabled": true`인 채널을 모두 인스턴스화. 인스턴스화 예외는 로그로 기록되고 해당 채널은 건너뜁니다. 마지막에 `_validate_allow_from()` 호출
- `_validate_allow_from()`: 채널의 `allow_from`가 `[]`이면 `SystemExit` 발생
- `start_service()`: `_dispatch_outbound()` 디스패처, `_inbound_consume_loop()` / `_outbound_consume_loop()` 소비자, 채널별 `_start_channel()` 작업을 예약하고 이벤트 루프를 영구 실행합니다(활성화된 채널이 없으면 경고 후 반환)
- `async stop_service()`: 디스패처 작업을 취소하고 모든 채널의 `stop()`을 호출한 뒤 이벤트 루프를 중지
- `_dispatch_outbound()`: 1초 `asyncio.wait_for` 타임아웃으로 `bus.consume_outbound()`를 폴링하고 각 메시지를 `self._channels[msg.channel].send(msg)`로 전달
- `set_inbound_consumer(cb)` / `set_outbound_consumer(cb)`: `(msg, channel) -> Awaitable[None]` 형태의 콜백 등록
- `_inbound_consume_loop()` / `_outbound_consume_loop()`: 버스에서 소비하고 구성된 각 채널에 대해 한 번씩 등록된 소비자를 호출
- 접근자: `get_channel(name)`, `get_status()`(채널별 `enabled` / `running`), `get_bus()`, `get_event_loop()`, `enabled_channels` 속성

### Channel Registry (registry.py)

사용 가능한 채널을 자동 발견합니다:

- `discover_channel_names() -> list[str]`: `plugins/channels/` 아래 채널 **디렉터리**(하위 디렉터리, 단일 파일 아님) 중 `core.py`를 포함하는 폴더를 검색하고 정렬된 이름을 반환
- `load_channel_class(module_name, strict_deps=True) -> type[BaseChannel]`: `plugins/channels/<module_name>/core.py`를 동적으로 임포트하여 처음 발견된 `BaseChannel` 하위 클래스를 반환. `strict_deps=False`인 경우 의존성 설치 실패로도 로드가 중단되지 않으며, 채널은 등록된 상태로 `start()`가 설치를 재시도
- `_ensure_deps(plugin_dir, plugin_name) -> bool`: 존재하는 경우 플러그인의 `requirements.txt` 설치(`uv pip install` 우선, `python -m pip` 폴백, 120초 타임아웃)
- `discover_plugins() -> dict[str, type[BaseChannel]]`: `entry_points(group="channels")`로 등록된 외부 플러그인 로드
- `discover_all() -> dict[str, type[BaseChannel]]`: 내장(디렉터리 검색, `strict_deps=False`로 로드)과 외부 채널을 병합 — 내장이 우선이며, 외부 플러그인은 내장 이름을 가릴 수 없음

> 단일 파일 플랫 모듈(예: `qq.py`)은 의도적으로 지원하지 않습니다. 각 채널은 `core.py`를 포함하는 폴더여야 하며 `__init__.py`는 필요하지 않습니다.

## 어댑터

### QQ (`plugins/channels/qq/`)

현재 번들된 유일한 어댑터입니다. `QQChannel(BaseChannel)`(`name = "qq"`, `display_name = "QQ"`)은 Tencent의 `botpy` SDK 기반으로 구축되었습니다(`qq-botpy>=1.2.1`, 플러그인의 `requirements.txt`에 기재):

- WebSocket 클라이언트: `botpy.Client` 서브클래스, `Intents(public_messages=True, direct_message=True)`
- 이벤트 핸들러: `on_c2c_message_create`, `on_group_at_message_create`, `on_direct_message_create` — 모두 `QQChannel._on_message()`로 전달
- 메시지 ID 기반 중복 제거(수신된 ID의 `deque(maxlen=1000)`)
- 그룹 메시지: `chat_id = group_openid`, 발신자는 `author.member_openid`. C2C 메시지: `chat_id`는 사용자 openid. `chat_id`별로 채팅 유형을 캐시하여 응답 API를 선택
- 응답: `post_c2c_message()` / `post_group_message()`. 페이로드는 `msg_format`에 따라 `msg_type` 2(markdown) 또는 0(plain)이며, 인바운드 `message_id`(`msg.metadata["message_id"]`에서 가져옴)와 증가하는 `msg_seq`를 참조
- `app_id` 또는 `secret`이 없으면 `start()`는 연결 없이 반환됩니다. 오류 시 5초마다 자동 재연결
- SDK는 로드/시작 시 `requirements.txt`에서 자동 설치됩니다. 연속 3회 설치 실패 시 60초간(쿨다운) 재시도를 중단

**구성 — 두 개의 파일:**

루트 토글은 `plugins/channels/config.json`(실제 내용):

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

자격 증명은 `plugins/channels/qq/config.json`(실제 내용):

```json
{
    "app_id": "",
    "receiver": ""
}
```

채널 구성 시 `QQChannel._merge_credentials()`가 플러그인 로컬 파일의 `app_id`와 `receiver`를 설정 섹션에 병합합니다. 실제 필드는 `QQConfig`(pydantic 모델)에서 옵니다:

- `enabled`: 채널 활성화/비활성화 (`ChannelManager`가 읽음)
- `app_id`: QQ 애플리케이션 ID (플러그인 로컬 파일에서. `secret`과 함께 `start()` 연결에 필수)
- `secret`: QQ 애플리케이션 시크릿 (루트 설정)
- `allow_from`: 허용된 발신자 ID. `"*"`는 모두 허용. 빈 목록은 `SystemExit`로 시작을 중단
- `msg_format`: `"plain"`(기본값) 또는 `"markdown"`
- `receiver`: 능동적(하트비트) 전송의 기본 `chat_id`
- `heartbeat`: `true`이면 하트비트 서비스가 에이전트 출력을 이 채널의 `receiver`로 전송 (`server/service/heartbeat.py`의 `process_heartbeat_notify()` 참조)
- `cron`: 기본 설정 파일에 존재하지만 channels 패키지는 읽지 않습니다

## 사용법

### Channel Manager 사용

```python
from channels import channel_manager

# 특정 채널 가져오기
qq_channel = channel_manager.get_channel("qq")

# 활성화된 모든 채널 이름 가져오기
enabled = channel_manager.enabled_channels

# 채널 상태 가져오기
status = channel_manager.get_status()

# 메시지 버스 가져오기
bus = channel_manager.get_bus()

# 이벤트 루프 가져오기
loop = channel_manager.get_event_loop()
```

### 새 채널 구현

`plugins/channels/` 아래에 채널 이름을 딴 새 **디렉터리(폴더)**를 만듭니다. 예: `plugins/channels/my_channel/`. 이 폴더에는 `BaseChannel` 하위 클래스를 정의(또는 재-export)하는 `core.py`를 넣습니다. `__init__.py`는 필요하지 않습니다:

```text
plugins/channels/
├── config.json              # 채널 설정
└── my_channel/              # ← 채널 폴더, 채널 이름을 따서 명명
    └── core.py              #  BaseChannel 하위 클래스 정의 / 재-export
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
        # 채팅 플랫폼에 연결. 인바운드 메시지마다 호출:
        # await self._handle_message(sender_id, chat_id, content, ...)

    async def stop(self) -> None:
        self._running = False
        # 연결 해제 및 정리

    async def send(self, msg: OutboundMessage) -> None:
        # 플랫폼을 통해 msg.content를 msg.chat_id로 전달
        ...

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return {"enabled": False, "allow_from": ["*"]}
```

레지스트리는 `plugins/channels/` 아래의 채널 디렉터리를 검색하고 각 폴더의 `core.py`를 동적으로 임포트하여(처음 발견된 `BaseChannel` 하위 클래스 사용) 채널을 자동 발견합니다. 폴더에 `requirements.txt`가 있으면 의존성이 자동 설치됩니다. 활성화하려면 `plugins/channels/config.json`에 `"enabled": true` 섹션을 추가합니다.

## 아키텍처

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

### 메시지 흐름

서버 시작 시 `server/__main__.py`가 `server.trigger`를 임포트하고, 이어서 `server.trigger.channels`가 임포트됩니다. 이 모듈은 `channel_manager` 싱글턴에 인바운드/아웃바운드 소비자를 등록하고 `channel_manager.start_service()`를 호출하는 데몬 스레드를 시작합니다.

**인바운드 (사용자 → 에이전트 → 응답):**

1. 플랫폼 이벤트가 `botpy` WebSocket을 통해 도착하여 `QQChannel._on_message()`로 디스패치됨
2. 중복 제거 및 파싱 후 `_handle_message()`가 `allow_from`을 확인하고 `bus.publish_inbound()`로 `InboundMessage`를 게시
3. `ChannelManager._inbound_consume_loop()`가 이를 소비하고 등록된 소비자 `_process_inbound()`(`server/trigger/channels/core.py`)를 호출: 이미지 URL은 base64로 변환, 채널 이름에서 세션 ID를 도출하여 `relation_register.register_channel_chat()`으로 등록, 에이전트 응답은 `server.service.async_generate()`로 생성
4. 응답은 직접 `channel.send(OutboundMessage(...))` 호출로 전달됨

**아웃바운드 (버스 경로):** `bus.publish_outbound()`를 호출한 메시지는 `_dispatch_outbound()`가 받아 `msg.channel`에 지정된 채널로 라우팅합니다(`_outbound_consume_loop()`의 소비자 `_process_outbound()`는 채널 세션 등록만 수행).

**능동 전송 (하트비트):** 채널 섹션이 `"heartbeat": true`이고 해당 플러그인 설정에 `receiver`가 정의되어 있으면, 하트비트 서비스(`server/service/heartbeat.py`)가 `channel.send()`로 에이전트 출력을 해당 채팅에 전달합니다.
