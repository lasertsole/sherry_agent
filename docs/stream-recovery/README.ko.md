# 🔁 스트림 복구

[**English**](README.md) · [中文](README.zh.md) · [日本語](README.ja.md) · **한국어**

> 턴 실행 중 WebSocket 연결이 끊겨도 클라이언트가 턴을 다시 실행하거나, 다른 뷰어가 이미 받은 프레임을 재생해서는 안 된다. Sherry는 텍스트 스냅샷과 턴별 시퀀스 워터마크로 실행 중인 턴을 복구한다.

## 적용 범위

- **턴이 최소 하나의 chunk를 생성한 뒤** 연결이 끊긴 뷰어는 재접속 시 `resume_stream`으로 실행 중인 턴의 스냅샷을 요청한다.
- **첫 chunk 이전**에 끊긴 뷰어는 대기 중인 payload를 다시 보낸다. 백엔드는 `client_msg_id`로 중복을 제거하므로 턴이 두 번 시작되지 않는다.
- 마지막 뷰어가 끊겨도 턴 자체는 계속 실행된다. socket 해제가 턴을 취소하지 않는다.
- 이 프로토콜은 스냅샷 기반이며 ZCode의 7이벤트 앵커 프로토콜이 아니다. 핵심 목표 — 재실행 없음, 재생 없음, 중복 없음, 서버가 권위 있는 텍스트 꼬리 — 는 충족한다.

## 와이어 프로토콜

1. 클라이언트는 agent socket에서 `{"type": "resume_stream", "session_id": ..., "content": {}}`를 보낸다.
2. 서버는 `active_turn_snapshot(session_id)`를 호출한다. 실행 중인 턴이 있으면 엔진의 `ai_text`, `has_reasoning`, 현재 턴별 `seq`를 반환한다.
3. 서버는 **해당 socket에만** `stream_resume` 프레임을 응답하며, `status`는 `ok`, `ended`, `gone`, `unknown` 중 하나다.
4. `ok`는 스냅샷을 담는다. `ended`는 실행 중인 턴은 없지만 이력이 있음(클라이언트가 다시 가져옴), `gone`은 세션이 삭제되었거나 영속화된 적 없음, `unknown`은 읽기 실패 시 fail-open 결과다.
5. 서버는 스냅샷을 브로드캐스트하지 않고 과거 프레임도 재생하지 않는다. 라이브 프레임은 생성될 때 한 번만 브로드캐스트된다.

## 클라이언트 계약

- 클라이언트는 먼저 버퍼링된 delta를 flush한다. 스냅샷이 이미 그것들을 포함한다.
- 해당 턴의 AI 행은 스냅샷 텍스트로 **교체**되며 절대 덧붙이지 않는다.
- 모든 라이브 프레임은 턴별 `seq`를 가지며, 스냅샷은 읽은 시점의 `seq`를 담는다. 복구된 클라이언트는 그 워터마크 이하의 프레임을 버려, 스냅샷 읽기와 이후 도착하는 인플라이트 프레임 사이의 경쟁을 닫는다.
- `stream:resumed`는 비즈니스 계층이 소비하는 버스 이벤트다. 워터마크는 chat store가 아니라 socket 프레임 컨텍스트에 저장된다.

## 다루지 않는 것

- reasoning delta는 복구하지 않는다. 스냅샷은 `has_reasoning`만 보고한다.
- 증분 tool-input JSON과 delta별 usage 텔레메트리는 스냅샷에 포함되지 않는다.
- 끊기기 전에 커밋된 도구 호출은 턴별 trajectory ledger(`GET /sessions/:session_id/trajectory`)에서 계속 조회할 수 있지만, 스트림 이벤트로 재생되지는 않는다.
- ZCode의 완전한 7이벤트 앵커 프로토콜은 구현하지 않으며 계획도 없다. 스냅샷 프로토콜이 사용자에게 보이는 복구 목표를 더 적은 장치로 충족한다.

## 코드 맵

- `server/service/stream_dispatch.py`: `active_turn_snapshot`과 활동 중 턴 레지스트리.
- `server/trigger/ws/messages.py`: `_handle_resume_stream`, socket별 응답.
- `runtime/session/ws_push.py`: 라이브 프레임용 `broadcast_to_session`. 재생 로그는 없다.
- `client/app/composables/bridge/agent-socket.ts`: 연결 끊김 감지와 `resume_stream` 요청.
- `client/app/composables/bridge/agent-socket-frames.ts`: `stream_resume` 라우팅과 스냅샷 `seq` 워터마크.
- `client/app/composables/use-chat-stream.ts`: 텍스트 교체와 `ended` / `unknown` 재조회 결정.

## 테스트

- `tests/server/test_stream_resume.py`: 브로드캐스트 정확히 1회, 죽은 socket 제거, 스냅샷은 요청자에게만, session 교차 없음, 워터마크 계약.
- `client/app/composables/__tests__/agent-socket-resume.test.ts`: 재접속 요청과 `ok` / `ended` / `gone` 처리.
- `client/app/composables/__tests__/agent-socket-frames.test.ts`: `stream_resume` 프레임 라우팅과 스냅샷 적용.

## 실패 모드

- 스냅샷 읽기가 실패하면 서버는 `unknown`을 답한다. 클라이언트는 낡은 텍스트를 조용히 유지하지 않고 이력을 다시 가져온다.
- 활동 중인 턴도 영속 이력도 없으면 응답은 `gone`이며 클라이언트는 아무것도 하지 않는다.
- 스냅샷 `seq` 이하의 프레임이 스냅샷 이후 도착하면 클라이언트는 이를 버린다. 따라서 중복 전달이 가시 텍스트를 중복시키지 않는다.
- 스냅샷은 요청한 socket에만 전달된다. 같은 session의 다른 뷰어는 라이브 프레임을 계속 받고, 스냅샷을 재생으로 보지 않는다.
