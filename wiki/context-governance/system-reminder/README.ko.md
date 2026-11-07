# 🧱 안정적인 시스템 프롬프트와 리마인더 층

[English](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> [Context Governance](../README.ko.md)의 일부: 시스템 프롬프트가 안정적 내용만 담고, 모든 라이브 뷰가 매 턴의 첫 모델 호출에서 `<system-reminder>` HumanMessage로 주입되는 이유.

---

시스템 프롬프트에는 **안정적** 내용만 들어갑니다: persona 파일, memory 블록, 스킬 인덱스. 모든 **라이브** 뷰 — 플랜 모드, 작업 디렉터리, 프로젝트 지시(`AGENTS.md`, 폴백 `CLAUDE.md`), todo 목록, 활성 작업 포인터, TaskFlow 보드, 연속성 컨텍스트 — 는 대신 각 턴의 첫 모델 호출에 `<system-reminder source="…">` HumanMessage로 주입됩니다(`DynamicReminderMiddleware` + `agent/middlewares/reminder_catalog/` 카탈로그). 가장 낮은 우선순위의 `current_time` 소스가 같은 블록에 매 턴 로컬 시계를 추가합니다. 영어 한 줄로, 예: `Current local time: 2026-11-06 09:41:07 (Friday) (CST, UTC+08:00)`. `transient=True`이므로 이전 턴의 줄은 제거되고 누적되지 않습니다. 또한 의도적으로 `only_if_changed`를 사용하지 않습니다 — 시계는 계속 진행되어야 하기 때문입니다. 이유는 두 가지입니다: 첫 토큰이 매 턴 바뀌는 프롬프트는 그 뒤 모든 것(persona 포함)의 prefix 캐시를 무효화합니다; 캐시 미스에서만 재구성되는 프롬프트는 다음 재구성까지 **오래된** todo 목록을 보여줍니다.

동결된 메모리 스냅샷에는 의도적인 예외가 하나 있습니다. 세션 모델 전환은 새로운 provider 캐시 네임스페이스로 들어가고, 사고 모드 전환은 동일한 prefix에서 `cache_read`가 0이 되는 것이 실측되었습니다——어느 쪽이든 캐시는 이미 차갑습니다. `ThinkingControlMiddleware`의 `before_model` 훅은 해당 턴의 `(thinking key, profile fingerprint, mapping fingerprint)`을 마지막 적용 값과 비교하고(그래프가 매 턴 재구성되므로 프로세스 수준에서 보관), 변경이 있으면 `system_prompt_injection` 전에 `MemoryStore` 스냅샷을 다시 로드하고 두 레지스터에서 `StateKey.SYSTEM_PROMPT`를 삭제합니다. 전환된 턴의 첫 모델 호출은 세션 중간에 기록된 메모리 항목(SQLite 백엔드. 이전 Markdown 파일은 부팅 시 한 번만 가져옴)을 봅니다. 첫 턴, 변경 없음, 재로드 실패는 캐시를 유지하며, 전송이 없으면 발화하지 않습니다.

FACTS.md는 전역이므로 디렉터리 전환이 메모리용 캐시 프롬프트를 무효화하지 않습니다. 같은 턴의 `project_instructions` 리마인더는 새 프로젝트의 `AGENTS.md`(폴백 `CLAUDE.md`)를 다시 읽고, `only_if_changed`로 내용이 달라질 때만 재주입합니다.

| 관심사 | 규칙 |
|---|---|
| 무엇을 주입하나 | builder가 텍스트를 돌려준 소스마다 리마인더 1건: 예산 순서는 높음→낮음, 전달 시 낮음→높음으로 뒤집어 최고 우선순위를 꼬리에 둡니다; `origin=<소스 id>`, `provenance="reminder_catalog"`, `internal=True` |
| 예산 | 호출마다 모든 소스 합계 `REMINDER_CATALOG["total_budget_chars"]`(30 000) — 초과 시 최저 우선순위를 통째로 버리고 문장 중간을 자르지 않습니다 |
| 스로틀 | 소스는 `cooldown_turns`(TaskFlow 보드 / boulder: 5)와 `only_if_changed`(동일 내용은 반복하지 않음)를 선언할 수 있습니다; 상태는 실제로 주입된 소스만 진행됩니다 |
| 압축 후 | 압축이 카탈로그를 리셋하므로 다음 턴에 모든 소스가 live state에서 재주입됩니다 — 리마인더 층은 기록의 생존에 의존하지 않습니다 |
| 이스케이프 | 페이로드 안에 중첩된 `<system-reminder>` 태그(todo 텍스트는 사용자 영향 가능)는 이스케이프됩니다. 그렇지 않으면 태그를 일찍 닫아 이 층을 사칭할 수 있습니다 |
| 턴 내 위치 | 턴의 첫 모델 호출에만 — TaskIntent와 같은 "마지막 메시지가 진짜 사용자 메시지" 판정. 인간 메시지를 훑는 스캐너(`split_into_turns`, HITL의 운영자 판정, 요약 라벨)는 `pub.func.message.system_reminder`를 통해 리마인더를 건너뜁니다 |

todo 목록에는 보호가 하나 더 있습니다: `PlanContinuationEnforcer`(`TodoContinuationEnforcer`에서 개명; 감독하는 "플랜" = todos + TaskFlow flows)는 `todowrite`를 호출하지 않은 턴 수를 세고, `TODOLIST_INFRA["todo_write_nudge_turns"]`(10)를 넘으면 계속 지시문 뒤에 `[REMINDER: TODO TRACKING]` 넛지를 덧붙입니다 — 스냅샷 기반 정체 감지가 볼 수 없는 유일한 형태입니다(TaskFlow를 진행하면 streak이 지워지고 todo 목록은 썩습니다).

## 📋 등록 소스

카탈로그는 9개 소스를 등록합니다(`agent/middlewares/reminder_catalog/sources.py::register_default_sources`). 모두 같은 채널(`CURRENT_TURN`)을 타며, `current_time`과 `memory_recall`을 제외하고 모두 `only_if_changed=True`를 가집니다——이전 주입과 **바이트 동일**한 페이로드는 쿨다운이 지났어도 재주입**되지 않습니다**. 카탈로그는**턴당 1회만, 첫 모델 호출에서** 해결됩니다(마지막 메시지가 진짜 사용자 메시지일 때). 재개 턴이 사용자 메시지보다 앞에 이미 블록을 가지고 있으면 재주입을 건너뜁니다.

| 소스(`source_id`) | 우선순위 | 예산(문자) | 쿨다운 | Transient | 주입 내용 |
|---|---|---|---|---|
| `memory_recall` | 88 | 4 000 | 3 턴 | No | 현재 요청과 관련된 의미 기억 히트(스위치가 켜져 있을 때만) |
| `plan_mode_block` | 85 | 2 000 | 0 —— 매 턴 | No | 플랜 모드 상태 / 지시(에이전트가 플랜 모드 중인지) |
| `todo_block` | 80 | 1 500 | 0 —— 매 턴 | No | 에이전트의 현재 todo / 작업 플랜 목록 |
| `taskflow_status` | 75 | 2 000 | 5 턴 | No | TaskFlow 보드 상태(스티어된 보드) |
| `boulder_status` | 70 | 1 000 | 5 턴 | No | Boulder / 활성 작업 포인터 |
| `continuity_context` | 65 | 1 000 | 0 —— 매 턴 | No | 턴 간 연속성 컨텍스트 |
| `project_instructions` | 62 | 8 000 | 0 —— 매 턴 | Yes | 프로젝트의 `AGENTS.md`(폴백 `CLAUDE.md`) |
| `project_dir_notice` | 60 | 500 | 0 —— 매 턴 | No | 작업 디렉터리 알림 |
| `current_time` | 10 | 120 | 0 —— 매 턴 | Yes | 로컬 시계(영어, 날짜·요일·초·UTC 오프셋) |

**빈도 읽는 법.** `cooldown_turns = 0`은*매 턴 대상*——이전 주입 이후 내용이 변하면 재주입됩니다; 내용이 거의 변하지 않는 소스(`project_instructions` 등)는 매 턴 대상이어도 주입은 드물고, 자주 변하는 소스(`todo_block` 등)는 대부분의 턴에 주입됩니다. `cooldown_turns = 5`(TaskFlow 보드, boulder)는*최대 5 턴에 1회*, 그리고 내용도 변했을 때만. 변하지 않은 보드는 대상이어도 5 턴 동안 조용합니다. 스로틀 관리 상태는 실제로 주입된 소스만 진행되며, 억제된 소스는 쿨다운을「헛으로 소비」하지 않습니다. `current_time`은 예외입니다: 내용에 초가 포함되고 `only_if_changed`가 꺼져 있어 매 턴 나타납니다.

**압축 필터링.** 각 source는 `compress_exclude`(기본 `True`)를 선언하고, `DynamicReminderMiddleware`가 이를 메시지 metadata에 기록하며, `_filter_summary_messages`가 요약 prompt를 만들기 전에 해당 reminder를 제거합니다. live view는 매 턴 다시 파생되므로 압축 전 스냅샷은 노이즈입니다. `continuity_context`만 `compress_exclude=False`를 설정하고 `[Continuity]`로 직렬화됩니다. 태그 도입 전에 주입된 오래된 reminder는 키가 없어 기존 동작을 유지합니다. 실제 사용자 턴은 `pub.func.message.classify.is_user_message`(`metadata.origin == "user"`, fail-closed)로 식별되므로 주입된 HumanMessage가 요약에서 `[User]`로 표시되는 일은 없습니다.

**턴 간 지속.** non-transient 리마인더는 트랜스크립트 내의 `HumanMessage`이며, 미들웨어의 whole-list rewrite가 후속 턴에서 `messages[:-1]`을 통해 재전송하므로, 압축으로 폐기될 때까지 컨텍스트 윈도우에 남습니다(압축 후 `reset_session`이 변화 감지 해시를 지우고, 다음 턴에 live state에서 재주입합니다). **Transient 소스**(`transient=True`)는 예외입니다: 재해결되지 않는 턴에는 rewrite가 `messages[:-1]`에서 오래된 리마인더를 제거하므로 `project_instructions`는 내용이 바뀐 턴(그리고 압축 후 첫 턴)에만 나타납니다. `current_time`은 매 턴 다시 해석되며, 이전 턴의 줄을 제거해 컨텍스트에 시계 줄이 항상 하나만 남습니다. 모델이 한 번 읽으면 충분하며 이후 모든 턴을 점유하지 않습니다.

**주입 게이트 체인.** 소스가 특정 턴에 주입되려면 다음 검사를 순서대로 모두 통과해야 합니다(`core.py`의 `resolve_injections` + `dynamic.py`의 미들웨어 게이트):

1. **카탈로그 활성** —— `REMINDER_CATALOG["enabled"]`가 `True`; 꺼져 있으면 아무것도 주입되지 않습니다.
2. **턴의 첫 모델 호출** —— 미들웨어는 마지막 메시지가 본물의(주입되지 않은) `HumanMessage`일 때만 발화; 턴 내의 도구 루프 호출과 이미 블록을 가진 resumed 턴은 건너뜁니다(`_already_injected_this_turn`).
3. **쿨다운 경과** —— `cooldown_turns > 0`이고 소스가 이전에 주입된 경우, 이전 주입에서 최소 N 턴 경과 필요; 미주입 소스는 즉시 통과합니다(turn 0부터 냉각하지 않습니다).
4. **builder가 내용 반환** —— builder 실행; `None`·공백·예외는 스킵(fail-open——깨진 1 소스가 층 전체를 죽이지 않음).
5. **내용 변화** —— `only_if_changed=True`인 경우, 내용의 SHA-256 해시를 마지막 주입 해시와 비교; 동일 내용은 스킵.
6. **총 예산** —— 생존 후보는 우선순위 내림차순 정렬; `REMINDER_CATALOG["total_budget_chars"]`(30 000)에 들어갈 때까지 최저 우선순위 소스를 통째로 버립니다. 버려진 소스는 표시된 것으로 **간주되지 않으며** 다음 턴에도 대상으로 남습니다.

**우선순위가 예산 순서.** `resolve_injections`는 우선순위 내림차순으로 정렬합니다; `REMINDER_CATALOG["total_budget_chars"]`(30 000) 상한 아래에서 최저 우선순위 소스를 통째로 버리고(중간을 자르지 않음), 나머지가 들어갈 때까지 반복합니다. 전달은 유지 세트를 뒤집어, 최고 우선순위 뷰(플랜 모드, 그리고 todo 목록)를 블록 꼬리, 턴의 사용자 메시지 바로 앞에 둡니다 — 라이브 상태가 먼저, 요청이 나중.

**도구 결과 보강**은 별도의 호출 단위 큐입니다: 도구 반환 후 `ToolMessage`에 조언을 추가합니다(JSON 도구는 `advisory` 필드, 일반 텍스트는 끝의 접미사). 이 턴 단위 리마인더 계층과 직교합니다 — [도구 결과 보강](../tool-result-enrichment/README.ko.md)을 참고하세요.

**본 카탈로그 외.** `TaskIntent` 스티어링, todo 계속 지시문, 워크스페이스 알림은 각각 고유 주입 경로를 유지하며 여기에 등록되지 않습니다; 카탈로그는 오버레이이고 마이그레이션은 단계적입니다.
**DelegationPressureGate** (`agent/middlewares/delegation_pressure/core.py`)는 warn / escalated 지시를 `<system-reminder source="delegation_pressure">` 형식과 표준 알림 metadata(`provenance="reminder_catalog"`)로 주입하지만, 카탈로그의 `resolve_injections`가 아니라 자체 `abefore_model` 경로를 사용합니다. 카탈로그는 턴의 첫 모델 호출(마지막 메시지가 실제 사용자 메시지일 때)에만 해석하지만, delegation pressure는 턴 중간(7 / 14회 도구 호출 이후, 이어지는 모델 호출)에 발생하므로 first-call-only 게이트가 막습니다. 개념적으로 최고 우선순위입니다: 발생 시 카탈로그는 no-op이고, 지시는 메시지 목록 끝, 생성 지점에 가장 가까운 위치에 추가됩니다. 도구는 계속 사용 가능하며 경고만 하고 차단하지 않습니다.
