# 미팅 모드

미팅 모드는 Sherry의 세 번째 프로덕트 모드입니다. 여러 등록된 전문가가 한 주제에 대해 토론하고 공통 결론에 도달한 뒤, 구조화된 계획을 전문가 팀 실행 구성 요소로 넘깁니다. 토론 단계와 실행 단계는 독립적이므로, 계획만 저장하고 나중에 실행할 수 있습니다.

## 미팅이 필요한 이유

단일 agent는 한 가지 관점에서만 답합니다. 미팅은 여러 도메인 전문가를 같은 대화에 참여시켜 결정의 의존 관계를 기록합니다. 프런트엔드 결정이 백엔드에 영향을 주고, 그것이 데이터베이스에 영향을 줍니다. 사용자는 "보스"이며 언제든 끼어들 수 있습니다. 참가자들은 서로 직접 호출하지 않으므로 토론이 교착 상태에 빠지지 않습니다.

## 아키텍처

```
User  ->  Host  ->  Selector  ->  Participants  ->  Conclusion
            |           |              |
         tools       one JSON       one statement
         allowlist   verdict        per turn
```

세 역할은 `agent/meeting/`에 있습니다. `host`는 조율만 담당하며 도구 표면은 조율용 allowlist로 제한됩니다. `selector`는 매 턴마다 하나의 `MeetingTurnDecision`을 반환합니다. 각 `participants`는 공유 기록을 바탕으로 한 번 발언합니다. `run_meeting`이 원래 루프이며, `run_meeting_with_interrupts`는 단계 전환, 끼어들기, 단계 allow-list 게이트를 추가합니다.

## 라이프사이클

미팅은 호스트가 주제를 확인하면서 시작되고 디스패칭 루프로 진입합니다. 셀렉터가 다음 발언자를 지정하고, 해당 참가자가 발언하며, 발언이 추가되어 저장됩니다. 셀렉터가 다음 발언자를 반환하지 않거나 40라운드 상한에 도달하면 호스트가 요약을 작성하고 종료 확인으로 이동합니다. 각 발언은 speaker kind와 message role과 함께 영속화됩니다.

## 끼어들기와 롤백

`apply_interruption`은 사용자 끼어들기를 여섯 유형 중 하나로 분류합니다. 보충은 계획을 바꾸지 않고 추가되고, 종료 요청은 종료 확인으로 이동합니다. 정정, 범위 변경, 우선순위 변경은 `apply_local_rollback`을 호출합니다. 영향을 받는 실행 계획 단계와 그것에 의존하는 모든 단계가 `needs_rediscussion`으로 표시되어 셀렉터가 다음 턴에서 다시 계획합니다. 단계 allow-list 게이트는 참가자가 자신을 선택한 단계 밖에서 발언하는 것을 막습니다.

## 결론에서 실행으로

`build_conclusion`은 TaskFlow 형태의 계획을 담은 `MeetingConclusion`을 생성합니다. 각 단계는 작업, 등록된 전문가 이름, 의존 관계를 가집니다. `execute_conclusion`은 결정적 id `meeting-{meeting_id}`로 플로를 생성하고 전문가 팀 메커니즘으로 단계를 디스패치합니다. 두 번째 실행 요청은 기존 플로를 찾아 중복 생성하지 않습니다. `reusable` 힌트는 TaskFlow 단계의 네이티브 필드가 아니므로 미팅 결론에 남습니다.

## 스트리밍

미팅 스트림은 `/meetings/ws`의 미팅별 WebSocket 채널입니다. agent 이벤트는 `MEETING_EVENT_PUSH` 훅으로 전송되며, 이 훅은 fail-open입니다. WS 서버가 없는 프로세스에서도 미팅은 정상적으로 실행됩니다. 프레임은 발언자 변경, 메시지 델타와 완료, thinking 델타, ask-user 프롬프트, 요약 초안을 다룹니다. Nuxt 페이지는 모든 프레임을 스토어의 프레임 리듀서로 보내며, mock 모드에서는 백엔드 없이도 페이지를 탐색할 수 있습니다.

## 영속화와 복구

미팅, 메시지, `meeting_compaction_segments`는 공유 registry 데이터베이스에 있습니다. 긴 토론은 미팅 소유 트리거로 summarization 미들웨어의 재사용 가능한 프리미티브를 활용해 압축되므로 메인 agent의 task-intent가 다시 실행되지 않습니다. 부팅 시 `auto_adjourn_at_boot`는 메모리 내 루프가 재시작 후 살아남을 수 없으므로 `process_restart` 이유로 활성 미팅을 취소합니다.

## API

HTTP 표면은 생성용 `POST /meetings`, 목록용 `GET /meetings`, 읽기용 `GET /meetings/one`과 `GET /meetings/messages`, 취소용 `DELETE /meetings`와, `POST /meetings/run`, `/meetings/interrupt`, `/meetings/reply`, `/meetings/confirm-end`, `/meetings/execute`, `GET /meetings/conclusion`입니다. 모든 라우트는 쿼리 파라미터나 JSON 본문을 사용하며 경로 보간은 사용하지 않습니다.

## 테스트와 게이트

`tests/agent/meeting/`은 루프, 분류, 롤백, 압축, 결론, 부팅 복구를 다루고, `tests/server/trigger/http/test_meetings_api.py`가 라우트를, `tests/server/trigger/ws/test_meeting_ws.py`가 프레임 전송을 다룹니다. 프런트엔드 테스트는 스토어 리듀서와 페이지를 고정합니다. 정적 게이트는 `ruff`, `basedpyright`, `lint-imports`이며, 문서 게이트는 parity, links, language입니다.
