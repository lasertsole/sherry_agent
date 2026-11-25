# 🛡️ 프로덕트 측 B클래스 평가 실전 경험

> [English](README.md) · [中文](README.zh.md) · 한국어 · [日本語](README.ja.md)

> 평가 하네스 관찰(external-bench / DeepSWE)과 프로덕트에서 실측한 실패 모드에서 승격한 4가지 fail-open 강화 사항입니다. A클래스와 달리 이는 가시성/가드레일입니다. 어느 것도 기존 게이트의 결정 경로를 바꾸지 않으며, 실패 시 no-op로 강등됩니다. 신뢰할 수 있는 출처: 각 절에 나열된 파일.

## 1. PathGuard 거부 카운트 (`agent/middlewares/path_guard/core.py`, `server/service/turn_runner.py`)

차단된 도구 호출은 일반 오류 `ToolMessage`를 반환하므로, 에이전트가 PathGuard 거부에 빠져 반복하면 이유가 보이지 않은 채 수백 번의 호출이 발생했습니다. PathGuard는 이제 동기 및 비동기 거부 분기 모두에서 **세션별 거부 카운트**(`record_denial` → `_PATH_DENIALS`)를 기록합니다. 횟수와 중복 제거된 거부 경로입니다. 턴 종료 시 `on_turn_finished`가 카운트를(한 번만) 드레인하여 제목이 `PathGuard 拒绝了写/读操作`인 일시적인 `system.notice` 카드를 횟수와 최대 5개 경로와 함께 푸시합니다. 이 알림은 기존 인라인 알림 스트립을 재사용하므로 새 소켓 프레임이나 토스트 핸들러는 추가되지 않았습니다. 세션 삭제 시 `clear_denials`를 호출하여 잔여물을 남기지 않습니다. 결정 경로는 변경되지 않습니다. 카운트는 가시성 용도로만 쓰이고, 빈 session id는 세지 않으며, 보고할 것이 없으면 드레인은 `None`을 반환합니다.

## 2. 진행 중인 스텝에 대한 wait-only 지시 (`agent/middlewares/todo_continuation/core.py`)

플랜 연속 강제자는 TaskFlow 스텝 자식이 아직 실행 중이어도 "다음 미완료 작업을 계속하라"고 말했고, 에이전트는 네 개의 자식이 실행 중인 상태에서 완료를 선언할 수 있었습니다. `_has_in_flight_steps(flows)`가 참(스템이 `dispatched` 또는 `ready`)이면 게이트는 대신 `[SYSTEM DIRECTIVE: STEPS IN FLIGHT]`를 선택합니다. `taskflow_step(action='wait_all')`를 호출하고, settle된 스텝을 `resume`하고, `taskflow_flow(action='finish')`로 플로우를 닫습니다. 모든 자식이 settle하기 전에는**완료를 선언하거나 새 작업을 시작하지 마십시오**. 진행 중인 스텝이 없으면 일반 연속 지시를 사용합니다. 전부 차단된 형태에는 blocked-only가 계속 적용됩니다(진행 중과 차단은 상호 배타적). 상태 블록과 보드 푸터는 유지됩니다. 지시는 결정을 좁힐 뿐 플랜의 나머지를 숨기지 않습니다.

## 3. 압축 시 리마인더 스냅샷 보존 (`agent/middlewares/reminder_catalog/dynamic.py`, `agent/middlewares/summarization/compression.py`)

턴 중간 압축은 cutoff 창 전체(내부의 모든 `<system-reminder>` 포함)를 요약 쌍으로 대체하여, 모델이 남은 턴을 todo/TaskFlow 앵커 없이 실행하게 했습니다. 압축 파이프라인은 이제 잘라내기 후(recovery context 재작성 전)에 `capture_reminder_snapshot`을 호출하고 결과를 요약 쌍과 보존된 테일 사이에 삽입합니다. `[summary, snapshot, preserved…]`입니다. 스냅샷은 source별 비-transient 마지막 리마인더의 **압축 시점** 버전이며 live state가 아닙니다. 따라서 모델은 타임라인을 봅니다. 요약 → 압축 시점 상태 → 더 새로운 preserved 리마인더. transient source(current_time, lsp_diagnostics, project_instructions)는 캡처하지 않습니다. source별로 스냅샷이 preserved의 **가장 오래된** 리마인더와 바이트 단위로 동일하면 삭제합니다(순수 중복). 최신이 아닌 최고와 비교하면 상태가 변했다가 되돌아갈 때 스냅샷을 잘못 삭제하는 것을 막습니다. 스냅샷은 지연 임포트이며 요약 쌍이 실제로 만들어진 경우에만 실행되고, 오류 시 "스냅샷 없음"으로 fail-open합니다. 동적 리마인더 미들웨어, 그 가드, 다음 턴 주입은 변경되지 않습니다.

## 4. 병렬 툴 배치의 집계 크기 가드 (`agent/safe_tool_node.py`, `pub/func/message/eviction.py`, `config/features/agent_side/tool_parallel.py`)

`ContextEviction`은 툴 노드 안에서 **호출별**로 에빅트하므로, 20K 문자 임계값 바로 아래에 있는 병렬 결과 N개가 모두 남아 합계가 컨텍스트 창을 초과합니다. per-call 훅은 합계를 볼 수 없습니다. `SafeToolNode`는 이제 배치 전체를 쥐고 있는 유일한 지점(`gather` 후, `_combine_tool_outputs` 전, enforce/non-enforce × sync/async의 4경로 모두)에서 `_enforce_batch_size_limit`를 적용합니다. 합계가 `TOOL_PARALLEL["batch_result_threshold_chars"]`(기본 100K, 0은 비활성화)를 넘으면 가장 큰 결과부터 기존 `evict_tool_result`(디스크 offload + head/tail 미리보기)로 에빅트하고, 실패 시 `hard_clip_tool_result`(메모리 잘라내기)로 폴백하여 배치가 맞아떨어지거나 더 이상 줄일 수 없을 때까지 반복합니다. 두 헬퍼는 이 경로에서 `force=True` 키워드를 받아 19K 단일 결과가 20K per-call 임계값 아래여도 에빅트될 수 있게 합니다. 기본 호출자는 바이트 단위로 불변입니다. 이미 에빅트된 결과와 크기 0 항목은 건너뛰고, 순서와 개수는 유지되며, 실패 시 항상 원래 결과를 보존합니다. 디스크 미리보기에는 약 4K 문자의 하한이 있어, 초대형 배치는 모든 신호를 희생해 임계값 아래로 만드는 대신 여러 배로 축소됩니다. 비동기 경로는 `asyncio.to_thread`로 가드를 실행해 이벤트 루프를 막지 않습니다. 기존 `read_file` 복구 힌트를 `offset=1`로 수정했습니다(이 툴은 1-indexed입니다).

## 안전성 불변 사항

- 네 가지 모두 **추가형**입니다. 기존 프롬프트 계약, 미들웨어 순서, 툴 스키마, 상태 키, 턴 라이프사이클 훅을 호환 불가하게 변경하지 않았습니다.
- 모든 읽기/가드 경로는 **fail-open**입니다. 빈 session id, 빈 카운트, store 고장, 캡처 오류, offload 실패는 "신호 없음/원래 결과 유지"로 강등되며 하드 실패가 되지 않습니다.
- 임계값은 `TOOL_PARALLEL`에 모여 있습니다. wait-only 규칙은 기존 `_has_in_flight_steps` 판정을 재사용하고, 스냅샷은 리마인더 카탈로그의 transient-source 레지스트리를 재사용합니다. 핫 패스에 마법 숫자가 없습니다.
