# 위협 모델

Sherry 가 무엇을 방어하고, 신뢰 경계가 어디에 있으며, 그리고 똑같이 중요한 것으로——어떤 방어가 OS 가 강제하는 경계이고 어떤 것이 프로세스 내 휴리스틱인지. 여기는 전체 지도이며, OS 격리의 세부 사항은 [샌드박스 문서](../sandbox/README.ko.md)가 담당합니다.

## 신뢰 경계

| # | 경계 | 횡단 지점 | 강제 주체 |
|---|---|---|---|
| 1 | 사용자 → 에이전트 | WebSocket 턴 | 게이트웨이 인증(Origin 허용 목록 + 부팅별 token) |
| 2 | LLM → 도구 | 도구 호출 | HITL 승인 게이트, `agent/middlewares/humanInTheLoop/detection.py` 위험 명령 목록 |
| 3 | 도구 출력 → 모델 컨텍스트 | 도구 결과 | **신뢰 불가 출력 펜스**, Prompt 주입 스캐너, 도구 결과 축출 |
| 4 | 하위 에이전트 → 상위 에이전트 | announce 파이프라인 | 완료 게이트, `SubagentCompletionDrain` |
| 5 | MCP 서버 → 에이전트 프로세스 | 도구 결과 | 신뢰 불가 출력 펜스(`mcp_` 접두 규칙; MCP 도구는 아직 없음) |
| 6 | HTTP 클라이언트 → 게이트웨이 | HTTP 경로 | 게이트웨이 인증 미들웨어, 쿼리 파라미터 타입 변환 |
| 7 | 파일 시스템 → 에이전트 | 파일 도구 | `PathGuard`, `O_NOFOLLOW`, 가상 경로 해석 |
| 8 | 샌드박스 자식 프로세스 → 호스트 프로세스 | 자식 프로세스 생성 | `scrub_env`, `bwrap`/`seatbelt` 격리 |
| 9 | 메모리 / 스킬 파일 → 시스템 프롬프트 | 프롬프트 조립 | 설치 시 스킬 스캔 게이트; **쓰기 시 주입 차단은 미구현** |
| 10 | TaskFlow step 결과 → 하위 step | DAG 간선 | 기대→실제 폐루프: schema 게이트, step judge, 증거 원장 |

## 데이터 분류

| 분류 | 예 | 보관 위치 |
|---|---|---|
| 민감 | API 키, token | 환경 변수; 어떤 자식 프로세스보다 먼저 `scrub_env`(`agent/tools/pub_base/env_scrub.py`)가 제거 |
| 사적 | 대화 기록 | MesMemory SQLite(WAL) |
| 내부 | 도구 결과 | 메시지 목록 + 축출 파일 |
| 신뢰 불가 | 웹 페이지 내용, 터미널 출력, MCP 결과 | 도구 메시지——읽을 **데이터**이며, 따를 지시가 아님 |

## 위협 분석

| 위협 | 기존 방어 | 격차 |
|---|---|---|
| 간접 prompt 주입(도구 출력) | **신뢰 불가 출력 펜스**(위조 구분자 무효화), 도구 결과 축출, 보이지 않는 Unicode 스캔 | — |
| 웹/터미널 텍스트 속 주입 지시 | Prompt 주입 스캐너(아래) | 각 호출면이 직접 적용해야 함 |
| 메모리 / 스킬 파일 주입 | 설치 시 스킬 스캔 게이트 | **메모리 쓰기 시 차단 스캔** |
| 로그 / 도구 출력으로의 키 유출 | **마스킹 엔진**: 로그 경로(항상 켜짐)와 유출되기 쉬운 도구 출력, 자식 프로세스는 `scrub_env` | `diagnose=True` traceback 지역 변수는 미포함(아래 참조) |
| URL 자격 증명 유출 | **URL 마스킹**: 스킴 확대, 중첩 퍼센트 디코딩(깊이 8), 쿼리 및 서명 파라미터 | — |
| 경로 탈출 | `PathGuard` + `O_NOFOLLOW` | — |
| Shell 주입 | `agent/middlewares/humanInTheLoop/detection.py` 목록(hardline 12개 + dangerous 59개) | — |
| 샌드박스 탈출 | `bwrap` / `seatbelt` | — |
| 추론 블록의 스트림 유출 | — | **think scrubber** |
| 자식 프로세스 env hijack(`LD_PRELOAD`, `BASH_ENV` 등) | 이름 기반 비밀 변수 제거 | **hijack 변수 차단** |
| 업로드 엔드포인트 내용 위조 | 게이트웨이 인증(Origin + token) | **바이트 서명과 선언 타입 일치 검증** |

## 신뢰 불가 출력 펜스

`agent/security/untrusted_wrapper.py` 가 구현하고
`agent/middlewares/context_eviction/core.py` 가 모든 도구 결과에 적용합니다. 공격자가 제어할 수
있는 도구의 결과는 `<untrusted_tool_result source="…" id="…">` 블록으로 감싸지고, 그 안의 권고문이
이 내용은 지시가 아니라 데이터라고 밝힙니다.

| 성질 | 구현 |
|---|---|
| 범위 | `web_search`, `tavily_search`(API 키를 설정하면 같은 도구가 Tavily 자체 이름으로 출하됨), `message_search`, 그리고 모든 `mcp_` 도구 |
| 구분자 위조 | 페이로드 안의 닫는 태그는 감싸기 전에 `</untrusted-tool-result>` 로 바뀌어 블록을 일찍 닫을 수 없습니다 |
| 순서 | 먼저 evict 후 감싸므로 펜스는 모델이 실제로 읽는 미리보기를 감쌉니다 |
| 원문 | 펜스되는 것은 모델이 보는 모습뿐이며, 내부 영속화가 가진 메시지는 결코 변형되지 않습니다 |
| 스위치 | `config/features/agent_side/untrusted_output.py` 의 `UNTRUSTED_OUTPUT["enabled"]`, 권고문도 같은 곳 |

이것은 **펜스이며 경계가 아닙니다**. 모델은 설득되어 무시할 수 있습니다. 가치는 경계가 모델이 읽는 위치에
명시된다는 점과, 가장 값싼 위조(블록을 닫는 것)가 무효화된다는 점입니다.

## 비밀 마스킹

`agent/security/redact.py` 가 텍스트의 자격 증명을 마스킹하며 두 곳에 적용됩니다. 스위치는 의도적으로 다릅니다:

| 면 | 적용 주체 | 스위치 |
|---|---|---|
| 모든 로그 레코드(콘솔 + 세 개의 로테이션 파일) | `agent/security/redact_formatter.py`(`logs/logger.py` 가 설치하는 loguru patcher) | 항상 켜짐——로그 파일은 세션보다 오래 살고, 읽는 사람은 그 비밀을 본 적이 없습니다 |
| 유출되기 쉬운 도구 출력(`terminal`, `python_repl`, 신뢰 불가 도구군, `mcp_*`) | 펜스와 같은 미들웨어 | `config/features/agent_side/redaction.py` 의 `REDACTION["tool_output_enabled"]` |
| 파일 도구(`read_file`, `patch_file`, `write_file`) | — | 의도적으로 마스킹하지 않습니다: 에이전트가 자기 설정을 편집하는데, 마스킹하면 "읽고 되쓰기"가 손실이 됩니다 |

대상: 벤더 키 접두(`sk-`, `ghp_`, `AKIA`, `xox*`, `AIza`, `hf_` 등), 여러 설정 형식의 비밀 이름 대입(`.env`, INI, YAML, TOML, JSON——`"CURATOR_API_KEY"` 같은 접두 키와 공백 포함 인용 값 포함), `Authorization` / `X-API-Key` 헤더, JWT, PEM 개인키 블록, URL 자격 증명과 쿼리 파라미터.

의지할 수 있는 성질:

* **멱등**——센티널은 어떤 계열과도 일치하지 않으므로 두 번 마스킹해도 결과가 같습니다.
* **스위치는 임포트 시 고정**(`SHERRY_REDACT`): 모델이 설득해 `export REDACT=false` 를 쓰게 해도 진행 중 세션을 해제할 수 없습니다. 로그 경로는 이 스위치의 영향을 받지 않습니다.
* **과잉 마스킹을 택합니다.** 값이 "자격 증명처럼 보여야" 한다고 요구하면 전부 소문자인 비밀번호를 놓치므로, 코드에서 비밀 이름 변수를 자기 자신에 대입하는 형태도 함께 바뀝니다(예시는 규칙 자체의 주석에 있습니다). 다시 쓰기가 실제로 해가 되는 면(문서)은 말뭉치 테스트로 고정했습니다.
* **알려진 한계**: 예외 traceback 은 살아 있는 프레임에서 렌더링되므로, 실패한 프레임의 지역 변수에만 있던 비밀은 error sink 의 `diagnose` 덤프에 나타날 수 있습니다.

## Prompt 주입 스캐너

`agent/security/threat_patterns.py` 는 공격자가 제어할 수 있는 텍스트를 스캔해 히트 ID 를 반환합니다. 세 단계가 있고 각 단계는 앞 단계의 상위 집합입니다:

| Scope | 추가되는 패턴 | 사용처 |
|---|---|---|
| `all` | 고전적 주입(ignore/disregard instructions, 역할 하이재킹, 시스템 프롬프트 추출), 키 유출, 숨겨진 HTML 주석 | 모든 도구 결과 |
| `context` | C2 / promptware 형태(노드 등록, heartbeat, tasking pull, 알려진 프레임워크 이름), 지시 파일 재작성 | 도구 결과와 컨텍스트 파일 |
| `strict` | SSH 백도어, shell rc 영속화, 하드코딩된 provider 비밀, 보이지 않는 Unicode | 메모리 쓰기, 스킬 설치 |

```python
from agent.security.threat_patterns import scan_for_threats, first_threat_message

if findings := scan_for_threats(page_text, scope="context"):
    block_reason = first_threat_message(page_text, scope="context")
```

호출자가 의존하는 성질:

* **절대 예외를 던지지 않고 주어진 내용을 변형하지 않습니다**; 알 수 없는 scope 는 `all` 로 폴백합니다——오타 하나로 도구가 죽어서는 안 되기 때문입니다.
* **스캔은 유계**: 앞쪽 65,536 자만 보고, 모든 패턴의 모든 수량자가 유계입니다——조작된 입력이 스캔 자체를 서비스 거부로 만들 수 없습니다.
* **메시지는 일치한 원문을 되비추지 않습니다**: `first_threat_message` 는 히트 ID 만 돌려주므로 탐지 결과를 로그나 화면에 안전하게 내보낼 수 있습니다.
* 이것은 **휴리스틱이며 경계가 아닙니다**. 유일한 강한 경계는 OS 격리입니다([샌드박스 문서](../sandbox/README.ko.md) 참조); 스캐너는 주입 비용을 높이고 호출자가 행동할 수 있는 신호를 줍니다.

이 저장소 자체 산출물로 실측: 실제 로그/도구 출력 112개 파일, 3.0 MB 텍스트를 0.50초에 스캔(약 6 MB/s), **히트 0건**. 같은 말뭉치에 주입 페이로드를 하나 넣으면 즉시 탐지됩니다——실제 내용에서는 조용하면서 존재 이유가 있는 형태는 잡아낸다는 뜻입니다.

