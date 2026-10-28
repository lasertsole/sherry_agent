# 🔁 스트림 복구

[English](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> 이 문서는 API 계약이 아니라 메커니즘을 기록합니다: 어떤 상태가 권위인지, 재접속이 그것을 어떻게 찾는지, 왜 델타 재생 대신 스냅샷 워터마크를 쓰는지.

## 설계 문제

턴은 장수명 async generator입니다. 출력은 생성되는 즉시 모든 라이브 socket으로 push되며, 클라이언트별 큐도 재생 로그도 없습니다. 시청자는 어느 chunk에서든 사라질 수 있고, 턴은 계속 실행되어야 합니다——이미 도구 호출을 수행하고 token을 소비했을 수 있기 때문입니다.

요구사항은 서로 당깁니다:

- 재접속은 턴을 재실행해서는 안 됩니다(부작용과 비용 중복).
- 재접속은 프레임을 재생해서는 안 됩니다(다른 시청자가 이미 받았다면 보이는 텍스트가 중복됨).
- 재접속을 위해 서버가 시청자별 모든 델타를 기억해서는 안 됩니다(무한 상태).
- 한 세션의 여러 시청자는 재생 커서를 공유하지 않고 같은 텍스트로 수렴해야 합니다.

답은 **한 시점의 스냅샷 + 턴 단위 시퀀스 워터마크**이며, 영속화된 히스토리와 같은 요청으로 함께 전달됩니다.

## 권위 상태

`StreamTurn`이 실행 중 턴의 권위 텍스트를 소유합니다. think scrubber를 통과한 가시 모델 텍스트는 `self.ai_text`에 추가되고, 추론은 `_has_reasoning`으로 별도 추적됩니다. 도구 호출은 `ai_text`에 속하지 않으며 trajectory ledger에 기록됩니다.

엔진은 `_run_frames`가 시작할 때 `_ACTIVE_TURNS[session_id]`에 등록되고 generator의 `finally`에서 자신을 제거합니다. 식별 검사로 오래된 턴이 새 턴을 제거하지 못합니다. turn runner/queue가 턴을 직렬화하므로 세션당 최대 한 턴이 활성입니다.

`StreamTurn`은 주변 turn scope에서 `turn_id`를 캡처합니다. `active_turn_snapshot()`는 엔진에서 네 필드를 읽습니다:

- `ai_text`: 지금까지 생성된 가시 텍스트;
- `_has_reasoning`: 추론 채널이 있는지;
- `_frame_seq`: 지금까지 찍힌 최대 프레임 시퀀스;
- `turn_id`: 텍스트가 속한 턴의 식별자.

읽기는 스트림 generator와 같은 이벤트 루프에서 실행되므로 텍스트와 시퀀스 번호가 같은 일관된 컷에서 읽히며 락이 필요 없습니다. 등록된 엔진이 없으면 실행 중 턴이 없고 스냅샷 함수는 `None`을 반환합니다. HTTP 히스토리 경로는 표시용 메타데이터로 `turn_num`(다음에 영속화될 턴 번호)을 덧붙입니다.

## 영속 실행: 크래시 후에도 살아남는 것

Sherry의 내구성 모델은 **"상태는 영속 + sweeper가 보완 + 새 turn이 재구동"**이며 Temporal식 액티비티 재실행이 아닙니다. 크래시는 재생할 수 없지만, 어떤 **상태**도 거짓말하지 않음을 보장할 수 있다면 복구에 진실된 근거가 생깁니다. 2026-10에 다섯 하위 시스템(HITL, 체크포인터, TaskFlow, 서브에이전트 레지스트리, 증거 원장)을 감사하고 첫 배치를 수정했습니다:

| 결함 | 수정 |
| :--- | :--- |
| 레지스트리의 종료 판정이 문자열 리터럴(`COMPLETED` 등)을 값이 `pending / running / interrupted / terminal`인 열거형과 비교——일치하지 않아 보존 상한이 동작하지 않고 메모리가 무한히 증가 | `ExecutionStatus.TERMINAL`과 비교. 상한은 활성 run은 유지하고 오래된 종료 run을 제거 |
| 세션 디렉터리 스윕이 원시 session id로 checkpoint를 조회. 체크포인터는 `session_thread_id(session_id)`(INTEGER)를 저장하므로 checkpoint만 남은 폴더가 빈 것으로 오인되어 삭제됨 | `pub.func`의 단일 `session_thread_id()` 매핑을 스윕에서 사용 |
| trajectory는 "쓰기 시 정리"라고 문서화했지만 `prune_session` 호출자가 없었고, 정렬도 항상 0인 `MAX(seq)`라 임의의 turn 집합이 남음 | 쓰기 시 연결하고 세션당 60초로 제한해 실행, 삽입 순서(rowid) 정렬 |
| 전역 keep-latest 정리가 `built_agent()`(즉 매 turn)에서, `busy_timeout` 없는 연결로 실행되고 keep-set을 삭제 전에 확정(그 사이 다른 세션이 커밋한 최신 checkpoint가 유실될 수 있음) | 부팅 시 1회(`server/service/checkpoint_maintenance.py`)로 이동, `BEGIN IMMEDIATE` 안에서 실행하고 keep은 최상위 행 우선 |
| 모든 체크포인터 연결에 `busy_timeout`이 없음(WAL과 달리 연결 단위 PRAGMA) | 단일 헬퍼(`agent/checkpointer/pragmas.py`, 30초)로 통일 |
| `delete_thread_history`에 보호가 없어 DB 잠금 시 메시지 저장소를 비운 뒤 purge가 중단——반쪽 삭제의 최악 | 각 단계를 보호하고 checkpoint 삭제를 가장 먼저 수행(재시도로 항상 완료 가능) |
| 종료 run의 전달 상태가 30초 주기 flush로만 디스크에 도달했고 그 창의 크래시는 SQLite를 TERMINAL+PENDING으로 남김——재시도 경로가 없어 완료 결과가 영구 유실 | 전달 상태 전이(`delivered / failed / suspended / in_progress`)를 즉시 영속화(`set_run_durable`) |
| auto-turn 드라이버가 빈 구현의 `apply_hitl_pending`을 상속해, headless turn 중단 후 그래프는 멈춰 있는데 세션은 idle로 보였음: 턴 종료 정리가 그 checkpoint를 지우고 drain이 새 메시지를 멈춘 그래프에 주입할 수 있었음 | 드라이버가 플래그를 설정. `submit_user_input`도 checkpoint의 interrupt를 busy로 간주(페일오픈 탐색). 재시작 후의 전형적 경우 |

감사의 남은 항목(세션 전체에 걸친 부팅 시 HITL 재구성, 영속 타임스탬프를 `time.monotonic()`에서 실제 시간으로, 재시작 후 in-flight step 복귀, P3/P4 증거·관측성 배치)은 계획에서 추적하며 완료로 간주하지 않습니다.


## 프레임 시퀀스와 워터마크

`StreamTurn.run()`이 내보내는 모든 프레임은 yield 전에 턴 단위 단조 `_frame_seq`를 찍습니다:

```text
_run_frames() produces a frame
  → run() increments _frame_seq
  → frame["seq"] = _frame_seq
  → frame["turn_id"] = self.turn_id
  → the frame is broadcast
```

`StreamDriver`도 `turn_started`, `done`, `hitl_request`, `error`, `stopped`에 `turn_id`를 찍습니다(chunk 루프 밖에서 만드는 프레임). 따라서 재접속 클라이언트는 턴 전환 후에도 각 프레임을 소속 턴으로 라우팅할 수 있습니다.

워터마크는 **턴 단위**입니다. 스냅샷은 읽은 시점의 `seq`와 `turn_id`를 운반하고, 프레임의 `turn_id`가 일치하고 `seq <= snapshot.seq`일 때만 버립니다. 새 턴은 `seq = 1`에서 다시 시작하므로 다른 턴의 워터마크보다 숫자가 작다는 이유만으로 오래된 데이터로 오인되지 않습니다.

## 재접속 시 한 번의 왕복

클라이언트는 재접속 때 `resume_stream`을 보내지 않습니다. 히스토리 요청을 한 번 보냅니다:

```text
GET /get_history_by_turn_page?session_id=…&include_active_turn=1
  → {messages: [...persisted rows...],
     active_turn: {text, has_reasoning, seq, turn_id, turn_num} | null}
```

`include_active_turn=1`이 없으면 엔드포인트는 기존처럼 행 배열을 반환하므로 모든 기존 호출자의 계약이 유지됩니다. 플래그가 있으면 영속화 히스토리와 실행 중 턴의 스냅샷이 함께 도착합니다: 연결이 끊긴 동안 완료된 턴은 `messages`에, 실행 중 턴은 `active_turn`에 들어갑니다.

WS handler `_handle_resume_stream`과 `stream_resume` 응답은 구버전 클라이언트용 폴백으로 남으며, 현재 클라이언트는 그 프레임을 보내지 않습니다.

## 클라이언트 스냅샷 큐

히스토리 요청이 진행 중일 때 socket은 들어오는 모든 WS 프레임을 적용하지 않고 보관합니다(`holdFrames`, 최대 1000 프레임, 초과 시 가장 오래된 것 버림). 응답이 오면:

1. 영속화 메시지를 채팅 목록에 병합(연결이 끊긴 동안 완료된 턴이 즉시 보임);
2. `active_turn`을 소속 턴의 행에 적용: 행에 `turn_id`가 있으면 그것으로, 없으면 `turn_num`, 둘 다 없으면 새 AI 행 생성;
3. socket이 스냅샷 워터마크(`seq`와 `turn_id`)를 기록;
4. 보관 프레임을 도착 순서로 해제. 같은 턴에서 `seq <= 워터마크`인 프레임은 버리고, 나머지는 자신의 `turn_id`로 정상 적용.

지연되거나 유실된 응답이 UI를 막지 않습니다: 대기는 10초로 제한됩니다. 타임아웃 시 응답은 채팅 병합에서 제외되고, 워터마크 없이 큐를 해제하며, socket의 종료 프레임이나 busy watchdog이 턴을 정리합니다. chunk를 아직 받지 않은 전송은 재개 가능하므로 idle 스냅샷으로 결산되지 않으며, chunk를 받은 전송만 연결 중 종료로 결산됩니다.

## 연결 중단 시 수명주기

`_release_socket`은 socket을 등록 해제하고 대기 중 도구 인자를 정리할 수 있지만 턴을 **취소하지 않습니다**. 턴 작업은 끝날 때까지 `_active_tasks`에 남고, `_ACTIVE_TURNS`는 연결 경로가 아니라 generator 자신의 `finally`에서 제거됩니다. 마지막 시청자가 떠나도 턴은 살아남습니다.

프레임은 정확히 한 번 브로드캐스트되고 재생되지 않습니다. 재접속 클라이언트는 스냅샷에서 권위 텍스트를 얻고 등록 이후 생성된 프레임만 받습니다——둘의 조합에는 빈틈이 없고 재생 로그도 필요 없습니다.

## 스냅샷이 생략하는 것

- 추론 델타: 스냅샷은 `has_reasoning`만 보고하고 추론 본문은 포함하지 않습니다.
- 증분 도구 입력 JSON과 delta 단위 사용량 텔레메트리.
- 도구 호출 카드의 스트림 이벤트. 확정된 도구 호출은 trajectory ledger(`GET /sessions/:session_id/trajectory`)에서 조회할 수 있습니다.

이 구분은 의도적입니다. 텍스트만이 영속 기록으로 부분 렌더링을 재구성할 수 없는 상태입니다. 도구 호출에는 이미 영속 ledger가 있으므로 스트림 프레임을 재생하면 기존 기록을 중복시키게 됩니다.

## 실패 의미론

- 스냅샷 이후 도착한 오래된 프레임은 턴 단위 워터마크로 버려져 가시 텍스트를 중복시키지 않습니다.
- 히스토리 응답이 없거나 타임아웃되면 워터마크 없이 보관 프레임을 해제하고 평소대로 적용합니다. watchdog은 마지막 안전망입니다.
- 연결 중 종료된 턴은 히스토리 행에 나타납니다. 스냅샷이 활성 턴 없음을 보고하면 chunk를 받은 전송은 결산됩니다. chunk를 받지 않은 전송은 재전송되고(backend는 `msg_id`로 중복 제거) 턴이 시작될 때까지 pending으로 남습니다.
- socket별 타기팅은 다중 시청자 독립성을 보존합니다: 한 시청자의 재접속이 다른 시청자의 커서나 스트림을 바꾸지 않습니다.

## 거부한 대안

- **재접속 시 턴 재실행.** 도구 부작용과 token 소비를 중복시키고 답을 바꿀 수 있습니다.
- **모든 델타 재생.** 시청자별 히스토리·커서·보존이 필요하며, `seq` 워터마크가 해결하는 경쟁이 다시 도입됩니다.
- **ZCode의 7이벤트 앵커 프로토콜.** 더 세밀한 추론/도구 입력 복구를 제공하지만 프런트엔드 프로토콜과 영속 anchor/tail-discard 파이프라인이 필요합니다. Sherry의 스냅샷 프로토콜은 더 적은 기계장치로 사용자 가시 복구 목표를 충족하며, 세밀한 이벤트 타입은 설계상 미구현으로 남습니다.

## 궤적 패널

원장에는 UI 소비자도 있습니다. SESSION 범위의 오른쪽 사이드바 탭
(`client/app/pages/home/components/TrajectoryPanel.vue`)이
`GET /sessions/:session_id/trajectory`를 읽어 턴별 타임라인을 표시합니다. 재연결
프로토콜이 아니라 디버그 뷰입니다. 열면 최신 500행 페이지를 두 번만 가져오고(최대 1000개
이벤트, 초과 시 잘림 안내), 새로고침은 버튼이며 라이브 구독은 없습니다.

이벤트는 `turn_id`로 묶고 `(ts, seq, event_id)`로 다시 정렬합니다. `read()`는 `seq`
순서이고 모든 라이프사이클 이벤트(turn_start / turn_end / compact / model_retry /
invalid_tool_call)가 `seq=0`이기 때문에, 다시 정렬하지 않으면 모두 도구 행보다 앞에
모이고 `turn_end`가 맨 앞에 오게 됩니다. 도구 행은 이름/상태/시간과 펼칠 수 있는
인수·결과 JSON을, 라이프사이클 행은 종류 배지와 펼칠 수 있는 payload를 보여줍니다.
헤더에는 필터식 검색(n/m 카운트), 두 개의 접기 스위치(도구 행은 기본 접힘),
모두 펼치기/접기, 턴 필터, 새로고침이 있습니다. 도구 카드의 기록 버튼은 `ChatBox.vue`를
통해 같은 단일 탭을 엽니다.

순수 함수(`groupTurns`, `summarizeTurns`, `formatDuration`, `previewOf`,
`eventMatches`)는 `client/app/composables/use-trajectory.ts`에 있고, bridge 호출은
`bridge/session.ts`의 `fetchTrajectory`입니다.

## 코드 맵

- `server/service/stream_dispatch.py`: `StreamTurn`(`ai_text`, `_frame_seq`, `turn_id`, `_ACTIVE_TURNS`)과 `active_turn_snapshot`.
- `server/service/stream_driver.py`: driver가 만드는 모든 프레임에 `turn_id`.
- `server/trigger/http/messages.py`: `GET /get_history_by_turn_page`의 `include_active_turn`.
- `server/trigger/ws/messages.py`: 구 `_handle_resume_stream` 경로와 `_release_socket`.
- `runtime/session/ws_push.py`: 정확히 한 번 브로드캐스트, 재생 로그 없음.
- `client/app/composables/messages.ts`: `get_history_page_with_active_turn`.
- `client/app/composables/use-session-lifecycle.ts`: 스냅샷을 반환하는 히스토리 병합.
- `client/app/composables/use-chat-stream.ts`: `syncHistory`, 스냅샷 적용과 턴 라우팅.
- `client/app/composables/bridge/agent-socket.ts`: 프레임 보관/해제와 스냅샷 워터마크.
- `client/app/composables/bridge/agent-socket-frames.ts`: 턴 단위 워터마크 필터.
- `tests/server/test_stream_resume.py`, `tests/server/service/test_stream_turn_id.py`, `tests/server/trigger/test_history_active_turn.py`, `client/app/composables/__tests__/agent-socket-resume.test.ts`: 위 불변식.
