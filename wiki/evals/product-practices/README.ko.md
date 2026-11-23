# 🧪 프로덕트 측 평가 실전 지식

> [English](README.md) · [中文](README.zh.md) · 한국어 · [日本語](README.ja.md)

> 6가지 메커니즘은 평가 하네스 강화(DeepSWE / external-bench)에서 시작되어 현재는 프로덕트의 일급 동작입니다. 모두 post-task 또는 fail-open이며 턴을 깨뜨리지 않습니다. 모두 실측된 평가 실패 패턴에서 비롯되었습니다. 신뢰할 수 있는 출처: 각 절에 명시된 파일.

## 1. 리프 타임아웃 1800초 (`agent/tools/subagent/config.py`)

`run_timeout_seconds` 기본값을 **1800초**(기존 600초)로 변경. DeepSWE 실측에서 600초는 20문제 중 9개, 7200초는 20/20 — 병목은 모델 능력이 아니라 자식 프로세스가 read-implement-test 도중 종료되는 것이었습니다. 깊이 사다리는 리프 위 한 단계마다 3배(depth-1 = 5400초). `evals/deepswe/suite.py`는 `_EVAL_CHILD_RUN_TIMEOUT_MULTIPLIER = 1.0`(프로덕트 기본값이 평가 예산을 이미 포함, 추가 ×3 불필요). 명시적 재정의 값은 여전히 최소 600 / 최대 7200으로 제한됩니다.

## 2. Memory 문자 상한 CJK 가중 (`agent/tools/memory.py`)

Memory 문자 상한(2200 / 1375 / 1375)은 `_weighted_char_count`를 통해 CJK 1자를 `chars_per_token / chars_per_token_cjk`(4/2 = 2)개의 가중 문자로 셉니다. `pub/func/cjk.py::count_cjk`와 토큰 추정 설정을 재사용. ASCII 용량은 불변, CJK는 2배 토큰을 초과하지 않고 동일 예산에 맞습니다. 모든 상한 검사(add / replace / evict / `_append_to_target`)와 사용자 표시 `[n% — X/Y chars]`가 가중 카운트를 사용합니다. 빈 값/실패는 fail-open 유지.

## 3. 턴 간 diff-hash 무진행 감지 (`agent/tools/todolist/stagnation_tracker.py`, `agent/middlewares/todo_continuation/core.py`)

계획 텍스트 정체 검사는 todo를 계속 편집하면서 코드 패치는 전혀 변하지 않는 "closure-flow 공전"을 잡지 못합니다. continuation 턴 종료 시마다 enforcer는 이벤트 루프 밖에서 스테이징된 + 스테이징되지 않은 바이너리 git diff를 해시합니다(`_working_tree_diff_hash`). 연속 `TODOLIST_INFRA["diff_stagnation_threshold"]`(기본 2)회 해시가 같고 **step 자식이 비행 중이 아닐** 때 `[SYSTEM DIRECTIVE: NO PROGRESS DETECTED]`를 주입하고 정지를 래치합니다. 실제 사용자 턴이나 자식 비행은 기준선을 리셋. git이 아닌 디렉터리는 빈 해시(fail-open, 오탐 없음). 계획 텍스트 정체 검사와 병렬로 동작하며 둘 중 하나라도 발화하면 게이트를 멈춥니다.

## 4. Verify 완료 전 settle 대기 (`config/features/agent_side/todolist_infra.py`, `agent/middlewares/todo_continuation/core.py`)

step 자식은 `done`을 보고했지만 파일 편집이 아직 디스크에 기록 중일 수 있습니다. 그 순간 verify를 완료하면 거짓 완료가 됩니다. reviewer가 완료하고 step 자식이 비행 중일 때 게이트는 `verify_settle_wait_s`(기본 30초, 평가에서는 180초)까지 settle을 기다립니다. 타임아웃 시 verify 지시문에 `[WARNING: SUB-TASKS STILL RUNNING]` 추가 — 완료는 차단하지 않으면서 패치가 불완전할 수 있음을 모델에 알립니다. 플로우 읽기 실패는 "settle 완료"로 강등(fail-open).

## 5. Reviewer 수용 체크리스트 (`agent/tools/taskflow/checklist_extractor.py`, `agent/tools/taskflow/tools/_dispatch.py`)

`functional_role="reviewer"`로 spawn된 reviewer는 task 끝에 번호가 매겨진 PASS/FAIL **ACCEPTANCE CHECKLIST**를 받습니다. 규범 문장(must / should / shall / raise / reject / only / never / charset / return / error 등)에서 추출, 상한 20개·중복 제거·대소문자 무시. `TASKFLOW_INFRA["reviewer_checklist_enabled"]`(기본 켜짐)로 제어. task 텍스트만 변경 — reviewer의 모델·도구·격리 컨텍스트는 불변. 멱등: 체크리스트 라벨이 있는 task는 재추가하지 않음(평가側 `set_reviewer_checklist`가 우선). 추출 실패는 fail-open.

## 6. Session 실행 보고서 (`pub/func/session_report/**`, `server/service/session_report_service.py`, `server/trigger/http/session_report.py`)

3개의 post-task 분석기를 평가 측에서 프로덕트 공용 계층으로 이전 — 핫 패스에 훅을 넣지 않습니다.

- **token tracker** — `usage_metadata`를 메인 + 역할별 자식 에이전트로 집계. USD 비용은 `SHERRY_SESSION_REPORT_TOKEN_PRICES`(또는 구 평가 변수)가 유효한 가격 파일을 가리킬 때만 출력.
- **context analyzer** — 호출별로 도구 호출을 HIT/WASTED 채점. `hit_rate = effective / (effective + wasted)`. 계획/오케스트레이션 도구는 미채점, 판단 불가는 HIT(보수적).
- **code-intel tracker** — 정밀 검색(LSP / explore / callers / semantic) 대 조잡 검색(search_files / read_file) 비율, fail-open ToolMessage 텍스트에서 강등 호출 감지.

보고서는 첫 API 읽기 시 생성되어 프로세스 내 캐시에 저장됩니다(턴 종료 백그라운드 태스크나 state register 쓰기 없음). 캐시 미스 시 `GET /sessions/:session_id/report`(또는 `?session_id=`, `?refresh=1`)가 즉석 생성. 클라이언트는 컨텍스트 뷰어에서 접이식 패널로 표시(`client/app/components/chat/SessionReport.vue`). 프로세스 재시작 후 다음 읽기에서 캐시를 다시 구성합니다.

## 안전 불변식

- 6개 모두 **추가형**: 기존 프롬프트 계약·도구 스키마·상태 리터럴·턴 라이프사이클 훅을 비호환하게 변경하지 않음(보고서 캐시는 프로세스 내부이며 state register key는 추가하지 않음).
- 모든 읽기 경로가 **fail-open**: git 부재, store 고장, 추출 실패, transcript 불가 모두 "신호 없음"으로 강등, 하드 실패 없음.
- 임계값은 `TODOLIST_INFRA` / `TASKFLOW_INFRA`에 집중. 핫 패스에 매직 넘버 없음.
