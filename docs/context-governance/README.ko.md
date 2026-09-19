# 🧭 컨텍스트 거버넌스: 영속화, 축출, 슬라이스와 오버플로 클립

[**English**](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> 원본 기록을 어떻게 영속시키고 모델 가시 컨텍스트를 어떻게 작게 유지하는가: 모델 경계마다·도구 반환마다의 write-once 영속화, 복구 가능한 프리뷰를 남기는 도구 결과 디스크 축출, 실행 시점 `read_file` 슬라이스, state에 전문을 남기는 인간 메시지 축출, 어떤 압축 라우트보다 먼저 도는 LLM 없는 테일 클립, 그리고 오래된 요약을 대화 페이로드에서 차단하는 체인 요약 필터링.

에이전트가 만드는 모든 메시지는 두 번 가치 있다: **원본 기록**(실제로 일어난 일 — 검색과 압축을 위해)과 **모델 컨텍스트**(지금 윈도에 들어가는 것)다. 이 페이지는 그 두 요구를 화해시키는 여섯 가지 메커니즘을 기록한다 — 모두 하나의 규칙을 공유한다: **데이터는 절대 잃지 않고, 줄이는 것은 모델 뷰뿐이며, 전문을 가리키는 포인터를 항상 남긴다.**

**사실상의 기준(source of truth):** `agent/middlewares/context_eviction/core.py`, `agent/middlewares/message_persistence/core.py`, `agent/middlewares/message_persistence/prepare.py`, `pub/func/message/eviction.py`, `pub/func/message/overflow_clip.py`, `pub/func/message/target_truncation.py`, `pub/func/message/tool_args_truncate.py`, `agent/middlewares/summarization/core.py`, `context_engine/store/core.py`, `config/features/agent_side/tool_result_eviction.py`, `config/features/agent_side/summarization.py`. 아래의 모든 주장은 해당 코드와 대조하여 검증했습니다.

## 🎯 개요와 파이프라인

이 메커니즘들은 하나의 파이프라인을 이룬다. 각 단계는 모델 뷰를 조금 더 줄이며, 그 어느 것도 원본 기록을 파괴하지 않는다:

```text
tool returns
  │  ContextEvictionMiddleware.wrap_tool_call
  │    generic tool, text > 20 000 chars → full text to evicted/, head+tail preview in state (P0-2)
  │    read_file                         → execution-time 4 000-char head slice, no file written (P2-4)
  ▼
graph state (tool results: preview only · human messages: full text + lc_evicted_to tag)
  │
  │  MessagePersistenceMiddleware
  │    wrap_tool_call  → flush the RAW result the moment the handler returns
  │    after_model     → flush new human/ai/tool messages at every model boundary
  ▼
MesMemory (full text; persisted_message_ids watermark = write-once)
  │
  │  context pressure triggers Summarization (T1–T5)
  │    1. tail clip      — no LLM: stub the trailing contiguous ToolMessage batch (P1-2)
  │    2. existing route — truncate_tool_results_only / compact_only / compact_then_truncate
  │    3. forced recovery (T4/T5 provider errors) — clip first, then compact + budget truncate, then retry
  ▼
session end → clear_session() removes the session folder (evicted/ + plans) and the watermark
```

| 단계 | 메커니즘 | LLM 비용 | 모델 뷰에 미치는 영향 |
|---|---|---|---|
| **도구 반환** | `ContextEvictionMiddleware` (P0-2 / P2-4) | 없음 | 일반 결과 > 20 000자 → head/tail 프리뷰 + 파일 포인터; `read_file` → 4 000자 슬라이스 + 안내 |
| **모델 경계** | `MessagePersistenceMiddleware`의 `after_model` | 없음 | 전문을 MesMemory에 기록(state 불변) |
| **인간 메시지** | `ContextEvictionMiddleware` (P1-9) | 없음 | 요청 뷰만 프리뷰로 절단; state/MesMemory는 전문 유지 |
| **오버플로(첫 수)** | `clip_overflow_tail` (P1-2) | 없음 | 꼬리 도구 결과를 스텁화; 메시지 정체성과 페어링 불변 |
| **오버플로(기존 라우트)** | `summarization` 4-라우트 디스패치 | compact 계열 라우트는 보조 LLM 1회 | 트렁케이션 및/또는 이력 압축 |
| **오버플로(프로바이더 오류)** | T4/T5 강제 복구 | compact 단계마다 1회 | 클립 → 압축 + 예산 트렁케이션, 최대 3회 재시도 |

## 🗂️ 정보 출처

그래프 state나 MesMemory에 도달하는 모든 정보는 아래 출처 중 하나로 들어온다. `origin` 열은 전량 출처 마커로 승격 중이다: `NULL`은 태깅 이전에 기록된 기존 사용자 메시지(읽기 측에서 `user`로 취급), `internal=True`를 가진 메시지는 **사용자 요청이 아니다** —— 요약의 Unresolved 목록은 사용자가 직접 보낸 메시지만 받는다(긍정 식별 / positive identification). 비메시지 출처(축출 파일, 계획 지식)도 함께 기재한다: `messages` 행이 되지는 않지만 주입 가능한 컨텍스트다. `planned` / `reserved`로 표시된 행은 아직 구현되지 않았다.

| 정보 출처 | origin / 마커 | internal | 발생 상황 | 영속화 | 주입 동작 |
|---|---|---|---|---|---|
| 프런트엔드 WS 사용자 메시지 | `origin='user'` | — | 사용자가 클라이언트에서 메시지 전송 | `messages` 행 `origin='user'` + 전문(경계마다 영속화) | state/MesMemory에 상주; 모델 뷰는 프리뷰로 축출될 수 있음; 요약은 사용자 요청 보존(다중 요청 목록 + 축자 텍스트 + 축출 포인터: `planned`) |
| 채널 사용자 메시지(QQ 등) | `origin='user'` | — | 사용자가 채널 어댑터로 전송 | 위와 같음 | 위와 같음 |
| TaskIntent 스티어링 / 리마인더 | `origin='task_intent'` | `True` | 계획 활성 유도 / 작업 의도 무장(`task_intent/core.py::_task_intent_message`) | `messages` 행 | **사용자 요청이 아님** —— Unresolved 목록에 들어가지 않음 |
| 서브에이전트 완료 캐리어 | `origin='subagent_completion'` | `True` | 백그라운드 서브에이전트가 완료 후 결과 통지 | `messages` 행(origin은 영속화 이음매에서 각인, `context_engine/store/core.py`) | 사용자 요청이 아님; 모델 뷰에 가시 |
| 하트비트로 트리거된 턴 | `origin='heartbeat'` | — | 하트비트 서비스의 턴(**현재 이런 경로 없음 —— `reserved`**) | — | 사용자 요청이 아님 |
| cron으로 트리거된 턴 | `origin='cron'` | `True` | 예약 작업의 세션 턴(`origin_for_source`) | `messages` 행 | 사용자 요청이 아님 |
| 압축 요약 쌍 | `lc_source='summarization'`(`additional_kwargs` 내, origin 열 아님) | — | 압축 산출물(`_build_new_messages`) | **MesMemory에 영속화되지 않음**; state 요약 쌍 | `<summary>`가 모델 뷰에 상주; `<prior-summary>`로 체인 연속 |
| 축출 파일 | 비메시지 —— 디스크 파일 | — | P0-2 / P1-9 축출 | `SESSIONS_DIR/<session_id>/evicted/`(바이트 단위 전문) | 필요 시 `read_file`; 요약 체인이 `evicted_refs[]` 포인터를 운반(`planned`) |
| 계획 지식 | 비메시지 —— 디렉터리 | — | plan extraction | `workspace/knowledge/plans/<plan_key>/` | `plan_ref`로 `<knowledge>` 블록 주입 |
| FACTS.md(`planned`, 미구현) | `workspace/memory/FACTS.md` | — | 계획 횡단 사실(EXPERIENCE_ROUTING_PLAN Part 3) | memory 파일 | `planned` —— 현행 시스템에 포함되지 않음 |

## 💾 경계마다의 영속화

`agent/middlewares/message_persistence/core.py`(`MessagePersistenceMiddleware`)는 이 페이지 전체가 기대는 영속화 토대다. **두 시점**에 기록한다:

| 메시지 | 영속화 시점 |
|---|---|
| `ToolMessage` | **도구 핸들러가 반환하는 순간** (`wrap_tool_call` / `awrap_tool_call`) |
| `HumanMessage` | 해당 턴의 첫 모델 경계 (`after_model` / `aafter_model`) |
| `AIMessage` (그 `tool_calls` 포함) | 모델이 생성한 직후의 경계 |
| HITL 거부 (`status="error"` ToolMessage) | 다음 모델 경계 — HITL이 이 미들웨어를 바깥에서 감싸므로 그 단락은 플러시에 도달하지 않는다 |

두 시점은 하나의 배치 파이프라인을 공유한다: 역할/마커 필터 → 워터마크 → HITL 거부 재페어링 + 도구 결과 중복 제거 → 기록 → 마킹. 중요한 세부:

- **`persisted_message_ids` 워터마크(write-once).** 기록 전에 `filter_persisted_message_ids`가 세션의 묘비에 이미 있는 조회 키를 가진 후보를 버리고, 기록 후에 `mark_message_ids_persisted`가 라이터에 전달된 모든 후보에 묘비를 남긴다. 워터마크는 SQLite 테이블(`context_engine/store/db.py`)이므로 재시작을 견딘다.
- **id + 핑거프린트 이중 조회.** 도구 결과는 반환 시점에 영속화되며 **그때는 그래프 reducer가 아직 id를 부여하지 않았다**; 따라서 워터마크 조회는 LangGraph 메시지 id와 `sha1:` 콘텐츠 핑거프린트(role + content + `tool_call_id`)를 모두 확인한다. 다음 경계와 재시작 후 재생 모두 두 키 중 하나로 같은 논리 메시지에 매칭된다(`prepare.py`의 `_watermark_lookup_keys`).
- **요약 쌍은 절대 영속화되지 않는다.** `_is_persistable`은 `additional_kwargs["lc_source"] == "summarization"` 태그가 붙은 모든 메시지(쌍의 AI 절반)를 버리고, `HumanMessageRowBuilder.build`(`context_engine/store/core.py`)는 인간 절반에 `None`을 반환한다. 압축 산출물은 프롬프트 발판이지 대화 이력이 아니다.
- **페일오픈.** `session_id`가 없으면 조용히 건너뛰고, 라이터 오류는 로그만 남기고 묘비를 남기지 않아 같은 배치가 다음 경계에서 재시도된다. 영속화가 턴이나 도구 결과를 깨뜨리는 일은 결코 없다.

이것이 이 페이지의 이후 메커니즘들이 그토록 적극적일 수 있는 이유다: **어떤 후속 단계가 줄이기 전에 모든 페이로드는 이미 영속화되어 있다.**

## 🗜️ 도구 결과 축출 (P0-2)

`ContextEvictionMiddleware.wrap_tool_call`은 도구 응답을 **그래프 state에 들어가기 전에** 가로챈다(`agent/middlewares/context_eviction/core.py`; 원시 함수는 `pub/func/message/eviction.py`):

- 추출 텍스트가 **`evict_threshold_chars`(20 000자)를 초과하는** 일반 결과는 `SESSIONS_DIR/<session_id>/evicted/<tool_call_id>_<md5[:8]>.txt`에 기록되고, 메시지 내용은 파일 경로를 담은 head/tail 프리뷰로 대체된다.
- `excluded_tools`(8개 이름 — `read_file`, `write_file`, `patch_file`, `search_files`, `list_files`, `memory`, `skill_view`, `skill_list`)는 그대로 통과한다: 페이로드가 이미 백엔드 파일시스템에 있거나 회수 비용이 낮다. `read_file`은 추가로 아래의 슬라이스 경로를 탄다.
- 대체는 `ToolMessage.model_copy`로 만들어지므로 메시지 `id`, `tool_call_id`, `name`, `status`, `additional_kwargs`가 모두 살아남는다 — 페어링, 새니타이저, 워터마크가 계속 같은 논리 메시지에 매칭된다.
- 멀티모달 비텍스트 블록(이미지/오디오/비디오)은 원문 그대로 유지되고 텍스트 블록만 교체된다.
- 안전 가드: 안전하지 않은 `session_id`(빈 값 / `.` / `..` / 구분자 포함)는 디스크를 건드리지 않고 건너뛴다; `[evicted to: …]` 마커가 있는 메시지는 다시 축출되지 않는다; 프리뷰가 원문보다 작아지지 않는 경우(한 줄짜리 거대 행)는 건너뛴다.

프리뷰 형식(`pub/func/message/eviction.py`, `preview_head_lines = preview_tail_lines = 5`):

```text
[evicted to: <path>]
--- head (5 lines) ---
<first 5 lines>
...
--- tail (5 lines) ---
<last 5 lines>
[full content: N chars, evicted at <ts>]

Use read_file(file_path='<path>', offset=0, limit=100) to read the full content in chunks.]
```

**세 개의 보관소** — 축출 후 각 사본이 있는 곳:

| 위치 | 보관 내용 |
|---|---|
| 그래프 state / checkpointer / 다음 모델 호출 | **프리뷰만** |
| MesMemory(`messages` 테이블) | **전문** — 도구 반환 순간 내부 영속화 계층이 기록 |
| `SESSIONS_DIR/<session_id>/evicted/` | 바이트 단위로 동일한 사본; `load_evicted()` / `read_file`로 회수 |

이 분할을 성립시키는 순서: `wrap_tool_call` 체인에서 `MessagePersistenceMiddleware`는 **가장 안쪽**에 있고 `ContextEvictionMiddleware`는 그 **바깥**에 있다. 따라서 원본 결과가 먼저 플러시되고 프리뷰만 state로 넘어간다. 워터마크는 양쪽 모두에서 덮인다: `model_copy`가 내부 플러시의 프로세스 내 `_db_persisted` 마커를 운반하고, 원본 기록이 성공한 경우 대체 메시지의 워터마크 키에 추가 묘비가 남는다(`_cover_with_watermark`) — 마커가 사라지고 프리뷰 핑거프린트가 원본과 더 이상 일치하지 않는 재시작을 이것이 덮는다. 원본 기록이 실패하면 아무 묘비도 남기지 않고 다음 경계가 프리뷰 내용으로 재시도한다.

## ✂️ `read_file` 슬라이스 (P2-4)

`read_file` 결과는 **축출되지 않는다** — 파일이 이미 디스크에 있으니 두 번째 사본을 쓰는 것은 순수한 중복이다. 대신 `slice_read_file_result`(`pub/func/message/eviction.py`)가 내용을 **앞 `_READ_FILE_SLICE_CHARS`(4 000)자**와 복구 안내(`"...[Output was truncated due to eviction threshold. Use read_file with offset and limit to retrieve specific portions.]"`)로 대체한다. 축출 파일은 쓰지 않으며, 헬퍼는 멱등하다: 이미 슬라이스된(안내가 있는) 결과는 그대로 반환된다.

이것은 2단계 축소의 **실행 시점** 절반이다. **압축 시점** 절반은 `pub/func/message/target_truncation.py::_truncate_read_file_content`에 있다: 압축이 컨텍스트를 자를 때 각 `ToolMessage`를 `tool_call_id`로 `read_file` 호출에 되돌려 매핑하고, `max_tool_output_chars`(2 000)의 head 30% + tail 30%를 남기며, 중간을 **파서가 도출한 1-based 연속 오프셋**을 담은 복구 안내로 바꾼다(`Use offset=<N> to continue reading…`; 페이로드가 파싱되지 않으면 "처음부터 다시 읽기"로 폴백).

두 단계는 구조적으로 상호 보완적이다: 압축 시점이 실행 시점에 슬라이스된 페이로드를 나중에 자르면 잘린 JSON이 더 이상 파싱되지 않으므로 압축 안내는 결정론적으로 재시작 형식으로 폴백한다 — 잘못된 오프셋이 방출되는 일은 결코 없고, 실행 시점 슬라이스 헬퍼가 자기 출력을 다시 슬라이스하는 일도 없다.

## 📥 인간 메시지 축출 (P1-9)

사용자는 어떤 도구도 만들지 않은 페이로드를 붙여넣을 수 있다: 로그, 문서, 전사록, 코드베이스 전체. `ContextEvictionMiddleware`는 DeepAgents의 인간 메시지 축출을 Sherry 고유의 분할로 이식했다.

- **트리거**(`before_model` / `abefore_model`): `human_evict_enabled`가 참이고 **마지막** 메시지가 `HumanMessage`이며 `lc_evicted_to`가 없고 추출 텍스트가 **`human_evict_threshold_chars`(200 000자)를 초과**할 때. 마지막 메시지만 검사하므로 과거 사용자 턴은 다시 들여다보지 않는다.
- **태깅 + 오프로드**(`evict_human_message`): 전문이 `SESSIONS_DIR/<session_id>/evicted/human-<msg_id|timestamp>.md`에 기록된 뒤, 훅이 부분 state 업데이트 `{"messages": [tagged]}`를 반환한다. `tagged`는 **id와 내용이 그대로**이고 `additional_kwargs["lc_evicted_to"]`만 추가된다. 표준 `add_messages` reducer가 메시지를 **id로 제자리 갱신**한다 — 메시지 리스트 재작성 없음, `DeltaChannel` 의존 없음, state 측에서의 프리픽스 캐시 무효화 없음. 파일이 태그보다 먼저 기록되므로 쓰기 실패가 매달린 포인터를 남기는 일은 없다.
- **모델 뷰**(`wrap_model_call` / `awrap_model_call`): `lc_evicted_to`를 가진 모든 `HumanMessage`는 **요청 안에서만**(`request.override(messages=...)`) state 텍스트로 만든 프리뷰로 대체된다: 축출 경로, head/tail 각 5줄, `read_file` 복구 안내. 비텍스트 블록(이미지/오디오/비디오)은 원문 그대로 유지된다(`_build_evicted_content`) — 미디어가 텍스트 파일로 오프로드되는 일은 없다.
- **자가 치유**: 축출 파일이 없으면(세션 디렉터리 정리, 디스크 문제) `_heal_eviction_file`이 다음 모델 호출에서 state 텍스트로 다시 쓴다 — 단 대상이 해당 세션 자신의 `evicted/` 디렉터리일 때만. 태그 안의 외부 경로는 경고와 함께 거부된다.

### 도구 결과와 반대인 3-상태 분할인 이유

| 위치 | 도구 결과(P0-2) | 인간 메시지(P1-9) |
|---|---|---|
| 그래프 state / checkpointer | 프리뷰만 | **전문** + `lc_evicted_to` 태그 |
| MesMemory(`messages` 테이블) | 전문 | **전문**(태그는 영속화를 필터하지 않는다) |
| 다음 모델 호출(요청 뷰만) | 프리뷰 | 프리뷰(경로 + `read_file` 힌트) |
| `SESSIONS_DIR/<session_id>/evicted/` | 바이트 단위 동일 사본 | 바이트 단위 동일 사본 |

이 비대칭은 메시지가 **언제** 영속화되는지에서 나온다. 도구 결과는 도구 반환 시점에 내부 영속화 계층이 플러시하므로 state가 안전하게 프리뷰만 가질 수 있다. 인간 메시지는 턴의 첫 `after_model` 경계에서 영속화된다 — state가 프리뷰만 가지면 MesMemory가 프리뷰를 아카이브하고 `message_search`와 압축 모두 진짜 텍스트를 잃는다. 그래서 state는 전문을 유지하고 요청 뷰만 절단된다. 메시지 id가 결코 바뀌지 않으므로 워터마크는 그대로이고 두 번째 행도 기록되지 않는다.

## ⚡ 오버플로 테일 클립 (P1-2)

최신 도구 출력은 대개 가장 큰 컨텍스트 소비자이자 가장 희생 가능한 부분이다 — 그리고 그 모두가 이미 영속화되어 있다. `pub/func/message/overflow_clip.py::clip_overflow_tail`은 이것을 **모든 오버플로 경로의 첫 번째, 제로 LLM 수**로 만든다:

- **라우트보다 먼저.** `Summarization._dispatch_overflow_route`(T1/T2/T3)는 모든 비-`fits` 라우트에 대해 실행 전에 클립을 돌리고, `_forced_recovery_request`(T4/T5)는 강제 압축 단계 전에 돌린다. 클립 **단독**으로 추정치가 경계선 아래로 떨어지면 요청은 스텁된 리스트 그대로 반환되고 라우트는 전혀 실행되지 않는다 — 예산 트렁케이션도, 보조 LLM 호출도 없다.
- **클립 방식.** 꼬리에서 **연속된 `ToolMessage` 배치**를 훑으며(첫 비-`ToolMessage` 또는 이미 스텁된 메시지에서 중단) 각 내용을 `model_copy`로 압축 스텁으로 바꾼다. 삭제·재정렬·주입되는 메시지는 없다: `id`, `tool_call_id`, `name`, `additional_kwargs`가 모두 살아남아 도구 호출/결과 페어링과 영속 워터마크가 그대로 유지된다.
- **마커 생존.** 스텁은 P0-2의 `[evicted to: …]` 포인터(+ `read_file` 힌트)와 P2-4 슬라이스 안내를 그대로 다시 실어, 클립 후에도 복구 경로가 계속 작동한다.
- **멱등.** 스텁은 스캔 대상 배치를 종료시키므로 두 번째 순회는 no-op — T4/T5 재시도 예산이 동일 클립으로 소모되는 일은 없고, 다음 시도는 압축으로 퇴화한다.
- **유계.** `overflow_clip_max_remove`(10)가 한 번의 클립이 스텁화할 수 있는 메시지 수 상한; `overflow_clip_min_keep`(5)이 짧은 트랜스크립트에서 클립을 비활성화; `overflow_clip_enabled`가 마스터 스위치다.

### 제로 LLM 고속 경로 vs. 퇴화 경로

| 상황 | 실행되는 것 | LLM 호출 |
|---|---|---|
| 클립 단독으로 추정치가 임계값 아래로 | 스텁 리스트 반환; 라우트 미실행 | **0** |
| 클립 불충분(또는 비활성 / 대상 배치 없음) | 클립 결과 폐기; 기존 라우트가 **원본 리스트 그대로** 실행 | 라우트 의존(compact 계열은 보조 LLM 호출) |
| T4/T5 프로바이더 오류, 첫 복구 시도 | 먼저 클립; 충분하면 스텁 리스트로 프로바이더 호출 재시도 | **0** |
| T4/T5 프로바이더 오류, 클립 불충분 | 강제 압축 + 예산 트렁케이션 후 재시도 | compact 단계마다 보조 LLM 1회(≤ `MAX_OVERFLOW_RETRIES = 3`) |

수용 조건은 엄격하다: 클립 **단독**이 순수 로컬 추정치를 경계선 아래로 내릴 때만 적용된다(`estimate_messages_tokens(messages, reported_tokens=0)` — 오래된 `usage_metadata`가 복구를 주도하는 일은 결코 없다). 불충분한 클립은 폐기되어 기존 라우트가 원본 리스트에서 동작한다.

## 🧵 체인 요약 필터링

압축은 이력을 `lc_source="summarization"` 태그가 붙은 `HumanMessage` / `AIMessage` 쌍(AI 절반이 `<summary>` 태그 안에 요약을 담는다)으로 대체한다. 다음 압축에서 그 오래된 요약을 직렬화된 `<conversation>`에 다시 넣으면 토큰 낭비와 요약기 혼란을 부른다.

`agent/middlewares/summarization/core.py`는 두 단계로 처리한다:

1. `_extract_previous_summary`가 트랜스크립트에서 이전 요약 텍스트를 꺼낸다(가장 최신 태그된 `AIMessage`, 그다음 태그된 `HumanMessage`를 찾는다).
2. `_filter_summary_messages`가 새 프롬프트로 직렬화될 리스트에서 `lc_source="summarization"` 메시지를 **전부** 제거한다. `_build_summary_prompt`는 추출된 텍스트를 `<prior-summary>`로 별도 주입하며 갱신 지시문과 나란히 놓는다; 모델은 하나의 통합 요약을 만들 것, 이전 요약은 이후 폐기된다는 것을 듣는다.

요약 쌍 자체도 MesMemory에 결코 들어가지 않는다([경계마다의 영속화](#-경계마다의-영속화) 참조) — 원본 이력은 원본 그대로다.

## 🔗 상호작용과 순서 보장

순서 보장(`agent/core.py`에서 검증, 리스트 순서 = 등록 순서):

| 훅 단계 | 여기서 중요한 순서 |
|---|---|
| `before_agent`(리스트 순) | `MultimodalProcessor`가 모델 루프 **전에** 돌므로 P1-9 태깅은 항상 미디어 힌트가 병합된 최종 텍스트를 본다 |
| `before_model`(리스트 순) | P1-9 태깅이 `ToolCallNormalize` / `SubagentCompletionDrainMiddleware`보다 먼저 |
| `wrap_model_call`(바깥→안) | ContextEviction(P1-9 뷰 대체) → … → Summarization(가장 안쪽, LLM에 가장 가까움) |
| `after_model`(역순) | `MessagePersistenceMiddleware`가 모델 이후 **첫** 훅 — HITL이 거부된 도구 호출을 벗겨내거나 `GraphInterrupt`를 올리기 전에 AI 메시지가 영속화된다 |
| `wrap_tool_call`(바깥→안) | IterationBudget → ToolGuardrails → ContextEviction → PathGuard → HeartbeatStaleness → HumanInTheLoop → **MessagePersistence(가장 안쪽)** — 원본 결과가 먼저 플러시되고 나가는 길에 프리뷰로 교체된다 |

상호작용 맵:

| 상호작용 메커니즘 | 무슨 일이 일어나는가 |
|---|---|
| **Summarization / 압축** | P1-9가 전문을 state에 남기므로 압축이 여전히 그것을 본다; 요약 쌍은 영속화와 다음 `<conversation>` 양쪽에서 제외된다 |
| **P1-2 오버플로 테일 클립** | `model_copy`로 `ToolMessage` 내용만 스텁화; `HumanMessage`는 절대 건드리지 않으므로 `lc_evicted_to` 태그와 state 전문이 모든 클립을 견딘다; 스텁은 P0-2 / P2-4 마커를 앞으로 운반한다 |
| **체인 요약 필터링** | 직렬화되는 대화에서만 `lc_source="summarization"` 메시지를 제거; state 트랜스크립트와 MesMemory 저장소는 불변 |
| **HITL** | 거부는 도구 반환 플러시를 우회하고(HITL이 바깥에서 영속화를 감쌈) 다음 경계에서 영속화된다; 영속화 배치는 거부된 도구 호출을 다시 붙여 거부가 짝지어진 AI 행을 유지하게 한다. HITL은 `HumanMessage`를 보지 않으므로 축출과 직교한다 |
| **`message_search`** | FTS5/SQLite 검색은 MesMemory 위에서 돌며, 거기에는 **완전한** 도구 결과와 **완전한** 인간 텍스트가 아카이브되어 있다 — 축출은 모델 뷰만 줄인다 |
| **프리픽스 캐싱** | P1-9는 id로 메시지를 제자리 갱신하고(내용 동일, id 동일) 다른 것은 다시 쓰지 않으므로, 모델 가시 프리픽스는 프리뷰 뷰가 실제로 다를 때만 무효화된다; 도구 축출은 메시지가 state에 들어가기 전에 일어나므로 모델이 보는 것은 처음부터 프리뷰판뿐이다 |
| **도구 페어링 / 새니타이저** | 모든 대체가 `id`와 `tool_call_id`를 보존한다; `sanitize_tool_use_result_pairing`이 축출 때문에 수리할 일은 결코 없고, 스텁은 계속 유효한 페어링 입력이다 |
| **서브에이전트 세션** | 자식 파이프라인은 `ContextEvictionMiddleware`와 `MessagePersistenceMiddleware`를 **등록하지 않는다**: 자식 트랜스크립트는 완전한 도구 결과를 유지하고 checkpoint에만 존재한다 |

## 🛠️ 설정

`TOOL_RESULT_EVICTION`(`config/features/agent_side/tool_result_eviction.py`):

| 키 | 기본값 | 의미 |
|---|---|---|
| `enabled` | `True` | 도구 결과 축출(P0-2) 마스터 스위치 |
| `evict_threshold_chars` | `20_000` | 이 문자 수를 넘는 텍스트를 축출 |
| `preview_head_lines` / `preview_tail_lines` | `5` / `5` | 프리뷰 head/tail 줄 수 |
| `eviction_subdir` | `"evicted"` | `SESSIONS_DIR/<session_id>/` 아래 하위 디렉터리 |
| `excluded_tools` | 8개 이름 | 절대 축출하지 않음(`read_file`은 슬라이스 경로로) |
| `human_evict_enabled` | `True` | 인간 메시지 축출(P1-9) 마스터 스위치 |
| `human_evict_threshold_chars` | `200_000` | 인간 메시지 트리거 임계값 |
| `human_preview_head_lines` / `human_preview_tail_lines` | `5` / `5` | 인간 메시지 프리뷰 head/tail 줄 수 |

`SUMMARIZATION`(`config/features/agent_side/summarization.py`) 중 이 페이지가 의존하는 키:

| 키 | 기본값 | 의미 |
|---|---|---|
| `overflow_clip_enabled` | `True` | P1-2 테일 클립 마스터 스위치 |
| `overflow_clip_max_remove` | `10` | 한 번의 클립이 스텁화하는 꼬리 메시지 상한 |
| `overflow_clip_min_keep` | `5` | 트랜스크립트 하한: 이 길이 이하면 결코 클립하지 않음 |
| `max_tool_output_chars` | `2_000` | 도구 결과의 압축 시점 클립 예산 |
| `content_head_ratio` / `content_tail_ratio` | `0.3` / `0.3` | 압축 시점 클립의 head/tail 유지 비율 |

이 노브들에는 환경 변수가 없다: 설계상 위 feature TypedDict들의 코드 기본값이다.

## 🧪 테스트 맵

| 스위트 | 커버 내용 |
|---|---|
| `tests/agent/middlewares/context_eviction/test_context_eviction.py` | P0-2/P2-4 미들웨어 동작: 축출, 제외, 슬라이스, 워터마크 커버, 페일오픈 |
| `tests/agent/middlewares/context_eviction/test_human_eviction.py` | P1-9 태깅, reducer 제자리 갱신, 모델 뷰 절단, 자가 치유, 미디어 보존 |
| `tests/agent/middlewares/message_persistence/test_message_persistence.py` | 경계 영속화, 워터마크 write-once, 거부 재페어링 |
| `tests/agent/middlewares/message_persistence/test_tool_result_persistence.py` | 도구 반환 플러시와 id 없는 핑거프린트 / 마커 상호작용 |
| `tests/agent/middlewares/message_persistence/test_compression_no_persistence.py` | 압축 경로가 MesMemory에 아무것도 쓰지 않음 |
| `tests/pub/func/message/test_eviction.py` | 순수 축출 원시 함수: 임계값, 프리뷰, 멱등성, 안전하지 않은 세션 id |
| `tests/pub/func/message/test_read_file_slice.py` | P2-4 실행 시점 슬라이스와 멱등성 |
| `tests/pub/func/message/test_overflow_clip.py` | P1-2 순수 클립: 꼬리 배치 감지, 게이트, 토큰 목표, 마커 보존 |
| `tests/agent/middlewares/test_summarization_overflow_clip.py` | P1-2 미들웨어 통합: 제로 LLM 복구, 퇴화, T4/T5, 동기/비동기 패리티 |
| `tests/agent/middlewares/test_summary_message_filtering.py` | 체인 요약 필터링과 `<prior-summary>` 주입 |
| `tests/context_engine/store/test_persisted_message_ids.py` | `persisted_message_ids` 워터마크 저장소 |
| `tests/full/test_context_governance_e2e.py` | 라이브 네트워크 e2e(실제 LLM + 실제 그래프): 여섯 메커니즘 종단 간 — 명시적으로 실행 |

```bash
# Hermetic suites (CI gate)
uv run pytest tests/agent/middlewares/context_eviction \
    tests/agent/middlewares/message_persistence \
    tests/pub/func/message/test_eviction.py \
    tests/pub/func/message/test_read_file_slice.py \
    tests/pub/func/message/test_overflow_clip.py \
    tests/agent/middlewares/test_summarization_overflow_clip.py \
    tests/agent/middlewares/test_summary_message_filtering.py \
    tests/context_engine/store/test_persisted_message_ids.py -q

# Live-network e2e — RUN EXPLICITLY, never part of the CI gate
uv run --no-sync pytest tests/full/test_context_governance_e2e.py -v
```

## 🧹 정리 의미론과 한계

- **`clear_session`은 한 번에 전부 지운다.** `server/DAO/messages.py::clear_session`은 세션의 MesMemory 행(`persisted_message_ids` 워터마크까지 함께 삭제), checkpointer 이력, 그리고 `SESSIONS_DIR/<session_id>/` 폴더 전체를 삭제한다 — 따라서 **`evicted/` 파일과 `plans/`가 세션과 함께 삭제된다**(`config/path.py::session_plans_dir`가 같은 전면 삭제 계약을 기록한다). 인메모리 레지스터는 마지막에 정리된다.
- **축출은 모델 뷰 축소이며 결코 삭제가 아니다.** 이 페이지가 줄이는 모든 페이로드는 MesMemory에 아카이브되거나, 그래프 state에 전문이 있거나(인간 메시지), 디스크 `evicted/` 아래에 있다 — 그리고 모든 프리뷰가 포인터를 운반한다.
- **`evicted/` 디렉터리는 세션 소유지만 가비지 컬렉션이 없다.** 파일은 `clear_session`까지 살아남는다; 메시지별 TTL은 없다. 거대한 도구 결과가 많은 장수 세션은 `workspace/sessions/<session_id>/evicted/`에 디스크 사용량을 쌓을 수 있다.
- **한 줄짜리 거대 행은 결코 축출되지 않는다.** head와 tail이 모두 전체 페이로드를 담게 되면 프리뷰가 원문보다 작아질 수 없어 메시지는 그대로 남는다(도구 경로와 인간 경로 모두).
- **`read_file` 슬라이스는 슬라이스 자체에서 복구할 수 없다 — 설계상.** 소스 파일이 복구 경로이며, 안내가 이어 읽는 방법을 정확히 알려준다. 파일이 없어지거나 삭제될 때만 무너진다.
- **인간 메시지 축출은 꼬리 메시지로 제한된다.** 거대 페이로드 뒤에 또 다른 사용자 턴이 이어져도 재검사되지 않는다; "마지막 메시지만"은 확정된 이력을 다시 들추지 않기 위한 의도적 설계다.
- **테일 클립은 꼬리 배치가 클 때만 도움이 된다.** 컨텍스트가 인간 턴이나 비도구 메시지에 먹혀 있으면 기존 라우트로 퇴화한다; P1-2는 최적화이지 보장이 아니다.
- **서브에이전트 트랜스크립트는 범위 밖이다.** 자식은 완전한 도구 결과를 유지한다(축출 없음, 영속화 없음) — 그 트랜스크립트는 checkpoint에만 존재하며 클라이언트 가시 MesMemory 이력에 결코 들어가지 않는다.
