# 경험 체계 아키텍처

[English](README.md) · [中文](README.zh.md) · [日本語](README.ja.md) · 한국어

이 문서는 경험 체계를 다룹니다: Agent가 실행 중에 **언제** 경험을 추출하고, **어떤 메커니즘으로** 추출하며, 경험이 **어디에** 기록되고, 생성된 스킬 라이브러리가 어떻게 유지되는지를 정리합니다. 라이프사이클에는 네 개의 추출 경로가 연결됩니다: 압축 시 memory review, 플랜이 턴 경계에서 완료될 때의 **스킬 리뷰**, 압축 후 todo fork, 그리고 메인 세션 삭제 시의 session-end salvage. 산출물은 네 개 저장소——MEMORY.md / USER.md / FACTS.md, `skills/auto/`, `todos.db`——와 감사용 원본 `handoff-tree.json`으로 들어갑니다. 아래 **Curator** 절은 스킬 리뷰의 기록 대상인 `skills/auto/`를 관리하는 백그라운드 패스를 기록합니다.

> **저장소 업데이트(2026-11-04):** 아래에서 MEMORY.md / USER.md / FACTS.md라고 부르는 세 메모리 출력은 이제 MesMemory `memory_entries` 테이블의 행(대상 `memory` / `user` / `facts`)입니다. 이전 Markdown 파일은 부팅 시 `memory_migration.py`가 한 번 가져와 `*.md.bak`으로 이름을 바꿉니다. 로더는 `MemoryStore.load_from_store()`이며, 각 행은 `origin_session`(출처)과 `usage_count` / `last_used`(사용 피드백)를 가집니다.

> 아래 모든 주장은 소스와 대조해 검증했습니다. 심볼 이름, 설정 키, 기본값, 경로는 모두 `agent/middlewares/`, `agent/tools/`, `config/features/` 코드에 실제로 존재합니다.

## 설계 원칙

1. **모든 추출은 기존 저장소를 확장합니다.** 산출물은 MEMORY.md / USER.md / FACTS.md, `skills/auto/`, `todos.db` 중 하나로 들어갑니다. 원본 handoff tree는 감사용이며 모델 대상 지식 저장소가 아닙니다. 병렬 저장소를 만드는 추출 경로는 없습니다.
2. **전면 fail-open.** 모든 트리거는 자신의 실패를 기록하고 삼킵니다. todo 저장소 손상, plan 파일 읽기 불가, LLM 호출 실패가 메인 대화 턴을 막거나 중단시키지 않습니다.
3. **턴 경로는 제로 블로킹.** 압축 후 todo fork와 압축 시점 백그라운드 리뷰는 모두 fire-and-forget 백그라운드 작업입니다. memory review와 스킬 리뷰는 BACKGROUND_REVIEW 레인에서 독립 자식 Agent로 실행되어 모델 호출을 절대 막지 않습니다.
4. **모든 추출 경로에 도구가 필요합니다.** memory review, 스킬 리뷰, todo fork는 모두 `create_agent` fork이며 각각 `memory`, `skill_manage`, `todowrite`를 통해 기록합니다. 독립적인 순수 추출 호출은 남아 있지 않습니다.
5. **플랜 완료는 스킬만 유지합니다.** 완료 경로는 knowledge도 FACTS도 쓰지 않습니다. 영속 산출물은 클래스 레벨 skill 변경 또는 judge가 승인한 no-op입니다.

## 트리거 × 메커니즘 × 기록 대상

| 트리거 | 메커니즘(fork agent 여부 / 호출 형태) | 기록 대상 |
|---|---|---|
| 압축마다 | memory review(`_execute_memory_review`): **버려질** 슬라이스만 **원문 그대로** 해당 턴이 사용한 모델 구성(`_build_session_model`)으로 보내고, 세션의 시스템 프롬프트와 도구 면도 재현해 프리픽스 캐시에 적중시킴; `BACKGROUND_REVIEW_CONFIG["memory_review_mode"] = "digest"`면 대신 접음(최근 24개는 원문, 그 이전은 한 줄로 절단) | `memory` 도구를 거쳐 MEMORY.md / USER.md / FACTS.md |
| 플랜 완료——턴 경계에서 모든 todo가 `completed` / `cancelled`(**압축 시점이 아님**) | 스킬 리뷰(`_run_skill_review`): 완료 훅(`PlanContinuationEnforcer` → `maybe_trigger_skill_review`)이 **보조 모델**로 전용 agent를 fork하고 `_SKILL_REVIEW_PROMPT`를 쓰며 **대화 기록을 전혀 넘기지 않음** | `skills/auto/`(클래스 레벨 스킬 + `<module>-notes`); judge가 승인한 no-op 허용; 원본 `handoff-tree.json`은 먼저 감사용으로 기록 |
| 압축 후(cut이 실제로 메시지를 버림) | todo fork(`update_todos_from_compaction`): fire-and-forget 백그라운드 리뷰 agent, `_COMPRESSION_TODO_PROMPT` | 메인 세션에 바인딩된 `todowrite` 심을 거쳐 `todos.db` |
| 메인 세션 삭제 시(purge 전) | session-end salvage(`salvage_session_memory`): checkpoint transcript를 스냅샷하고 모델/시스템 프롬프트/도구 면을 캡처하며, 일반 압축 컷을 **읽기 전용**으로 계산한 뒤 독립적인 2파 memory review를 예약한다 — 1파는 버려질 프리픽스, 2파는 memory 재로드와 프롬프트 재구축 후 보존 꼬리 | `memory` 도구를 거쳐 MEMORY.md / USER.md / FACTS.md |

## 트리거 상세

### 1. 압축 시점 memory review

`schedule_memory_review`(`agent/middlewares/summarization/background_reviews.py`, Summarization 미들웨어가 메시지를 실제로 버리는 compact마다 호출)는 압축마다 `_execute_memory_review(session_id, system_prompt, messages_to_summarize)`를 fire-and-forget 작업으로 디스패치하여 `background_review_memory_lock`(`state_register_mem`) 아래에서 실행합니다. 넘겨받는 것은 **버려질** 슬라이스——잃게 될 사실이 거기에 있고, 대화 전체의 일부일 뿐입니다——이며 기본 `cache_warm` 모드에서는 **원문 그대로** 모델로 전송됩니다(`BACKGROUND_REVIEW_CONFIG["memory_review_mode"] = "digest"`일 때만 `digest_history`로 접음). 둘 중 하나의 리뷰 락이 잡혀 있으면 압축은 디스패치를 완전히 건너뜁니다(큐잉 없음).

`_execute_memory_review`(`agent/middlewares/summarization/background_reviews.py`)는 `_create_memory_review_agent`로 리뷰 agent를 만들고, 렌더링된 `_MEMORY_REVIEW_PROMPT`를 `HumanMessage`로 버려질 슬라이스에 덧붙여 호출합니다. **구조적으로 캐시 온존:** 압축 절단은 프리픽스 절단이고, 버려질 슬라이스는 시스템 프롬프트 바로 뒤에 위치하는 부분 그 자체입니다——같은 모델 구성(`_build_session_model`: 세션의 provider/model/thinking/temperature. 그래프는 턴마다 재구축되고 클라이언트가 닫히므로 구성에서 **새 인스턴스**를 구축), 같은 시스템 프롬프트, 같은 도구 면으로 되돌려 보내면 메인 모델 자신의 프리픽스를 재현하고 제공자는 프리픽스 캐시에서 응답합니다; 새 입력은 리뷰 지시뿐이고 리뷰가 읽는 것은 **실제 원문 그대로**입니다. 프롬프트는 지속적인 사용자 특성(persona, 선호, 개인 정보)과 행동 기대치를 `user` 대상에, 시스템 수준 / 프로젝트 횡단 함정을 `facts` 대상에(프로젝트별 사실은 MEMORY.md 또는 AGENTS.md에) `memory` 도구로 저장하라고 요구합니다. 렌더링 시 현재 FACTS.md 내용과 문자 상한(`_render_prompt_facts`)을 함께 전달하고, 편집자처럼 유지할 것——같은 함정을 병합하고, 오래된 항목을 교체하며, 파일을 부풀리느니 아무것도 저장하지 않기를 우선할 것——을 지시합니다. 저장할 것이 없으면 "Nothing to save."라고 답하고 멈춥니다.

- 리뷰 agent는 세션 자체 모델(`_build_session_model`, 프리픽스 캐시 재사용) 위의 별도 `create_agent`이며, 미들웨어는 `[_BackgroundReviewLimitTool(), ToolCallNormalize(), ToolGuardrails(), IterationBudget(90)]`, checkpointer 없음.
- `_BackgroundReviewLimitTool`(`allowed_metadata_key` 미지정)은 metadata에 `background_review: True`가 있는 도구만 통과시킵니다. `memory`가 이 마커를 가지므로(대화형 `question`과 플랜 모드 두 도구 포함) memory review는 memory를 쓸 수 있지만 임의의 메인 도구는 호출할 수 없습니다. 스킬 3종(`skill_list` / `skill_view` / `skill_manage`)은 **의도적으로 미마커**——스킬 추출은 플랜 완료 패스의 담당이며, 그쪽은 마커 대신 명시적인 도구 면을 바인딩합니다.
- fork의 메시지는 로그만 남깁니다. `res["messages"]`의 어떤 내용도 메인 그래프로 들어가지 않습니다.

### 2. 스킬 리뷰(todo 완료, 턴 경계)

`_detect_todo_all_complete(session_id)`는 플랜이 완료될 수 있는 각 턴에서 평가되고 완료 사이클마다 한 번 발화합니다: todos가 존재하고 모두 `completed` / `cancelled`이며 `skill_review_fired`가 설정되지 않음. fired 플래그를 세우고 목록이 불완전해지면 리셋합니다. 읽기는 fail-open이며, 완료 훅이 매 턴 확인하므로 짧은 세션도 추출됩니다.

플래그만으로는 completed todo의 in-place 편집을 볼 수 없으므로 `_plan_snapshot_hash`(id + content + status + plan_ref)를 마지막 **성공** pass의 `skill_review_snapshot_hash`와 비교합니다. 내용이 바뀌면 다시 추출하고, 그대로면 건너뛰며, 실패 pass는 cooldown 후 재시도합니다.

`skill_review_enabled`와 `skill_review_enabled`가 켜져 있으면 hook이 BACKGROUND_REVIEW 레인에서 `_run_skill_review`을 fire-and-forget으로 실행합니다:

1. `_build_plan_context`는 plan 파일, todos, 자식 Agent runs(result_text 24 KB 제한), 그리고 제한된 `feedback`(최근 메시지의 `skill_view` 호출 + `.usage.json` 최근 항목 = `loaded_skills`, 최근 턴의 사용자 수정 = `user_corrections`)을 모읍니다. internal carrier, system reminder, workspace notice는 제외합니다. todo가 없으면 `{}`를 반환해 pass를 건너뜁니다.
2. LLM 전에 원본 merged handoff tree를 `handoff-tree.json`에 원자적으로 써서 감사용으로 남깁니다. 모델 대상 지식 읽기 경로가 아닙니다.
3. `_SKILL_REVIEW_PROMPT`를 압축 컨텍스트로 렌더링해 전용 agent(보조 모델, temperature=0, **대화 없음**)에 넘깁니다. 도구 면은 `skill_list` / `skill_view` / `skill_manage`만; `knowledge`와 `memory`는 없어 지식이나 FACTS를 쓸 수 없습니다.
4. Prompt는 증거를 명시적으로 매핑합니다: `success_steps` → 절차/검증, `failed_branches` → pitfalls / `when_not_to_use`, `hard_constraints` → guardrails, `locked_decisions` → rationale, `user_corrections` → body / `when_to_use`. 이번에 로드한 skill, 기존 class-level umbrella, support file, 마지막으로 새 class-level skill 순서. 구체적 이유가 있는 no-op만 허용합니다.

구현: `summarization/background_reviews.py`의 `_run_skill_review`이 `_create_skill_review_agent`를 `skill_review_lock` 아래에서 사용합니다. `state_register_db`가 지문을 저장하고, `outcome`은 subagent runs에서 오며, 실패 플래그는 `False`입니다.

스킬 리뷰는 snapshot/judge 트랜잭션으로 실행됩니다:

- **snapshot**: `skill_review.snapshot_auto_skills()`가 첫 시도 전에 `skills/auto`(skill, support file, usage sidecar)를 캡처합니다.
- **write**: 리뷰가 skill을 생성/수정합니다. `set_current_write_origin("background_review")`로 새 skill이 agent-created로 표시되어 curator 관리 대상이 됩니다.
- **judge**: `skill_review.judge_skill_update()`가 보조 모델에 `verdict`, `grounded_in_run`, `reusable`, `trigger_quality`, `quality`, `no_op_justified`, `feedback`을 요청합니다. `quality >= BACKGROUND_REVIEW_CONFIG["skill_review_quality_threshold"]`, `trigger_quality >= 6`, grounded, reusable이 통과 조건이며 no-op은 `no_op_justified`가 필요합니다.
- **retry/rollback**: 거부된 시도는 pre-review snapshot으로 복원하고 judge feedback과 함께 재시도합니다(기본 최대 2회). 소진되면 다시 복원하고 `skill_review_failed_hash` + 시각을 기록하며 성공 fingerprint를 진행하지 않습니다. 이후 턴에서 `skill_review_failure_cooldown_s` 후 재시도할 수 있고, plan이 바뀌면 즉시 재시도합니다.
- **선택적 PTC 컴파일(통과 후)**: `PTC["ptc_prefill_enabled"]`가 켜져 있으면 같은 백그라운드 작업이 성공한 TaskFlow step마다 `scripts/ptc__<plan_key>__<step_id>.py`를 업데이트된 skill에 컴파일합니다. best-effort이며 judge를 다시 실행하지 않고 승인된 skill을 롤백하지 않습니다.

이 경로는 knowledge(`task-*.json` / `wave-*.json` / `plan-summary.json`)도 FACTS도 쓰지 않습니다. 기존 knowledge 파일은 디스크에 남지만 legacy이며, 모델 대상 prompt block과 `knowledge` 도구는 retired입니다.

### 3. 압축 후 todo fork

같은 압축 지점에서 `_schedule_compression_todo_update`(`summarization/core.py` -> `background_reviews.schedule_compression_todo_update`)가 `update_todos_from_compaction`을 fire-and-forget `asyncio.create_task`로 예약합니다. 스케줄러는 **모두** 만족해야 합니다:

- `compression_todo_update_enabled`(`SUMMARIZATION`, 기본 `True`);
- cut이 실제로 메시지를 버렸음;
- 해당 세션에 `compression_todo_update_lock`이 잡혀 있지 않음;
- 세션에 비어 있지 않은 todo 목록이 있음;
- 실행 중인 이벤트 루프가 존재함(동기 압축 경로는 건너뛰고 debug 로그를 남김).

`update_todos_from_compaction`은 시스템 프롬프트가 `_COMPRESSION_TODO_PROMPT`이고 도구가 정확히 하나인 백그라운드 리뷰 agent를 실행합니다: `_build_main_session_todowrite(session_id)`. fork는 현재 todo 목록을 버려진 슬라이스와 대조하여, 실제로 끝난 항목을 `completed`, 포기되거나 대체된 항목을 `cancelled`로 표시하고, 증거가 있는 새 작업을 `pending`으로 추가하며, **전체** 목록을 한 번의 `todowrite` 호출로 되씁니다(델타가 아니라 전체 교체). 변화가 없으면 그대로 되씁니다.

fork의 결과 메시지는 로그만 남깁니다. 메인 그래프나 그 checkpointer에 아무것도 도달하지 않으며, 파생 세션의 미들웨어 상태는 `finally`에서 정리됩니다.

### 4. 세션 삭제(메인만)

`purge_session`이 메인 세션을 파기하기 전에 `salvage_session_memory`가 압축 후 checkpoint transcript, 모델, 시스템 프롬프트, 도구 면을 스냅샷하고, 일반 압축 컷을 **읽기 전용**으로 계산한 뒤 독립적인 백그라운드 2파 review를 예약합니다. 삭제 요청은 모델을 기다리지 않습니다. 동기 단계는 스냅샷뿐이며 예산은 5초입니다. 사이드 채팅과 서브에이전트 자식은 이 경로에 들어오지 않습니다.

- **1파**는 `messages[:cutoff]`——압축이 버렸을 프리픽스——를 캡처한 모델, 시스템 프롬프트, 도구 면으로 처리합니다. 압축 시 review와 같은 cache-warm 형태이며 요약을 만들지 않고 압축 상태도 건드리지 않습니다.
- **2파**는 `messages[cutoff:]`——보존되는 꼬리——를 처리합니다. 1파가 memory를 쓴 뒤 작업은 `MEMORY.md`/`USER.md`/`FACTS.md`를 다시 읽고 시스템 프롬프트를 비영속적으로 재구축하며(세션은 삭제됨), `memory` 전용 도구 면으로 꼬리를 review합니다. 재구축 실패 시 캡처한 프롬프트로 폴백합니다.
- 두 파 모두 BACKGROUND_REVIEW 레인에서 파별 파생 세션 키(`<session_id>::session-end-salvage:1|2`)와 checkpointer 없이 실행됩니다. 키는 `finally`에서 정리됩니다. `memory` 도구의 `background_review_memory` metadata가 실행 게이트이므로 1파는 cache-warm을 위해 세션 도구 면을 재현할 수 있지만 삭제 후 `knowledge`나 plan 도구를 호출할 수는 없습니다.
- 스냅샷, 프롬프트 재구축, 타임아웃, 모델 예외 등 모든 실패는 기록하고 삼킵니다. 프로세스 종료는 미완료 백그라운드 review를 포기할 수 있으며 이는 즉시 삭제의 수용된 트레이드오프입니다. 설정: `BACKGROUND_REVIEW_CONFIG["session_end_review_enabled"]`와 `BACKGROUND_REVIEW_CONFIG["session_end_review_timeout_s"]`(파별, 기본 120초).

## Curator(스킬 큐레이션)

**frontmatter 계약(인덱스 자체가 트리거).** `<available_skills>`는 스킬의 frontmatter만 렌더합니다 — 본문은 로드 후에 읽힙니다 — 따라서 이 필드들이 라우팅 신호입니다: `description`(능력 요약), `when_to_use`(**필수**: 이 경로들이 만들거나 고치는 모든 스킬에, 그것을 로드해야 하는 구체적 시나리오/사용자 의도를 명시), `when_not_to_use`(선택: 제외 조건과 대안). 로더는 두 태그를 CJK 인식 토큰 예산(60 / 50 / 50 토큰)으로 렌더하고 XML 이스케이프합니다. 클라이언트 스킬 툴팁도 같은 세 필드를 보여줍니다. 큐레이터는 더 좁은 두 번째 층을 더합니다: 엄브렐라의 frontmatter가 스킬 전체를 덮고, 각 `##` 하위 능력 섹션이 자체 `**When to use:**` 줄로 시작합니다(실제 제외가 있을 때만 `**When NOT to use:**`도). 그래서 모델은 `skill_view` 후 올바른 섹션을 고를 수 있습니다. 결정적 폴백도 같은 모양을 쓰며 조건은 병합 이유에서 추출합니다.

Curator(`context_engine/curator/`)는 `skills/auto/` 스킬 라이브러리의 라이프사이클을 담당하는 백그라운드 패스이며, 바로 위의 추출 경로 2가 기록하는 대상입니다. 스킬만 읽고 씁니다. MEMORY.md / USER.md / FACTS.md와 `todos.db`는 범위 밖입니다.

**무엇인가.** 정기 cron이 아니라 유휴 트리거 오케스트레이터입니다. 서비스 엔트리포인트가 `context_engine.curator.init()`으로 데몬 스레드(`curator-timer`)를 시작합니다(`server/__main__.py:140`; HTTP-only 모드에서는 건너뜀, `server/__main__.py:66-73`). 스레드는 3600초마다 깨어나 `maybe_run_curator(idle_for_seconds=...)`를 호출합니다(`context_engine/curator/__init__.py:122-140`). 패키지 임포트는 부수 효과가 없으며 스레드를 시작하는 것은 `init()`뿐입니다(`context_engine/curator/__init__.py:151-165`).

**언제 실행되는가.** 두 게이트를 모두 통과할 때만 리뷰가 실행됩니다:

- `should_run_now()`——활성화됨, 일시정지 아님, `last_run_at`이 유효 간격을 초과(`context_engine/curator/transitions.py:21-39`). 유효 간격은 `curator.interval_hours`(기본 168시간 / 7일, `config/sherry_settings.py:43`)이며, 클라이언트에서 `.curator_state`의 `auto_interval_days`로 1~5일로 덮어쓸 수 있습니다(`context_engine/curator/config.py:97-107`);
- Agent 유휴 시간이 `curator.min_idle_hours`(기본 2) 이상(`context_engine/curator/orchestrator.py:108-115`). 모든 사용자 턴마다 이 유휴 타이머가 리셋됩니다(`server/service/messages.py:190`).

UI에서는 `POST /curator/run`으로 강제 실행할 수 있으며, 워커 스레드에서 `run_curator_review()`를 호출합니다(`server/trigger/http/curator.py:129-160`). 아카이브는 바로 옆에 노출됩니다: `POST /curator/restore`(body `{"name": "<skill>"}` → `{success, message}`; 알 수 없는 이름 / bundled/hub 가림 / 대상 존재는 HTTP 400)와 `GET /curator/archived`(`{success, archived, count}`)이며, 둘 다 curator 계층 `restore_skill()` / `list_archived()`가 받칩니다(`server/trigger/http/curator.py:163-204`).

**라이프사이클 규칙.** `apply_automatic_transitions()`(`context_engine/curator/transitions.py:41-91`)는 모든 `skills/auto/**/SKILL.md`를 순회하며(`context_engine/curator/usage.py:161-180`) 각 스킬에 대해: pinned는 건너뜀; `stale_after_days`(기본 30일) 동안 활동이 없으면 `stale`로 표시; `archive_after_days`(기본 90일)를 넘기면 `skills/.archive/`로 **아카이브**되고 `archived`로 표시; 다시 활동이 있거나 stale 창 안에서 한 번도 사용되지 않은 스킬은 재활성화합니다. **세 가지 제거 모두 복구 가능**: 90일 전이, 통합 소스(아카이브 시 umbrella 이름을 담은 `ABSORBED_INTO` 마커 기록), 정리된 스킬은 모두 `skills/.archive/`로 이동하며 `curator restore <name>`으로 되돌릴 수 있습니다. 복구는 같은 단계에서 curator 측 레코드를 `active`로 되돌리고(agent 측 포워더가 agent 텔레메트리 레코드도 동기화), 복구된 스킬은 즉시 `apply_automatic_transitions`에 재진입해 네 분기 어디에도 해당하지 않는 상태로 남지 않습니다; 같은 초의 아카이브 충돌은 중첩되지 않는 평면 형제 디렉터리(`<skill>-<timestamp>`)가 됩니다. 되돌릴 수 없이 삭제하는 것은 명시적 삭제——`skill_manage(action="delete")` 또는 클라이언트 삭제 엔드포인트——뿐입니다. 아카이브는 자동으로 비워지지 않습니다——`curator restore`로 복원하거나 직접 정리하세요. 기본값은 `config/sherry_settings.py:42-48`.

**LLM 통합.** `curator.consolidate`가 켜져 있으면(기본 켜짐), `run_curator_review()`가 비-pinned 스킬 후보 목록을 렌더링하고(`context_engine/curator/review.py:69`) 메인 LLM(temperature 0.3)에게 겹치는 좁은 스킬들을 클래스 레벨 umbrella 스킬로 병합하도록 요청합니다(`CURATOR_REVIEW_PROMPT`, `context_engine/curator/review.py:16`). 새 umbrella와 지원 파일은 `skills/auto/` 아래에 생성·영속화됩니다(`_generate_umbrella_skill`, `context_engine/curator/umbrella.py:86`; `_apply_consolidation`, `context_engine/curator/orchestrator.py:163`). 통합은 이 패스의 유일한 LLM 단계이며, 실패는 잡히고 실행은 계속됩니다.

**네 개 추출 경로와의 관계.** 경로 2(압축 시점 skill review)가 생산자입니다: `skill_manage`를 통해 `skills/auto/`를 생성·패치합니다. Curator는 바로 그 산출물의 하류 유지보수자로, skill review이 만든 스킬을 전이·통합·정리합니다. 나머지 세 경로는 `skills/auto/`를 전혀 건드리지 않으므로(각각 MEMORY.md / USER.md / FACTS.md, `todos.db`, 삭제 시 memory salvage에 씀) Curator와 교차하지 않습니다.

**상태와 경계.** 실행 상태는 `skills/.curator_state`(`context_engine/curator/constants.py:3`, `context_engine/curator/state.py`가 읽고 씀); 각 실행은 `logs/curator/{timestamp}/` 아래에 `run.json` + `REPORT.md`를 씁니다(`context_engine/curator/constants.py:4`; `context_engine/curator/report.py:196-205`). 범위는 `skills/auto/`뿐——내장 스킬은 절대 건드리지 않고, pinned 스킬은 모든 파괴적 전이를 우회하며, 패스 전체가 백그라운드 스레드에서 실행되어 대화 턴 경로를 차지하지 않습니다. 전체 세부사항: [curator/README.md](../../context_engine/curator/README.ko.md).

## 격리와 안전

| 가드 | 보호 대상 |
|---|---|
| skill-review 도구 면 | `skill_list` / `skill_view` / `skill_manage`만 바인딩하며 knowledge/memory는 없습니다. |
| 백그라운드 리뷰 metadata 허용 목록(`metadata["background_review"]`, `_BackgroundReviewLimitTool` 기본 규칙) | memory review와 skill review는 background_review용으로 태그된 도구만 호출할 수 있습니다. 다른 모든 메인 도구는 오류 `ToolMessage`를 반환하고 실행되지 않습니다. |
| 압축 metadata 허용 목록(`metadata["todo_update"] is True`, `_BackgroundReviewLimitTool(allowed_metadata_key="todo_update")`) | 압축 fork는 해당 metadata 마커가 붙은 `todowrite` 심만 허용합니다. |
| 파생 세션 키 `<id>::compression-todo` | 압축 fork의 `IterationBudget` / `ToolGuardrails` / `ToolCallNormalize` 상태 키가 메인 세션과 충돌하지 않습니다. fork가 도는 동안 메인 세션은 `awrap_model_call` 중이기 때문입니다. `compression_todo_update_lock`만 의도적으로 메인 세션에 기록되어 교차 경로 재진입 조정자 역할을 합니다. |
| 파생 세션 키 `<id>::session-end-salvage:1|2` | 삭제 후 2파 review는 파별 파생 키로 실행되고 엔진 상태는 `finally`에서 정리되어 삭제된 세션 state를 되살리지 않습니다. 작업 집합 `_SESSION_END_REVIEW_TASKS`가 강한 참조를 유지합니다. |
| `background_review_memory` 실행 게이트(`metadata["background_review_memory"] is True`, `_BackgroundReviewLimitTool(allowed_metadata_key="background_review_memory")`) | 1파는 cache-warm을 위해 세션 도구 면을 재현할 수 있지만 실행 가능한 것은 `memory`뿐입니다. 삭제 후 `knowledge`와 plan 도구는 거부됩니다. |
| 메인 세션 바인딩 `todowrite` 심 | fork 그래프는 파생 키로 돌기 때문에, 상태 주입된 실제 `todowrite`는 잘못된 세션을 해석합니다. 심은 실제 도구의 `args_schema`와 `description`을 그대로 재사용하고(스키마 드리프트 제로), 주입된 `session_id`를 버리며, 빌드 시점에 캡처한 메인 세션 id를 바인딩합니다. |
| 읽기 전용 fork, checkpointer 없음 | 각 백그라운드 리뷰 / 스킬 리뷰 fork의 결과 메시지는 로그만 남기고 버립니다. fork에는 checkpointer가 없어 메인 그래프 상태를 쓸 수 없습니다. |
| 세션별 재진입 락 | `background_review_memory_lock`, `skill_review_lock`, `compression_todo_update_lock`이 같은 추출 경로의 중복 실행을 막습니다. 리뷰 락이 잡혀 있는 동안 압축 스케줄러는 디스패치를 완전히 건너뜁니다(큐잉 없음). |
| fail-open 경계 | 각 경로는 `try/except`로 작업을 감싸고, 기록하고 반환합니다. 어떤 추출 실패도 턴, 압축, 다른 추출로 전파되지 않습니다. |
| 백그라운드 작업 참조 유지 | `_COMPRESSION_TODO_TASKS`와 `_SESSION_END_REVIEW_TASKS` 집합이 강한 참조를 유지해 asyncio가 진행 중 작업을 GC하지 못하게 합니다. |

## 저장소와 상한

| 저장소 | 경로(상수) | 상한 |
|---|---|---|
| `memory` target | MesMemory `memory_entries` 행(`target='memory'`) | 2200자(`MEMORY_TOOL["memory_char_limit"]`, `/memory/settings`에서 재정의 가능) |
| `user` target | MesMemory `memory_entries` 행(`target='user'`) | 1375자(`MEMORY_TOOL["user_char_limit"]`, 재정의 가능) |
| `facts` target | MesMemory `memory_entries` 행(`target='facts'`, 유일한 전역 대상) | 1375자(`MEMORY_TOOL["facts_char_limit"]`, 재정의 가능); `add`가 상한을 넘으면 가장 적게 사용된 항목부터 보관 |
| legacy plan knowledge | `workspace/knowledge/plans/<plan_key>/` | 폐기: 이전 `task-<n>.json` / `wave-<n>.json` / `plan-summary.json`은 디스크에 남지만 모델 대상 경로는 읽지도 쓰지도 않음 |
| handoff audit | `workspace/knowledge/plans/<plan_key>/handoff-tree.json` | 리뷰 전에 쓰는 원본 handoff tree; prompt block 아님 |
| 스킬 | `config.path.AUTO_SKILLS_DIR`(`skills/auto/`) | `SKILL.md`와 `references/` / `templates/` / `scripts/` 지원 파일; PTC는 `ptc__<plan_key>__<step_id>.py`, LDW는 `ldw__<plan_key>.py` |
| 스킬 | `config.path.AUTO_SKILLS_DIR`(`skills/auto/`) | `SKILL.md`와 `references/`, `templates/`, `scripts/` 지원 파일 |
| todos | `agent/tools/todolist/data/todos.db`(`store_sqlite._DB_PATH`) | 세션 범위 목록, 전체 교체 쓰기 |

## 설정 스위치

| 키 | 위치 | 기본값 | 효과 |
|---|---|---|---|
| `compression_todo_update_enabled` | `SUMMARIZATION`(`config/features/agent_side/summarization.py`) | `True` | 압축 후 todo fork 활성화 |
| `skill_review_enabled` | `BACKGROUND_REVIEW_CONFIG`(`config/features/agent_side/background_review.py`) | `True` | 스킬 리뷰 마스터 스위치(skill-only, knowledge 미기록) |
| `skill_review_max_attempts` | `BACKGROUND_REVIEW_CONFIG` | `2` | 최초 + judge feedback 재시도 최대 1회 |
| `skill_review_quality_threshold` | `BACKGROUND_REVIEW_CONFIG` | `6` | 통과 최소 `quality` |
| `skill_review_feedback_turns` / `_max_items` / `_chars` | `BACKGROUND_REVIEW_CONFIG` | `12` / `8` / `2000` | 제한된 사용자 수정 컨텍스트 |
| `skill_review_loaded_skills_max` | `BACKGROUND_REVIEW_CONFIG` | `20` | loaded-skill 목록 상한 |
| `skill_review_failure_cooldown_s` | `BACKGROUND_REVIEW_CONFIG` | `3600` | 동일 실패 plan snapshot 재시도 쿨다운 |
| `session_end_review_enabled` | `BACKGROUND_REVIEW_CONFIG` | `True` | 메인 세션 삭제 시 salvage 활성화 |
| `session_end_review_timeout_s` | `BACKGROUND_REVIEW_CONFIG` | `120` | 삭제 salvage의 파별 모델 호출 예산 |
| `compaction_cooldown_rounds` | `SUMMARIZATION` | `3` | 실제 압축 후 능동 압축 쿨다운 |
| `memory_char_limit` / `user_char_limit` / `facts_char_limit` | `MEMORY_TOOL`(`config/features/agent_side/memory_tool.py`) | `2200` / `1375` / `1375` | MEMORY.md / USER.md / FACTS.md 상한 |

## 검증

대상 테스트:

검증 메모: Persistence assertions live in `tests/agent/middlewares/message_persistence/`. `skill_view` 로드 기록, `test_skill_review.py`, `test_skill_review_feedback.py`, `test_skill_review_gate.py` 추가.



```bash
uv run pytest \
    tests/agent/middlewares/test_session_end_review.py \
    tests/agent/middlewares/test_compression_todo_update.py \
    tests/agent/middlewares/test_compression_background_reviews.py \
    tests/agent/middlewares/test_compression_cooldown_persist.py \
    tests/agent/middlewares/system_prompt/test_skill_review.py \
    tests/agent/middlewares/test_skill_review_module_notes.py \
    tests/agent/middlewares/test_skill_review_gate.py \
    tests/agent/middlewares/test_skill_review_feedback.py \
    tests/agent/middlewares/test_skill_review.py \
    tests/agent/tools/test_memory_store.py \
    tests/agent/tools/test_memory_store_facts.py -q
```

- `test_session_end_review.py`: 삭제 스냅샷, 읽기 전용 압축 컷, 1파 → 프롬프트 재구축 → 2파 순서, cutoff/빈 입력/스위치/중복/실패 분기, 파생 상태 정리, 두 파가 MEMORY.md/USER.md에 도달하는지 검증합니다.
- `test_compression_todo_update.py`: 트리거 게이트, fire-and-forget 예약, 재진입 락, fail-open 해제, 프롬프트 내용, `todo_update` metadata 게이트, 그리고 완전한 fork 격리(파생 키, 메인 세션 `todowrite` 심, checkpointer / 메시지 누출 없음).
- `test_compression_background_reviews.py`: 압축 시점 디스패치(memory review가 compact 접점에서 발화; 컷 없는 압축은 아무것도 디스패치하지 않음). `test_memory_review_digest.py`가 버려질 슬라이스 배선, cache_warm의 모델 재구축(descriptor 필드, 환경 기본값 폴백, 세션 도구 면), `memory_review_mode="digest"`에서의 접기를 고정합니다. 영속화 단언은 `tests/agent/middlewares/message_persistence/`에 있습니다.
- `test_compression_cooldown_persist.py`: 쿨다운의 재시작 간 생존.
- `test_skill_review_trigger.py`: `_detect_todo_all_complete` 네 분기, `schedule_memory_review`의 압축마다 memory review 디스패치와 락 의미론, `_build_plan_context`.
- `test_skill_review_module_notes.py`: `<module>-notes` carry-forward 가이던스, 스킬 리뷰 프롬프트로의 라이브 FACTS.md 렌더링, 스텁 LLM 동작(create는 `skills/auto/<module>-notes/`에 안착, 기존 동명 스킬은 patch되고 중복 생성되지 않음).
- `test_skill_review_gate.py`: snapshot/restore 롤백, 변경 경로 감지, judge의 pass/retry/no-op/unavailable 판정.
- `test_skill_review_feedback.py`: 유계 사용자 수정, 내부 캐리어 필터링, `skill_view` 로드 스킬 추출과 usage fallback.
- `test_skill_review.py`: judge 승인 no-op, feedback 재시도, 스냅샷 롤백, 소진 후 실패 마커와 실패 쿨다운.
- Persistence assertions live in `tests/agent/middlewares/message_persistence/`.
- `test_memory_store.py`: MEMORY.md / USER.md 저장소 의미론.
- `test_memory_store_facts.py`: `facts` 대상——add/replace/remove, 상한 초과 시 가장 오래된 항목 롤오프, 원자적 쓰기, 로드 시 파일 생성, 라이브 내용 렌더링, 프롬프트 스냅샷 주입(빈 파일 → 블록 없음).

AI 판정 평가: `evals/skill_review/suite.py`는 완료된 plan 실행(plan 파일, 완료된 todos, 합성 자식 Agent 실행 기록)을 구성하고 실제 `_run_skill_review`을 호출한 뒤, 보조 LLM judge에게 생성된 스킬이 이번 실행에 진정으로 근거하며 재사용 가능하고 일반적이지 않은지 판정하게 합니다. 모든 쓰기는 샌드박스로 리디렉션되어 실제 `skills/auto/`와 `workspace/`는 절대 건드리지 않습니다.

```bash
uv run python evals/evals.py skill_review
```

이 스위트는 `knowledge_retired`, `skills_created`, `ai_judge_skill_review`, `no_repo_pollution`을 검사합니다.

## 알려진 경계

- **압축 fork에는 `todoread`를 주지 않습니다.** `todo_update` 마커가 붙은 `todowrite` 심만 허용합니다. fork는 현재 목록을 프롬프트로 받으므로 `todoread`는 의도적으로 태그하지 않습니다(`agent/tools/todolist/tools/__init__.py`).
- **FACTS.md는 단일 파일 메모리입니다.** MEMORY.md / USER.md와 같은 기구(단일 파일, `§` 구분 항목, 원자적 rename 쓰기, 파일별 문자 상한)를 공유하고 매 시스템 프롬프트에 주입됩니다. 계층형 / 분류형 저장소가 아니며, 상한을 넘으면 가장 오래된 항목부터 롤오프합니다.
- **쿨다운은 능동 압축만 억제합니다.** `compaction_cooldown_rounds`(3)는 실제 압축 후 T1 / T2 / T3 압축을 막습니다. T4 / T5 제공자 오류 복구는 쿨다운, 턴별 시도 상한, 기타 반스래싱 게이트를 구조적으로 우회합니다.

## 관련 문서

- [세션 메모리 아키텍처](../session_memory/README.md): 이 저장소들을 뒷받침하는 세션 메모리 아키텍처.
- [요약 압축](../summarization/README.md): 압축 트리거와 백그라운드 리뷰 및 todo fork를 게이트하는 쿨다운.
- [장기 작업](../long-running-tasks/README.md): TaskFlow와 skill review이 읽는 todo 계획 계층.
- [미들웨어 README](../../agent/middlewares/README.md): `@dynamic_prompt` 시스템 프롬프트 주입과 Summarization 참고.
- [Curator README](../../context_engine/curator/README.ko.md): 위에서 설명한 백그라운드 스킬 유지보수 오케스트레이터.
