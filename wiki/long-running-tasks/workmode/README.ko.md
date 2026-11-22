# 🛠️ WorkMode 하위 메커니즘 — 작업 수준 신뢰성과 조율

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> [장기 실행 작업](../README.ko.md)의 일부：실행 중인 TaskFlow를 중단, 묵시적 정지, 저품질 단계, 반복 계획, 작업 사실 누락으로부터 지키는 다섯 가지 선택적 메커니즘。WorkMode식 장기 작업 체계의 **하위 메커니즘**을 빌려 오되 3계층 조직은 복제하지 않습니다。전문가 패널 모드가 "누가 하는가"에 답한다면, 이는 "어떻게 신뢰할 수 있게 하는가"에 답합니다.

**코드 진실 원천：** `agent/middlewares/task_intent_router/`、`agent/tools/taskflow/watchdog.py`、`agent/tools/taskflow/step_judge.py`、`agent/tools/taskflow/template_store.py`、`agent/tools/taskflow/tools/`、`agent/tools/subagent/spawn/system_prompt.py`、`config/features/agent_side/taskflow_infra.py`、`config/path.py`。아래의 모든 상수와 코드 경로는 대조되었습니다。

## 다섯 메커니즘 개요

| 메커니즘 | 해결 문제 | 위치 | 기본값 |
| :--- | :--- | :--- | :--- |
| 의도 분류 입력 라우팅 | 실행 중 사용자 메시지가 작업을 무조건 중단 | `agent/middlewares/task_intent_router/`의 before-agent 미들웨어 | 활성 flow만；fail-open |
| 워치독 + 데드락 감지 | 정지·공전하는 flow를 아무도 발견하지 못함 | `agent/tools/taskflow/watchdog.py`，하위 에이전트 sweeper가 검사 | 경고만, flow를 변경하지 않음 |
| 점수 기반 자기 평가 재실행 | 이진 judge에 품질 구배와 재시도 상한이 없음 | `step_judge.py::judge_step_self_eval` ＋ resume 소비 | 단계별 opt-in（`self_evaluate`） |
| 계획 템플릿 재사용 | 같은 분해를 매번 처음부터 다시 만듦 | `agent/tools/taskflow/template_store.py`、`taskflow_flow` 액션 | opt-in 액션 |
| plan/environment 이중 파일 | 작업 사실（경로／스택／제약／자격 증명 참조）이 대화에 흩어짐 | flow `state["environment"]` → 자식 시스템 프롬프트 | 비면 프롬프트 불변 |

다섯 가지 모두 **추가적**입니다。활성 flow, `self_evaluate`, 템플릿 액션, `environment`가 없으면 동작은 이전과 바이트 단위로 동일합니다。

---

## 1. 의도 분류 입력 라우팅

TaskFlow 실행 중 새 사용자 메시지는 먼저 `agent/middlewares/task_intent_router/intent.py`의 `classify_turn_intent()`로 분류됩니다。**rules-only**이며 LLM을 호출하지 않고 기존 TaskIntent의 4개 언어 키워드 매칭을 따릅니다（중국어·영어·일본어·한국어 취소어 인식）。판단할 수 없는 입력은 `CONTINUE`로 해결됩니다（fail-open：메시지를 잃거나 오라우팅하지 않음）。

우선순위：`CANCEL` > `SOFT_PREEMPT` > `RESUME` > `CONTINUE` > `NEW`。

| 의도 | 트리거 | 라우트 |
| :--- | :--- | :--- |
| `CANCEL` | 명확한 중지어（`停下`、`cancel`、`やめて`、`중단`） | `taskflow_flow(action="cancel")`로 모든 활성 flow 취소 후 짧은 알림 주입 |
| `SOFT_PREEMPT` | "잠깐／방향 조정"류 표현 | 다음 turn에 advisory steering 주입；실행 중 단계는 취소·중단하지 않음 |
| `RESUME` / `CONTINUE` | HITL 응답 또는 작업 보충 | 무동작——기존 turn／HITL 경로가 처리 |
| `NEW` | 명백히 무관한 새 주제 | advisory steering；서버측 대기 입력이 권위 있는 큐로 유지 |

`TaskIntentRouterMiddleware`는 `agent/middlewares/specs.py`에서 main 위치 30에 등록되며（`ORDER IS A CONTRACT`）, 선택적 `DelegationPressureGate` 바로 앞에 있습니다。**활성 flow가 없으면 무동작**이므로 짧은 작업은 이 경로에 들어가지 않습니다。agent 계층은 server 큐를 import할 수 없으므로 SoftPreemption과 New는 서버측 큐를 직접 조작하지 않고 turn에 advisory steering을 주입해 구현합니다。내부／시스템 지시 메시지는 재분류되지 않습니다（취소 알림이 두 번째 취소를 일으키지 않음）。

---

## 2. 워치독 + 데드락 감지

`agent/tools/taskflow/watchdog.py`는 기존 호출 수준 가드（InvalidToolCallRepair、LLMRetry、MaxTokensBoost）에 작업 수준 생존성 감지를 추가합니다。하위 에이전트 registry sweeper가 liveness 이후 `sweep_and_notify()`를 호출합니다。감지는 순수 **fail-open**이며 flow 상태를 전혀 쓰지 않고 `system.notice` 경고（`source="taskflow:watchdog"`）만 보냅니다。취소·재시도·계속 결정은 agent 또는 사용자가 내립니다。

`WatchdogAlert(flow_id, session_id, kind, detail)`의 kind：

- **하트비트 만료**（`heartbeat_timeout_s`，기본 600초）：flow가 비종료 상태이고 `updated_at`이 임계값보다 오래됨。
- **무진행**（`deadlock_no_progress_s`，기본 1800초）：flow가 오래 running이고 단계 상태가 변하지 않음。
- **도구 루프**（`deadlock_tool_fail_count`，기본 3）：같은 step／도구가 같은 인자로 반복 실패。
- **judge 루프**（`deadlock_judge_fail_count`，기본 5）：step의 judge가 계속 retry를 반환。

모든 임계값은 `config/features/agent_side/taskflow_infra.py`에 있습니다。`0`으로 설정하면 해당 검사가 비활성화됩니다。알림 실패는 삼켜져 알림 채널 고장이 sweeper를 막지 않습니다。

---

## 3. 점수 기반 자기 평가 재실행

`StepJudge`의 기본 경로는 불변입니다：이진 `PASS` / `RETRY` / `BLOCK` / `UNKNOWN` 판정이며 `UNKNOWN`（judge 불가）은 fail-closed입니다。그 위에 `step_judge.py::judge_step_self_eval`이 `self_evaluate: true` 단계에 **선택적 점수 경로**를 추가합니다：

- 엄격한 reviewer LLM이 step의 `judge_criteria` / `validation_criteria`에 대해 1〜10으로 채점하고 `{"score", "reason", "feedback"}`를 반환。
- 점수 ≥ `self_eval_min_score`（기본 7）→ step은 `DONE`。
- 점수가 미만이고 재시도 예산（`self_eval_max_retries`，기본 5）이 남음 → `RETRY`하며 점수와 피드백을 step에 추가。
- 예산 소진 → `forced_pass: true`로 step을 `DONE` 처리해 주관적 채점이 무한 루프되지 않게 함。
- judge 불가시는 `score=None`을 반환하고 설정된 `on_judge_error` 전략을 따릅니다——조용히 통과시키지 않습니다。

세 step 필드（`self_evaluate`、`self_eval_min_score`、`self_eval_max_retries`）는 `run_task`의 `TaskflowStepInput` 스키마와 `_OPTIONAL_STEP_FIELDS` 허용 목록에 있습니다。`self_evaluate`가 없는 step은 원래 이진 judge 경로를 그대로 따릅니다。

---

## 4. 계획 템플릿 재사용

`agent/tools/taskflow/template_store.py`는 완료된 flow의 **분해 구조**（step id、작업、의존、기능 역할、judge／validation 기준、response schema）를 `APP_HOME/taskflow_templates/<name>.json`（`config.path.TASKFLOW_TEMPLATES_DIR`）에 저장합니다。런타임 필드（자식 세션 key、결과、revision、타임스탬프）는 허용 목록으로 제거되므로 템플릿은 재실행을 기다리는 빈 껍데기이며 지난 실행의 리플레이가 아닙니다。

두 `taskflow_flow` 액션이 노출합니다：

- `save_as_template(flow_id, name, description="")` —— flow의 step 구조를 원자 기록（파일 잠금＋`tempfile`＋`os.replace`）。
- `load_from_template(name, flow_id, description="")` —— 템플릿의 step 구조로 새 flow를 만들고 todo 루트를 묶음。

이는 PTC/LDW replay와 명확히 **상호 보완**입니다：템플릿은 "작업을 어떻게 분해했는가"를 재사용하고, PTC/LDW는 "정확한 실행 호출 열"을 재사용합니다。한 작업이 템플릿을 load한 뒤 미리 컴파일된 스크립트로 호출을 리플레이할 수 있습니다。

> 단계적 step 전개는 **채택하지 않습니다**。Sherry의 중첩 TaskFlow가 이미 충족합니다：최상위 flow는 굵은 step을 유지하고 step의 자식 ORCHESTRATOR가 자체 서브 flow를 만듭니다。진행은 `progress_push` / `system.notice`로 위에 전달됩니다。DAG 상태 기계에 `expandable → expanded` 전환을 추가해도（step은 대화가 아닌 SQLite에 있으므로）압축 이득이 없고 부분 전개 finish 게이트라는 부담만 늘어납니다。

---

## 5. plan/environment 이중 파일

flow 생성 시 `taskflow_flow(action="create", environment={...})`를 지정할 수 있습니다。사전은 `state["environment"]`에 그대로 저장되고（`taskflow_create.py`）, 자격 증명은 참조（`keychain://...`）로 유지되며 해석되지 않습니다。`store_sqlite.get_flow_environment_sync(flow_id)`가 전역 고유 flow id로 다시 읽습니다。

step 파발 시 `_dispatch.dispatch_child()`는 명시적으로 전달된 `environment`를 우선하고, 없으면 소속 flow의 environment를 읽어 `spawn_subagent_direct()`에 전달합니다。`SpawnRequest.environment`가 `SubagentPromptContext`로 옮기고, `agent/tools/subagent/spawn/system_prompt.py`의 `_render_environment_section()`이 역할 지시 **뒤**, 규칙 **앞**에 `## Task Environment` 블록을 그립니다。문자열이 아닌 값은 JSON으로 렌더링되고, 값 중 `credential_ref`가 있으면 이는 승인된 자격 증명 도구로 해석하는 참조이며 바로 쓸 비밀이 아니라고 자식에게 알립니다。

보장 사항：

- environment가 비거나 없으면 섹션이 **전혀 없어** 기존 자식 프롬프트는 바이트 단위로 불변。
- environment 읽기 실패 시 파발은 계속（fail-open）, 자식에게 environment 블록이 없을 뿐。
- 파발 시 명시 `environment`가 flow 수준 값보다 우선。
- environment는 작업 수준이며 세션 수준 `project_dir` / `AGENTS.md` 주입과 직교합니다。

---

## 설정 참조

| 설정（`TASKFLOW_INFRA`） | 기본 | 용도 |
| :--- | :--- | :--- |
| `heartbeat_timeout_s` | `600.0` | 하트비트 만료 경고 임계값 |
| `deadlock_tool_fail_count` | `3` | 동일 도구 연속 실패 횟수 |
| `deadlock_judge_fail_count` | `5` | judge 연속 retry 횟수 |
| `deadlock_no_progress_s` | `1800.0` | 무진행 경고 임계값 |

## 테스트

- `tests/agent/middlewares/test_task_intent_router.py` —— 4언어 분류、활성 flow 무동작、취소 라우팅、내부 메시지 처리。
- `tests/agent/tools/taskflow/test_watchdog.py` —— 각 경고、임계값、종료 상태 필터、알림 fail-open。
- `tests/agent/tools/taskflow/test_self_eval.py` —— 점수 파싱、통과／재실행／강제 통과、불가 전략、기본 judge 비영향。
- `tests/agent/tools/taskflow/test_template_store.py` —— 구조 허용 목록、원자 CRUD、merged 액션。
- `tests/agent/tools/taskflow/test_environment.py` —— 영속화、동기 읽기、파발 주입／우선순위／fail-open。
- `tests/agent/tools/subagent/spawn/test_system_prompt_environment.py` —— 블록 렌더링、자격 증명 참조、빈 값 하위 호환。

`uv run pytest tests/agent/tools/taskflow -q`로 실행합니다（CI 게이트는 프로세스 분리 러너 `tests/run_tests_split.py`）。
