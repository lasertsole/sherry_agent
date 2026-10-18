# 🔁 스트림 복구

[**English**](README.md) · [中文](README.zh.md) · [日本語](README.ja.md) · **한국어**

> 이 페이지는 API 계약이 아니라 메커니즘을 기록한다: 어떤 상태가 권위 있는지, 재접속이 그것을 어떻게 찾는지, 왜 delta 재생 대신 스냅샷 워터마크를 쓰는지.

## 설계 문제

턴은 오래 실행되는 async generator다. 출력은 생성되는 즉시 모든 라이브 socket으로 푸시되며, per-client 큐도 replay log도 없다. 뷰어는 어느 chunk에서든 사라질 수 있지만, 턴은 이미 도구 호출을 수행하고 토큰을 소비했을 수 있으므로 계속 실행되어야 한다.

요구 사항은 서로 상충한다:

- 재접속이 턴을 다시 실행해서는 안 된다. 부작용과 비용이 중복된다.
- 재접속이 프레임을 재생해서는 안 된다. 다른 뷰어가 이미 받았을 수 있고, 가시 텍스트가 중복된다.
- 재접속을 위해 서버가 뷰어별로 모든 delta를 보관해서는 안 된다. 상태가 무한해진다.
- 한 session의 여러 뷰어는 재생 커서를 공유하지 않고 같은 텍스트로 수렴해야 한다.

해답은 이벤트 로그가 아니라 **시점 스냅샷과 시퀀스 워터마크**다.

## 권위 있는 상태

`StreamTurn`이 실행 중인 턴의 권위 있는 텍스트를 소유한다. 가시 모델 텍스트는 think scrubber를 통과한 뒤 `self.ai_text`에 추가되고, reasoning은 `_has_reasoning`으로 별도 추적된다. 도구 호출은 `ai_text`의 일부가 아니며 trajectory ledger에 기록된다.

엔진은 `_run_frames` 시작 시 `_ACTIVE_TURNS[session_id]`에 등록하고 generator의 `finally`에서 제거하며, identity check로 오래된 턴이 새 턴을 제거하지 못하게 한다. turn runner/queue가 턴을 직렬화하므로 session당 활성 턴은 최대 하나다.

`active_turn_snapshot()`은 그 엔진에서 세 필드를 읽는다:

- `ai_text`: 지금까지 생성된 가시 텍스트;
- `_has_reasoning`: reasoning 채널이 존재하는지;
- `_frame_seq`: 지금까지 부여된 최고 프레임 시퀀스.

이 읽기는 stream generator와 같은 이벤트 루프에서 실행되므로 텍스트와 시퀀스 번호가 하나의 일관된 컷에서 읽힌다. 락은 필요 없다. 등록된 엔진이 없으면 실행 중인 턴이 없고 스냅샷 함수는 `None`을 반환한다.

## 프레임 순서와 워터마크

`StreamTurn.run()`이 내보내는 모든 프레임은 yield 전에 턴별 단조 `_frame_seq`를 받는다:

```text
_run_frames()가 프레임을 생성
  → run()이 _frame_seq를 증가
  → frame["seq"] = _frame_seq
  → 프레임이 브로드캐스트됨
```

스냅샷은 읽을 당시의 `_frame_seq`를 담는다. 그 숫자가 워터마크다:

- `seq <= snapshot.seq`인 프레임은 스냅샷 컷에서 이미 `ai_text`에 반영되어 있다.
- 그런 프레임은 재접속 socket으로 가는 중이거나 스냅샷 응답 후에 도착할 수 있다.
- 따라서 복구된 클라이언트는 워터마크 이하의 모든 프레임을 버리고 해당 턴의 AI 행을 스냅샷 텍스트로 교체한다.

이것은 선형화 논증이다. 스냅샷은 스트림을 가로지르는 컷이고, `seq`가 그 컷을 식별하며, 대체 텍스트가 그 컷의 상태다. 클라이언트는 스냅샷이 어떤 delta를 포함하는지 알 필요가 없다.

## 연결 끊김 중 수명 주기

`_release_socket`은 socket을 등록 해제하고 pending tool arguments를 정리할 수 있지만 턴을 **취소하지 않는다**. 턴 태스크는 완료될 때까지 `_active_tasks`에 남고, `_ACTIVE_TURNS`는 연결 끊김 경로가 아니라 generator 자체의 `finally`에서 제거된다. 따라서 마지막 뷰어가 떠나도 턴은 살아남는다.

재접속 시 클라이언트는 `_handle_resume_stream`을 호출한다. 핸들러는 활성 엔진을 읽는다:

- 엔진이 있음 → `ok`와 스냅샷;
- 엔진은 없지만 session에 영속 메시지가 있음 → `ended`(클라이언트가 이력을 다시 가져옴);
- 엔진도 이력도 없음 → `gone`;
- 읽기 실패 → `unknown`. 클라이언트는 낡은 텍스트를 조용히 유지하지 않고 다시 가져온다.

응답은 요청한 socket에만 전송된다. 다른 뷰어는 라이브 프레임을 계속 받고, 스냅샷은 브로드캐스트도 재생 소스도 아니다.

## 왜 replay log가 없는가

replay log는 모든 턴의 모든 delta를 저장하고 뷰어별로 보관하며 per-viewer 커서를 유지해야 한다. 또한 보존 정책, 백프레셔, "전달됨"과 "인플라이트"를 구분하는 방법이 필요하다 — 같은 워터마크 문제에 로그를 붙인 것뿐이다.

스냅샷 방식의 상태는 유계다: 실행 중인 턴마다 텍스트 누산기 하나와 카운터 하나. 본질적으로 멱등하다: 클라이언트가 한 프레임을 놓쳤든 천 프레임을 놓쳤든 스냅샷으로 AI 행을 교체한 결과는 같다. 재접속 비용은 O(현재 텍스트)이며 O(이력)이 아니다.

트레이드오프는 명시적이다: 스냅샷이 복구하는 것은 **가시 텍스트 꼬리**이지 모든 중간 이벤트가 아니다.

## 스냅샷이 의도적으로 생략하는 것

- reasoning delta: 스냅샷은 `has_reasoning`만 보고하고 reasoning 텍스트는 포함하지 않는다.
- 증분 tool-input JSON과 delta별 usage 텔레메트리.
- 스트림 이벤트로서의 도구 호출 카드. 커밋된 도구 호출은 trajectory ledger(`GET /sessions/:session_id/trajectory`)에서 계속 조회할 수 있다.

이 분담은 의도적이다. 텍스트는 영속 기록으로 부분 렌더링을 재구성할 수 없는 유일한 상태이며, 도구 호출에는 이미 영속 원장이 있다. 그 스트림 프레임을 재생하면 이미 있는 기록을 중복시킬 뿐이다.

## 실패 의미론

- 스냅샷 이후 도착한 오래된 프레임은 워터마크로 버려져 가시 텍스트를 중복시키지 않는다.
- `unknown`과 `ended`는 모두 이력 재조회로 이어지고, `gone`은 아무 동작도 하지 않는다.
- 스냅샷 읽기는 스트림의 이벤트 루프에서 실행되므로 일관적이다. 프레임은 `ai_text`에 포함되어 워터마크 이하이거나, 더 새로워 필터를 통과한다.
- socket별 응답은 다중 뷰어 독립성을 유지한다. 한 뷰어의 재접속이 다른 뷰어의 커서나 스트림을 바꾸지 않는다.

## 거부된 대안

- **재접속 시 턴 재실행.** 도구 부작용과 토큰 소비를 중복시키고 답을 바꿀 수 있다.
- **모든 delta 재생.** per-viewer 이력, 커서, 보존이 필요하며 `seq` 워터마크가 해결한 경쟁이 다시 나타난다.
- **ZCode의 7이벤트 앵커 프로토콜.** reasoning/tool-input의 더 세밀한 복구를 제공하지만 프론트엔드 프로토콜과 영속적인 anchor/tail-discard 파이프라인이 필요하다. Sherry의 스냅샷 프로토콜은 사용자에게 보이는 복구 목표를 더 적은 장치로 충족한다. 세밀한 이벤트 타입은 설계상 구현하지 않는다.

## 코드 맵

- `server/service/stream_dispatch.py`: `StreamTurn`(`ai_text`, `_frame_seq`, `_ACTIVE_TURNS`)과 `active_turn_snapshot`.
- `server/trigger/ws/messages.py`: `_handle_resume_stream`과 `_release_socket`.
- `runtime/session/ws_push.py`: 정확히 한 번의 브로드캐스트. replay log는 없다.
- `client/app/composables/bridge/agent-socket.ts`: 연결 끊김 감지와 `resume_stream`.
- `client/app/composables/bridge/agent-socket-frames.ts`: 스냅샷 `seq` 워터마크와 프레임 필터.
- `client/app/composables/use-chat-stream.ts`: 스냅샷 교체와 이력 재조회.
- `tests/server/test_stream_resume.py`, `client/app/composables/__tests__/agent-socket-resume.test.ts`: 위 불변 조건.
