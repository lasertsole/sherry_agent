# 🧠 Summarization 내부 — 트렁케이트, 컴팩트, 요약, 가드

[English](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> [Summarization](../README.ko.md)의 일부: 내부 트랙 — 예산 트렁케이션과 TTL, 컴팩트 파이프라인, 미디어 오프로드, LLM 요약 체인, 정적 폴백, 출력 쌍, 안티-스래싱 가드 매트릭스, 시스템 프롬프트 갱신, 등록 지점.

---

## ✂️ 트렁케이트 트랙: 예산 트렁케이션과 TTL 모듈

`_run_budget_truncation`(overflow.py:219) 안에서는 두 개의 트렁케이션 레이어가 순서대로 돕니다:

**1단계 — 도구 호출 인자**(`pub/func/message/tool_args_truncate.py`): JSON 직렬화가 `MIN_ARGS_CHARS_TO_TRUNCATE (500)`자를 넘는 모든 `AIMessage.tool_calls[].args` — 해당 도구가 `PROTECTED_TOOLS`에 없는 경우 — 는 `MAX_TOOL_ARGS_CHARS (2_000)`자로 봉인되는 `{"_truncated_args": "head…[args truncated, omitted N chars]…tail"}`로 대체됩니다(머리 30% / 꼬리 30%, 도구 결과와 같은 비율). 덕분에 `args`는 dict로 남고(LangChain의 `ToolCall.args` 타입), 모든 프로바이더 어댑터에서 JSON 직렬화 가능하며, 모델은 인자가 잘렸음을 볼 수 있습니다. 최근 `TRUNCATABLE_RECENT_SKIP (6)`개 메시지는 건너뛰고, 대체된 `AIMessage`는 `model_copy` 클론입니다 — tool_call_ids는 절대 건드리지 않으므로 AIMessage↔ToolMessage 페어링은 온전히 유지됩니다.

**2단계 — 도구 결과**: `pub/func/message/tool_result_ttl.py`는 트렁케이트 트랙이 사용하는 제자리 절단을 제공합니다. 설계 불변식(하중 지지):

- **제자리만** — 이 모듈은 메시지를 절대 삭제, 재정렬, pop하지 않습니다; `msg.content`(또는 content 리스트 블록)만 수정하고 인덱스를 반환합니다. 이것이 프로바이더 API와 `ToolCallNormalize`가 의존하는 tool-call/`ToolMessage` 페어링을 보존합니다.
- **비어 있지 않은 플레이스홀더** — 잘려나간 결과는 항상 비어 있지 않은 내용을 유지합니다: `ToolCallNormalize.before_model`은 **빈 `ToolMessage`를 드롭**해서 트랜스크립트를 정화하므로, 빈 플레이스홀더는 조용히 페어링을 깨뜨립니다.
- **머리 30% / 꼬리 30% 보존**(`CONTENT_HEAD_RATIO` / `CONTENT_TAIL_RATIO`)에 생략 마커를 붙입니다.

미들웨어가 실제로 소비하는 것: `truncate_tool_args`(1단계, 인자)와 **`truncate_to_budget`**(2단계, 도구 결과) — 라우터의 후보 목록으로 구동되며, `_run_budget_truncation`(overflow.py:219)이 예산(`usable × TRUNCATE_BUDGET_RATIO`)에 맞을 때까지 후보를 자릅니다. 1단계는 변경하지 않고 새 `AIMessage`를 반환하므로, 이 함수는 최종 목록을 반환하고 모든 호출자는 그 목록을 `request.override`에 **반드시** 넣어야 합니다.

**read_file 결과는 복구 가능하게 유지됩니다**: `pub/func/message/target_truncation.py`의 머리+꼬리 클립(비 LLM 전략 `_run_non_llm_strategies`가 실행)은 각 `ToolMessage`를 `tool_call_id`로 AIMessage 도구 호출에 되짚습니다; 도구가 `read_file`이고 `args.file_path`가 있으면 잘린 중간은 익명 마커 대신 복구 안내로 대체됩니다. 안내는 같은 머리 30% / 꼬리 30% 비율을 유지하고 원본 `file_path`를 명시하며 `Use offset=<N> to continue reading: read_file(file_path='<path>', offset=<N>, limit=500)`를 제시합니다. `N`은 **머리에 완전히 남지 않은 첫 행의 절대(1-based) 파일 행 번호** — 따라서 `offset=100`으로 읽은 페이지는 머리가 실제로 멈춘 지점부터 이어지고, 중간에 잘린 행은 다시 읽히며 절대 건너뛰지 않습니다. offset을 도출할 수 없으면(페이로드가 read_file JSON 결과가 아니면) 안내는 `offset=1`부터 다시 읽기를 요청합니다 — 추측 offset은 내지 않습니다. 다른 모든 도구는 익명 `...[truncated N chars]...` 마커를 바이트 단위로 유지합니다.

TTL 레지스트리 자체(`record_first_seen` / `select_expired` / `truncate_expired`, `PRUNE_TTL_SECONDS = 300`, `TTL_REGISTRY_MAX_ENTRIES = 512`, `tool_call_id` 키, 재시작 시 휘발)는 오늘날 **테스트 스위트만 사용**합니다 — 미들웨어에는 나이 기반 만료 로직이 연결되어 있지 않습니다("[정직함과 한계](../README.ko.md#%EF%B8%8F-정직함과-한계)" 참조).

## 🔁 컴팩트 트랙: `_apply_compression` 내부

`_apply_compression`(compression.py:85; 비동기 쌍둥이 compression.py:98)은 다음 순서로 실행됩니다:

1. **복구 컨텍스트 캡처**(`_capture_recovery_context`, compression.py:363): 마지막 사용자 요청(≤ 800자)과 파일 작업 래칫 — `read`/`write` 계열 도구 호출에서 경로를 추출하고(summary_generation.py:390), 이전 라운드의 집합과 병합(읽은 것은 기억되고, 수정된 파일이 읽기 전용으로 강등되는 일은 없음).
2. **비 LLM 전략**(`_run_non_llm_strategies`, compression.py:314): `중복 제거 → 프루닝 → 타깃 트렁케이트 → 도구 인자 트렁케이트`(상세는 [트렁케이트 트랙](#-트렁케이트-트랙-예산-트렁케이션과-ttl-모듈)). 이것들은 공짜입니다 — 모델 호출 없음.
3. **LLM 사용 여부 결정**:

   ```
   if tokens_after_non_llm > budget × 2  OR  skip_llm  OR  nothing was reduced:
       summarize [0:cutoff] and rebuild   → strategy "llm_summary" / "fallback"
   else:
       keep as-is                          → strategy "non_llm_sufficient"
   ```

   비 LLM 축소가 첫 기회를 얻습니다; 히스토리가 여전히 보존 예산의 두 배를 넘을 때(또는 거버너가 LLM 요약을 비활성했거나, 비 LLM 전략이 아무것도 줄이지 못했을 때)에만 보조 LLM을 씁니다.
4. **공격적 백스톱**(`_aggressive_truncate`, compression.py:360): 결과가 *그래도* 너무 크면, `AGGRESSIVE_TRUNCATE_CHARS (1 000)`자를 넘는 모든 `ToolMessage`가 마커와 함께 하드 컷됩니다 — 같은 상한을 넘는 모든 도구 호출 인자 JSON도 마찬가지입니다(머리만, `PROTECTED_TOOLS` 면제, `{"_truncated_args": ...}`로 대체).
5. **요약 자체 절단**(`_truncate_summary_messages`, compression.py:417): `SUMMARY_TOTAL_MAX_CHARS (16 000)`자를 넘는 기존 요약 메시지(`lc_source == "summarization"`)는 머리 30% / 꼬리 30%로 재절단됩니다(`_truncate_content`, compression.py:414).
6. **복구 주입**(`_inject_recovery_context`, compression.py:379): 캡처한 파일 작업 래칫이 요약의 `## Relevant Files` 섹션으로 재작성되어, 체크포인트가 항상 최신 읽기/수정 파일 맵을 품도록 합니다.
7. **장부 기록**(`_record_compression`, thrash.py:117), 마지막으로 `request.override(messages=..., system_message=...)`.

### 💾 압축 시점 nudge

메시지 영속화는 압축 경로 밖에서 동작합니다: `MessagePersistenceMiddleware`(`agent/middlewares/message_persistence/`)가 새 human/AI 메시지를 각 모델 호출 경계에서, 도구 결과를 반환 시 MesMemory로 플러시하고, 영속 워터마크 `persisted_message_ids`로 write-once를 보장합니다. compact는 압축과 아래 nudge 스케줄만 담당합니다. 트리거 의미론은 `agent/middlewares/README.md`를 참조하세요.

**압축 시점 nudge**(`agent/middlewares/summarization/nudges.py::schedule_compression_nudges`): 메모리 리뷰(`_nudge_memory`)는 압축마다 디스패치됩니다; 플랜 추출은 같은 시점에 `_detect_todo_all_complete`를 평가합니다. 둘 다 NUDGE 레인에서 fire-and-forget으로 디스패치되어 모델 호출을 막지 않습니다. nudge 락이 잡혀 있는 동안 압축은 디스패치를 완전히 건너뜁니다(큐잉 없음). 단발 `nudge_plan_extraction_fired` 플래그는 완료 사이클당 1회 추출을 보장하며, 한 번도 압축하지 않는 세션은 플랜 추출을 발화하지 않습니다.

**절단점 선택**(`_determine_cutoff`, compression.py:277): 히스토리를 턴으로 쪼개고, **최신에서 거꾸로** 걸으며 보존 예산 `clamp(window × 0.25, 2 000, 15 000)`(`_calculate_preserve_budget`, compression.py:70)에 맞춰 누적합니다; 통째로 안 들어가는 턴은 턴 중간에서 쪼개질 수 있습니다. `_adjust_for_orphan_pairs`(compression.py:311)가 절단점을 거꾸로 걸어 `ToolMessage`가 `AIMessage` 도구 호출과 떨어지는 경우가 없도록 합니다. 마지막 턴 비율 게이트가 발동하지 않는 한(마지막 사용자 턴 ≥ 전체 토큰의 `LAST_TURN_RATIO_THRESHOLD (0.5)` — `_check_last_turn_ratio`, wrap 진입 core.py:421 / core.py:503에서 호출), 절단점은 마지막 `HumanMessage`를 넘지 않습니다.

모든 실패 모드는 fail-open입니다: `_apply_compression`이 예외를 던지면 로그만 남기고 원본 요청이 그대로 진행됩니다 — 깨진 압축이 턴을 망치는 일은 없습니다.

## 🖼️ 압축 시점 미디어 오프로드 (인라인 미디어 → 참조)

압축이 base64 페이로드를 보조 요약 모델에 넘겨서는 안 됩니다. 요약될 프리픽스(`current_messages[:cutoff]`)를 직렬화하기 전에 `offload_inline_media`(`agent/middlewares/summarization/media_offload.py`)가 **그 범위만** 모든 인라인 미디어 블록을 다시 씁니다:

- `data:` URL / base64 페이로드(`image_url` / `audio_url` / `video_url`, 맨 `base64` 필드, `audio_bytes` / `video_bytes`, 또는 Anthropic 형식 `source.data`)를 디코드해 `SESSIONS_DIR/<session_id>/media/{sha256(raw)[:16]}{ext}`에 씁니다; 확장자는 매직 바이트에서 `media_handlers._infer_extension`으로 도출합니다;
- 동일 바이트는 한 번만 씁니다 —— 내용 해시 파일명이 단일 및 여러 압축에 걸쳐 중복을 제거합니다;
- 블록은 텍스트 포인터 `[evicted to: <path>]`로 대체됩니다. `pub/func/message/eviction.py`가 내는 것과 같은 마커라서 `_collect_evicted_refs`가 줍고 `_finalize_summary_doc`이 경로를 `SummaryDoc.evicted_refs`로 실어 나릅니다(*Evicted References*로 렌더링);
- 디코드나 쓰기에 실패한 블록은 `<media error="failed_to_offload" />`가 됩니다 —— 미디어 실패가 압축을 망치는 일은 없습니다.

보존 윈도우(`current_messages[cutoff:]`)는 절대 건드리지 않습니다: 미디어는 해당 턴이 실제로 요약될 때까지 남습니다. 두 압축 경로 모두 nudge 스케줄링과 메모리 플러시 전에 프리픽스 슬라이스에 오프로드를 호출합니다 —— `_apply_compression_under_lock`(동기)와 `_aapply_compression_under_lock`(비동기). 요약 프롬프트(`_SUMMARY_JSON_RULES` / `_SUMMARY_TEMPLATE`)는 포인터가 존재한다는 것, `evicted_refs`로 그대로 옮겨야 한다는 것, 시각/오디오/비디오 세부를 지어내지 말 것, 페이로드는 경로에서 가져올 수 있다는 것을 모델에 알립니다.

미디어 파일은 세션 트리 안에 있으므로 `clear_session()`이 `evicted/` 및 `plans/`와 함께 삭제합니다.

## 📝 LLM 요약: 프롬프트, 체이닝, 폴백

`_create_summary` / `_acreate_summary`(summary_generation.py:710 / summary_generation.py:758):

1. **직렬화**(`_serialize_for_summary`, summary_generation.py:207): 각 메시지가 태그 붙은 한 줄로 변합니다 — `[User]:`(≤ 2 000자), `[Assistant]:`(≤ 2 000자), `[Assistant tool call]: name(args: > 500 chars → head 300 + tail 150 + omission marker)`, `[Tool result|Tool error] (id):`(> 2 000자 → 1 800자 보존 + 생략 마커).
2. **이전 체크포인트 체이닝**(`_extract_previous_doc` / `_extract_previous_summary`): `additional_kwargs["lc_source"] == "summarization"`인 가장 최신 `AIMessage`를 찾아 먼저 구조화 `summary_doc` 페이로드로 읽습니다(`<prior-summary>`용으로 Markdown 재렌더링). 페이로드가 없는 메시지 — 레거시 세션, 또는 free-form으로 폴백한 회차 — 는 기존대로 `<summary>…</summary>` 본문에서 파싱합니다. 이전 Doc이 있으면 프롬프트가 `conversation + <prior-summary-json> + _SUMMARY_PROMPT_UPDATE_STRUCTURED`가 됩니다. 레거시 Markdown은 `<prior-summary>`로 그대로 주입되며, 업그레이드 후 첫 압축 라운드가 곧바로 새 `SummaryDoc`을 출력합니다(마이그레이션 불필요).
3. **구조화 출력**(`summary_doc.py::SummaryDoc`): 보조 모델은 `with_structured_output(SummaryDoc, method="json_mode")`로 래핑됩니다. 설정된 `glm-5.3-flash` 엔드포인트는 함수 호출 스키마를 무시하므로(기본 method는 자유 텍스트를 반환해 Pydantic 파서가 거부 — 실측 완료) `json_mode`가 주 티어입니다. 파싱/검증 실패는 원시 호출 + `json_repair`(`_sync_json_repair_doc` / `_async_json_repair_doc`)로 강등되고, 그것도 실패하면 레거시 free-form Markdown 프롬프트가 마지막 LLM 티어이며 정적 폴백이 최종 가드입니다.
4. **호출**은 보조 모델에 `config={"metadata": {"lc_source": "summarization"}}`로 수행되어, 다운스트림 도구 체인이 요약 호출을 식별할 수 있게 합니다.
5. **가드 레일:** free-form 응답이 비어 있거나 지나치게 짧으면 결정론적 요약으로 폴백하고, 예외도 마찬가지입니다. 실패 시 LLM이 최후의 발언권을 가지는 일은 없습니다.

**렌더링(Doc → Markdown).** `render_summary_markdown`은 순수·결정론적입니다(같은 Doc → 같은 바이트, 프리픽스 캐시 안전). 기존 섹션 골격을 출력하고, 배열이 비어 있지 않을 때만 *Active Plan Notes* / *Evicted References*를 덧붙입니다:

| `SummaryDoc` 필드 | 렌더링되는 섹션 | 코드층 cap |
| :--- | :--- | :--- |
| `latest_user_request` | `## Latest Unresolved User Request` | 그대로, 상한 없음 |
| `goal` | `## Goal` | — |
| `constraints` | `## Constraints & Preferences` | — |
| `completed` | `### Completed` | `completed[-5:]` |
| `in_progress` / `blocked` | `### In Progress` / `### Blocked` | — |
| `key_decisions` | `## Key Decisions` | `key_decisions[-5:]` |
| `next_steps` | `## Next Steps` | — |
| `critical_context` | `## Critical Context` | `critical_context[-3:]` |
| `relevant_files` | `## Relevant Files` | — |
| `active_plan_notes` | `## Active Plan Notes`(비어 있지 않을 때만) | `active_plan_notes[-20:]` |
| `evicted_refs` | `## Evicted References`(비어 있지 않을 때만) | `evicted_refs[-20:]` |

cap은 `cap_summary_doc`의 배열 슬라이스(체인에 저장되는 형태)이고, 렌더러는 표시 시 다시 슬라이스해 `"(N earlier items omitted for brevity)"`를 덧붙입니다. `evicted_refs`는 코드가 유지합니다: `_collect_evicted_refs`가 압축 범위의 `[evicted to: <path>]` 마커와 인간 메시지의 `lc_evicted_to` 태그를 스캔하고, `_finalize_summary_doc`이 이전 Doc 항목과 새 항목을 순서대로 중복 제거해 병합합니다. `_inject_recovery_context`는 파일 작업 래칫을 렌더링된 `## Relevant Files` 섹션과 저장 Doc의 `relevant_files` 필드 양쪽에 되씁니다.

### 🗂️ Active Plan Notes와 계획 컨텍스트 주입

압축은 계획을 인식합니다. 보조 모델 호출 전에 `_get_plan_context_sync(session_id)`(`core.py`, TaskFlow 블록 옆)가 이 세션이 실행 중인 계획을 권위 블록으로 렌더링합니다:

- 계획 활성 판정은 `agent/tools/todolist/knowledge/ownership.py`의 두 원시 연관 소스를 재사용합니다 — 세션의 `plan_ref` state 키와 이 세션의 todo 중 하나에 있는 `plan_ref`(`plan_context.py::resolve_active_plan`); **boulder 소스는 의도적으로 제외**됩니다.
- 주입 내용은 **계획 파일 상대 경로 + 계획 이름 + 미완료 todo 요약**이며, 계획 본문은 절대 주입하지 않습니다(모델이 경로를 `read_file`할 수 있음). todos가 모두 `completed` / `cancelled`인 세션은 **활성 계획이 아닙니다**.
- 이 블록은 두 프롬프트 경로(최초 요약과 체인 업데이트, 구조화와 레거시 free-form) 모두에 주입됩니다.

`SummaryDoc.active_plan_notes`는 모델이 아니라 파이프라인이 소유하므로, 계획 범위의 교훈은 계획이 활성인 동안 계속 유지됩니다:

- 체인 업데이트에서 이전 Doc의 항목을 **그대로, 순서대로 상속**하며, 모델은 새 한 줄 교훈(`증상 -> 회피책`)을 덧붙이는 것만 할 수 있습니다.
- 배열 상한은 `active_plan_notes[-20:]`; 체인 렌더는 `(N earlier items omitted for brevity)`를 덧붙이고 저장 페이로드는 잘라낸 꼬리를 유지합니다.
- 계획이 완료되면(todos가 모두 `completed` / `cancelled`) 리졸버가 "활성 계획 없음"을 보고하여 렌더에서 `## Active Plan Notes` 섹션이 사라지고 다음 Doc의 배열은 비워집니다. `plan_ref`가 전혀 없는 세션도 마찬가지로 배열을 갖지 않습니다.

**최신 사용자 요청 그대로 + 퇴출 포인터.** *Latest Unresolved User Request* 섹션은 결코 잘리지 않습니다: `latest_user_request`는 그대로 제시되며(레거시 템플릿의 `max 800 chars` 지시도 제거됨), 직렬화되는 `<conversation>`은 **state**에서 옵니다 — 퇴출된 인간 메시지는 state에 전체 텍스트와 `lc_evicted_to` 태그를 유지하고 모델 뷰만 미리보기입니다. 최신 사용자 요청 자체가 퇴출된 경우, 파이프라인이 그 `[evicted to: <path>]` 포인터를 필드에 덧붙입니다(코드 소유, `_latest_human_eviction_ref`). 따라서 요약 체인은 항상 디스크의 전체 텍스트로 돌아가는 길을 보존합니다. 내부 주입(`metadata.internal`)은 이 앵커를 빼앗지 않습니다.

**체이닝 요약 필터링**(`_filter_summary_messages`): 이전 체크포인트가 있으면 그 Human/AI 쌍을 직렬화된 `<conversation>` 입력에서 제거합니다 — 추출된 이전 요약은 `<prior-summary>`(또는 `<prior-summary-json>`)로만 주입되므로, 오래된 요약 텍스트는 프롬프트에 정확히 한 번만 나타납니다. 이 쌍은 **양쪽 모두** `additional_kwargs={"lc_source": "summarization"}`을 가지며, 이는 양쪽 모두 MesMemory에 들어가지 않게 합니다(`MessagePersistenceMiddleware._is_persistable` + `HumanMessageRowBuilder`): 이 쌍은 압축의 내부 산출물이지 대화 기록이 아닙니다. 필터는 `_extract_previous_doc` / `_extract_previous_summary` **이후**에 실행되어(체이닝은 여전히 이전 요약을 봅니다), 필터된 목록이 직렬화·프롬프트 구성·두 정적 폴백 분기(LLM 실패/너무 짧은 응답)에 쓰입니다 — `_apply_compression_under_lock` / `_aapply_compression_under_lock`의 `skip_llm` 경로도 마찬가지입니다. opencode-dev의 `hidden` 집합, deepagents의 `_filter_summary_messages`와 정렬됩니다.

레거시 프롬프트 템플릿(`_SUMMARY_TEMPLATE`)은 free-form 폴백 골격을 계속 고정합니다 — *Latest Unresolved User Request / Goal / Constraints & Preferences / Progress(Completed ≤ 5 · In Progress · Blocked) / Key Decisions ≤ 5 / Next Steps / Critical Context ≤ 3 / Relevant Files* — "비어 있어도 모든 섹션을 유지"와 기밀 규칙("NEVER include API keys, tokens, passwords, secrets")을 요구합니다. 구조화 경로는 Markdown 골격을 `_SUMMARY_JSON_RULES`(JSON 필드 목록 + 동일 기밀 규칙)로 대체합니다. 필드 의미는 Pydantic `SummaryDoc` 모델 자체에 정의됩니다.

**사용자 요청 출처의 정식 식별.** 영속화된 각 `human` 행은 `origin`을 가집니다(전송 진입점에서 각인): WS/채널 사용자 입력은 `"user"`, 오케스트레이터 스티어링 주입은 `"task_intent"`, 완료 캐리어는 `"subagent_completion"`, Cron 전달 턴은 `"cron"`(`ai`/`tool` 행은 `NULL` 유지; origin 태깅 이전 행은 레거시 user 메시지로 읽음). *Latest Unresolved User Request*의 출처는 이 열로 정식 식별됩니다: 사용자 출처만(`origin = 'user'` 또는 레거시 `NULL`) 사용자 요청이며, 내부 주입(`task_intent` / `subagent_completion` / `cron`)은 요청으로 인용되지 않습니다. routing plan의 복수형 `unresolved_user_requests[]` 목록도 동일한 정식 필터를 사용합니다.

## 🧱 정적 폴백 (LLM 없는 요약)

`_build_static_fallback_summary`(:296)는 모델 호출 0회로 같은 섹션 골격을 만듭니다:

- 마지막 사용자 요청 → *Latest Unresolved User Request*; 첫 요청 → *Goal*;
- 결정 키워드(`decided`, `choosing`, `because`, `therefore`)를 포함한 AI 텍스트 → *Key Decisions*, 아니면 *Completed*;
- 모든 도구 호출 → *Completed*; 경로 같은 토큰(`/` 또는 `\` 포함, 또는 `.py`/`.md`/`.js`/`.ts`/`.json`으로 끝) → *Relevant Files*(≤ 10, `http` 링크 제외);
- 에러 `ToolMessage` → *Blocked*와 *Critical Context*.

`skip_llm`이 활성화되면 그대로 사용되고, 짧거나 실패한 LLM 요약의 안전망이기도 합니다.

## 📦 출력: 요약 메시지 쌍

`_build_new_messages`(summary_generation.py:806)는 요약 텍스트를 감싸 정확히 두 메시지를 내보냅니다:

```
[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted …
Respond ONLY to the latest user message that appears AFTER this summary.

<summary>
…summary Markdown…
</summary>

--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---
```

- **HumanMessage** `"What did we do so far?"` — 역할 교대를 유지하는 중립적 질문; AI 절반과 같은 `lc_source` 마커를 가집니다.
- **AIMessage**, `additional_kwargs={"lc_source": "summarization"}` 포함 — 이후 턴들이 (a) 이전 체크포인트를 찾아 체이닝하고, (b) 체이닝 재요약 입력에서 이 쌍을 제거하고 프루닝이 체크포인트에서 멈추게 하며, (c) 테스트가 대체된 후 요약이 모델 뷰에서 삼켜질 수 있음을 단증하는 마커.
- 이 쌍은 MesMemory에 절대 들어가지 않습니다: `MessagePersistenceMiddleware._is_persistable`이 `lc_source="summarization"` 양쪽 절반을 건너뜁니다(human 행 빌더도 같은 게이트를 가집니다).
- AIMessage는 구조화 문서 본체도 보관합니다: `additional_kwargs["summary_doc"]`(cap 후 `SummaryDoc`, JSON 직렬화 가능한 순수 dict — 체인 캐리어, `<prior-summary-json>`으로 재투입). free-form 폴백 회차는 이 페이로드가 없고 `_extract_previous_summary`가 `<summary>` 본문 파싱으로 폴백합니다.
- 전체 콘텐츠는 `SUMMARY_TOTAL_MAX_CHARS (16 000)`으로 봉인되고, 머리/꼬리 30/30 보존.

## 🛡️ 안티-스래싱 가드 매트릭스와 성능 저하 복구

상태는 세션 범위 `state_register_mem`의 **열세 개** `summarization_*` 키(state_aliases.py:17–24)에 살습니다. `_reset_turn_state`(thrash.py:157)는 매 턴 시작 시 그중 **열한 개**를 리셋합니다; `summarization_last_user_question`, `summarization_cooldown_rounds`는 의도적으로 턴마다 리셋되지 **않습니다**.

| 가드 | 키 | 임계값 | 효과 |
| :---- | :-- | :-------- | :----- |
| 턴 쿨다운 | `summarization_cooldown_rounds` | `COMPACTION_COOLDOWN_ROUNDS = 3` | 실제 compact마다 무장(core.py:345); **모든** 모델 호출이 감소(thrash.py:87); T1 compact 라우트, T2 선제, T3를 차단 — T4/T5 강제 링은 절대 차단하지 않음 |
| 턴당 압축 수 | `summarization_turn_attempts` | `MAX_COMPRESS_ATTEMPTS_PER_TURN = 3` | core.py:345이 증가; T2 선제 + T3 억제(강제 링은 면제) |
| 오버플로 재시도 (T4/T5 공유) | `summarization_overflow_retries` | `MAX_OVERFLOW_RETRIES = 3` | 두 에러 클래스가 공유하며 턴마다 리셋; 성공한 강제 단계마다 증가; 소진 → 원본 프로바이더 에러 전파 |
| 세션 총 압축 | `summarization_compression_count` | `MAX_TOTAL_COMPRESSION_ATTEMPTS = 5` | `_should_skip_compression`(thrash.py:110)이 True 반환 — 선제 압축 완전 정지 |
| 연속 무효 | `summarization_compression_ineffective` | `INEFFECTIVE_THRESHOLD = 2` | `skip_llm` 설정 — 비 LLM 전략만 |
| 유효성 판정 | (`_record_compression`, thrash.py:117) | 메시지 수 감소 **또는** 토큰 축소 ≥ `MIN_EFFECTIVENESS_PCT (0.05)` | 성공한 비 LLM 전략(`dedup`/`prune`/`truncate`/`fallback`/`aggressive`)이 `skip_llm`을 다시 해제 |
| 성능 저하 복구 예산 | `summarization_recovery_attempts` | `MAX_RECOVERY_ATTEMPTS = 2` | 성능 저하 모니터가 시작하는 강제 복구의 상한 |

**성능 저하 모니터**(`_monitor_degradation`, thrash.py:131): 이번 호출에서 실제로 압축이 일어났을 때만 consult됩니다(`_compaction_just_happened` 플래그). 모델 응답에 텍스트가 없으면 카운터가 증가하고; `DEGRADATION_NO_TEXT_THRESHOLD (3)`회 연속 빈 응답 — 그리고 `summarization_recovery_attempts < 2`인 동안 — `force_recovery`를 설정하고 무효 연속 기록과 세션 압축 카운트를 지웁니다. 비어 있지 않은 응답은 카운터를 리셋합니다. 이것은 "압축 → 모델 혼란 → 빈 출력 → 재압축"의 병적 루프를 잡습니다. 상호작용에 주의: force 플래그는 wrap 진입(core.py:426)에서 `_should_skip_compression` **보다 먼저** 읽히고, 스킵 게이트는 카운터를 리셋하고 진행하며 그 플래그를 소비합니다(thrash.py:110–115) — 복구 압축은 정확히 한 번 실행됩니다.

## 🔄 시스템 프롬프트 갱신

메인 에이전트 전용(`need_update_system_prompt=True`): 압축 후 미들웨어가 시스템 프롬프트를 재구축해 `system_prompt` 상태 키에 기록하므로, 다음 모델 호출은 페르소나 파일 / 장기 기억을 지금 현재 상태 그대로 봅니다. 두 전달 경로: 압축 직후의 `request.override(system_message=SystemMessage(...))`, 그리고 — T1 compact가 이미 일어났지만 안티-스래싱 게이트가 두 번째 압축을 막을 때 — 재구축된 프롬프트는 게이트 경로에서 여전히 전달됩니다(core.py:441–459). `@dynamic_prompt` 시스템 프롬프트 미들웨어가 없는 체인(서브에이전트 / nudge 파이프라인)은 이 미들웨어가 전달해 주기 때문입니다. 게이트 경로는 요청의 현재 system message와 **내용이 다를 때만** 주입합니다: 내용이 같으면 override도 새 `SystemMessage`도 만들지 않습니다(재주입하지 않음).

## 📌 등록 지점

```python
# agent/core.py:152 — 메인 에이전트 (Summarization은 마지막 미들웨어:
# 가장 안쪽 wrap 레이어, LLM에 가장 가까움)
Summarization(
    need_update_system_prompt=True,
    model=auxiliary_llm,
    main_llm_context_window=main_llm_max_tokens,
    trigger=[("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO))],
    keep=("messages", 10),
)

# agent/tools/subagent/spawn/core.py:755 — 워커 에이전트 (첫 미들웨어)
Summarization(
    model=auxiliary_llm,
    main_llm_context_window=main_llm_max_tokens,
    trigger=[
        ("messages", 40),
        ("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO)),
    ],
    keep=("messages", 10),
)
```
