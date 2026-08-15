# Future Subagent — Python 하위 에이전트 시스템

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> 기존 `agent/tools/subagent/`(Commander/Worker 패턴)와 공존하는 다중 레벨 하위 에이전트(subagent) 시스템의 Python 구현입니다. 7개 구현 단계 전체 + robustness-plan-v3 개선 + 버그 수정 + OpenClaw 정렬 + 깊이 정렬 + 연결(wiring) 수정이 모두 완료되었습니다. 203개 테스트 통과.

## 빠른 탐색

| 문서 | 용도 |
|----------|---------|
| [AGENTS.md](./AGENTS.md) | **진입점** — 프로젝트 규칙, 현재 진행 상황 |
| [architecture.md](./docs/architecture.md) | 전체 아키텍처, 디렉터리 구조, 모듈 의존성 그래프 |
| [decisions.md](./docs/decisions.md) | 주요 기술 의사결정 기록(22개 결정) |
| [integration.md](./docs/integration.md) | 기존 시스템과의 통합 계획 |

---

## 실행 원칙

### 1. 시스템 개요

Subagent 시스템의 핵심 목표는 메인 에이전트가 복잡한 작업을 병렬 하위 작업으로 분해하고, 이를 독립적인 자식 에이전트에 전달하여 실행하게 하며, 완료 시 결과를 부모 에이전트에게 안정적으로 전달하는 것입니다. 전체 시스템은 세 가지 핵심 파이프라인에 의해 구동됩니다:

```
┌──────────────────────────────────────────────────────────────────┐
│  부모 에이전트 (LangGraph CompiledStateGraph)                     │
│    │                                                             │
│    ├─ 1. sessions_spawn ──► 생성 파이프라인 ──► 자식 에이전트 비동기 │
│    │                                                             │
│    ├─ 2. sessions_yield ──► 현재 턴 일시중지, 자식 대기           │
│    │                                                             │
│    ├─ 3. sessions_send  ──► A2A 양방향 (EventBus 경유)            │
│    │                                                             │
│    └─ 4. 자식 완료 ──► Announce 파이프라인 ──► 전달 경유          │
│                              EventBus + Registry 라이프사이클      │
└──────────────────────────────────────────────────────────────────┘
```

### 2. 생성 파이프라인 — 자식 에이전트 생성 및 전달

`spawn_subagent_direct()`는 시스템의 진입점입니다. LLM이 `sessions_spawn` 도구를 호출하면 다음 흐름이 실행됩니다:

```
spawn_subagent_direct(task, requester_session_key, agent_id, mode, ...)
  │
  ├── 1. 검증 단계
  │     ├── task가 비어있지 않은지 검증
  │     ├── task_name 정규화 (비알파벳·숫자를 _로 대체, 64자로 제한)
  │     ├── target_policy 검증 (agent_id가 allow_agents 화이트리스트에 있는지)
  │     ├── 깊이 계산: parent_depth + 1, ≤ max_spawn_depth (기본 3) 검증
  │     ├── 동시성 검증: 활성 자식 수 < max_children_per_agent (기본 5)
  │     └── 런타임 격리 검증 (런타임 간 생성 차단)
  │
  ├── 2. 역할 및 기능 해석
  │     └── resolve_subagent_capabilities(depth, max_depth)
  │           ├── depth == 0       → MAIN,        control_scope=CHILDREN
  │           ├── 0 < depth < max  → ORCHESTRATOR, control_scope=CHILDREN
  │           └── depth >= max     → LEAF,         control_scope=NONE
  │
  ├── 3. 컨텍스트 준비
  │     ├── 사고 수준 재정의 해석 (plan.py)
  │     ├── 첨부 파일 디스크 실체화 (attachments.py)
  │     │     안전 검사 포함: 경로 우회, 크기 제한, 파일 수
  │     ├── 도구 정책: DEFAULT_SUBAGENT_BLOCKED_TOOLS = [sessions_spawn,
  │     │   sessions_yield, skill_manage, memory]
  │     │   ORCHESTRATOR 역할은 sessions_spawn과 sessions_yield 자동 해제
  │     ├── 컨텍스트 모드: ISOLATED (빈 컨텍스트) 또는 FORK (부모
  │     │       전사 복사 via agent.aget_state() — 결정 9)
  │     ├── 스레드 바인딩: SESSION 모드 → bind_thread_for_subagent_spawn()
  │     │       채널 스레드 + delivery_origin 생성 (결정 11)
  │     ├── 런타임 격리: resolve_runtime_isolation() + cwd 검증
  │     │       (결정 15)
  │     ├── 출처 라우팅: resolve_requester_origin_for_child()
  │     └── 범위 해석: 역할별 resolve_least_privilege_scopes()
  │
  ├── 4. 실행 등록
  │     ├── child_session_key 생성 = "agent:{agent_id}:subagent:{uuid}"
  │     ├── SubagentRunRecord 생성 (UUID, execution=RUNNING, delivery=PENDING)
  │     ├── 메모리 dict + SQLite에 저장
  │     └── 터미널 세대 등록 (TerminalGenerationTracker)
  │
  ├── 5. 프롬프트 구성
  │     ├── build_subagent_system_prompt(role, task, ...)
  │     │   ├── 6-섹션 구조: Your Role / Rules / Output Format /
  │     │   │     What You DON'T Do / Sub-Agent Spawning / Session Context
  │     │   ├── 안티-폴링 규칙 (능동 상태 폴링 금지)
  │     │   ├── 출력 길이에 대한 잘라내기 힌트
  │     │   ├── LEAF: output_schema에서 가져온 구조화된 출력 템플릿
  │     │   └── ORCHESTRATOR: "You MAY spawn further subagents via sessions_spawn."
  │     ├── 시스템 프롬프트에 첨부 파일 위치 힌트 추가
  │     ├── output_schema에서 구조화된 출력 프롬프트 추가 (swarm 모드)
  │     └── build_subagent_initial_user_message(task, context)
  │           └── 구조화된 봉투: [Subagent Context] / [Subagent Task] / [Subagent Additional Context]
  │
  ├── 6. 비동기 전달 (Fire-and-Forget)
  │     └── asyncio.create_task(_execute_subagent(...))
  │
  └── 7. 즉시 반환
        └── SpawnResult { status: "accepted", child_session_key, run_id }
```

#### 자식 에이전트 실행

`_execute_subagent()`는 자식 에이전트의 전체 라이프사이클을 담당하는 백그라운드 asyncio 태스크입니다:

```
_execute_subagent(run, system_prompt, user_message, forked_messages, ...)
  │
  ├── 1. 자식 에이전트 빌드
  │     ├── 모든 도구를 얻기 위해 build_main_tools() 호출
  │     ├── tool_allow/tool_deny로 필터링 (deny 목록이 우선)
  │     ├── LLM 빌드: ORCHESTRATOR → build_main_llm(), LEAF → build_auxiliary_llm()
  │     ├── 독립 SQLite 체크포인터 생성
  │     └── 다섯 개 미들웨어로 create_agent():
  │           ├── Summarization(trigger=[fraction:0.5, messages:40, tokens:30000])
  │           ├── IterationBudget(60)     — 최대 반복 횟수
  │           ├── ToolGuardrails()        — 도구 안전 가드레일
  │           ├── ToolCallNormalize()     — 도구 호출 정규화
  │           └── HeartbeatStaleness()    — 하트비트 모니터링
  │
  ├── 2. 실행
  │     ├── 메시지 목록 조합: forked_messages + HumanMessage(user_message)
  │     └── await asyncio.wait_for(child_agent.ainvoke(...), timeout)
  │
  ├── 3. 결과 추출
  │     └── ainvoke가 반환한 마지막 메시지에서 result_text 추출
  │
  └── 4. Finally (성공/실패 여부와 무관하게 항상 실행)
        ├── TimeoutError → outcome = TIMEOUT
        ├── Exception   → outcome = ERROR
        ├── complete_run(run_id, outcome, result_text)  — Registry 업데이트
        │     └── result_text는 24000자로 제한
        └── run_subagent_announce_flow(updated_run)      — Announce 트리거
```

### 3. Registry — 실행 상태 레지스트리

Registry는 전체 시스템의 상태 허브로, 모든 자식 에이전트 실행 기록의 라이프사이클을 관리합니다.

#### 저장 아키텍처

```
┌─────────────────────────────────────────────────┐
│  메모리 저장소 (registry/memory.py)              │
│  threading.Lock 보호 dict[str, SubagentRunRecord] │
│  ↓ 주기적 스냅샷                                 │
│  SQLite (registry/store_sqlite.py)              │
│  future_subagent/data/subagent_registry.db      │
│  테이블: subagent_runs(run_id PK, data JSON)     │
└─────────────────────────────────────────────────┘
```

- 메모리가 기본 저장소입니다. 모든 읽기/쓰기 연산은 인메모리 dict를 직접 대상으로 합니다
- SQLite는 영구 백업으로, Sweeper가 호출하는 `periodic_persist(interval=30s)`를 통해 스냅샷됩니다
- 시작 시 `init_registry()`가 SQLite에서 기존 기록을 복원합니다
- 단일 기록의 upsert/delete는 SQLite에 실시간으로 동기화됩니다

#### SubagentRunRecord 핵심 필드

| 카테고리 | 필드 | 설명 |
|----------|-------|-------------|
| **식별** | `run_id` | UUID, 고유 식별자 |
| | `task_run_id` | steer/restart에도 안정적인 ID |
| | `child_session_key` | `"agent:{agentId}:subagent:{uuid}"` |
| | `requester_session_key` | 부모 세션 키 |
| **생성 매개변수** | `spawn_mode` | RUN (일회성) / SESSION (영속) |
| | `context_mode` | ISOLATED / FORK |
| | `depth` | 중첩 깊이 |
| | `role` | MAIN / ORCHESTRATOR / LEAF |
| **소유권** | `completion_owner_session_key` | 완료 전달을 소유하는 세션 키 |
| | `spawned_by` | 생성을 시작한 주체 |
| | `spawned_cwd` | 생성 시점의 작업 디렉터리 |
| **범위** | `scopes` | 부여된 권한 범위 |
| | `inherited_tool_policy_version` | 상속된 도구 정책 버전 |
| **스키마** | `output_schema` | 구조화된 출력 검증용 JSON 스키마 |
| **실행** | `execution.status` | RUNNING → INTERRUPTED → TERMINAL |
| | `execution.outcome` | OK / ERROR / TIMEOUT / UNKNOWN |
| **전달** | `delivery.status` | PENDING → IN_PROGRESS → DELIVERED |
| | `delivery.attempt_count` | 전달 재시도 횟수 |
| **첨부 파일** | `attachments_dir` | 첨부 파일 디렉터리의 절대 경로 |
| | `attachments_root_dir` | 안전한 정리 검증을 위한 루트 디렉터리 |

### 4. 세 가지 핵심 상태 머신

#### 1. ExecutionState — 실행 상태 머신

```
    RUNNING ──────────────────► INTERRUPTED
      │                            │
      │ (완료/오류/타임아웃)        │ (재개)
      ▼                            │
    TERMINAL ◄─────────────────────┘
      ▲
      │ (재시작)
      └────────────────────────────
```

- `RUNNING`: 자식 에이전트 실행 중
- `INTERRUPTED`: yield/steer로 일시중지됨
- `TERMINAL`: 최종 상태 (완료/오류/타임아웃), 되돌릴 수 없음

#### 2. CompletionDeliveryState — 전달 상태 머신

```
    not_required ──(RUN 모드 스킵)──► delivered

    pending ──► in_progress ──► delivered
                    │
                    ├──(실패)──► failed ──(재시도)──► pending
                    │                               │
                    │    (재시도 소진 + 소프트 캡)     │
                    │                               ▼
                    └──(소프트 캡 초과)──► suspended ──► discarded
```

- `not_required`: SESSION 모드는 전달이 필요 없음
- `pending → in_progress → delivered`: 정상 전달 경로
- `failed → pending`: 지수 백오프 재시도 (1s, 2s, 4s; 최대 3회)
- `suspended → discarded`: 일시중지가 한도를 초과하면 폐기됨

#### 3. CleanupState — 정리 상태 머신

```
    registered ──► cleanup_handled ──► cleanup_completed_at
```

- `resolve_deferred_cleanup_decision()`가 세션 삭제 여부를 결정합니다
- cleanup="delete" AND 전달 완료/폐기/불필요 → 삭제
- 전달 일시중지/실패 → 유지
- 첨부 파일 정리는 심볼릭 링크 우회 보호가 있는 `safe_remove_attachments_dir()`를 사용합니다

### 5. Announce 파이프라인 — 결과 알림 및 전달

자식 에이전트가 완료된 후, Announce 파이프라인은 결과를 부모 에이전트에게 안정적으로 전달합니다.

```
자식 에이전트 실행 완료
  │
  └──► run_subagent_announce_flow(run)
         │
         ├── 사전 가드
         │     ├── execution.status != TERMINAL → 건너뜀
         │     ├── completion.required == False → 건너뜀
         │     └── delivery.status == DELIVERED → 건너뜀 (멱등성)
         │
          └──► deliver_subagent_announcement(run)
                │
                ├── 1. 프로세스 내 멱등성 확인
                │     └── _is_already_delivered(run) → 인메모리 세트 확인
                │         key = "subagent_announce:{run_id}:gen:{generation}"
                │         세트 캡 10K, 가득 차면 가장 오래된 5K 퇴출
                │
                ├── 2. 하드 캡 확인
                │     └── 대기 중인 하위 개수 ≥ hard_cap(50) → 즉시 SUSPENDED
                │
                ├── 3. 하위 확인
                │     └── 요청자에게 대기 중인 하위가 있을 때만 wake 전달
                │
                ├── 4. IN_PROGRESS 표시
                │
                ├── 5. 재시도 루프 (최대 3회)
                │     ├── _do_deliver(ctx)
                │     │     ├── InboundMessage 구성:
                │     │     │     channel = "system"
                │     │     │     sender_id = "subagent"
                │     │     │     chat_id = "direct"
                │     │     │     session_id = requester_session_key
                │     │     │     metadata.injected_event = "subagent_result"
                │     │     │     content = 포맷된 결과 (4K로 잘라냄)
                │     │     └── get_event_bus().publish_internal(msg)
                │     │     fire_delivery_target_hook() → 리다이렉트 허용
                │     │
                │     ├── 성공 → DELIVERED 표시 + 멱등성 키 기록 → 반환
                │     ├── 일시적 실패 → sleep [5s/10s/20s] → 재시도
                │     ├── 압축 오류 → sleep [1s/2s/4s/8s] → 재시도
                │     └── 영구 실패 → 재시도 없음
               │
                ├── 6. 재시도 소진
                │     ├── FAILED 표시
                │     └── 대기 중 개수 ≥ soft_cap(25) → SUSPENDED 표시
                │
                └── 7. 정리
                     └── cleanup="delete" → safe_remove_attachments_dir()
```

#### 전달 메시지 형식

```
**Subagent Result** [{label}]
Status: completed successfully / failed: {error} / timed out
Task: {task description}
Result:
{result_text, truncated at 4000 chars}
```

### 5.1 Swarm/Collect 모드 (v3)

Swarm 시스템은 FIFO 스케줄링과 동시성 제어로 하위 작업의 동시 배치 실행을 지원합니다:

```
configure_swarm_group(SwarmGroupConfig(group_id="g1", max_concurrent=3))
  │
  ├── reserve_swarm_run(group_id, task, requester)
  │     └── FIFO에 인큐 + state=RESERVED 설정
  │
  ├── activate_swarm_run(run_id)
  │     └── 디큐 + state=ACTIVE 설정 (max_concurrent 준수)
  │
  ├── complete_swarm_run(run_id, outcome)
  │     └── state=COMPLETED/FAILED 설정 + 다음 예약 자동 활성화
  │
  └── build_structured_output_prompt(output_schema)
        └── 구조화된 출력을 위한 JSON 스키마 프롬프트 생성

validate_structured_output(result_text, output_schema)
  │
  ├── result_text를 JSON으로 파싱
  ├── 필수 필드 존재 여부 확인
  ├── 필드 유형을 스키마와 대조 검증
  └── (is_valid, error_message) 반환

SwarmGroupConfig 필드: group_id, max_children_per_group (5), max_total_per_group (0=무제한), max_concurrent (3)

reserve_swarm_run(group_id, task, requester, launch_fingerprint=None)
  │
  ├── launch_fingerprint 제공 시 → _launch_fingerprints에서 멱등성 히트 확인
  └── 새 실행 → FIFO에 인큐 + state=RESERVED 설정

_pump_lane(group_id)
  │
  ├── max_concurrent에 대한 가용 슬롯 확인
  ├── 슬롯 사용 가능 시 예약된 실행 자동 활성화
  └── 활성화 시 _on_swarm_run_started 콜백 트리거

onStartFailure 처리:
  │
  ├── 실행 자동 실패 처리 (state=FAILED)
  └── 대기 중인 다음 예약 실행 자동 활성화
```

### 5.2 전달 이중 경로 라우팅 (v3)

Announce 전달은 이제 요청자 유형에 따라 라우팅됩니다:

```
deliver_subagent_announcement(run)
  │
  ├── 요청자가 subagent인 경우 → _deliver_internal_injection()
  │     ├── metadata.internal = True
  │     ├── 내용: "[Subagent Internal] {label}: {status}"
  │     └── 사용자 표시 출력 없음
  │
  └── 요청자가 사용자 세션인 경우 → _deliver_completion_message()
        ├── 검토 지시가 포함된 전체 마크다운 형식
        ├── 내용: "**[Subagent Task]** [{label}]..."
        └── "请审阅以上子 Agent 执行结果，如需进一步操作请指示。"
```

### 5.3 세대 보호 라이프사이클 및 킬 중재 (v3)

```
complete_subagent_run(run_id, outcome, expected_generation)
  │
  ├── TerminalGenerationTracker.is_callback_current()
  │     └── 오래된 세대 콜백 거부
  │
  ├── _arbitrate_kill_vs_completion(run, outcome)
  │     ├── kill_reconciliation 없음 → 통과
  │     ├── Kill + 결과 있는 Provider OK → Provider 승리
  │     └── Kill + 기타 outcome → Kill 승리
  │
  ├── _should_suspend_pending_final_delivery()
  │     └── cleanup="keep" + complete + ok + expects + PENDING → 일시중지
  │
  └── _start_announce_cleanup_flow()
        ├── SettleWakeBatch: IDLE → COMPLETING → SETTLED → DONE
        └── 세대 가드가 있는 지연 정리
```

### 5.4 킬 대상 상태 해석 및 가시성 (v3)

```
resolve_kill_target_state(run) → "killable" | "finalizing" | "terminal"

kill_subagent_run_with_cascade(run_id, cascade=True)
  │
  ├── 대상 상태 해석
  │     ├── "terminal" → 반환 (이미 완료됨)
  │     ├── "finalizing" → 1초 대기 후 재확인
  │     └── "killable" → 킬 수행
  │
  ├── 킬 조정 스냅샷 저장
  ├── 태스크 취소 + 세션 큐 비우기
  ├── cascade인 경우: 모든 자식을 재귀적으로 킬
  └── 모든 자식이 정착되면 부모 깨우기

is_subagent_run_visible_to_session(run, session_key)
  ├── controller_session_key 일치 → 표시
  ├── requester_session_key 일치 → 표시
  └── 그 외 → 표시 안 됨
```

### 6. 깊이 및 역할 시스템 — 계층적 제어

Subagent 시스템은 다중 레벨 중첩을 지원하며, 깊이와 역할을 통해 재귀 생성 기능을 제어합니다:

```
depth 0:  MAIN 에이전트
           ├── 자식 에이전트 생성 가능
           └── control_scope = CHILDREN

depth 1:  ORCHESTRATOR (max_depth > 1인 경우)
           ├── 자식 에이전트 계속 생성 가능
           └── control_scope = CHILDREN

depth 2:  ORCHESTRATOR (max_depth > 2인 경우)
           ├── 자식 에이전트 계속 생성 가능
           └── control_scope = CHILDREN

depth N:  LEAF (depth == max_spawn_depth)
           ├── 자식 에이전트 생성 불가
           └── control_scope = NONE
```

기본 `max_spawn_depth = 3`, 3단계 트리 구성: MAIN → ORCHESTRATOR → LEAF

**깊이 계산**: `requester_session_key`에서 부모 깊이를 추출합니다. 세션 키 형식 `"agent:{id}:subagent:{uuid}"`에서 `:subagent:`가 나타나는 수가 깊이입니다.

**도구 정책 연동**:
- LEAF 역할은 `DEFAULT_SUBAGENT_BLOCKED_TOOLS` (`sessions_spawn`, `sessions_yield`, `skill_manage`, `memory`)로 완전히 제한되어 `sessions_spawn` 호출 불가
- ORCHESTRATOR 역할은 `sessions_spawn`과 `sessions_yield` 자동 해제, 재귀 생성 활성화
- 이를 통해 중첩 깊이에 대한 하드 제약을 우회할 수 없음을 보장

### 7. 첨부 파일 시스템

생성 파이프라인은 파일 첨부를 자식 에이전트에 전달하는 것을 지원합니다:

```
materialize_subagent_attachments(attachments, child_workspace, ...)
  │
  ├── 1. 검증
  │     ├── 파일 이름: 경로 우회 없음, 제어 문자(C0+DEL) 없음, 예약 이름 없음, 중복 이름 없음
  │     ├── 개수 제한: 생성당 최대 50개 파일
  │     ├── 크기 제한: 파일당 1MB, 생성당 총 5MB
  │     └── mount_path 정화: 영숫자 + ._-/
  │
  ├── 2. 격리된 디렉터리로 쓰기
  │     └── <childWorkspace>/.openclaw/attachments/<uuid>/
  │
  ├── 3. 매니페스트 생성
  │     └── 파일 이름, 크기, SHA-256 해시가 있는 .manifest.json
  │
  └── 4. 시스템 프롬프트 접미사 반환
        └── "Attachments: N file(s), M bytes. Available at: .openclaw/attachments/<uuid>"
```

### 8. 백그라운드 데몬 메커니즘

#### Sweeper (레지스트리 스캐너)

```
registry/sweeper.py — 60초 간격 루프

각 스윕에서 실행:
  1. recover_orphaned_runs()       — 고아 실행 복구
  2. finalize_suspended_deliveries() — 일시중지된 전달 재시도/폐기
  3. persist_runs_to_disk()        — SQLite에 스냅샷
```

고아 기준: `RUNNING` AND `started_at`이 None이 아님 AND 경과 시간이 계층 임계값(cron=2h, subagent=6h, interactive=24h) 초과. Sweeper는 끼인(wedged) 실행을 건너뜁니다.

#### Followup (타임아웃 검사기)

```
followup/core.py — sweeper_interval * 2 간격 루프

각 검사에서 실행:
  1. 모든 실행 순회
  2. run_timeout_seconds를 초과하는 RUNNING 실행 찾기
  3. 강제 복구를 위해 recover_orphaned_runs() 호출
```

#### 고아 복구

```
orphan/recovery.py — run_id당 지연 스케줄링

각 고아 실행에 대해:
  1. delay_seconds 대기 (기본 120s)
  2. 아직 살아있고 종료되지 않았는지 확인
  3. reconcile_orphaned_run() → TERMINAL + TIMEOUT 표시
  4. run_subagent_announce_flow() 트리거 → 부모 에이전트에 타임아웃 결과 전달
```

중복 제거: 각 `run_id`는 최대 한 번만 복구가 예약됩니다.

### 9. LLM 도구 인터페이스

#### sessions_spawn — 자식 에이전트 생성

| 매개변수 | 유형 | 기본값 | 설명 |
|-----------|------|---------|-------------|
| `task` | str | 필수 | 작업 설명 |
| `task_name` | str\|None | None | 안정적인 별칭 |
| `label` | str\|None | None | 표시 레이블 |
| `agent_id` | str | "main" | 대상 에이전트 ID |
| `thinking` | str\|None | None | 사고 모드 재정의 |
| `mode` | str | "run" | "run" (일회성) / "session" (영속) |
| `cleanup` | str | "delete" | "delete" / "keep" |
| `context` | str | "isolated" | "isolated" / "fork" |
| `attachments` | list\|None | None | 파일 첨부 (name, content, encoding, mount_path) |

반환: `"Subagent spawned: status={status}, run_id={id}, session_key={key}"`

#### sessions_yield — 일시중지 및 대기

메인 에이전트에게 현재 턴을 종료하고 자식 결과를 기다리라고 신호합니다. 이는 **신호 도구**입니다 — 스레드를 차단하지 않고, 프레임워크에 현재 턴을 일시중지할 수 있음을 알립니다.

반환: `"Turn yielded. You will be resumed when subagent results arrive."`

#### sessions_send — 양방향 통신

| 매개변수 | 유형 | 설명 |
|-----------|------|-------------|
| `target_session_key` | str | 대상 자식 에이전트의 세션 키 |
| `message` | str | 메시지 내용 |
| `max_turns` | int | 최대 라운드 (기본 1) |

`get_event_bus().publish_internal()`을 통해 `metadata.injected_event = "subagent_message"`로 대상 메시지를 전달합니다.

#### agents_list — 사용 가능한 에이전트 목록

구성에서 `allow_agents` 화이트리스트를 반환합니다.

#### subagents_list — 자식 에이전트 상태 목록

현재 세션 아래의 활성 및 최근 자식 에이전트를 반환합니다:

```
Subagents: total=5, active=3, recent=2

Active:
  - [abc12345] research (depth=1, role=leaf, model=gpt-4, runtime=30s, pending=0)
  - [def67890] analysis (depth=1, role=leaf, model=gpt-4, runtime=2.5m, pending=0)
  - [ghi11223] writer (depth=1, role=orchestrator, model=gpt-4, runtime=1.2h, pending=2)

Recent:
  - [jkl44556] lookup status=ok runtime=45s
  - [mno77889] verify status=timeout runtime=5.0m
```

#### sessions_kill — 자식 에이전트 취소

| 매개변수 | 유형 | 기본값 | 설명 |
|-----------|------|---------|-------------|
| `run_id` | str | 필수 | 킬할 실행 ID |
| `reason` | str | "killed" | 킬 사유 |

실행 중인 자식 에이전트를 취소합니다. 컨트롤러 세션만 킬할 수 있습니다. 계단식 킬(모든 자식 재귀 킬)을 지원합니다. 킬 조정은 동시 완료와 중재합니다.

`kill_all_controlled_subagent_runs(requester_session_key)` — 한 호출로 세션의 킬 가능한 모든 자식을 킬합니다.

#### sessions_steer — 자식 에이전트 조정/재시작

| 매개변수 | 유형 | 기본값 | 설명 |
|-----------|------|---------|-------------|
| `run_id` | str | 필수 | 조정할 실행 ID |
| `new_instructions` | str | 필수 | 주입할 새 지시 |

실행 중인 자식 에이전트에 새 지시를 주입합니다. 실행은 `pause_reason="steer"`와 함께 INTERRUPTED 상태로 전환되고 `generation`이 증가합니다.

### 10. 훅 프로토콜

훅 메커니즘은 외부 코드가 자식 에이전트 라이프사이클 이벤트를 들을 수 있게 합니다:

```python
from future_subagent.hooks.base import register_start_hook, register_stop_hook
from future_subagent.hooks.progress import register_spawned_hook, register_ended_hook, register_delivery_target_hook

async def on_start(event: SubagentStartEvent):
    print(f"Subagent started: {event.child_session_key}")

async def on_stop(event: SubagentStopEvent):
    print(f"Subagent stopped: {event.child_status}")

async def on_delivery_target(run, target_session_key):
    return None  # 리다이렉트할 session_key를 반환하거나 None

register_start_hook(on_start)
register_stop_hook(on_stop)
register_delivery_target_hook(on_delivery_target)
```

| 이벤트 | 필드 |
|-------|--------|
| `SubagentStartEvent` | `parent_session_key`, `child_session_key`, `child_role`, `child_goal` |
| `SubagentStopEvent` | `parent_session_key`, `child_session_key`, `child_role`, `child_status`, `child_summary`, `duration_ms` |

훅은 등록 순서대로 순차 실행되며, 예외는 삼켜지고 흐름을 방해하지 않습니다.

### 11. 기존 시스템과의 공존

| 차원 | 기존 subagent (`agent/tools/subagent/`) | 새 subagent (`future_subagent/`) |
|-----------|---------------------------------------------|---------------------------|
| 도구 이름 | `subagent` | `sessions_spawn`, `sessions_yield`, `sessions_send`, `sessions_kill`, `sessions_steer`, `agents_list`, `subagents_list` |
| 관리자 | `SubagentManager` (싱글턴) | `SubagentRegistry` (dict + SQLite) |
| 자식 에이전트 | Commander + Worker (두 계층) | LangGraph 에이전트 직접 생성 |
| 깊이 | 단일 레벨 | 다중 레벨 중첩 (기본 3레벨) |
| 통신 | 단방향 반환 | 양방향 (`sessions_send`) |
| 동시성 | 제한적 | 최대 5개 병렬 |
| 영속성 | 메모리 | SQLite + 메모리 |
| 지식 그래프 | 예 (draft→distill→ingest) | 아직 없음 |
| 전달 채널 | MessageBus | EventBus (자체) |
| 미들웨어 | — | Summarization + IterationBudget + ToolGuardrails + ToolCallNormalize + HeartbeatStaleness |
| 유연성 | 낮음 (경직된 패턴) | 높음 (역할 기반) |

두 도구 세트는 충돌 없이 `_MAIN_TOOLS_BUILDERS`에 동시에 등록되어 있어 점진적 마이그레이션이 가능합니다.

### 12. 핵심 설계 결정

| 결정 | 선택 | 근거 |
|----------|--------|-----------|
| 자식 에이전트 실행 | `CompiledStateGraph.ainvoke()` | LangGraph 인프라 재사용, 네이티브 비동기 |
| 전달 채널 | 자체 `EventBus.publish_internal()` | 전역 MessageBus에서 분리, 독립적 진화 |
| 영속성 | aiosqlite만 (JSON 폴백 없음) | 이미 프로젝트 의존성, SQLite는 크로스플랫폼에서 안정적 |
| 샌드박스 | ACP 포트 없음 | 동일 프로세스 실행, 도구 거부 목록으로 권한 제어 |
| 수율 구현 | `asyncio.Event` + Registry 콜백 | Python에는 게이트웨이 스티어링이 없으며, Event가 그에 해당 |
| A2A 통신 | EventBus + 세션 키 라우팅 | 기존 메시징 메커니즘 재사용 |
| 공존 전략 | 독립 신규 모듈, 별도 도구 네임스페이스 | 기존 기능을 깨지 않고 점진적 마이그레이션 |
| 전체 포크 컨텍스트 | 체크포인터의 `agent.aget_state()` | 결정 9: 외부 `parent_messages` 파라미터 불필요 |
| 차단된 도구 | `sessions_spawn`, `sessions_yield`, `skill_manage`, `memory` | 재귀적 스폰 및 권한 상승 방지 |

---

## 구성

모든 구성은 `SubagentConfig` (Pydantic 모델, 싱글턴)로 관리됩니다:

| 파라미터 | 기본값 | 설명 |
|-----------|---------|-------------|
| `max_spawn_depth` | 3 | 최대 중첩 깊이 |
| `max_children_per_agent` | 5 | 에이전트당 최대 동시 자식 수 |
| `run_timeout_seconds` | 300.0 | 자식 에이전트 실행 타임아웃 |
| `require_agent_id` | False | agent_id 필수 여부 |
| `allow_agents` | `["*"]` | 허용 agent_id 화이트리스트 |
| `default_cleanup` | "delete" | 기본 정리 정책 |
| `default_context_mode` | ISOLATED | 기본 컨텍스트 모드 |
| `announce_retry_max` | 3 | 최대 전달 재시도 횟수 |
| `announce_retry_delay_base_ms` | 1000 | 지수 백오프 기준 (1s, 2s, 4s) |
| `delivery_suspend_soft_cap` | 25 | 전달 일시중지 소프트 임계값 |
| `delivery_suspend_hard_cap` | 50 | 전달 일시중지 하드 임계값 |
| `delivery_suspend_target` | 10 | 압력 가지치기 대상 수 |
| `lifecycle_grace_period_seconds` | 15.0 | 오류/타임아웃 확정 전 유예 기간 |
| `sweeper_interval_seconds` | 60 | 스위퍼 스캔 간격 |
| `orphan_recovery_delay_seconds` | 120 | 고아 복구 지연 |
| `announce_expiry_ms` | 7,200,000 | 전달 소프트 만료 (2h) |
| `announce_hard_expiry_ms` | 86,400,000 | 전달 하드 만료 (24h) |
| `max_announce_retry_count` | 10 | 최대 announce 재시도 횟수 |
| `stale_unended_threshold_seconds` | 7200 | 미종료 스테일 실행 임계값 |
| `recent_ended_window_seconds` | 1800 | 표시용 최근 종료 창 |
| `steer_rate_limit_ms` | 2000 | 스티어 레이트 제한 |
| `archive_after_minutes` | 1440 | 분 단위 자동 아카이브 |
| `attachments_enabled` | True | 첨부파일 허용 여부 |
| `attachments_max_files` | 50 | 스폰당 최대 파일 수 |
| `attachments_max_file_bytes` | 1MB | 단일 파일 최대 크기 |
| `attachments_max_total_bytes` | 5MB | 최대 총 첨부 크기 |

---

## 프로젝트 현황

**7개 단계 모두 완료 (2026-07-15). Robustness-plan-v3 개선 완료 (2026-07-22). 버그 수정 + OpenClaw 정렬 + 깊이 정렬 + 배선 수정 완료 (2026-07-23).** 203개 테스트 통과. 규칙은 [AGENTS.md](./AGENTS.md), 기술 결정은 [decisions.md](./docs/decisions.md)를 참조하세요.

