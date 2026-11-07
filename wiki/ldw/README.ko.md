# 🔁 LDW 리플레이

[English](README.md) · [中文](README.zh.md) · [日本語](README.ja.md) · **한국어**

> 검증된 TaskFlow plan의 workflow 수준 리플레이입니다. step DAG를 구조화 메타데이터로 컴파일하고, vendored [langchain-dynamic-workflow](https://github.com/OWNER/langchain-dynamic-workflow) 코어가 콘텐츠 해시 journal로 실행합니다. [PTC](../ptc/README.md)의 도구 호출 수준 prefill과互补 관계입니다.

정보 출처: `vendor/ldw/**`, `agent/tools/ldw/**`, `agent/tools/subagent/ldw_shim.py`, `config/features/agent_side/ldw.py`, `tests/agent/tools/ldw/**`, `tests/agent/tools/subagent/test_ldw_shim.py`.

## 🎯 개요

LDW 리플레이는 TaskFlow에서 이미 성공한 plan의 **두 번째 실행 경로**입니다.

| 경로 | 단위 | 캐시 | 트리거 |
|---|---|---|---|
| TaskFlow | step / subagent | evidence ledger + registry | 일반 plan 실행 |
| PTC 리플레이 | step 내부 도구 호출 | skill 스크립트 | executor spawn prefill |
| LDW 리플레이 | step DAG | prompt + agent_type + model + schema journal | replay 횟수가 임계값에 도달한 뒤 `workflow_replay` 도구 |

LDW 코어는 `vendor/ldw/`에 vendoring되며 import 시 최소 `deepagents` stub을 설치합니다. sherry는 자체 LangGraph leaf runnable을 사용하므로 LDW의 sandbox/leaf 구성 경로는 호출되지 않습니다.

## 🧭 트리거와 도구

main agent에는 main-only `workflow_replay` 도구가 있습니다.

```json
{"action": "status", "variant_input": "", "flow_id": "", "plan_key": "", "force": false}
```

- `status`는 replay 횟수와 스크립트 경로를 반환합니다.
- `compile`은 현재 TaskFlow flow DAG에서 `ldw__<plan_key>.py`를 생성합니다.
- `replay`는 횟수를 증가시키고, `LDW["min_replays"]` 미만이면 TaskFlow에 머물며, 임계값 이상이면 LDW를 실행합니다.

임계값은 의도적으로 lazy입니다. 빈도가 낮은 plan에 LDW 초기화 비용을 지우지 않습니다. 기본적으로 세 번째 replay에서 현재 TaskFlow flow로 컴파일하며, skill review가 미리 컴파일하지 않습니다.

`variant_input`은 현재 작업 입력이며 각 step prompt에 들어가 journal key를 구분합니다. step 접미사도 `agent_type`에 포함됩니다(PTC 스크립트가 step 단위이기 때문).

## 🧱 컴파일과 산출물

`compile_plan_to_workflow(plan_knowledge)`는 JSON으로 영속화 가능한 메타데이터를 반환합니다.

```json
{"plan_key": "...", "flow_id": "...", "steps": [{"step_id": "step-1", "task_template": "...", "agent_type": "executor@step-1", "deps": [], "response_schema": null}]}
```

`build_orchestrate(meta, variant_input)`가 LDW `orchestrate`를 만듭니다. 각 `DagNode`는 선행 결과를 받고, prompt에 `[Current task input]`과 `[Previous step results]`를 넣으므로 변형과 상류 출력이 다르면 journal key도 달라집니다.

산출물은 소유 skill의 `scripts/` 바로 아래에 둡니다.

```text
skills/auto/<owning-skill>/scripts/ldw__<plan_key>.py
skills/auto/<owning-skill>/scripts/ptc__<plan_key>__<step_id>.py
```

LDW 스크립트는 `PLAN_KEY`, `REQUIRED_AGENT_TYPES`, `WORKFLOW_META`를 모듈 상수로 가집니다. closure, source 문자열, `workflow.json` sidecar는 저장하지 않습니다.

## ⚙️ 실행과 journal

- `register_roster(meta, session_id)`는 step마다 session-bound runnable을 만들고 `session_id`를 runnable config로 전달합니다.
- leaf는 role의 도구 허용 목록으로 만든 LangGraph 그래프입니다. session을 공유 그래프에 굽지 않습니다.
- journal miss 시 executor leaf는 먼저 step의 PTC 스크립트를 `run_ptc`로 실행할 수 있습니다. PTC 실패는 hard gate이며 ReAct로 폴백합니다.
- `SqliteWorkflowStore`(`src/data/workflow_runs.db`)가 cross-process resume을 지원합니다. 같은 prompt / agent_type / model / schema의 재리플레이는 zero-call입니다.
- 같은 plan의 리플레이는 per-plan lock으로 직렬화합니다.

## 🪜 강등과 수리

LDW에는 step retry/judge가 없습니다.

- 실패한 DAG 노드는 `None`이 되고 의존 노드는 skip됩니다.
- 기존 "dag 재실행" L2는 삭제되었습니다. root 실패, 실패율 >50%, 실패 step에 의존 노드가 있으면 **L2 TaskFlow 강등**으로 바로 가고, 그 외 실패 leaf는 partial을 받아들입니다.
- TaskFlow 강등은 실패 step만 재실행합니다. 성공 step은 journal로 skip하고, 성공 결과는 journal에 씁니다.
- 수리 추출은 실제 성공 경로와 failed_branches를 plan knowledge에 병합하고, 조건이 맞으면 LDW 스크립트를 재컴파일합니다. `merge_knowledge`, `extract_react_repair_experience`, `extract_failure_experience`, `repair_extraction`을 제공합니다.

## 🔧 구성

| 키 | 기본값 | 효과 |
|---|---|---|
| `LDW["min_replays"]` | `3` | LDW workflow를 컴파일하기 전 replay 횟수 |
| `LDW["script_max_bytes"]` | `65536` | `ldw__<plan_key>.py` 하나의 상한 |
| `LDW["journal_path"]` | `src/data/workflow_runs.db` | cross-process journal 경로 |
| `PTC["ptc_prefill_enabled"]` | `False` | LDW leaf가 쓰는 PTC 스크립트 활성화 |

## 🔒 한계

- 리플레이 가속기이며 sandbox나 retry 엔진이 아닙니다.
- LDW leaf는 role 도구 정책을 상속합니다. LDW sandbox/deepagents 경로는 의도적으로 사용하지 않습니다.
- plan / 변형이 바뀌면 journal key가 달라져 재실행됩니다. 오래된 스크립트가 현재 prompt를 이기지 않습니다.
- 처음 두 replay는 명시적으로 TaskFlow에 머뭅니다. 세 번째부터 LDW 비용을 지불합니다.
- LDW 실행이 실패하면 도구는 partial/error를 반환하고 호출자는 TaskFlow로 돌아갑니다. skip을 성공으로 취급하지 않습니다.

## 🗺️ 테스트

| 범위 | 테스트 |
|---|---|
| vendored core import + DAG + journal + 실패 전파 | `tests/agent/tools/subagent/test_ldw_shim.py` |
| 메타데이터 / deps / 변형 prompt | `tests/agent/tools/ldw/test_compiler.py` |
| 임계값 / SQLite journal / compile-on-miss | `tests/agent/tools/ldw/test_replay.py` |
| 수리 병합 / 추출 경로 | `tests/agent/tools/ldw/test_repair.py` |
| main-only 도구 표면 | `tests/agent/tools/ldw/test_tool.py` |
| 실제 main agent가 `workflow_replay` 호출 | `tests/full/test_ldw_replay_e2e.py` |
