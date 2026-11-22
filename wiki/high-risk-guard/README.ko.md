# 🛡️ 고위험 작업 보호

[English](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> 되돌릴 수 없는 작업을 어떻게 식별하고, 한 번만 승인하며, 재생 시 거부하고, 하위 에이전트에서 메인 에이전트의 사람 승인으로 버블하는지 설명합니다.

정보 출처: `agent/tools/pub_base/high_risk_guard_store.py`, `agent/tools/pub_base/high_risk_policy.py`, `agent/tools/high_risk_guard.py`, `agent/middlewares/tool_guardrails/core.py`, `agent/middlewares/humanInTheLoop/strategies.py`, `config/features/agent_side/high_risk_guard.py`, `server/trigger/http/high_risk_guard.py`, `client/app/pages/home/components/HighRiskGuardPanel.vue`.

## 🎯 위협 모델

검증 재실행, 도구 재시도, 재디스패치된 TaskFlow 단계는 같은 **되돌릴 수 없는** 작업을 두 번 실행할 수 있습니다. 프로덕션 배포, 결제 청구, 강제 푸시. 소스 편집 재실행은 정상 작업이므로 "비멱등"만으로는 트리거가 될 수 없습니다 — 가드는 **고위험**을 기준으로 합니다.

| 계층 | 답하는 질문 | 메커니즘 |
| :---- | :------ | :-------- |
| L1 메타데이터 | 어떤 도구가 되돌릴 수 없는 결과를 낼 수 있나 | `metadata.high_risk` (MCP는 기본 true) |
| L2 키워드 원장 | 어떤 작업을 사람이 봐야 하나 | 내장 + session/global 키워드, 부분 문자열 스캔 |
| L3 실행된 작업 원장 | 이 정확한 호출이 이미 실행됐나 | `(루트 세션, 도구, args_hash)` 행 + 첫 결과 |
| L4 승인 | 첫 실행을 누가 승인하나 | 메인 HITL 카드, 하위 에이전트의 `[NEEDS_APPROVAL]` 버블 |

각 계층은 독립적입니다. 사용자가 첫 실행을 승인해도 원장은 재생을 거부하고, 키워드 일치는 원장에 기록이 없을 때도 먼저 확인합니다.

## ⚙️ 키워드 원장

하나의 SQLite 테이블(`src/data/high_risk_guard.db`의 `high_risk_keywords`)이 내장 재정의와 사용자 키워드를 보관합니다. 키워드는 **대소문자 무시 부분 문자열**로 일치하며(단어 경계 없음: `password`는 `user_password=…`와 일치), 모든 키워드는 **3자 이상**(유니코드 문자)이어야 합니다 — 2자 항목은 거의 모든 한국어 문장과 일치합니다.

| 필드 | 의미 |
| :---- | :------ |
| `session_id` | 세션 id, `global`, 또는 내장 기본값 |
| `keyword` | 스캔 대상 텍스트 |
| `policy` | `block`(스캔/확인) 또는 `allow`(스캔 안 함) |
| `source` | `builtin`, `user`(패널), `agent`(도구) |
| `executed` | 1 = 이 세션에서 이미 실행됨 |
| `result_summary` | 첫 결과. 재생 거부 시 표시 |

해석 순서는 session → global → 내장 기본값입니다. `allow` 행은 다른 범위가 `block`이어도 키워드를 활성 목록에서 제거합니다. 내장 목록은 자격 증명, 금융 식별자, 파괴적 셸 작업, 파괴적 SQL을 다룹니다(`rm -rf`, `mkfs`, `git push --force`, `DROP TABLE`, `password`, `private_key` 등).

## 📒 실행된 작업 원장

원장에는 **고위험이고 비멱등**인 도구만 들어갑니다: PTC의 `execute_code`와 모든 MCP 도구(기본). `terminal`, `python_repl`, `write_file`, `patch_file`은 들어가지 않습니다 — 셸 명령이나 편집은 정상 작업이며, 특정 명령을 확인하게 만드는 것은 키워드 스캔입니다.

원장 행은 **루트 메인 세션**(하위 에이전트는 `APPROVAL_SCOPE_PARENT` 해석), 도구 이름, 안정적인 인수 다이제스트(정렬된 JSON, 원시 인수는 저장하지 않음)를 키로 합니다. 재실행은 실행 전에 거부되고 첫 `result_summary`가 반환됩니다. 행은 설정된 보존 기간(24시간)이 지나면 만료되고 세션 삭제 시 제거됩니다. 패널의 삭제가 명시적인 "한 번 더 실행"입니다.

**실행된** 키워드도 같은 일회성 대우를 받습니다: 승인된 작업이 성공하면 키워드가 실행됨으로 표시되고, 이후 그 키워드를 포함한 호출은 첫 결과와 함께 거부되며, 패널(또는 `high_risk_guard action=reset_keyword`)이 표시를 지울 때까지 유지됩니다.

### 패널 API의 전송 게이트

`args_preview`에는 민감한 값이 포함될 수 있으므로, 모든 패널 라우트(`GET/POST/PUT/DELETE /sessions/:sid/high-risk-keywords*`, `/high-risk-ops/:id`, `/high-risk-keywords/builtin`)는 **안전한 전송**을 요구합니다(`server/trigger/http/transport_guard.py`).

* **동일 호스트** — loopback 피어 또는 `Host: localhost/127.0.0.1/[::1]`는 통과(데스크톱 webview는 `http://localhost:8080`으로 직접 연결).
* **암호화** — `X-Forwarded-Proto: https` 또는 `wss`는 통과. Fleet Ingress가 자신의 TLS 스킴(`tls_cert`/`tls_key`)에서 이 헤더를 재발급합니다. 클라이언트가 보낸 값은 `SHERRY_INGRESS_TRUST_FORWARDED`가 설정된 경우에만 신뢰하므로, 평문 원격 클라이언트가 `https`를 위조할 수 없습니다.
* **그 외는 축소** — 원격 평문 HTTP/WS 호출에는 **HTTP 200 + 빈 `keywords`/`ops` + `degraded: true` + 한국어 알림**을 반환하고 실제 데이터는 반환하지 않습니다. 쓰기도 동일하게 거부됩니다. 패널은 전송 오류 대신 알림을 표시하며 민감 데이터가 안전하지 않은 경로로 새지 않습니다.

이는 전송 계층 게이트이며 게이트웨이 Origin/token, CSRF, 로그인 게이트를 보완합니다(대체하지 않음).

## 🔁 메인 에이전트와 하위 에이전트

| | 메인 에이전트 | 하위 에이전트 |
| :--- | :--- | :--- |
| 키워드 일치 | `HighRiskKeywordApprovalHandler`가 승인/거부 카드를 띄움 | `ToolGuardrails`가 `[NEEDS_APPROVAL]`을 반환하고 자식은 완료 |
| 원장 일치 | 첫 결과와 함께 거부(카드 없음) | 동일 |
| 사람 승인 후 | 이 세션의 `KEYWORD_APPROVALS`에 승인 + 인수 다이제스트 기록 | 부모가 `approval_context`로 단계를 재개하고 새 자식이 승인을 받음 |
| YOLO | 가역 도구는 카드 생략, PTC/MCP는 여전히 확인 | `full_access` 자식은 키워드 스캔 생략, 원장은 유효 |
| confirm_all | 평소처럼 카드 | 자식은 거부(`[BLOCKED: confirm_all mode]`) |

HITL 카드는 `interrupt()` 전에 세션 하트비트를 동결하고(`HEARTBEAT_SKIP`) 재개 패스가 돌아온 뒤 해제하므로, 승인이 느려도 턴이 "정체"로 죽지 않습니다.

### 버블 경로

1. 자식의 가드레일이 `[NEEDS_APPROVAL: keyword=…, tool=…]`을 반환합니다.
2. 자식이 완료되고, 완료 메시지 빌더가 `[SYSTEM: …]` 안내를 덧붙여 부모에게 `question` 사용법과 재개 방법을 알립니다.
3. 부모가 `question`으로 사용자에게 묻습니다(일반 HITL 카드).
4. 승인 후 부모는 `taskflow_step(action="resume", step_outcome="approved", step_id=…, approval_context={keyword, tool})`를 호출합니다 — 직접 spawn이면 `sessions_spawn(approval_context=…)`입니다.
5. 승인은 **새 자식 자신의** 레지스터(`KEYWORD_APPROVALS`)에 기록되어 그 spawn만 키워드를 통과할 수 있습니다. 다음 무관한 spawn은 다시 묻습니다.
6. 거부 시 `blocked`로 재개하며 의존 단계는 차단된 상태로 남습니다.

`approval_context`를 나르는 것은 TaskFlow 단계뿐입니다: `_OPTIONAL_STEP_FIELDS` → `step_spawn_kwargs` → `dispatch_child` → `SpawnRequest.approval_context`. 승인된 단계의 일반 재시도는 승인을 유지하고(schema 재시도는 같은 작업), 새 단계는 상속하지 않습니다.

## 🧰 도구, API, 패널

`high_risk_guard`(메인과 하위 모두)가 원장과 작업을 관리합니다: `add_keyword`, `list_keywords`, `remove_keyword`, `set_policy`, `reset_keyword`, `register_op`, `list_ops`, `remove_op`. 자식의 쓰기는 루트 세션으로 해석됩니다.

| 엔드포인트 | 용도 |
| :------- | :------ |
| `GET /sessions/:sid/high-risk-keywords` | 키워드 + 원장(패널 일괄 로드) |
| `POST /sessions/:sid/high-risk-keywords` | `{keyword, scope}` 추가 |
| `PUT /sessions/:sid/high-risk-keywords` | `{keyword, policy}` 설정 |
| `DELETE /sessions/:sid/high-risk-keywords/:id` | 행 삭제 |
| `POST /sessions/:sid/high-risk-keywords/reset` | 실행 표시 지우기 |
| `DELETE /sessions/:sid/high-risk-ops/:id` | 원장 행 삭제 |
| `GET /high-risk-keywords/builtin` | 읽기 전용 내장 목록 |

도구 모음의 방패 버튼이 현재 세션의 `HighRiskGuardPanel`을 엽니다: 키워드 추가/삭제, block↔allow 전환, 표시 초기화, 원장 행 삭제(명시적 재실행).

## 🧪 테스트

| 스위트 | 범위 |
| :---- | :----- |
| `tests/agent/tools/pub_base/test_high_risk_guard_store.py` | 검증, 스캔, 범위, 정책, 실행 표시, 원장 왕복, 정리, 보존 |
| `tests/agent/middlewares/test_high_risk_guard.py` | 원장 거부/재생, 키워드 NEEDS_APPROVAL, 승인, YOLO, confirm_all, 루트 범위 |
| `tests/agent/middlewares/humanInTheLoop/test_high_risk_keyword_handler.py` | 핸들러 일치, 실행됨 거부, 승인/거부, 하트비트 동결, 헤드리스 거부 |
| `tests/agent/tools/test_high_risk_guard_tool.py` | 에이전트 도구의 모든 액션 + 자식→루트 범위 |
| `tests/agent/tools/taskflow/test_approval_resume.py` | 승인 후 재디스패치, 재시도 시 승인 유지, 검증 거부 |
| `tests/agent/tools/subagent/test_child_access_inheritance.py` | 접근 모드 상속, 승인 범위의 토큰 루트 해석, 승인 스탬프 |
| `tests/server/trigger/http/test_high_risk_guard_api.py` | 키워드 CRUD, 정책, 초기화, 원장 삭제 |
| `client/app/pages/home/components/__tests__/HighRiskGuardPanel.integration.test.ts` | 패널 렌더, 추가/정책/초기화/삭제, 백엔드 거부 |

## ⚠️ 정직한 한계

- **스캔은 트립와이어이지 파서가 아닙니다.** 무관한 문자열 속 키워드(문서 작성 명령의 `echo "password"`)도 확인하며, 등록 단어가 없는 표현의 작업은 통과합니다. 목록은 운영자/에이전트가 유지합니다.
- **키워드의 실행 표시는 세션 전체입니다.** 계획의 "단계마다 다시 묻기"는 여기서 의도적으로 더 엄격합니다: 처음 승인된 작업이 키워드를 실행됨으로 표시하고, 이후 그 키워드를 포함한 모든 호출은 표시를 지울 때까지 거부됩니다. 재생에는 안전하지만 정당한 두 번째 실행에는 패널 조작이 필요합니다.
- **주석 없는 도구는 저위험으로 취급됩니다.** 명시적 `metadata.high_risk`가 우선하며, 키가 없는 `mcp__` 도구는 고위험이고 나머지 주석 없는 도구는 아닙니다. 위험한 작업을 평범한 이름 뒤에 숨긴 플러그인은 스스로 주석해야 합니다.
- **MCP는 접두사로 원장에 들어가며 기본 비멱등입니다.** 정말 안전한 MCP 읽기는 `high_risk: False` / `idempotent: True`를 선언할 수 있습니다.
- **원장은 인수 미리보기(≤300자)를 저장합니다.** 로컬 `high_risk_guard.db`(추적되는 workspace 아님)에 남지만, 미리보기에는 호출이 포함한 값이 남을 수 있습니다. 패널 API는 동일 호스트 또는 HTTPS/WSS 전송에서만 이를 반환합니다(그 외에는 축소 알림).
- **전송 신뢰는 헤더 기반입니다.** `X-Forwarded-Proto`는 신뢰할 수 있는 프록시에서만 채택됩니다. 다른 곳에서 TLS를 종료하는 배포에서는 ingress에 `SHERRY_INGRESS_TRUST_FORWARDED`를 설정해 실제 스킴을 전달해야 합니다.
- **버블은 부모가 턴을 잡는 것에 의존합니다.** 완료 메시지가 안내를 자동으로 덧붙이지만, 실제 승인은 메인 에이전트가 해야 하는 `question` 호출입니다. 완료 메시지를 읽지 않는 부모는 단계를 blocked로 남깁니다.
- **보존은 지연 실행입니다.** 만료된 행은 원장 접근 시 최대 1시간에 한 번 정리되므로, 24시간을 넘겨 다음 정리까지 남을 수 있습니다.
