# 슈퍼 에이전트

GitHub 이슈와 Feishu 토픽의 메시지를 추적 가능한 작업으로 바꾸고, Sherry worker
서브에이전트에 위임한 뒤 원래 채널로 결과를 보고하는 별도 프로세스 오케스트레이터입니다.
자체 SQLite 상태를 소유하며 좁은 루프백 이음새를 통해서만 Sherry와 통신합니다.

## 별도 프로세스인 이유

Sherry는 실행 단위이고 슈퍼 에이전트는 관리자입니다. 분리하면 관리자의 SQLite 상태,
tmux 세션, 매시간 검사가 Sherry 재시작 후에도 살아남고, Sherry가 다운되어도
"Trigger를 계속 큐에 넣고 아무것도 잃지 않는" 축소 모드로 유지되어 워크플로가
멈추지 않습니다. 슈퍼 에이전트는 Sherry 핵심 코드를 절대 임포트하지 않고
`/internal/superagent/*` 루프백 라우트만 호출합니다.

## 아키텍처

```
GitHub / Feishu  →  Sources  →  Engine  →  Effects  →  Sherry workers
                                  ↓          ↓
                             SQLite Store   Outbox → channel replies
```

Engine은 Trigger, Thread, Decision, Ticket의 유일한 기록자입니다. 모든 명령은
하나의 SQLite 트랜잭션이며, 어댑터(Sources, Executor, Outbox, Assessor)는 비동기로
좁은 포트를 통해 Engine과 통신합니다.

- **Sources**는 외부 이벤트를 Trigger로 번역하고 회신을 보냅니다.
- **Engine**은 다섯 부분을 한 방향으로 실행합니다: Intake → Evals → Tickets →
  Assignments → Replies.
- **Effects**는 영속적이며 병합·재시도 가능한 큐입니다——모든 부수 효과는
  재시작 후에도 살아남습니다.
- **Outbox**는 채널 Receipt의 유일한 기록자이며 순서와 멱등성을 보장합니다.
- **Passes**는 2초마다 만료된 Effects, 그다음 만료된 Evaluations를 실행합니다.

## 권한 부여와 평가

Policy는 admission 표와 `off`, `observe`, `auto` 세 가지 accept mode로 Trigger가
명시적인지 추론인지 결정합니다. 추론된 작업은 auto 모드와 규칙이 모두 활성화된
경우에만 수용됩니다. 평가는 전용 Sherry 세션에 위임되며, 제한된 thread 창을 읽어
Decision을 반환합니다. HITL 답변이 거부이면 fail closed로 blocked Ticket이 됩니다.

## 설정과 수명 주기

`superagent.jsonc`는 상태 디렉터리, Sherry 루프백 URL, 패널 포트, Source 설정을
담습니다. `scripts/superagent`는 전용 tmux 세션에서 `start`, `stop`, `restart`,
`status`를 제공하며 종료 코드는 0(정상), 1(미실행／런타임 오류), 2(부품 오류),
3(설정 미완료)입니다. 매시간 검사는 이 코드를 읽고 1일 때만 재시작합니다.
전체 운영 절차는 [deployment](deployment.md)를 참조하세요.

## 패널

Nuxt `/superagent` 페이지는 루프백 패널 API를 폴링하며 여섯 탭을 제공합니다:
Overview, Sources, Triggers, Evals, Execution, Settings. API는 루프백 보호
(host + origin + 요청 헤더)되어 있어 다른 출처의 페이지로는 구동할 수 없습니다.
설정은 리비전이 있으며 오래된 저장은 거부됩니다.

## 테스트와 게이트

`tests/superagent/`는 패키지를 미러링하며 도메인 모델, Engine, Policy, Effects,
Outbox, Sources, 패널에 대한 단위 테스트와 End-to-End 체인을 가집니다:
GitHub issue → Ticket → PR 및 Feishu mention → Ticket (둘 다 CLI와 worker를 mock).
정적 게이트는 `ruff`, `basedpyright`, `lint-imports` 계층 계약이며, 문서 게이트는
parity, links, language입니다.
