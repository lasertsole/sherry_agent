# Plan-Then-Execute 플랜 모드

[English](README.md) · [中文](README.zh.md) · [日本語](README.ja.md) · **한국어**

세션은 실행 전에 설계할 수 있습니다: 에이전트가 **플랜 모드**에 들어가 읽기 전용 도구만으로 작업하고, 계획을 제출한 뒤 사용자의 승인 전까지 어떤 파일도 쓰지 않습니다. 다중 파일 변경으로 곧바로 돌진하는 것이 가장 어긋나기 쉬운 길이며, 사용자는 방향을 바로잡을 기회를 얻지 못합니다.

사실 출처: `agent/tools/plan_mode/`(두 도구 + 공유 상태 헬퍼), `agent/middlewares/tool_selection/core.py`(허용 목록), `workspace/prompt_builder.py::_build_plan_mode_block`(시스템 리마인더), `config/features/agent_side/plan_mode.py`(조정 값).

## 흐름

```
                  enter_plan_mode
   needs_planning ────────────────► planning ──────────────► ready
        ▲                              │  ▲                   │
        │        reject (no message)   │  │ reject (message)  │ enter_plan_mode(replan=True)
        └──────────────────────────────┘  └─── revise ────────┘
                                                               │
                                            needs_replan ◄─────┘
                                                  │  (exit_plan_mode + 승인)
                                                  └──────────► ready
```

1. `enter_plan_mode(reason)` — 에이전트가 계획이 필요한 작업이라고 판단한 경우(다중 파일 변경, 아키텍처 결정, 불명확한 요구사항, 여러 유효한 접근). 세션 상태는 `planning`이 되고 도구 면은 읽기 전용 허용 목록으로 좁아집니다.
2. 에이전트는 탐색하고(`read_file`, `search_files`, `message_search`, 스킬 뷰어), 모호한 점은 `question`으로 사용자에게 묻고 설계합니다. 플랜 모드의 턴은 `question` 또는 `exit_plan_mode` 호출로만 끝날 수 있습니다.
3. `exit_plan_mode(plan)` — 계획을 `workspace/sessions/<session_id>/plans/plan-<revision>.md`에 저장하고 승인을 위해 그래프를 중단합니다(채팅에는 **승인**과 **수정 요청**이 있는 카드가 표시됩니다).
4. **승인** → 상태 `ready`. 계획은 세션의 참조로 남고(시스템 프롬프트가 매 턴 그 파일을 지목합니다) 쓰기 도구가 돌아옵니다. **수정 요청**(reject + 피드백) → 피드백이 에이전트로 돌아가고 수정 후 재제출합니다. **거부**(메시지 없음) → 계획은 폐기되고 재계획 여부는 에이전트가 정합니다.

## 읽기 전용 보장

상태가 `planning` / `needs_replan`인 동안 `ToolSelectionMiddleware`는 기존의 세션별 도구 선택과 똑같은 두 단계로 허용 목록을 적용합니다: 모델에는 허용 목록만 **제시**되고, 그래도 도착한 호출(오래된 checkpoint, 환각된 이름)은 실행 단계에서 **거부**되어 "Plan mode is active"를 돌려받습니다. 기본 허용 목록은 `config/features/agent_side/plan_mode.py::PLAN_MODE["allowed_tools"]`입니다. 세션 자체의 선택에도 묶이지만, 두 플랜 도구만은 항상 도달 가능합니다 — 나갈 수 없는 모드는 함정입니다.

서브에이전트라는 유일한 구멍은 의도적으로 막았습니다: `sessions_spawn`은 허용 목록에 **없습니다**. 자식은 자기 세션 상태로 동작하므로 승인 전에 쓸 수 있기 때문입니다.

## 가드

* **수정 예산** — `PLAN_MODE["max_mutations"]`(5): 계획당 "수정 요청" 5회까지. 초과하면 `exit_plan_mode`가 재제출을 거부하고 사용자에게 승인/거부를 요청하게 합니다.
* **리비전 결속** — 승인은 `sha256(plan)[:16]`에 묶입니다. 승인 대기 중 저장된 리비전이나 계획 **파일**이 바뀌면 승인은 폐기되고 재제출해야 합니다.
* **크기 상한** — `PLAN_MODE["max_plan_chars"]`(20000). 더 긴 계획은 "줄여라"로 거부되며 잘리지 않습니다.
* **기능 끔** — `PLAN_MODE["enabled"] = false`이면 두 도구가 등록되지 않고 나머지는 그대로입니다.

## 사용자에게 보이는 것

계획은 입력 슬롯에 승인 카드로 나타납니다(HITL이 쓰는 것과 같은 면): 계획 본문, 피드백 입력, **수정 요청**, **승인**. 승인하면 실행 도구가 살아 있는 채로 턴이 재개되고, 수정 요청이면 피드백과 함께 재개되어 에이전트가 계획을 고칩니다.

## 다른 게이트와의 관계

플랜 모드는 "실행 전 승인", HITL은 "실행 중 승인"(실행이 시작되면 원래대로 해당 작업을 막습니다)입니다. TaskFlow는 "승인 후 실행": 계획의 번호 단계를 flow 단계나 todo로 등록하면 완료 게이트가 결과를 판정합니다.
