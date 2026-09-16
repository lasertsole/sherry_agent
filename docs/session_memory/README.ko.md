# 세션 메모리 아키텍처(SESSION 플랜)

[English](README.md) · [中文](README.zh.md) · [日本語](README.ja.md) · 한국어

SESSION 메모리 플랜의 전체 14개 기능(opencode-dev / oh-my-openagent / hermes-agent / openclaw 참조)이 구현되었으며 LT-1…LT-8 장기 작업 편성도 포함됩니다. 설계 규칙: 모든 기능은 기존 인프라(계층형 facts, 세션 연속성, 상태 레지스터, MesMemory 마이그레이션)의 확장이며 병렬 스토어는 만들지 않습니다.

> 상태(2026-09-13): 플랜 폐기. 이 README가 참조 기준입니다.

## 구현된 기능

| 항목 | 기능 | 위치 |
|---|---|---|
| P0-1 | 압축 전 메모리 플러시 | `agent/middlewares/summarization/memory_flush.py` |
| P0-2 | 압축 쿨다운 재시작 간 유지 | `agent/middlewares/summarization/core.py`의 `_COOLDOWN_PERSIST_KEYS` |
| P0-3 | SQLite 압축 락(TTL, fail-open) | `agent/middlewares/summarization/compaction_lock.py`, 마이그레이션 v10 |
| P0-4 | 도구 출력 한 줄 요약 | `pub/func/message/tool_output_prune.py` |
| P1-1 | 압축 체크포인트 + 복원 | 마이그레이션 v15, `restore_compaction_checkpoint` |
| P1-2 | 메시지 멱등 영속화 | 마이그레이션 v11(`idempotency_key` + 부분 유니크 인덱스) |
| P1-3 | `context_eligible` 히스토리 프로젝션 | 마이그레이션 v12, 조회 시 기본 필터 |
| P1-5 | 메시지 트리 + 제로카피 fork | 마이그레이션 v14(`parent_message_id`, `session_leafs`) |
| P2-1 | 추가 전용 이벤트 로그 + 프로젝터 | 마이그레이션 v16, `context_engine/events/` |
| P2-2 | Context Epoch 스냅샷 | 마이그레이션 v17(`context_epoch` 테이블), `ContextEpoch` |
| P2-3 | 이중 워터마크 facts 추출 | `context_engine/facts/`, `TieredMemoryStore.add_fact`로 기록 |
| P2-4(부분) | steer/queue 이중 전달 | `steering_queue.py` + `SubagentCompletionDrainMiddleware` + `auto_turn` |
| P2-5 | 벡터 의미 검색 | 마이그레이션 v13, `context_engine/embeddings/`, `message_search --semantic` |
| LT-1…8 | TaskFlow 편성 | `docs/long-running-tasks/` |

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

- **`context_engine/facts/`** —— 이중 워터마크 커서(`cursor.py`, `state_register.db` 영속화), 보조 LLM 추출기(`extractor.py`, json_repair 파싱 + 카테고리 폴백), 큐 편성(`queue.py`). `ContextEngineHook.aafter_agent`에 fire-and-forget 백그라운드 작업으로 연결되며 facts는 기존 `TieredMemoryStore.add_fact`(facts/*.md)로 기록됩니다.
- **`context_engine/events/`** —— 추가 전용 이벤트 로그(세션별 무결 시퀀스: `types.py`, `store.py`), 체크포인트 이벤트를 P1-1 읽기 모델에 매핑하는 `EventProjector`.
- **`context_engine/embeddings/`** —— 벡터 의미 검색: 지연 embed 백엔드(프로젝트 임베드 모델, 테스트에서 대체 가능), 멱등 LEFT-JOIN 인덱서, 코사인 순위付け. `message_search` 도구(`semantic: true`)로 노출.
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
    tests/context_engine/facts/test_facts_extraction.py \
    tests/context_engine/embeddings/test_semantic_search.py -q
```

## 평가

```bash
uv run python evals/evals.py session_memory
```

평가 샌드박스에서 라이브 서브시스템을 채점——7개 체크: 쿨다운 재시작 생존, 락 상호 배제, 체크포인트 복원, 멱등 리플레이, 컨텍스트 프로젝션, 실 LLM facts 추출, 실 임베드 의미 순위. `evals/session_memory/suite.py` 참조.
