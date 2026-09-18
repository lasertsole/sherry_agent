# 세션 메모리 아키텍처(SESSION 플랜)

[English](README.md) · [中文](README.zh.md) · [日本語](README.ja.md) · 한국어

SESSION 메모리 플랜의 전체 13개 기능(opencode-dev / oh-my-openagent / hermes-agent / openclaw 참조)이 구현되었으며 장기 작업 편성도 포함됩니다. 설계 규칙: 모든 기능은 기존 인프라(세션 연속성, 상태 레지스터, MesMemory 마이그레이션)의 확장이며 병렬 스토어는 만들지 않습니다.

> 상태(2026-09-13): 플랜 폐기. 이 README가 참조 기준입니다.

## 구현된 기능

| 기능 | 위치 |
|---|---|
| 압축 전 메모리 플러시 | `agent/middlewares/summarization/memory_flush.py` |
| 압축 쿨다운 재시작 간 유지 | `agent/middlewares/summarization/core.py`의 `_COOLDOWN_PERSIST_KEYS` |
| SQLite 압축 락(TTL, fail-open) | `agent/middlewares/summarization/compaction_lock.py`, 마이그레이션 v10 |
| 도구 출력 한 줄 요약 | `pub/func/message/tool_output_prune.py` |
| 압축 체크포인트 + 복원 | 마이그레이션 v15, `restore_compaction_checkpoint` |
| 메시지 멱등 영속화 | 마이그레이션 v11(`idempotency_key` + 부분 유니크 인덱스) |
| `context_eligible` 히스토리 프로젝션 | 마이그레이션 v12, 조회 시 기본 필터 |
| 메시지 트리 + 제로카피 fork | 마이그레이션 v14(`parent_message_id`, `session_leafs`) |
| 추가 전용 이벤트 로그 + 프로젝터 | 마이그레이션 v16, `context_engine/events/` |
| Context Epoch 스냅샷 | 마이그레이션 v17(`context_epoch` 테이블), `ContextEpoch` |
| steer/queue 이중 전달 | `steering_queue.py` + `SubagentCompletionDrainMiddleware` + `auto_turn` |
| 벡터 의미 검색 | 마이그레이션 v13, `context_engine/embeddings/`, `message_search --semantic` |
| TaskFlow 편성 | `docs/long-running-tasks/` |

## MesMemory 마이그레이션(v10–v17)

| 버전 | 스키마 |
|---|---|
| v10 | `compression_locks` —— 세션별 압축 락(기본 키 = 상호 배제) |
| v11 | `messages.idempotency_key` + 부분 유니크 인덱스(크래시 재시도 중복 제거) |
| v12 | `messages.context_eligible` —— 컨텍스트 프로젝션 플래그(기본 1) |
| v13 | `message_embeddings` —— 의미 검색 벡터 인덱스 |
| v14 | `messages.parent_message_id` + `session_leafs` —— 메시지 트리 |
| v15 | `compaction_checkpoints` + `messages.compacted` / `compaction_checkpoint_id` |
| v16 | `events` —— 추가 전용 로그, 세션별 무결 `seq` |
| v17 | `context_epoch` —— 시스템 컨텍스트 베이스라인 / 스냅샷 |

## 주요 컴포넌트

- **`context_engine/events/`** —— 추가 전용 이벤트 로그(세션별 무결 시퀀스: `types.py`, `store.py`), 체크포인트 이벤트를 체크포인트 읽기 모델에 매핑하는 `EventProjector`.
- **`context_engine/embeddings/`** —— 벡터 의미 검색: 지연 embed 백엔드(프로젝트 임베드 모델, 테스트에서 대체 가능), 멱등 LEFT-JOIN 인덱서, 코사인 순위付け. `message_search` 도구(`semantic: true`)로 노출.
- **`agent/tools/message_search.py`** —— 2단계 조회: 영속화된 `messages` 테이블에서 FTS5를 먼저 검색하고, 일치 항목이 없으면 세션의 최신 체크포인트(`SRC_DIR/checkpoints/sqlite.db`의 `state["messages"]`)로 폴백하여 아직 영속화되지 않은 턴을 최신순으로 키워드 매칭합니다(`_CHECKPOINT_SCAN_MAX_MESSAGES` / `message_search_max_session_chars`로 상한). 폴백 히트에는 `source="checkpoint"`가 붙습니다. 영속화가 이제 각 모델 경계와 각 도구 반환 시 실행되므로, 이 폴백은 "체크포인트가 저장소보다 앞서 있는" 좁은 창에서만 작동합니다 — 다음 모델 경계에서 영속화되기를 기다리는 HITL 거부(도구 결과는 반환 시 이미 기록됨).
- **`agent/middlewares/message_persistence/`** —— write-once 세션 영속화, 두 시점: human/AI 메시지는 각 모델 호출 경계에서, 도구 결과는 반환되는 순간. `persisted_message_ids` 워터마크가 각 메시지를 정확히 한 번 착지시켜, 영속화가 더 이상 압축 발화에 의존하지 않습니다.
- **도구 결과 크기 거버넌스** —— `ToolResultEvictionMiddleware`가 과대한 결과(> 20 000자)를 state에 들어가기 전에 `SESSIONS_DIR/<session_id>/evicted/`로 오프로드합니다(state에는 head+tail 프리뷰만 남음); `clear_session()`은 세션 디렉터리를 통째로 삭제하므로 축출 파일도 함께 사라집니다. P1-2 오버플로 테일 클립은 꼬리 `ToolMessage` 내용을 `model_copy`로 스텁 처리할 뿐(정체성과 페어링 불변) — 데이터는 잃지 않습니다. 모든 결과는 이미 MesMemory에 영속화되었고 오프로드된 본문도 디스크에 남기 때문입니다: `message_search`가 텍스트를 불러오고, `read_file`이 축출 파일을 다시 읽습니다.
- **`agent/middlewares/summarization/compaction_lock.py`** —— SQLite 압축 락(TTL 자가 복구, 동기 + 비동기 획득, 타임아웃 시 fail-open).
- **`runtime/session/state_register.py`** —— `context_epoch` 테이블 기반의 `ContextEpoch` 라이프사이클(initialize / prepare / replace / advance).

## 테스트

```bash
uv run pytest tests/agent/middlewares/test_compaction_lock.py \
    tests/agent/middlewares/test_compression_cooldown_persist.py \
    tests/context_engine/store/test_add_messages_idempotency.py \
    tests/context_engine/store/test_compaction_checkpoints.py \
    tests/context_engine/store/test_message_tree.py \
    tests/context_engine/events/test_events.py \
    tests/runtime/test_context_epoch.py \
    tests/context_engine/embeddings/test_semantic_search.py \
    tests/agent/tools/test_message_search_checkpoint_fallback.py -q
```

## 평가

```bash
uv run python evals/evals.py session_memory
```

평가 샌드박스에서 라이브 서브시스템을 채점——6개 체크: 쿨다운 재시작 생존, 락 상호 배제, 체크포인트 복원, 멱등 리플레이, 컨텍스트 프로젝션, 실 임베드 의미 순위. `evals/session_memory/suite.py` 참조.
