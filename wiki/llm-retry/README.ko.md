# 🔁 LLM 재시도 예산과 카운트

[English](README.md) · [中文](README.zh.md) · [日本語](README.ja.md) · **한국어**

> Sherry가 제공자 재시도를 어떻게 세는지: 모델 호출마다 하나의 물리 시도 순서, 별도의 예산 시도, 예산을 소비하지 않는 추가 시도, 턴당 fallback 전환 상한, 그리고 다음 턴으로 넘어가지 않는 타임아웃 차단.

## 카운트 모델

ZCode adapter는 `attempt`를 한 번의 모델 요청에 대한 지역 변수로 둡니다. Sherry도 같은 모델을 따릅니다: `wrap_model_call` / `awrap_model_call`는 매번 `retry_count = 0`에서 시작하고, 새 모델 호출은 새 예산입니다.

WS 이벤트는 네 개의 숫자를 전달하므로 UI가 추측할 필요가 없습니다:

| 필드 | 의미 |
| --- | --- |
| `attempt` | 추가 시도를 포함한 물리 요청 번호 |
| `budget_attempt` | 물리 시도에서 `extra_attempts`를 뺀 값 |
| `extra_attempts` | 인증 갱신 / 서명 복구 시도. 예산을 소비하지 않음 |
| `max_attempts` | `1 + max_retries + extra_attempts` |

`retries = budget_attempt - 1`. 기본값 `max_retries = 10`이므로 일반 턴은 `2/11`, ..., `11/11`로 표시됩니다.

## 카운터가 초기화되는 경우

예산은 요청 경로가 바뀔 때만 초기화됩니다. 추가 시도는 뺄셈으로 처리하며 초기화가 아닙니다.

| 이벤트 | 예산 | 추가 | 이유 |
| --- | --- | --- | --- |
| 모델 호출 진입 | 초기화 | 초기화 | 각 모델 호출은 자체 `retry_count`를 가짐 |
| fallback 전환 | 초기화 | 초기화 | 새 모델, 새 요청, 새 예산 |
| 멀티모달 강등 | 초기화 | 초기화 | 요청이 skill 경로로 이동 |
| 콘텐츠 필터 재바인딩 | 초기화 | 초기화 | 요청이 다른 모델로 이동 |
| 인증 갱신 | 변경 없음 | +1 | 추가 시도이며 `max_retries`를 소비하면 안 됨 |
| 서명 복구 | 변경 없음 | +1 | 같은 규칙 |
| 빈 완료 | +1 | 변경 없음 | 실제 재시도이며 자체 상한 1을 가짐 |
| 성공 | 폐기 | 폐기 | 카운터는 호출 지역 변수 |

텍스트도 tool call도 usage도 없는 빈 완료는 일반 재시도 1회를 소비하고, 추가로 `empty_completion_retries = 1`로 제한됩니다. 이는 ZCode의 `canRetryEmptyCompletion`과 같으며 `retryCount < 1`과 `attempt < maxAttempts`를 모두 확인합니다.

## fallback 전환

fallback 후보는 새 모델 요청을 시작하므로 재시도 예산이 0에서 시작합니다. 상한이 없으면 N개 후보가 `11 × N`개의 물리 요청을 보낼 수 있습니다. 그래서 Sherry는 턴마다 `llm_fallback_switches`를 기록하고 `max_fallback_switches`(기본 `1`)에 도달하면 전환을 거부합니다. `0`은 이미 고정된 후보도 비활성화합니다. 세션의 fallback 인덱스는 바뀌지 않으므로 이후 턴도 이미 선택된 후보에서 시작합니다.

## 타임아웃 차단 범위

`llm_stale_streak`는 연속 timeout 실패를 셉니다. `stale_giveup_scope = "turn"`(기본)에서 `before_agent`가 메인 agent 턴 시작마다 초기화합니다. 차단은 `stale_giveup_threshold = 5`에서 현재 턴을 중단하지만 다음 턴으로 이어지지 않습니다. `"session"`은 이전의 턴 간 동작을 복원합니다. ZCode에는 동등한 턴 간 차단이 없으며, 이는 Sherry의 의도적인 보호이지 parity 주장이 아닙니다.

## 이벤트, UI, 원장

scheduled / succeeded / exhausted마다 `model_retry` 이벤트를 하나 기록하고 세션 socket으로 보냅니다:

```text
{"event":"model_retry","session_id":…,"content":{
  "status":"scheduled","attempt":2,"budget_attempt":2,
  "max_attempts":11,"retries":1,"extra_attempts":0,
  "fallback_switches":0,"reason":"rate_limit","delay_ms":1000}}
```

스트리밍 중에는 채팅 행이 `attempt/max_attempts`를 표시합니다. 원장은 각 전환을 별도 행으로 저장합니다(이벤트 id에 attempt와 status 포함). 프런트엔드 표시는 메모리에만 있어 기록을 다시 불러오면 사라지지만, 원장은 영구 기록입니다.

## 구현되지 않은 것

- 인증 갱신과 thinking 서명 복구: `models/LLMs`에 제공자 갱신 진입점이 아직 없습니다. 이벤트 필드는 준비되어 있지만 반드시 실패할 가짜 시도는 하지 않습니다.
- off-peak 큐 롤백: ZCode의 게이트웨이 계약이 없습니다.
- `bounded` / `unbounded` / `single` 예산 모드: 현재 이를 필요로 하는 프로덕션 호출자가 없습니다.

## 코드 맵

- `agent/middlewares/llm_retry/core.py`: 루프, 카운터, fallback 상한, `before_agent`.
- `agent/middlewares/llm_retry/retry_events.py`: 이벤트 payload와 WebSocket 전송.
- `config/features/agent_side/llm_retry.py`: `LLM_RETRY` 기본값.
- `runtime/session/state_keys.py`: `LLM_FALLBACK_SWITCHES`, `LLM_STALE_STREAK`.
- `client/app/composables/use-chat-stream.ts`: `retryStatus` 매핑.
- `client/app/pages/home/components/ChatBox.vue`: 행 내 재시도 표시.
- `tests/agent/middlewares/test_llm_retry.py`: 카운트, fallback 상한, 차단 범위.
