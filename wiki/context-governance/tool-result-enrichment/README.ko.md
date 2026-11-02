# 🔧 도구 결과 보강 큐

[English](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> [Context Governance](../README.ko.md)의 일부: 등록 기반 큐가 매 도구 호출 후 `ToolMessage`의 content에 조언 힌트를 추가합니다. [system-reminder 카탈로그](../system-reminder/README.ko.md)를 모델링했지만, 턴마다가 아닌 도구 호출마다 발동합니다.

---

보강 미들웨어(`ToolResultEnrichmentMiddleware`)는 `wrap_tool_call` 체인의 **최외곽**입니다 — `ToolGuardrails` 이전에 등록되어, 다른 모든 미들웨어가 처리한 최종 결과를 봅니다. handler가 반환한 후, 등록된 모든 인젝터를 실행하여 `None`이 아닌 출력을 수집하고 `ToolMessage.content`에 주입합니다.

## JSON 인식 듀얼 모드 주입

미들웨어는 content 타입을 자동 감지하여 주입 방식을 선택합니다:

| content 타입 | 주입 방식 | 예 |
|---|---|---|
| 유효한 JSON dict | JSON 객체 내에 `"advisory"` 필드 추가 후 재직렬화 | `read_file`: `{"content": "...", "truncated": false}` → `{"content": "...", "truncated": false, "advisory": "[Advisory: ...]"}` |
| 일반 텍스트 | 끝에 `\n\n{suffix}` 추가 | `terminal`: `"Exit code: 0\noutput..."` → `"Exit code: 0\noutput...\n\n[Advisory: ...]"` |

**JSON 필드 주입이 안전한 이유**: `read_file`은 `json.dumps({"content": ..., "truncated": bool, ...})`를 반환합니다. eviction / truncation 파이프라인의 세 곳(`eviction.py`, `target_truncation.py`)이 이 content에 `json.loads`를 수행하지만 — `content`와 `truncated` 키만 읽습니다. 새 `"advisory"` 필드는 무시됩니다. 도구 선언이나 제외 집합이 필요 없습니다 — `try json.loads` 자동 감지가 모든 것을 처리합니다.

**error 결과는 건너뜀**: `status="error"`인 ToolMessage(ToolGuardrails BLOCK/HALT, HITL 거부 등)는 그대로 반환됩니다 — 실패한 도구 호출은 힌트를 받지 않습니다.

## 등록된 인젝터

카탈로그는 `ToolResultInjector` 데이터클래스로 인젝터를 등록합니다(`agent/middlewares/tool_result_enrichment/core.py`). 각각은 `injector_id`, `builder(tool_name, tool_args, result, session_id) → str | None`, `priority`, 선택적 `tools_filter`, `only_if_changed`를 선언합니다.

| 인젝터 | 우선순위 | `only_if_changed` | 적용 대상 | 주입 내용 |
|---|---|---|---|---|
| `delegation_hint` | 80 | `False` | 모든 도구 | `read_calls >= warn_after`(7) 또는 `>= block_after`(14)일 때 위임 압력 조언. 게이트의 `_PressureState`를 직접 읽습니다. |
| `hook_context` | 60 | `True` | 모든 도구 | `PostToolUse` 훅의 `additionalContext`(`UserHooksMiddleware`), `_dispatch`가 stash하고 인젝터가 drain합니다. |

**우선순위 정렬**: 우선순위 오름차순으로 정렬 — 낮은 우선순위가 먼저 추가(텍스트 상단), 높은 우선순위가 마지막에 추가(텍스트 하단 — 모델 주의력에 가장 가까움).

## DelegationPressureGate 통합

게이트의 warn / escalated 지시는 **오직** `delegation_hint` 인젝터를 통해 전달됩니다 — `abefore_model` HumanMessage 주입은 없습니다. `read_calls`가 임계값에 도달하면, **모든** 후속 도구 결과(read, write, 모든 도구)가 조언을 담습니다:

- `read_calls >= 7`(`warn_after`): `[Advisory: N read/exploration calls this turn. Consider delegating via sessions_spawn...]`
- `read_calls >= 14`(`block_after`): `[Advisory: N read/exploration calls this turn — Delegate NOW via sessions_spawn...]`

성공적인 `sessions_spawn`(accepted/pending)은 `_post_tool`에서 `read_calls`를 0으로 리셋 — spawn 결과 자체는 힌트를 담지 않습니다(인젝터가 0을 읽음 → 임계값 미만 → `None` 반환). 후속 도구 결과도 7회 더 read가 누적될 때까지 힌트가 없습니다.

## UserHooksMiddleware 연결

`UserHooksMiddleware._dispatch`는 각 훅의 stdout JSON에서 `additionalContext`를 파싱합니다(`HookOutcome.additional_context`). Step 1(읽기 전용 이벤트)에서 이 필드는 파싱되었지만 폐기되었습니다. 보강 큐가 이를 연결: `_dispatch`가 `stash_hook_context(session_id, text)`로 텍스트를 stash하고, `hook_context` 인젝터가 stash를 drain하여 `[Hook context]: ...`로 도구 결과에 추가합니다. 훅은 결과를 차단, 교체 또는 재작성할 수 없습니다 — 텍스트 추가만 가능합니다.

## system-reminder 카탈로그와의 비교

| | reminder 카탈로그 | 도구 결과 보강 |
|---|---|---|
| 발동 시점 | 매 턴의 첫 모델 호출(`before_model`) | 매 도구 호출의 반환(`wrap_tool_call`) |
| 운반체 | `HumanMessage`(별도 메시지) | `ToolMessage.content`(필드 또는 추가) |
| 우선순위 정렬 | 예산 할당(최저 우선순위 폐기) | 추가 순서(낮은 우선순위 먼저, 높은 우선순위 나중) |
| 관계 | 직교 — 공존 | 직교 — 공존 |

힌트는 모델이 이미 처리하는 도구 결과에 탑재됩니다 — 별도 메시지를 생성하지 않아 주의력을 빼앗지 않습니다. JSON 도구(`read_file`)의 힌트는 모델이 이미 파싱하는 JSON 내의 필드이고, 일반 텍스트 도구의 힌트는 content 끝의 접미사입니다. 두 경우 모두 모델의 주의력은 도구의 실제 출력에 있고, 조언은 부수적으로 보입니다.
