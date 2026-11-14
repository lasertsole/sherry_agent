# 🔁 LDW 리플레이

[English](README.md) · [中文](README.zh.md) · [日本語](README.ja.md) · **한국어**

> 검증된 TaskFlow 계획을 위한 워크플로 수준 리플레이입니다. 스텝 DAG는 구조화 메타데이터로 컴파일되고, vendored [langchain-dynamic-workflow](https://github.com/OWNER/langchain-dynamic-workflow) 코어가 내용 해시 journal로 실행합니다. LDW 리플레이가 실패하면 같은 검증된 DAG에서 새 TaskFlow flow를 자동 생성하고, 성공한 결과를 journal에 다시 기록합니다.

정보 출처: `vendor/ldw/**`, `agent/tools/ldw/**`, `agent/tools/subagent/ldw_shim.py`, `config/features/agent_side/ldw.py`, `tests/agent/tools/ldw/**`, `tests/full/test_ldw_real_plan_e2e.py`, `tests/agent/tools/subagent/test_ldw_shim.py`.

## 🎯 개요

LDW 리플레이는 이미 TaskFlow에서 성공한 계획의 **두 번째 실행 경로**입니다:

| 경로 | 단위 | 캐시 | 트리거 |
|---|---|---|---|
| TaskFlow | step / subagent | evidence ledger + registry | 일반 계획 실행 |
| PTC 리플레이 | step 내부의 도구 호출 | skill 스크립트 | executor spawn 프리필 |
| LDW 리플레이 | step DAG | prompt + agent_type + model + schema로 키가 정해지는 journal | 리플레이 횟수가 임계값에 도달한 뒤의 `workflow_replay` 도구 |

LDW 코어는 `vendor/ldw/`에 전체 vendored되어 있고, import 시 최소 `deepagents` stub을 설치합니다. Sherry가 자체 LangGraph leaf runnable을 제공하므로 LDW의 sandbox/leaf 구성 모듈은 호출되지 않습니다.

## 🧭 트리거와 도구

main agent에는 main-only `workflow_replay` 도구가 있습니다:

```json
{"action": "status", "variant_input": "", "flow_id": "", "plan_key": "", "force": false}
```

- `status`는 리플레이 횟수, 최소 임계값, 컴파일된 스크립트 경로, 현재 session에 읽을 수 있는 TaskFlow DAG가 있는지를 반환합니다.
- `compile`은 현재 TaskFlow flow DAG에서 `ldw__<plan_key>.py`를 생성합니다.
- `replay`는 리플레이 횟수를 늘린 뒤 `LDW["min_replays"]` 미만이면 TaskFlow에 머물고, 그 이상이면 LDW를 실행합니다. LDW의 부분 실패는 자동으로 TaskFlow 강등으로 들어갑니다.
- 반환 JSON은 구조화되어 있습니다: `status=ok|taskflow|degraded|partial|no_workflow|no_plan|disabled|error`와 `failed_steps`, `journal_hits`, `leaf_calls`, `degraded`, 해당되는 repair 필드. `ok`는 `ok`, `taskflow`, `degraded`에서 참입니다.

리플레이 임계값은 의도적으로 지연됩니다. 빈도가 낮은 계획은 LDW 준비 비용을 지불하지 않습니다. 계획은 임계값(기본 3)에 도달할 때 현재 TaskFlow flow에서 컴파일되며, skill review가 미리 컴파일하지 않습니다.

`variant_input`은 현재 작업의 구체 입력입니다. 각 step prompt에 들어가므로 journal key에도 들어가고, 같은 계획이라도 변형이 다르면 예전 캐시에 맞지 않습니다. step 접미사도 `agent_type`의 일부입니다. PTC 스크립트가 step 단위이기 때문입니다.

## 🧱 컴파일과 산출물

`compile_plan_to_workflow(plan_knowledge)`는 JSON으로 영속화 가능한 메타데이터를 반환합니다:

```json
{"plan_key": "...", "flow_id": "...", "steps": [{"step_id": "step-1", "task_template": "...", "agent_type": "executor@step-1", "deps": [], "response_schema": null, "step_timeout_seconds": null, "retry_policy": null}]}
```

`build_orchestrate(meta, variant_input)`은 LDW `orchestrate` 호출 가능 객체를 만듭니다. 각 `DagNode`는 선행 결과를 받고, prompt에는 `[Current task input]`, `[Hard constraints]`, `[Previous step results]`가 들어갑니다. 이것이 변형과 상류 출력이 바뀌어도 내용 해시 journal을 정확하게 유지합니다. schema가 있는 step은 `ctx.agent(..., schema=...)`를 호출하므로 leaf는 자유 텍스트가 아니라 검증된 구조화 객체를 반환합니다.

산출물은 소유 skill의 최상위 `scripts/`에 있습니다:

```text
skills/auto/<owning-skill>/scripts/ldw__<plan_key>.py
skills/auto/<owning-skill>/scripts/ptc__<plan_key>__<step_id>.py
```

LDW 스크립트는 `PLAN_KEY`, `REQUIRED_AGENT_TYPES`, `WORKFLOW_META`를 Python 모듈 상수로 저장합니다. 클로저, 소스 문자열, `workflow.json` 사이드카는 저장하지 않습니다. 작성기는 JSON 텍스트가 아니라 Python 리터럴을 사용합니다. `json.dumps`는 소문자 `true` / `false` / `null`을 출력하므로 `additionalProperties: false`가 있는 schema는 스크립트를 읽을 수 없게 만들기 때문입니다.

## ⚙️ 런타임과 Journal

- `register_roster(meta, session_id)`는 **step마다 builder 하나**를 등록합니다. builder는 LDW가 schema에 묶인 runnable을 요청하게 하고, `agent_type`의 step 접미사는 journal과 PTC 조회를 step 단위로 유지합니다.
- leaf는 실제 child 계약을 재사용합니다. `_assemble_child_tools`가 역할 allow-list를 적용하고 code intelligence, ast-grep, executor 전용 PTC를 주입합니다. session id는 runnable config를 통해 전달되며 공유 그래프에 구워지지 않습니다.
- leaf 모델은 역할 tier와 `step_model` 재정의를 따릅니다. schema가 있는 leaf는 thinking을 강제로 끄며, child agent의 구조화 출력 마지막 단계와 맞춥니다.
- journal miss에서 executor leaf는 먼저 `run_ptc`로 해당 step의 PTC 스크립트를 실행할 수 있습니다. PTC 실패는 `[PTC Pre-run Progress]`를 덧붙이고 ReAct로 계속합니다. PTC의 schema miss는 실패이며 성공이 아닙니다.
- `src/data/workflow_runs.db`의 `SqliteWorkflowStore`가 프로세스 간 재개를 지원합니다. 같은 prompt / agent_type / model / schema의 반복 리플레이는 journal 결과를 반환하고 leaf 호출은 0입니다. 각 결과는 `journal_hits`와 `leaf_calls`를 보고합니다.
- 계획별 잠금이 동시 리플레이를 직렬화합니다. `LDW["real_replay_timeout_s"]`는 한 번의 LDW 실행을 제한하며, 타임아웃은 멈춤이 아니라 실패이자 강등 후보입니다.

## 🪜 자동 TaskFlow 강등

LDW 실행이 실패하고 `LDW["auto_degrade_enabled"]`가 켜져 있으면 어댑터는 **같은 검증된 DAG**에서 실행을 다시 구성합니다. 오래된 flow를 reset하거나 다시 실행하지 않습니다.

1. 새 `ldw-<plan_key 앞 8자>-<8자 uuid>` TaskFlow flow를 만들고, 원본 flow가 아직 있으면 `resumed_from=<origin>`을 붙입니다.
2. None이 아닌 모든 LDW 결과를 DONE step으로 미리 넣고 합성 `ldw-journal:<flow>:<step>` child key와 결과 레코드를 붙입니다. 실패했거나 끝나지 않은 step만 ready/blocked로 남습니다.
3. ready step을 정상 TaskFlow로 dispatch하므로 retry policy, schema gate, StepJudge, child 완료 의미가 일반 실행과 완전히 같습니다.
4. `taskflow_wait_all`로 폴링한 뒤 settled child마다 `taskflow_resume`을 호출합니다. 타임아웃되거나 kill된 child는 failure로 선언됩니다. `auto_dispatch`가 다음 물결을 시작합니다.
5. 모두 완료된 flow는 정상 finish합니다. finish가 거부되거나 미완료 작업이 있으면 flow를 종결(fail 또는 cancel)해 활성 고아 flow를 남기지 않습니다.
6. 같은 prompt/agent_type/model/schema key로 TaskFlow 성공 결과를 LDW journal에 다시 기록하므로, 그 변형의 다음 LDW 리플레이는 0회 호출입니다.

step이 실패하면 `repair_extraction`이 replay flow의 child handoff를 읽고 실제 성공 경로와 실패 분기를 계획 지식에 병합하며, `success-steps.json`을 영속화하고 LDW 스크립트를 다시 컴파일합니다. Repair는 fail-open입니다. 영속화나 재컴파일 오류는 결과에 반환되지만 리플레이 자체를 크래시시키지 않습니다. PTC 스크립트는 skill-review 컴파일러 소유이며, LDW repair는 자기 산출물만 다시 컴파일합니다.

## 🔧 설정

| 키 | 기본값 | 효과 |
|---|---|---|
| `LDW["enabled"]` | `True` | 마스터 스위치. False면 모든 리플레이가 TaskFlow 경로에 머뭅니다 |
| `LDW["min_replays"]` | `3` | 계획이 TaskFlow를 떠나 LDW 스크립트를 컴파일하기 전 리플레이 횟수 |
| `extraction.flow_extraction_enabled`(프리셋 캐릭터 → 고급) | `True` | 사용자 대상 컴파일 스위치(전역 전용). 꺼져 있으면 `action=compile`, 누락 스크립트 컴파일, 수리 재컴파일을 거부. 기존 스크립트 재생은 가능 |
| `LDW["script_max_bytes"]` | `65536` | `ldw__<plan_key>.py` 하나의 상한 |
| `LDW["journal_path"]` | `src/data/workflow_runs.db` | 프로세스 간 journal 위치 |
| `LDW["auto_degrade_enabled"]` | `True` | LDW 실패 후 TaskFlow replay flow를 만들고 구동 |
| `LDW["repair_enabled"]` | `True` | 강등 실패 후 repair 추출 / 재컴파일 실행 |
| `LDW["real_replay_timeout_s"]` | `900.0` | 한 번의 LDW 실행 실시간 상한 |
| `LDW["max_failed_steps"]` | `5` | 실패 step이 이 수를 넘으면 강등을 거부 |
| `PTC["ptc_prefill_enabled"]` | `False` | LDW leaf가 사용하는 선택적 PTC 스크립트를 켬 |

## 🔒 제한

- 이것은 리플레이 가속기이며 sandbox도 재시도 엔진도 아닙니다.
- LDW leaf는 역할 도구 정책을 상속합니다. LDW의 sandbox/deepagents 경로는 의도적으로 사용하지 않습니다.
- journal은 **정확 일치**입니다: `prompt` / `agent_type` / `model` / `schema` / `isolation`를 해시하고 변형이 바뀌면 다시 실행합니다. fuzzy 매칭과 prompt의 공백/Unicode 정규화는 의도적으로 하지 않습니다——step prompt에는 공백이 의미를 갖는 코드나 구조화된 상류 결과가 포함될 수 있어, 근접 일치 캐시는 잘못된 출력을 하류로 넘길 수 있습니다. miss 비용은 실제 실행 한 번이며 성공 후 기록됩니다.
- 계획이나 변형이 바뀌면 다른 journal key가 되어 다시 실행됩니다. 오래된 스크립트가 현재 prompt보다 우선하지 않습니다.
- 다시 실행한 실패 step의 부작용은 롤백되지 않습니다. journal은 이전에 성공한 step만 건너뜁니다.
- 강등 시 성공 step은 절대 다시 spawn되지 않습니다. 실패 step은 TaskFlow의 retry/judge를 따르며 LDW만의 재시도 루프는 없습니다.

## 🗺️ 테스트

| 영역 | 테스트 |
|---|---|
| Vendored 코어 import + DAG + journal + 실패 전파 | `tests/agent/tools/subagent/test_ldw_shim.py` |
| 메타데이터 / 의존성 / 변형 / schema prompt | `tests/agent/tools/ldw/test_compiler.py` |
| 스크립트 리터럴 / 왕복 / 안전하지 않은 key | `tests/agent/tools/ldw/test_scripts.py` |
| 임계값 / SQLite journal / 자동 강등 결과 | `tests/agent/tools/ldw/test_replay.py` |
| Leaf 도구 면 / schema / PTC 성공과 폴백 | `tests/agent/tools/ldw/test_leaf.py` |
| 강등 프리시드 / resume / journal 기록 | `tests/agent/tools/ldw/test_taskflow_adapter.py` |
| Repair 병합 / 영속화 / 재컴파일 | `tests/agent/tools/ldw/test_repair.py` |
| Main-only 도구 면과 degraded 상태 | `tests/agent/tools/ldw/test_tool.py` |
| 실제 TaskFlow + 실제 모델 리플레이 / 두 번째 실행 / 강등 / repair / 실제 agent | `tests/full/test_ldw_real_plan_e2e.py` |
