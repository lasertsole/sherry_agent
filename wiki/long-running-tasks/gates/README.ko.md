# 🚦 판정 게이트 — 스텝이나 플로우가 "완료"인지 누가 결정하는가

[English](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> [장시간 작업](../README.ko.md)의 일부: "결과가 돌아왔다"에서 "이 스텝/플로우는 완료"까지의 모든 게이트 — 호출자가 선언한 결말, Tier 1 스키마 게이트, 실패 분류기, 보조 LLM인 StepJudge, `taskflow_finish`의 A–E — 그리고 그들 사이의 우선순위.

주장은 사실이 아닙니다. 자식 서브에이전트가 "완료했습니다"라고 말해도, 그것이 의미가 있는지 판단하는 것은 엔진입니다. 경계마다 한 번씩, 두 층위로 판정합니다:

| 층위 | 질문 | 게이트 |
| :--- | :--- | :--- |
| **스텝**(`taskflow_resume`) | 이 결과가 호출자의 요구를 충족했는가? | 선언된 `step_outcome` → Tier 1 스키마 → 실패 분류기 → Tier 2 StepJudge |
| **플로우**(`taskflow_finish`) | 이 DAG가 실제로 끝났는가? | A DAG 완전성 → B 미해결 스텝 → C 증거 → E 목표 판정 |

**소스 코드:** `agent/tools/taskflow/tools/taskflow_resume.py`, `agent/tools/taskflow/tools/taskflow_finish.py`, `agent/tools/taskflow/tools/_retry.py`, `agent/tools/taskflow/step_judge.py`, `agent/tools/taskflow/evidence_collector.py`, `config/features/agent_side/step_judge.py`, `config/features/agent_side/goal_gate.py`. 아래의 모든 상수·판정·거부 문구는 이 코드와 대조해 검증했습니다.

## 우선순위 (기억할 단 하나의 규칙)

**호출자가 선언한 결말은 모든 게이트에 우선하며, 그 외에 다른 게이트를 단락시키는 것은 없습니다.** `step_outcome`은 호출자가 실행이 어떻게 끝났는지 말하는 수단입니다 — `failure`는 스텝을 `failed`로 만들고 판정기를 완전히 건너뛰며, `skipped`는 `skipped`로 만들고(의존 스텝은 `skipped`로 연쇄), `success` / `partial`은 결과에 기록한 뒤 게이트는 평소대로 돕니다. 알 수 없는 값은 어떤 상태 변경보다 먼저 거부됩니다. 아래 내용은 모두 호출자가 아무것도 선언하지 않았을 때의 이야기입니다.

## 스텝 층위(`taskflow_resume`)

### 0. 실패 감지 — 애초에 실패인가?

Tier 1 스키마 검증이**먼저 실행되며**(아래 참조), `schema_validated`를 산출합니다. 그 결과는 실패 분류기가 소비하는 두 신호 중 하나입니다 — 다른 하나는 결과 텍스트:

- **구조.** `schema_validated=false`(Tier 1의 출력)는 텍스트 분류가 볼 수 없는 실패이며 여기서 재디스패치됩니다 — **판정 호출 전**이므로 깨진 형태가 판정 토큰을 쓰지 않습니다.
- **결과 텍스트.** `classify_failure`(`_retry.py`)는 패턴 표에 대한 소문자 부분 문자열 스캔이며, 부정된 표현을 미리 제거합니다(`cannot connect`를 연결 실패로 읽으면 안 됩니다). 패턴 표 밖의 표현으로 된 실패는 잡히지 않습니다 — 분류기는 휴리스틱이지 판정이 아닙니다.

`retry_policy`를 가진 스텝은 분류된 실패를 확정하지 않고 대체 자식에게 재디스패치합니다(정책 예산이 남아 있는 동안). `taskflow_wait_all`은 텍스트를 반환하지 않은 죽은 자식을 분류할 수 없으므로 예산이 남아 있으면 항상 하나를 소비합니다. 정책이 없으면 실패를 기록하고 스텝은 확정됩니다(호출자가 달리 말하지 않았다면 `failed`).

### Tier 1 — 구조(무료)

스텝이 `response_schema`를 선언하면 결과를 파싱하고(호출자가 `structured_result`를 넘겼다면 그것, 아니면 결과 텍스트) 그 스키마로 검증합니다(`validate_structured_output`):

- **불일치**는 오류와 함께 `schema_validated=false`로 기록되고 실패로 취급되며 재시도 유형은 `schema_error`입니다 — 판정 호출은 절대 쓰지 않고, 재시도되지 않은 불일치는 스텝을 `failed`로 만듭니다.
- **불일치는 재시도에게 알려줍니다.** schema 실패는 판정보다 먼저 단락되므로 `judge_feedback`은 비어 있습니다. 그래서 대체 자식의 작업에는 검증기 자체의 메시지(`Your previous output failed schema validation: <필드 경로, expected/got>.`)가 `## Previous Attempt Feedback`으로 실리고, 자식은 스텝의 spawn 계약 일체(`response_schema` → 구조화 출력 요구, 기능 역할, 모델 재정의, 실행 타임아웃)를 상속합니다 — schema도 실패 이유도 볼 수 없는 재시도는 추측할 수밖에 없습니다.
- **형태는 생성 시점에도 강제됩니다.** 스텝이 `response_schema`를 선언하면 자식 에이전트는 LangChain의 `response_format`과 함께 만들어집니다(원시 schema는 `AutoStrategy`로 해석되어, 공급자가 지원하면 네이티브 구조화 출력, 아니면 구조화 출력 도구 호출). 그래서 뚜렷한 불일치는 이 검증기가 아니라 모델 단계에서 멈춥니다. schema 본문은 자식의 system prompt에 프롬프트 수준 폴백으로 남고, 이 Tier 1 검사는 두 번째 그물로 남습니다 — 공급자 가장자리 사례나 구조화 출력 도구를 호출하지 않는 모델은 여기서 잡힙니다. `sessions_spawn`은 같은 `output_schema` 필드를 노출하므로 도구 기반 자식도 같은 방식으로 구속됩니다.
- **일치**는 `structured_result` + `schema_validated=true`를 저장하고 Tier 2로 넘어갑니다. 구조만으로 내용이 옳다는 것이 증명되지 않습니다 — 그것이 두 번째 층이 존재하는 이유입니다.

### Tier 2 — 의미론(보조 LLM)

스텝에 `judge_criteria`(또는 레거시 `validation_criteria`)가 있으면 `judge_step_result`(`step_judge.py`, temperature 0)가 결과를 심사하고(검증된 `structured_result`가 있으면 그것, 아니면 결과 텍스트) 세 가지 판정 중 하나를 돌려줍니다:

| 판정 | 효과 |
| :--- | :--- |
| **`pass`** | 스텝을 `done`으로 만들고 의존 스텝을 해제합니다 |
| **`retry`** | 공유 `_retry` 시임으로 재디스패치하고, 스텝 자신의 `retry_count` 예산(`STEP_JUDGE["max_retries"]`, 기본 2)을 재사용하며, 판정기의 지침을 `## Previous Attempt Feedback`으로 대체 작업에 덧붙입니다(`with_judge_feedback`). 예산이 소진되면 스텝은 `blocked`가 됩니다 |
| **`block`** | 판정기의 이유와 함께 스텝을 `blocked`로 만듭니다(호출자의 결정: 수정·그 가지 취소·재계획) |

동작을 바꾸는 세부사항:

- **증거 요약이 동승합니다.** `STEP_JUDGE["evidence_aware"]`(기본 켜짐)이면 프롬프트가 그 플로우의 `collect_evidence_summary`를 싣습니다 — 자식이 기록한 검증 행과 거기서 파생된 스테일 여부입니다([증거 원장](../gates/evidence-ledger/README.ko.md) 참조).
- **항상 페일오픈.** 모델 오류나 파싱 불가능한 응답은 `pass`로 강등됩니다: 도달할 수 없는 판정기가 진행을 막지 않습니다. 대가는 명시적입니다 — 판정은 최선의 노력이며, 기준 충족은 결코 하드 보장이 아닙니다.
- **확정된 스텝만.** 판정기는 스텝이 실제로 확정되었을 때 돕니다(`redispatched_key is None`). 재시도 정책으로 재디스패치된 스텝은 기준을 보존한 채 최종 resume에서 판정됩니다.
- **`taskflow_resume`에 `validation_criteria`를 넘기면** 그 호출의 저장값을 **덮어씁니다** — 자식이 실제로 한 일에 따라 기준을 조이거나 바로잡기 위해서입니다.
- **결과 텍스트 예산.** `STEP_JUDGE["max_result_chars"]`(8000)가 판정 프롬프트가 보는 범위를 정합니다.

### 그리고: `done`은 "설정된 게이트를 통과했다"는 뜻

스텝의 `done`은 그 자체로 "작업이 옳다"는 뜻이 아닙니다. **어떤** 기대도 설정되지 않았다면(`response_schema`, `judge_criteria` / `validation_criteria`, `retry_policy`) 옛 의미 — "결과가 주입되었다" — 로 강등되므로, 오류를 보고한 자식도 `done`이 되어 후속 스텝을 해제할 수 있습니다. 기대를 선언하거나(`response_schema`는 무료로, 판정 호출 전에 검증됩니다) `step_outcome="failure"`를 보고하면 폐루프가 그것을 잡습니다: `failed` / `skipped` / `cancelled`는 실제 스텝 상태가 되고, 실패는 의존 스텝을 절대 해제하지 않으며, `taskflow_finish`는 미해결 스텝을 안고 완료하는 것을 거부합니다.

## 플로우 층위(`taskflow_finish`)

`taskflow_finish`는 DONE 전환을 네 검사로 순서대로 게이트합니다:

| 게이트 | 검사 | 거부 사유 |
| :--- | :--- | :--- |
| **A — DAG 완전성** | 모든 스텝이 `done` 또는 `blocked` | `step(s) not done/blocked` |
| **B — 미해결 스텝 없음** | `failed`·`skipped`·`cancelled`·`blocked` 스텝이 없음 | 해당 스텝을 id로 지목하고 기록된 이유를 함께 제시 |
| **C — 증거** | 플로우 증거에 `FAIL`, `[broken]`, `[stale]` 행이 없음([증거 원장](../gates/evidence-ledger/README.ko.md) 참조) | `failing verification evidence` / `confirmed regression` / `stale evidence` |
| **E — 목표 판정** | 수집된 결과가 플로우의 `description`을 충족하는지 보조 모델이 판정 | `taskflow_replan`을 지목하는 거부 |

모든 게이트는 **페일오픈**입니다: 읽을 수 없는 collector, 모델 오류, 파싱 불가능한 판정은 완료를 막지 않고 통과시킵니다. Gate E는 `description`이 있고 스텝 수가 `GOAL_GATE["min_steps"]`(3) 이상인 플로우에서만 돕니다 — 한두 스텝짜리 플로우는 자식 층위 [완료 판정기](../../subagent/README.ko.md)가 이미 덮고 있고, 같은 작업에 두 번째 판정을 얹는 것이 바로 그 중복이기 때문입니다. `GOAL_GATE["enabled"]`가 마스터 스위치이고 `GOAL_GATE["max_result_chars"]`(4000)가 넘겨지는 각 결과의 창입니다.

## 이웃 게이트 (다른 페이지, 같은 생각)

이 페이지가 소유하는 것은 TaskFlow의 게이트입니다. "주장은 사실이 아니다"라는 규칙은 다른 곳에서도 반복되며, 각자 있는 자리에 기록되어 있습니다:

- **[증거 원장](../gates/evidence-ledger/README.ko.md)** — 기록된 검증 트레일. 그 요약은 Gate C의 입력이자 StepJudge 프롬프트의 증거 절입니다.
- **[서브에이전트 완료 게이트](../../subagent/README.ko.md)** — 자식 층위의 두 겹: CompletionJudge 목표 루프(DONE / CONTINUE, `COMPLETION_JUDGE["goal_max_turns"]`로 유계; 자식의 원장이 통과·비스테일 행을 보이면 첫 호출을 건너뜀)와 완료 drain의 프로그램 게이트(`SubagentCompletionDrainMiddleware`, 부모 측).
- **[TodoList 강제 계층](../todolist/enforcement/README.ko.md)** — 계획 측: E1–E7 강제 계층(턴 종료 연속 게이트와 그 예산 포함).
- **루프 방지** — [wiki/loop-prevention](../../loop-prevention/README.ko.md)가 가드들의 고도 매트릭스(턴 / 스텝 / 서브에이전트 / 파이프라인)와 계층별 페일오픈 정책을 모아 둡니다.

## 설정

| 노브 | 기본값 | 효과 |
| :--- | :--- | :--- |
| `STEP_JUDGE["max_retries"]` | 2 | 판정기가 쓸 수 있는 `retry` 판정 수(스텝 자신의 `retry_count` 예산) |
| `STEP_JUDGE["max_result_chars"]` | 8000 | 판정 프롬프트의 결과 텍스트 창 |
| `STEP_JUDGE["evidence_aware"]` | True | 증거 요약을 판정 프롬프트에 넣을지 |
| `GOAL_GATE["enabled"]` | True | Gate E의 마스터 스위치 |
| `GOAL_GATE["min_steps"]` | 3 | Gate E가 도는 최소 플로우 규모 |
| `GOAL_GATE["max_result_chars"]` | 4000 | 목표 판정 프롬프트의 결과별 창 |

## 테스트

`tests/agent/tools/taskflow/`가 이 계층을 고정합니다: `test_validation.py`(기준 저장, 판정, 덮어쓰기, 페일오픈), `test_step_judge.py`(파싱, 판정 정규화, 프롬프트 조립), `test_resume_with_judge.py`(resume + 판정: pass / retry / block, 재시도 피드백 주입, 예산 소진), `test_finish_gate.py`(게이트 A–C), `test_retry_policy.py`(분류 + 재디스패치), `test_expectation_closure.py`(기대→실제 폐루프 규칙), `test_chain_smoke.py`(판정 → 재시도 피드백 → 목표 루프 → 완료 게이트를 스텁 보조 LLM에 대해 통과시키는 스모크).
