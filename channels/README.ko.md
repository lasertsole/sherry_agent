# Channels

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

---

이 모듈은 다양한 채팅 플랫폼(Telegram, QQ, WhatsApp 등)과 통합하기 위한 통합 인터페이스를 제공합니다. EMA AI 에이전트와 서로 다른 메시징 플랫폼 간의 메시지 수신, 전송 및 라우팅을 처리합니다.

## 개요

channels 모듈은 새로운 채팅 플랫폼 통합을 쉽게 추가할 수 있는 플러그인 기반 아키텍처를 구현합니다. 각 채널은 `BaseChannel`을 확장하는 클래스로 구현되며 메시지 버스를 통해 시스템과 통신합니다. 채널 구현은 이 패키지 내부가 아니라 `plugins/channels/` 아래의 별도 Python 파일로 존재하며(런타임 시 pkgutil로 발견), 이 패키지 내부에는 없습니다.

## 주요 기능

- **통합 인터페이스**: 모든 채팅 플랫폼이 공통 `BaseChannel` 인터페이스를 공유합니다
- **플러그인 시스템**: 내장 채널(`plugins/channels/` 아래, pkgutil로 발견)과 entry points를 통한 외부 플러그인을 모두 지원합니다
- **메시지 라우팅**: 인바운드 및 아웃바운드 메시지 라우팅을 자동 처리합니다
- **접근 제어**: 허용된 발신자에 대한 화이트리스트(`allow_from`)를 구성할 수 있습니다. 빈 목록은 모두 거부합니다
- **비동기 처리**: asyncio 기반으로 동시 메시지 처리를 지원합니다
- **안전한 시작**: `_validate_allow_from()`는 모든 발신자를 거부하게 될 잘못 구성된 채널을 방지합니다

## 파일 구조

```
channels/
├── __init__.py      # 패키지 내보내기 (BaseChannel, channel_manager)
├── base.py          # BaseChannel 추상 클래스 - 채널 인터페이스 정의
├── manager.py       # ChannelManager - 모든 채널 및 메시지 라우팅 조정
├── registry.py      # 채널 발견 - 내장 및 플러그인 채널 자동 발견
└── README.zh.md     # 中文文档
```

> **참고**: 채널 구현(예: QQ, Telegram, WhatsApp)은 이 패키지에 **저장되지 않습니다.** 런타임 시 `plugins/channels/`에서 로드됩니다 (registry.py 참조).

## 핵심 컴포넌트

### BaseChannel (base.py)

모든 채널 구현의 인터페이스를 정의하는 추상 기본 클래스:

- `start()`: 메시지 수신 시작 (추상 메서드)
- `stop()`: 채널을 중지하고 리소스를 정리 (추상 메서드)
- `send(msg)`: 채널을 통해 메시지 전송 (추상 메서드)
- `is_allowed(sender_id)`: 발신자가 허용되었는지 확인 — 빈 `allow_from`은 모두 거부, `"*"`는 모두 허용
- `_handle_message(sender_id, chat_id, content, ...)`: 권한 확인 후 `InboundMessage`를 버스에 게시
- `default_config()`: 클래스 메서드 — 기본 구성 dict 반환 (플러그인에서 오버라이드)
- `is_running`: 속성 — 채널이 현재 실행 중인지 확인

### ChannelManager (manager.py)

활성화된 모든 채널을 조정합니다:

- 구성에서 채널 초기화 (`plugins/channels/config.json`)
- 채널 수명주기 관리 (`start_service()` / `stop_service()`)
- `_dispatch_outbound()`를 통해 아웃바운드 메시지를 적절한 채널로 라우팅
- 인바운드/아웃바운드 소비자 루프 실행 (`_inbound_consume_loop`, `_outbound_consume_loop`)
- 모든 채널의 상태 정보 제공
- 시작 시 `allow_from`을 검증하여 잘못된 구성을 방지
- 모듈 수준 싱글턴 생성: `channel_manager`

### Channel Registry (registry.py)

사용 가능한 채널을 자동 발견합니다:

- `discover_channel_names()`: pkgutil로 `plugins/channels/`의 `.py` 모듈을 검색
- `load_channel_class(module_name)`: 채널 모듈을 동적으로 가져와 첫 번째 `BaseChannel` 하위 클래스를 찾음
- `discover_plugins()`: `entry_points(group="channels")`로 등록된 외부 플러그인 로드
- `discover_all()`: 내장 및 외부 채널 병합 (내장 우선 — 외부가 내장 이름을 가릴 수 없음)

## 사용법

### 구성

채널은 `plugins/channels/config.json`에서 구성됩니다:

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

구성 옵션:
- `enabled`: 채널 활성화/비활성화
- `app_id`: 채팅 플랫폼의 애플리케이션 ID
- `secret`: 애플리케이션 비밀 키
- `allow_from`: 허용된 발신자 ID 목록 (`"*"`는 모두 허용, 빈 목록은 시작 오류 발생)
- `msg_format`: 메시지 형식 ("plain" 또는 "markdown")

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

`plugins/channels/` 아래에 새 `.py` 파일을 만듭니다:

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
        # 채팅 플랫폼에 연결

    async def stop(self) -> None:
        self._running = False
        # 연결 해제 및 정리

    async def send(self, msg: OutboundMessage) -> None:
        # 플랫폼을 통해 메시지 전송
        pass

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return {"enabled": False, "allow_from": ["*"]}
```

레지스트리는 `plugins/channels/`를 검색하여 자동으로 채널을 발견하고 로드합니다.

## 아키텍처

```
┌─────────────────────────────────────────────────────────┐
│                    ChannelManager                        │
│  - 채널 수명주기 관리                                     │
│  - dispatch 루프를 통해 메시지 라우팅                     │
│  - 인바운드/아웃바운드 소비자 조정                        │
│  - 시작 시 allow_from 검증                              │
└─────────────────────────────────────────────────────────┘
                               │
         ┌─────────────────────┼─────────────────────┐
         ▼                     ▼                     ▼
┌───────────────┐   ┌───────────────┐   ┌───────────────┐
│  QQ 채널      │   │  Telegram     │   │  WhatsApp     │
│  (플러그인)   │   │  채널 (플러그인) │   │  채널 (플러그인) │
│  plugins/     │   │               │   │               │
│  channels/    │   │               │   │               │
│  qq.py        │   │               │   │               │
└───────────────┘   └───────────────┘   └───────────────┘
         │                     │                     │
         └─────────────────────┼─────────────────────┘
                               ▼
                     ┌─────────────────┐
                     │   Message Bus   │
                     │  (bus 모듈)     │
                     └─────────────────┘
```

메시지 흐름:
- 인바운드: 채팅 플랫폼 → 채널 → 메시지 버스 → AI 에이전트
- 아웃바운드: AI 에이전트 → 메시지 버스 → 채널 → 채팅 플랫폼
