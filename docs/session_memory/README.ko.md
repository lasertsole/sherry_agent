# 세션 메모리 아키텍처(SESSION 플랜)

[English](README.md) · [中文](README.zh.md) · [日本語](README.ja.md) · 한국어

`TODO/SESSION_MEMORY_BORROWING_PLAN.md`(opencode-dev / oh-my-openagent / hermes-agent / openclaw 참조)의 구현 현황. 설계 규칙: 모든 신규 기능은 기존 long-running-task 인프라(계층형 facts, 세션 연속성, 상태 레지스터)의 확장으로 구현하며 병렬 스토어를 만들지 않는다.

## 구현됨

| 항목 | 기능 | 위치 |
|---|---|---|
| P0-1 | 압축 전 메모리 플러시 | `agent/middlewares/memory_flush.py` |
| P0-2 | 압축 실패 쿨다운 재시작 간 유지 | `agent/middlewares/summarization.py`의 `_COOLDOWN_PERSIST_KEYS` |
| P0-3 | SQLite 압축 락(TTL, fail-open) | `agent/middlewares/compaction_lock.py`, MesMemory 마이그레이션 v10 |
| P0-4 | 도구 출력 한 줄 요약 | `pub/func/message/tool_output_prune.py` |
| P1-2 | 메시지 멱등 영속화 | MesMemory 마이그레이션 v11(`idempotency_key` + 부분 유니크 인덱스) |
| P1-3 | `context_eligible` 히스토리 프로젝션 | MesMemory 마이그레이션 v12, 조회 시 기본 필터 |
| P2-4(부분) | steer/queue 이중 전달 | `steering_queue.py` + `SubagentCompletionDrainMiddleware` + `auto_turn` |
| LT-* | TaskFlow DAG / 예산 / 마감 / 재시도 / 연속성 | `docs/long-running-tasks/` |

## 로드맵(미구현)

P1-1 압축 체크포인트 복원 · P1-4 세션 간 리콜 · P1-5 전사 트리 및 메시지 분기 · P2-1 이벤트 소싱 마이그레이션 · P2-2 Context Epoch 스냅샷 · P2-3 이중 워터마크 facts 추출(기존 계층형 facts 스토어 위에 구현) · P2-5 벡터 의미 검색.

## 테스트

```bash
uv run pytest tests/agent/middlewares/test_compaction_lock.py tests/agent/middlewares/test_compression_cooldown_persist.py tests/context_engine/store/test_add_messages_idempotency.py -q
```
