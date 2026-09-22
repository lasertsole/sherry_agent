# 🧠 메모리와 연속성

[English](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> [Long-Running Tasks](../README.ko.md)의 일부: 2계층 메모리 시스템, 압축 전 메모리 플러시, 요약 ↔ TaskFlow 조정, 서브에이전트 메모리 역류, 도구 출력 한 줄 요약, 세션 연속성, TaskFlow 자동 재개.

---

## 🧠 계층형 메모리

두 계층은 *어떻게* 모델에 도달하는지로 구분됩니다:

| 계층 | 저장소 | 위치 | 프롬프트 포함? |
| :--- | :--- | :--- | :--- |
| **L1 —— 정제 메모리** | `MEMORY.md`(에이전트 노트) + `USER.md`(사용자 프로필) | `workspace/memory/`(`MEMORY_DIR`) | 예——동결 스냅샷으로 항상 주입 |
| **L2 —— 원시 이력** | `mes_memory.db`(SQLite, WAL, FTS5) | `src/store/mes_memory/mes_memory.db` | 아니오——`context_engine` / `message_search`가 검색 |

파일은 **한 줄짜리 구분자 `§`로 나뉜 일반 텍스트 항목**입니다 — `ENTRY_DELIMITER = "\n§\n"`(`agent/tools/memory.py:53`). YAML frontmatter도, 불릿 접두사도 없습니다. 항목은 여러 줄일 수 있습니다.

계층 1은 `MemoryStore`가 관리합니다(`memory.py:104`): 파일당 문자 한도 `2200`(memory)과 `1375`(user), 주입 스캔(`_MEMORY_THREAT_PATTERNS`, `memory.py:68`)이 프롬프트 주입과 자격 증명 유출을 거부하고, 크로스 플랫폼 파일 잠금, 원자적 쓰기, 정확 일치 중복 제거를 갖춥니다. 라이브 항목은 즉시 변경되는 반면, 프롬프트는 `load_from_disk()`에서 캡처한 **동결 스냅샷**을 사용해 세션 동안 프리픽스 캐시를 안정적으로 유지합니다.

`memory` 도구는 `scope="main_only"`로 태그되어 서브에이전트는 절대 볼 수 없습니다.

**그래프 상태 체크포인트 저장소.** 세션의 LangGraph 상태는 `src/checkpoints/sqlite.db`에도 영속화되며, 위 두 계층과 별개입니다: `built_agent()` 호출마다 스레드별 최신 체크포인트로 정리되고(`ThreadSafeAsyncSqliteSaver.aclean_old_checkpoints`, `agent/core.py:227`), `auto_vacuum=0`에서는 DELETE가 페이지를 해제할 뿐 파일을 줄이지 않으므로, 같은 호출이 정리 직후 `PRAGMA freelist_count × page_size`를 읽고 해제된 공간이 `_VACUUM_THRESHOLD_BYTES`(10 MB, `agent/checkpointer/thread_safe_checkpointer.py`)를 초과할 때만 `VACUUM`을 실행합니다 — 페일오픈: VACUUM 오류는 로그만 남기고 정리 결과는 그대로 유지됩니다.

## 🔥 압축 전 메모리 플러시

요약 미들웨어가 오래된 메시지를 버리기 전에, `agent/middlewares/summarization/memory_flush.py`는 값싼 모델에게 지속적 사실을 `MEMORY.md`에 저장할 마지막 기회를 줍니다. 트리거는 `should_flush(discarded_messages, estimated_tokens)`(`memory_flush.py:43`)입니다:

```python
if not MEMORY_FLUSH["enabled"]:
    return False
total_chars = sum(len(_msg_to_text(m)) for m in discarded_messages)
if total_chars >= MEMORY_FLUSH["force_flush_chars"]:   # 50_000
    return True
return estimated_tokens >= MEMORY_FLUSH["soft_threshold_tokens"]   # 8_000
```

발화하면 `run_memory_flush`(비동기) / `run_memory_flush_sync`가 주입된 팩토리로 모델을 구성하고 단일 일반 텍스트 추출 프롬프트(`_FLUSH_PROMPT`, `memory_flush.py:19`)를 사용합니다. 출력은 `§`로 구분된 `Environment / Project / Decision / User / Tool` 사실 목록입니다. 빈 결과나 리터럴 `(none)`은 건너뜁니다. 추출 텍스트는 `MemoryStore.append_entries(new_entries)`(`memory.py:281`)로 넘어가며, 이는 `§`로 나누고, 각 후보를 주입 스캔하고, 기존 집합과 중복 제거하고, 덧붙이고, 2200자를 넘는 동안 가장 오래된 항목을 축출하고, 마지막으로 한 번의 원자적 쓰기를 수행합니다. `append_entries`는 항상 `MEMORY.md`를 대상으로 합니다. 모든 실패 경로는 `False`를 반환하고 삼켜집니다 — 플러시가 압축을 막을 수 없습니다.

⚠️ **배선 상태.** `Summarization.__init__`은 `memory_store` / `llm_factory`를 받으며(둘 다 기본 `None`, `summarization/core.py:259-260`), 둘 다 설정된 경우에만 `_apply_compression`(`summarization/compression.py:138`)과 `_aapply_compression`(`summarization/compression.py:221`) 안에서 플러시를 호출합니다. 현재 프로덕션 인스턴스 — 메인 에이전트 `agent/core.py:204`과 서브에이전트 `agent/tools/subagent/spawn/core.py:909` — 는 이들을 전달하지 **않습니다**. 따라서 플러시는 구현·테스트되었지만 호출 지점이 저장소와 `factory(model=…, max_tokens=…, timeout=…)` 형태의 팩토리를 제공할 때까지 잠재 상태에 머뭅니다.

## 🔗 요약 ↔ TaskFlow 조정

압축이 LLM 프롬프트를 구성할 때, `_get_taskflow_context_sync(session_id)`(`agent/middlewares/summarization/core.py:122`)가 이 세션의 활성 flow를 렌더링하여 요약 프롬프트의 **마지막** 부분으로 덧붙입니다(`_build_summary_prompt`, `summarization/summary_generation.py:554`):

```python
taskflow_ctx = _get_taskflow_context_sync(session_id)
if taskflow_ctx:
    parts.append(taskflow_ctx)
```

이 블록은 `## Current TaskFlow State (authoritative)`를 제목으로 하며(`summarization/core.py:137`), 세션이 소유한 최대 3개 flow(저장소 읽기가 SQL 계층에서 `session_id`로 범위가 정해지며 Python 재필터가 없습니다)에 대해 flow id/상태, 설명, `done/total` 진행과 상태 내역, 마지막 두 완료 단계, 처음 두 대기 단계, 대기 이유를 나열합니다. DAG 헬퍼 `step_status`와 `steps_summary`를 재사용하며 완전히 페일오픈입니다(`except Exception → ""`). 결정론적 폴백 요약(`_build_static_fallback_summary`)은 이 블록을 **포함하지 않습니다** — LLM 프롬프트 전용 추가입니다.

## 🧠 서브에이전트 메모리 역류

`SubagentCompletionDrainMiddleware`(`agent/middlewares/subagent_completion_drain/core.py`)는 큐에 쌓인 서브에이전트 완료 메시지의 부모 턴 수용 지점입니다: `before_model`에서 세션의 `SteeringQueue`를 재수화하고 배출한 뒤, 재구성된 완료 캐리어 메시지를 주입합니다. **배출이 비어 있지 않으면** 공유 메모리를 부모의 인메모리 뷰와 조정합니다:

```python
# subagent_completion_drain/core.py:93-117
def _backflow_shared_memory() -> None:
    from agent.tools.memory import memory_store
    memory_store.load_from_disk()
    for target in ("memory", "user"):
        memory_store.save_to_disk(target)
```

부모와 자식은 **하나의 프로세스 전역 `MemoryStore`** 를 공유하므로 자식의 쓰기는 이미 파일 수준에서 보입니다. 표류할 수 있는 것은 부모의 인메모리 뷰 — 라이브 항목과 시스템 프롬프트 구성에 쓴 **동결 스냅샷** — 이며, 이는 프로세스 밖 작성자가 `MEMORY.md` / `USER.md`를 갱신했을 때 일어납니다. **먼저 재로드**하는 순서가 핵심입니다: 오래된 인메모리 목록을 재로드 전에 영속화하면 동시 작성자를 덮어쓰므로, 조정은 대상마다 load → persist여야 합니다.

배출과 마찬가지로 역류도 **페일오픈**입니다 — 메모리 I/O 실패는 로그로 남기고 삼키며, 완료 캐리어는 부모 턴에 도달합니다. 배출은 내부 완료 캐리어에 Sisyphus 검증 리마인더를 덧붙여, 완료가 검증된 결과가 아니라 `DoneClaim`임을 부모에게 상기시킵니다(todo를 완료로 표시하기 전에 `todoread`로 검증하고, 수용 기준에 비추고, 오래된 상태를 조사하십시오). `enforce_verification=True`(`EVIDENCE_LEDGER["enforce_on_complete"]`에서 옴)이면 리마인더 대신, 세션에 통과 evidence가 없을 때 프로그램 게이트 메시지가 덧붙습니다.

## ✂️ 도구 출력 요약

비-LLM 프루닝에서 크기가 큰 오래된 `ToolMessage` 내용은 보통 마커로 정리됩니다. `pub/func/message/tool_output_prune.py`는 맨 마커 `_PRUNE_MARKER = "[Old tool result content cleared]"`(`tool_output_prune.py:22`)를 **도구별 한 줄 요약**으로 대체하여, 결과에 무엇이 있었는지에 대한 단서를 모델이 유지하게 합니다:

```python
# _TOOL_SUMMARY_TEMPLATES (tool_output_prune.py:43-65)
"read"/"read_file"   -> "[read] read file, {len} chars, {lines} lines"
"write"/"write_file" -> "[write] wrote file, {len} chars"
"edit"/"edit_file"   -> "[edit] edited file, {len} chars"
"grep"               -> "[grep] search done, {lines} matches"
"glob"               -> "[glob] matched {n} files"
"bash"/"shell"       -> "[bash] exit_code={code}, output {len} chars"
"taskflow_summary"   -> "[taskflow_summary] {len} chars"
"taskflow_run_task"  -> "[taskflow_run_task] dispatched, {len} chars"
"taskflow_resume"    -> "[taskflow_resume] injected result, {len} chars"
"memory"             -> "[memory] {first 80 chars}..."
default              -> "[tool] output {len} chars, first 100: ..."
```

`prune_tool_outputs(messages, protect_tokens=…, min_reduction_tokens=…, protected_tools=None, estimator=None)`(`tool_output_prune.py:104`)는 최신→오래된 순으로 메시지를 순회하고, 첫 요약 메시지에서 멈추며, 최신 `prune_protect_tokens`(40 000)를 보호하고, 보호 대상 도구(`{"memory", "skill_view", "skill_list"}`)를 건너뛰며, 해제된 토큰이 `prune_min_reduction_tokens`(5 000)에 도달할 때만 반영합니다. 교체된 메시지는 `additional_kwargs["status"] = "compacted"`와 `["original_length"]`를 지닌 `model_copy` 복제본입니다. 요약은 200자로 제한되며, 템플릿 예외는 마커로 폴백합니다. 호출자는 `Summarization._run_non_llm_strategies`입니다(`summarization/compression.py:314`).

## 🔄 세션 연속성

세션이 정리될 때, `context_engine/session_continuity.py`가 종료 상태를 영속화하여 다음 세션이 연속성을 제시할 수 있게 합니다. `server/DAO/messages.py::clear_session`은 삭제 전에 `auto_save_on_session_end(session_id)`를 **0단계**로 호출합니다(`server/DAO/messages.py:27-33`). 이 함수는:

1. `runtime.session.relation_register`를 통해 `channel_id`/`chat_id`를 해석합니다(`_get_channel_chat_for_session`, `session_continuity.py:167`).
2. 최근 3턴을 읽고 마지막 AI 응답을 `_MAX_SUMMARY_CHARS = 500`으로 자릅니다(`session_continuity.py:28`).
3. 해당 세션의 활성 flow id를 수집합니다.
4. `save_session_end_state(...)`를 `src/data/session_continuity/{safe-key}.json`에 씁니다(`session_continuity.py:25`). 필드는 `last_session_id`, `ended_at`, `ended_ts`, `summary`, `taskflow_ids`입니다.

메시지 저장소 삭제 후, `clear_session`은 해당 세션의 **계획 저장소**도 정리합니다——`agent.tools.todolist.registry.store_sqlite.delete_todos_by_session(session_id)`와 `agent.tools.taskflow.registry.store_sqlite.delete_flows_by_session(session_id)`——따라서 정리된 세션에는 todo나 태스크 플로우 잔여물이 남지 않습니다. 삭제는 베스트 에포트이며(실패는 로그로 남고 나머지 정리를 막지 않음), `session_id`가 일치하는 행만 삭제되고, 격리 이전의 태스크 플로우 행(`session_id = ''`)은 절대 매칭되지 않습니다.

다음 세션은 `build_continuity_prompt(session_id)`(`session_continuity.py:80`)로 이를 읽습니다. 이는 `workspace/prompt_builder.py:169`의 `_build_continuity_block`에서 호출되며 전체 프롬프트를 구성할 때 주입됩니다(`prompt_builder.py:289-295`):

```
## Last Session (continuity)
Last conversation ended with: <요약 ≤ 500자>
Related tasks: <최대 3개 flow id>
If the user says 'continue' or doesn't specify a new task, refer to the above context first.
```

세션은 자기 자신의 상태를 받지 않습니다(`last_session_id == session_id → ""`). 조회에 **channel id와 chat id가 모두** 필요하므로, 채널 바인딩이 없는 순수 WebSocket 세션은 연속성 블록을 받지 못합니다. 저장소는 데이터베이스가 아니라 파일 시스템의 JSON(`channel:chat` 키, 퇴화 시 `session_id`)입니다.

## ♻️ TaskFlow 자동 재개

활성 flow는 시스템 프롬프트로 다시 떠올라 새 세션이 미완료 작업을 이어받을 수 있게 합니다. 세 개의 독립적 판독기가 같은 레시피를 씁니다 — 세션 범위의 `get_active_flows_sync(session_id)`를 `PromptDataProvider.get_active_flows(session_id)`를 통해 가져옵니다:

| 판독기 | 위치 | 목적 |
| :--- | :--- | :--- |
| `_build_taskflow_block` | `workspace/prompt_builder.py:145` | 시스템 프롬프트의 `## Pending TaskFlows` |
| `_get_taskflow_context_sync` | `agent/middlewares/summarization/core.py:122` | 압축 요약 프롬프트의 TaskFlow 블록 |
| `_get_active_taskflow_ids_sync` | `context_engine/session_continuity.py:215` | 영속 연속성 상태의 `taskflow_ids` |

`creator_session_key`는 flow 생성 시 `requester_session_key(session_id)` = `f"agent:main:session:{session_id}"`로 찍힙니다(`taskflow_create.py:38`, `_shared.py:21`). `get_active_flows_sync()`(`store_sqlite.py:466`)는 `running`과 `waiting` flow만 리비전 순으로 반환하며, 이벤트 루프가 필요 없는 stdlib `sqlite3` 경로를 사용합니다. 실패 시 `[]`를 반환합니다.

시스템 프롬프트 블록(`prompt_builder.py:140`)은 다음과 같습니다:

```
## Pending TaskFlows
- [running] flow-1: "<설명>" | 2/5 steps done | next: step-3 "<작업>"
Use taskflow_summary to inspect a flow and continue execution.
```

최대 3개 flow로 제한되며, 파일 필터로 프롬프트를 구성할 때(`selected_file_names is not None`) 억제됩니다. 모든 읽기는 페일오픈입니다.

