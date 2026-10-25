# Session Token 예산

> 세션별 token 상한, 턴 간 사용량 누적, 추정 비용.

## 🎯 개요

세션은 선택적 token **상한**(`StateKey.TOKEN_BUDGET_CAP`)을 가질 수 있습니다.
`TOKEN_BUDGET_USED >= cap`이면 `TokenBudget`
(`agent/middlewares/token_budget/core.py`)이 다음 model call을 종단
`AIMessage`로 단락시키고 provider에 도달하지 않습니다. 턴은 요약 요청으로
끝나며, 운영자는 상한을 올리거나 카운터를 초기화할 수 있습니다.

미들웨어는 `IterationBudget` 옆에 있으며 항상 켜져 있고(비활성화 불가),
상한이 없으면 완전한 pass-through입니다. token 수는 provider의
`usage_metadata`에서 오므로 정확하고, 비용은 추정값입니다.

## ⚙️ 설정

- `PUT /sessions/budget`으로 상한 설정(`null` 해제, `0` 무제한). 턴 실행 중
  변경은 `TOKEN_BUDGET_CAP_PENDING`에 PARK되고 턴 경계에서 promote됩니다
  (thinking/model 제어와 동일).
- `GET /sessions/budget`은 `{cap, used, cost, limited, pending}` 반환.
- `POST /sessions/budget/reset`은 `TOKEN_BUDGET_USED` /
  `TOKEN_BUDGET_COST` / `TOKEN_BUDGET_LIMITED`를 지우고 상한은 유지.
- `TOKEN_BUDGET_USED` / `TOKEN_BUDGET_COST`는 턴을 넘어 누적되고 mem + DB
  레지스터에 이중 기록됩니다. 턴별 iteration budget으로는 초기화되지 않습니다.
  `clear_session`은 다른 session state와 함께 삭제합니다.
- `budget_warning`은 `MODEL_PRICING["budget_warn_threshold"]`(0.80)에서 1회,
  `budget_limited`는 상한 최초 도달 시 1회 WS로 best-effort 전송됩니다.

- 시스템 설정 → Token 예산 탭(`TokenBudgetPanel.vue`)에서 열린 세션의 상한을 편집하고 사용량을 초기화합니다. 셸의 활성 세션을 따르며, 세션이 없으면 빈 상태를 표시합니다.
## 🧮 가격 조회

가격표는 `.models`에서 사용자가 설정합니다:

```jsonc
"MODEL_PRICING_TABLE": "{\"glm-5\": {\"input\": 0.5, \"output\": 1.5}, \"_default\": {\"input\": 0.5, \"output\": 1.5}}"
```

값은 JSON OBJECT를 JSON 문자열로 직렬화한 것이며, 평면 `.models` key/value
편집기에서도 동작합니다. 기존 파일에 표가 없으면 import 시 추가됩니다
(템플릿이 기본 항목 제공).

`TokenBudget._cost_delta`는 네 단계로 비용을 해석합니다:

1. `usage_metadata` / `response_metadata`의 provider 직접 반환 cost
   (`cost` / `total_cost` / `estimated_cost`, 또는 `prompt_cost` +
   `completion_cost`)를 그대로 사용;
2. 없으면 `config/features/infra_side/model_pricing.py::resolve_model_pricing`이
   사용자 표(정확한 이름, 다음 `/` 뒤의 이름)를 조회;
3. 다음으로 로컬 litellm 캐시 `src/data/litellm_prices.json`(gitignored;
   `uv run --no-sync python scripts/sync_litellm_prices.py`로 갱신);
4. 마지막으로 표의 `_default`(표 자체를 쓸 수 없으면 `input $0.5/M` /
   `output $1.5/M`).

상한은 token만 기준으로 하며 이 추정값에 의존하지 않습니다.

## 🌐 HTTP API

| Method | Path | Body / query |
| :--- | :--- | :--- |
| `GET` | `/sessions/budget` | `?session_id=<sid>` |
| `PUT` | `/sessions/budget` | `{session_id, cap: int \| null}` |
| `POST` | `/sessions/budget/reset` | `{session_id}` |

## 🧪 테스트

`tests/agent/middlewares/test_token_budget.py`(단락, 누적, provider cost,
warning/limited 이벤트), `tests/server/test_token_budget_settings.py`
(idle 쓰기, busy park/promote, reset, latch 해제),
`tests/config/test_model_pricing.py`(표 로드, litellm/default 우선순위,
provider 접두 이름),
`tests/server/trigger/http/test_token_budget_api.py`(HTTP 상한 설정 → 미들웨어
누적 → 단락 → GET 보고 → reset의 엔드투엔드 경로).
