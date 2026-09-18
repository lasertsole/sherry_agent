# 경험 체계 아키텍처

[English](README.md) · [中文](README.zh.md) · [日本語](README.ja.md) · 한국어

이 문서는 경험 체계를 다룹니다: Agent가 실행 중에 **언제** 경험을 추출하고, **어떤 메커니즘으로** 추출하며, 경험이 **어디에** 기록되고, 생성된 스킬 라이브러리가 어떻게 유지되는지를 정리합니다. 라이프사이클에는 네 개의 추출 경로가 연결됩니다: 압축 시 memory review(`nudge_memory_threshold`회 압축마다), 압축 시 todo 전부 완료일 때의 plan extraction, 압축 전 memory flush, 압축 후 todo fork. 산출물은 네 개 저장소——MEMORY.md / USER.md, plan 지식 디렉터리, `skills/auto/`, `todos.db`——로 들어가며, 아래 **Curator** 절이 plan extraction의 기록 대상인 `skills/auto/`를 관리하는 백그라운드 패스를 기록합니다.

> 아래 모든 주장은 소스와 대조해 검증했습니다. 심볼 이름, 설정 키, 기본값, 경로는 모두 `agent/middlewares/`, `agent/tools/`, `config/features/` 코드에 실제로 존재합니다.

## 설계 원칙

1. **모든 추출은 기존 저장소를 확장합니다.** 산출물은 MEMORY.md / USER.md, plan 지식 디렉터리, `skills/auto/`, `todos.db` 중 하나로 들어갑니다. 병렬 저장소를 만드는 추출 경로는 없습니다.
2. **전면 fail-open.** 모든 트리거는 자신의 실패를 기록하고 삼킵니다. todo 저장소 손상, plan 파일 읽기 불가, LLM 호출 실패, 커서 손상이 메인 대화 턴을 막거나 중단시키지 않습니다.
3. **턴 경로는 제로 블로킹.** 압축 후 todo fork와 압축 시점 nudge는 모두 fire-and-forget 백그라운드 작업입니다; memory review와 plan extraction은 NUDGE 레인에서 독립 자식 Agent로 실행되어 모델 호출을 절대 막지 않습니다.
4. **메커니즘을 필요에 맞게 선택합니다.** 도구 사용이 필요한 작업만 완전한 `create_agent` fork를 씁니다(memory nudge, plan extraction, todo fork). 순수 추출(memory flush)은 보조 LLM 호출 한 번으로 끝냅니다.

## 트리거 × 메커니즘 × 기록 대상

| 트리거 | 메커니즘(fork agent 여부 / 호출 형태) | 기록 대상 |
|---|---|---|
| `nudge_memory_threshold`회 압축마다(기본 10) | memory nudge(`_nudge_memory`): `create_agent` nudge agent를 fork하고 `_MEMORY_REVIEW_PROMPT` 사용 | `memory` 도구를 거쳐 MEMORY.md / USER.md |
| 압축 시 todo 목록이 전부 완료(`completed` / `cancelled`) | plan extraction(`_nudge_plan_extraction`): nudge agent를 fork하고 `_PLAN_EXTRACTION_PROMPT` 사용 | ① 지식 JSON ② `skills/auto/` |
| 압축 전(cut이 실제로 메시지를 버림) | memory flush(`run_memory_flush[_sync]`): 값싼 LLM 호출 한 번, agent 아님 | `MemoryStore.append_entries`를 거쳐 MEMORY.md와 USER.md |
| 압축 후(cut이 실제로 메시지를 버림) | todo fork(`update_todos_from_compaction`): fire-and-forget nudge agent, `_COMPRESSION_TODO_PROMPT` | 메인 세션에 바인딩된 `todowrite` 심을 거쳐 `todos.db` |

## 트리거 상세

### 1. 압축 시점 memory review

`schedule_compression_nudges`(`agent/middlewares/summarization/nudges.py`, Summarization 미들웨어가 메시지를 실제로 버리는 compact마다 호출)는 압축마다 `state_register_db`의 `nudge_review_memory_count`를 1회 증가시킵니다. 카운터가 `nudge_memory_threshold`(기본 10)에 도달하면 카운터를 0으로 되돌리고 `_nudge_memory(session_id, system_prompt, messages)`를 fire-and-forget 작업으로 디스패치하여 `nudge_review_memory_lock`(`state_register_mem`) 아래에서 실행합니다. 둘 중 하나의 nudge 락이 잡혀 있으면 압축은 카운터를 늘리지만 디스패치는 하지 않습니다.

`_nudge_memory`(`agent/middlewares/summarization/nudges.py`)는 `_create_nudge_agent`로 nudge agent를 만들고, 대화에 `_MEMORY_REVIEW_PROMPT`를 `HumanMessage`로 덧붙여 호출합니다. 프롬프트는 지속적인 사용자 특성(persona, 선호, 개인 정보)과 행동 기대치를 `memory` 도구로 저장하라고 요구하며, 저장할 것이 없으면 "Nothing to save."라고 답하고 멈춥니다.

- nudge agent는 메인 LLM 위의 별도 `create_agent`이며, 미들웨어는 `[_NudgeLimitTool(), ToolCallNormalize(), ToolGuardrails(), IterationBudget(90)]`, checkpointer 없음.
- `_NudgeLimitTool`(`allowed_metadata_key` 미지정)은 metadata에 `nudge: True`가 있는 도구만 통과시킵니다. `memory`, `skill_list`, `skill_view`, `skill_manage`, `knowledge`가 이 마커를 가지므로 nudge agent는 memory를 쓸 수 있지만 임의의 메인 도구는 호출할 수 없습니다.
- fork의 메시지는 로그만 남깁니다. `res["messages"]`의 어떤 내용도 메인 그래프로 들어가지 않습니다.

### 2. 압축 시점 plan extraction(todo 완료)

`_detect_todo_all_complete(session_id)`(`summarization/nudges.py`)는 압축마다 평가되며 완료 사이클마다 한 번 발화합니다:

- todo가 존재하고, 모든 todo가 `completed` 또는 `cancelled`입니다.
- `nudge_plan_extraction_fired`(`state_register_db`)가 아직 설정되지 않았습니다.

전이 시 플래그를 세우고, 목록이 전부 완료가 아니면 `False`로 되돌리므로 다음 완료 사이클에서 다시 발화합니다. 읽기는 fail-open. 감지가 압축 시점에만 실행되므로, 한 번도 압축하지 않는 세션은 plan extraction을 발화하지 않습니다.

`plan_extraction_enabled`가 켜져 있고 감지가 발화하면 `_nudge_plan_extraction`이 같은 접점에서 fire-and-forget으로 디스패치되어 `nudge_plan_extraction_lock` 아래에서 실행됩니다:

1. `_build_plan_context`가 plan 파일(`plan_ref` 상태 우선, 없으면 `plan_ref`를 가진 첫 todo), todo 목록, start-work ledger(`.omo/start-work/ledger.jsonl`), 이 세션의 자식 Agent 실행 기록(`result_text`는 24 KB로 절단, `outcome`, task)을 모읍니다. todo 목록이 없으면 `{}`를 반환하며, 호출자는 이를 보고 건너뜁니다.
2. plan 컨텍스트를 채운 `_PLAN_EXTRACTION_PROMPT`를 대화와 함께 nudge agent(같은 빌더, 같은 `nudge: True` 게이트)로 보냅니다.

프롬프트는 두 종류의 산출물을 만듭니다:

- **Part 1: 구조화 지식.** `knowledge(action="write", ...)`가 JSON 문서를 `config.path.PLAN_KNOWLEDGE_DIR`(`workspace/knowledge/plans/<plan-name>/`)에 씁니다: `task-<position>.json`, `wave-<index>.json`, `plan-summary.json`. 각 task는 `failure_set`, `success_path`, `method`를, wave는 실패 / 성공 패턴을, plan은 전체 방법, 핵심 실패 / 성공, 재사용 패턴을 담습니다.
- **Part 2: 스킬 라이브러리 갱신.** `skill_manage`가 로드되었거나 기존인 클래스 레벨 스킬을 패치하고, 지원 파일을 추가하거나, `skills/auto/` 아래 새 클래스 레벨 umbrella를 만듭니다. 프롬프트는 명시적으로 능동적이며("most completed plans produce at least one skill update") 사용자 교정, 워크플로 교정, 비자명한 기법, 오래된 스킬을 일급 신호로 나열합니다.

이후 읽기는 같은 도구(`knowledge(action="read")`)가 제공하고, 압축된 plan 요약은 `build_knowledge_block`(`knowledge/prompt_block.py`)이 시스템 프롬프트에 자동 주입합니다.

### 3. 압축 전 memory flush

두 압축 경로(`agent/middlewares/summarization/core.py`의 `_apply_compression_under_lock`과 `_aapply_compression_under_lock`)는 cut이 메시지를 버릴 때, 요약 생성 전에 flush를 실행합니다:

- 동기 경로: `run_memory_flush_sync(...)`;
- 비동기 경로: `await run_memory_flush(...)`.

`should_flush`(`agent/middlewares/summarization/memory_flush.py`)의 게이트는 `MEMORY_FLUSH["enabled"]`에 더해 `total_chars >= force_flush_chars`(50 000) 또는 `estimated_tokens >= soft_threshold_tokens`(8 000)입니다. flush는 버려지기 직전 텍스트에 대한 `llm.ainvoke` / `llm.invoke` 한 번이고, 프롬프트는 `_FLUSH_PROMPT`이며 `_build_llm`이 `model=MEMORY_FLUSH["model"]`, `max_tokens=2048`, `timeout=30`으로 만듭니다. **agent가 아니며** 도구도 없습니다.

응답은 `§`로 구분된 항목으로 파싱됩니다. 빈 결과나 `(none)`은 아무것도 쓰지 않습니다. 그렇지 않으면 `MemoryStore.append_entries`가 각 항목을 라우팅합니다: `^\s*user\s*:`(대소문자 무시)에 맞으면 USER.md, 나머지(Environment / Project / Decision / Tool / 접두사 없음)는 MEMORY.md. `append_entries`는 대상 파일에 대해 중복을 제거하고, `add`와 달리 가장 오래된 항목을 밀어내어 각 파일의 상한 안에 머뭅니다.

flush는 교차 세션 facts만 추출합니다. 임시 작업 진행은 의도적으로 요약에 맡깁니다. 예외는 기록하고 삼키며, flush가 압축을 막는 일은 없습니다.

### 4. 압축 후 todo fork

같은 압축 지점에서 `_schedule_compression_todo_update`(`summarization/core.py` -> `nudges.schedule_compression_todo_update`)가 `update_todos_from_compaction`을 fire-and-forget `asyncio.create_task`로 예약합니다. 스케줄러는 **모두** 만족해야 합니다:

- `compression_todo_update_enabled`(`SUMMARIZATION`, 기본 `True`);
- cut이 실제로 메시지를 버렸음;
- 해당 세션에 `compression_todo_update_lock`이 잡혀 있지 않음;
- 세션에 비어 있지 않은 todo 목록이 있음;
- 실행 중인 이벤트 루프가 존재함(동기 압축 경로는 건너뛰고 debug 로그를 남김).

`update_todos_from_compaction`은 시스템 프롬프트가 `_COMPRESSION_TODO_PROMPT`이고 도구가 정확히 하나인 nudge agent를 실행합니다: `_build_main_session_todowrite(session_id)`. fork는 현재 todo 목록을 버려진 슬라이스와 대조하여, 실제로 끝난 항목을 `completed`, 포기되거나 대체된 항목을 `cancelled`로 표시하고, 증거가 있는 새 작업을 `pending`으로 추가하며, **전체** 목록을 한 번의 `todowrite` 호출로 되씁니다(델타가 아니라 전체 교체). 변화가 없으면 그대로 되씁니다.

fork의 결과 메시지는 로그만 남깁니다. 메인 그래프나 그 checkpointer에 아무것도 도달하지 않으며, 파생 세션의 미들웨어 상태는 `finally`에서 정리됩니다.

## Curator(스킬 큐레이션)

Curator(`context_engine/curator/`)는 `skills/auto/` 스킬 라이브러리의 라이프사이클을 담당하는 백그라운드 패스이며, 바로 위의 추출 경로 2가 기록하는 대상입니다. 스킬만 읽고 씁니다. MEMORY.md / USER.md, plan 지식 디렉터리, `todos.db`는 범위 밖입니다.

**무엇인가.** 정기 cron이 아니라 유휴 트리거 오케스트레이터입니다. 서비스 엔트리포인트가 `context_engine.curator.init()`으로 데몬 스레드(`curator-timer`)를 시작합니다(`server/__main__.py:140`; HTTP-only 모드에서는 건너뜀, `server/__main__.py:66-73`). 스레드는 3600초마다 깨어나 `maybe_run_curator(idle_for_seconds=...)`를 호출합니다(`context_engine/curator/__init__.py:122-140`). 패키지 임포트는 부수 효과가 없으며 스레드를 시작하는 것은 `init()`뿐입니다(`context_engine/curator/__init__.py:151-165`).

**언제 실행되는가.** 두 게이트를 모두 통과할 때만 리뷰가 실행됩니다:

- `should_run_now()`——활성화됨, 일시정지 아님, `last_run_at`이 유효 간격을 초과(`context_engine/curator/transitions.py:21-39`). 유효 간격은 `curator.interval_hours`(기본 168시간 / 7일, `config/sherry_settings.py:43`)이며, 클라이언트에서 `.curator_state`의 `auto_interval_days`로 1~5일로 덮어쓸 수 있습니다(`context_engine/curator/config.py:97-107`);
- Agent 유휴 시간이 `curator.min_idle_hours`(기본 2) 이상(`context_engine/curator/orchestrator.py:320-336`). 모든 사용자 턴마다 이 유휴 타이머가 리셋됩니다(`server/service/messages.py:190`).

UI에서는 `POST /curator/run`으로 강제 실행할 수 있으며, 워커 스레드에서 `run_curator_review()`를 호출합니다(`server/trigger/http/curator.py:107-125`).

**라이프사이클 규칙.** `apply_automatic_transitions()`(`context_engine/curator/transitions.py:41-100`)는 모든 `skills/auto/**/SKILL.md`를 순회하며(`context_engine/curator/usage.py:158-179`) 각 스킬에 대해: pinned는 건너뜀; `stale_after_days`(기본 30일) 동안 활동이 없으면 `stale`로 표시; `archive_after_days`(기본 90일)를 넘기면 `skills/.archive/`로 **아카이브**되고 `archived`로 표시——`curator restore <name>`으로 복구 가능(이 자동 경로는 아무것도 삭제하지 않음); 다시 활동이 있거나 stale 창 안에서 한 번도 사용되지 않은 스킬은 재활성화합니다. 기본값은 `config/sherry_settings.py:42-48`.

**LLM 통합.** `curator.consolidate`가 켜져 있으면(기본 켜짐), `run_curator_review()`가 비-pinned 스킬 후보 목록을 렌더링하고(`context_engine/curator/orchestrator.py:65-81`) 메인 LLM(temperature 0.3)에게 겹치는 좁은 스킬들을 클래스 레벨 umbrella 스킬로 병합하도록 요청합니다(`CURATOR_REVIEW_PROMPT`, `context_engine/curator/orchestrator.py:18-45`). 새 umbrella와 지원 파일은 `skills/auto/` 아래에 생성·영속화됩니다(`_generate_umbrella_skill`, `context_engine/curator/orchestrator.py:412`; `_apply_consolidation`, `context_engine/curator/orchestrator.py:755`). 통합은 이 패스의 유일한 LLM 단계이며, 실패는 잡히고 실행은 계속됩니다.

**네 개 추출 경로와의 관계.** 경로 2(압축 시점 plan extraction)가 생산자입니다: `skill_manage`를 통해 `skills/auto/`를 생성·패치합니다. Curator는 바로 그 산출물의 하류 유지보수자로, plan extraction이 만든 스킬을 전이·통합·정리합니다. 나머지 세 경로는 `skills/auto/`를 전혀 건드리지 않으므로(각각 MEMORY.md / USER.md, plan 지식 디렉터리, `todos.db`에 씀) Curator와 교차하지 않습니다.

**상태와 경계.** 실행 상태는 `skills/.curator_state`(`context_engine/curator/constants.py:3`, `context_engine/curator/state.py`가 읽고 씀); 각 실행은 `logs/curator/{timestamp}/` 아래에 `run.json` + `REPORT.md`를 씁니다(`context_engine/curator/constants.py:4`; `context_engine/curator/report.py:196-205`). 범위는 `skills/auto/`뿐——내장 스킬은 절대 건드리지 않고, pinned 스킬은 모든 파괴적 전이를 우회하며, 패스 전체가 백그라운드 스레드에서 실행되어 대화 턴 경로를 차지하지 않습니다. 전체 세부사항: [curator/README.md](../../context_engine/curator/README.ko.md).

## 격리와 안전

| 가드 | 보호 대상 |
|---|---|
| nudge metadata 허용 목록(`metadata["nudge"]`, `_NudgeLimitTool` 기본 규칙) | memory nudge와 plan extraction은 nudge 단계용으로 태그된 도구만 호출할 수 있습니다. 다른 모든 메인 도구는 오류 `ToolMessage`를 반환하고 실행되지 않습니다. |
| 압축 metadata 허용 목록(`metadata["todo_update"] is True`, `_NudgeLimitTool(allowed_metadata_key="todo_update")`) | 압축 fork는 해당 metadata 마커가 붙은 `todowrite` 심만 허용합니다. |
| 파생 세션 키 `<id>::compression-todo` | 압축 fork의 `IterationBudget` / `ToolGuardrails` / `ToolCallNormalize` 상태 키가 메인 세션과 충돌하지 않습니다. fork가 도는 동안 메인 세션은 `awrap_model_call` 중이기 때문입니다. `compression_todo_update_lock`만 의도적으로 메인 세션에 기록되어 교차 경로 재진입 조정자 역할을 합니다. |
| 메인 세션 바인딩 `todowrite` 심 | fork 그래프는 파생 키로 돌기 때문에, 상태 주입된 실제 `todowrite`는 잘못된 세션을 해석합니다. 심은 실제 도구의 `args_schema`와 `description`을 그대로 재사용하고(스키마 드리프트 제로), 주입된 `session_id`를 버리며, 빌드 시점에 캡처한 메인 세션 id를 바인딩합니다. |
| 읽기 전용 fork, checkpointer 없음 | 각 nudge / 추출 fork의 결과 메시지는 로그만 남기고 버립니다. fork에는 checkpointer가 없어 메인 그래프 상태를 쓸 수 없습니다. |
| 세션별 재진입 락 | `nudge_review_memory_lock`, `nudge_plan_extraction_lock`, `compression_todo_update_lock`이 같은 추출 경로의 중복 실행을 막습니다. nudge 락이 잡혀 있는 동안 압축 스케줄러는 그 압축을 계수하지만 디스패치는 하지 않습니다. |
| fail-open 경계 | 각 경로는 `try/except`로 작업을 감싸고, 기록하고 반환합니다. 어떤 추출 실패도 턴, 압축, 다른 추출로 전파되지 않습니다. |
| 백그라운드 작업 참조 유지 | `_COMPRESSION_TODO_TASKS` 집합이 강한 참조를 유지해 asyncio가 진행 중 작업을 GC하지 못하게 합니다. |

## 저장소와 상한

| 저장소 | 경로(상수) | 상한 |
|---|---|---|
| MEMORY.md | `config.path.MEMORY_DIR / "MEMORY.md"`(`workspace/memory/MEMORY.md`) | 2200자(`MemoryStore.memory_char_limit`) |
| USER.md | `config.path.MEMORY_DIR / "USER.md"`(`workspace/memory/USER.md`) | 1375자(`MemoryStore.user_char_limit`) |
| plan 지식 | `config.path.PLAN_KNOWLEDGE_DIR`(`workspace/knowledge/plans/<plan>/`) | plan마다 `task-<n>.json`, `wave-<n>.json`, `plan-summary.json`. 필드 상한은 프롬프트로 유도(150 / 100자) |
| 스킬 | `config.path.AUTO_SKILLS_DIR`(`skills/auto/`) | `SKILL.md`와 `references/`, `templates/`, `scripts/` 지원 파일 |
| todos | `agent/tools/todolist/data/todos.db`(`store_sqlite._DB_PATH`) | 세션 범위 목록, 전체 교체 쓰기 |

## 설정 스위치

| 키 | 위치 | 기본값 | 효과 |
|---|---|---|---|
| `MEMORY_FLUSH_ENABLED` | env -> `MEMORY_FLUSH["enabled"]`(`config/features/agent_side/memory_flush.py`) | `1`(켜짐) | 압축 전 flush 마스터 스위치 |
| `MEMORY_FLUSH_MODEL` | env -> `MEMORY_FLUSH["model"]` | `""`(팩토리 기본 사용) | flush에 쓸 값싼 추출 모델 |
| `soft_threshold_tokens` | `MEMORY_FLUSH` | `8000` | 버려진 슬라이스가 이 규모 이상이면 flush |
| `force_flush_chars` | `MEMORY_FLUSH` | `50000` | 문자 수가 이를 넘으면 무조건 flush |
| `output_max_tokens` | `MEMORY_FLUSH` | `2048` | flush 호출 출력 상한 |
| `timeout_seconds` | `MEMORY_FLUSH` | `30` | flush 호출 타임아웃 |
| `compression_todo_update_enabled` | `SUMMARIZATION`(`config/features/agent_side/summarization.py`) | `True` | 압축 후 todo fork 활성화 |
| `plan_extraction_enabled` | `NUDGE`(`config/features/agent_side/nudge.py`) | `True` | todo 완료 시 plan extraction 활성화 |
| `nudge_memory_threshold` | `NUDGE` | `10` | memory review 간격이 되는 압축 횟수 |
| `compaction_cooldown_rounds` | `SUMMARIZATION` | `3` | 실제 압축 후 능동 압축 쿨다운 |
| `memory_char_limit` / `user_char_limit` | `MemoryStore.__init__` | `2200` / `1375` | MEMORY.md / USER.md 상한 |

## 검증

대상 테스트:

```bash
uv run pytest \
    tests/agent/middlewares/test_compression_todo_update.py \
    tests/agent/middlewares/test_compression_nudges.py \
    tests/agent/middlewares/test_compression_cooldown_persist.py \
    tests/agent/middlewares/test_memory_flush.py \
    tests/agent/middlewares/system_prompt/test_plan_extraction.py \
    tests/agent/tools/test_memory_store.py -q
```

- `test_compression_todo_update.py`: 트리거 게이트, fire-and-forget 예약, 재진입 락, fail-open 해제, 프롬프트 내용, `todo_update` metadata 게이트, 그리고 완전한 fork 격리(파생 키, 메인 세션 `todowrite` 심, checkpointer / 메시지 누출 없음).
- `test_compression_nudges.py`: 압축 시점 nudge 디스패치(memory review + plan extraction이 compact 접점에서 발화; 컷 없는 압축은 아무것도 디스패치하지 않음). 영속화 단언은 `tests/agent/middlewares/message_persistence/`에 있습니다.
- `test_compression_cooldown_persist.py`: 쿨다운의 재시작 간 생존.
- `test_memory_flush.py`: flush 게이트, 라우팅, 비블로킹 실패.
- `test_plan_extraction.py`: `_detect_todo_all_complete` 네 분기, `schedule_compression_nudges`의 압축 시점 카운터/락 의미론, 디스패치, `_build_plan_context`.
- `test_memory_store.py`: MEMORY.md / USER.md 저장소 의미론.

AI 판정 평가: `evals/nudge_extraction/suite.py`는 완료된 plan 실행(plan 파일, 완료된 todos, 합성 자식 Agent 실행 기록)을 구성하고 실제 `_nudge_plan_extraction`을 호출한 뒤, 보조 LLM judge에게 생성된 스킬이 이번 실행에 진정으로 근거하며 재사용 가능하고 일반적이지 않은지 판정하게 합니다. 모든 쓰기는 샌드박스로 리디렉션되어 실제 `skills/auto/`와 `workspace/`는 절대 건드리지 않습니다.

```bash
uv run python evals/evals.py nudge_extraction
```

이 스위트는 `knowledge_written`, `skills_created`, `ai_judge_skill_quality`, `no_repo_pollution`을 검사합니다.

## 알려진 경계

- **압축 fork에는 `todoread`를 주지 않습니다.** `todo_update` 마커가 붙은 `todowrite` 심만 허용합니다. fork는 현재 목록을 프롬프트로 받으므로 `todoread`는 의도적으로 태그하지 않습니다(`agent/tools/todolist/tools/__init__.py`).
- **memory flush는 교차 세션 facts만 추출합니다.** 임시 작업 진행은 요약에 속하며 MEMORY.md / USER.md에 들어가지 않습니다.
- **쿨다운은 능동 압축만 억제합니다.** `compaction_cooldown_rounds`(3)는 실제 압축 후 T1 / T2 / T3 압축을 막습니다. T4 / T5 제공자 오류 복구는 쿨다운, 턴별 시도 상한, 기타 반스래싱 게이트를 구조적으로 우회합니다.

## 관련 문서

- [세션 메모리 아키텍처](../session_memory/README.md): 압축 전 memory flush.
- [요약 압축](../summarization/README.md): 압축 트리거와 flush 및 todo fork를 게이트하는 쿨다운.
- [장기 작업](../long-running-tasks/README.md): TaskFlow와 plan extraction이 읽는 todo 계획 계층.
- [미들웨어 README](../../agent/middlewares/README.md): `@dynamic_prompt` 시스템 프롬프트 주입과 Summarization 참고.
- [Curator README](../../context_engine/curator/README.ko.md): 위에서 설명한 백그라운드 스킬 유지보수 오케스트레이터.
