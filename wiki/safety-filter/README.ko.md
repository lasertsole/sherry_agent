# 라이브 안전 필터

> 입출력 키워드 규칙, 선택적 LLM 심사, 그리고 "보내기 전에 검사하는" 스트림 절단.

## 🎯 개요

이 기능 이전에는 모델과 시청자 사이에서 텍스트를 거르는 장치가 없었습니다.
필터는 스위치(`sherry.jsonc` 의 `safety_filter.enabled`, 기본 꺼짐)로 세 계층을 켭니다.

1. **입력 키워드 규칙** —— `block` 에 걸리면 프로바이더를 호출하지 않고 거부합니다
   (`agent/middlewares/safety_filter/middlewares.py::InputSafetyFilter`).
2. **LLM 심사(선택)** —— 설정한 심사 모델이 판단합니다. 마스터 스위치, `off` 가 아닌 전략,
   모델 이름 세 가지가 모두 있어야 실행됩니다.
3. **출력 키워드 규칙** —— 완성된 답변은 전체 또는 부분 대체되고
   (`OutputSafetyFilter`), 스트림 답변은
   `agent/middlewares/safety_filter/stream_guard.py` 가 청크마다 세척합니다.

규칙은 사용자가 관리합니다. 앱은 **단어 목록을 포함하지 않습니다**(목록 선정은 법적 판단이며
이 프로젝트가 대신 결정하지 않습니다). 규칙은 워크스페이스 안에서
`safety_filter.keyword_file` 로 지정한 파일에 있습니다.

## ⚙️ 설정

`sherry.jsonc` 를 **실시간으로** 읽습니다(mtime 캐시, 재시작 불필요).

- `safety_filter.enabled` —— 마스터 스위치.
- `safety_filter.llm_moderation_mode` —— `off` / `on_keyword_hit` / `always`.
- `safety_filter.llm_moderation_model` —— 심사 모델 이름. 비어 있으면 전략과 무관하게 LLM 심사는 실행되지 않습니다.
- `safety_filter.keyword_file` —— 워크스페이스 안의 규칙 파일 이름.

설정 화면은 **시스템 설정 → 라이브 안전**(`SafetyFilterPanel.vue`)입니다. 마스터 스위치,
LLM 스위치(모델 이름을 입력하기 전에는 비활성), 전략 선택, 테스트 검사가 있는 규칙 편집기,
최근 필터 기록을 보여 줍니다.

## 🧩 세 계층

- 규칙 형식: `{"rules": [{"pattern": "…", "type": "exact|contains|regex",
  "action": "block|replace|warn", "scope": "input|output|both",
  "replacement": "…"}]}`. 저장 전에 규칙 수, pattern 길이, 대체 텍스트 길이, 정규식 유효성을 검사합니다.
- `block` 은 거부/전체 대체, `replace` 는 일치 부분 대체, `warn` 은 보고만 합니다.
  입력 측에서 사용자의 원문을 고쳐 쓰지 않습니다.
- 판정마다 이벤트를 하나 기록하고(상한이 있는 링 버퍼, 최신 우선) `safety_filter` WS 프레임을
  보냅니다. 클라이언트는 toast 를 표시합니다(block 은 오류 수준).
- LLM 심사는 **실패하면 통과**시킵니다. 타임아웃, 요청 실패, 해석할 수 없는 응답은 턴을 계속
  진행시키고 경고만 남깁니다. 프로바이더 자체의 정렬은 여전히 작동합니다.

## 🚂 스트림 세척

`StreamTextGuard` 는 끝의 `stream_buffer_window` 문자(기본 20)를 붙잡아 두고
이미 검사한 텍스트만 내보냅니다.

- 버퍼 어디서든 `block` 이 걸리면 래치하고 버퍼를 버린 뒤 대체 메시지를 한 번 보냅니다.
- 해제 지점 안에서 끝나는 `replace` 일치는 먼저 대체하고, 해제 지점이 일치를 자르면 해제 지점을 그 시작으로 되돌립니다.
- 창보다 긴 패턴은 끝의 flush 에서만 완성되므로 절단 전에 보이는 글자는 최대 `len(match) - 1` 자입니다.

스트림 답변은 `OutputSafetyFilter` 를 거치지 않습니다. 세척이 프레임 생성 전에 일어나므로
클라이언트와 저장된 행이 일치합니다.

## 🌐 HTTP API

| Method | Path | Body / query |
| :--- | :--- | :--- |
| `GET` | `/safety-filter/rules` | — |
| `PUT` | `/safety-filter/rules` | `{rules: [...]}` |
| `POST` | `/safety-filter/test` | `{text, scope}` |
| `GET` | `/safety-filter/log` | `?session_id=<sid>&limit=50` |

스위치와 심사 모델은 `sherry.jsonc` 키이며 `PUT /sherry-config` 로 기록합니다
(전용 엔드포인트 없음).

## 🧪 테스트

`tests/agent/middlewares/safety_filter/test_rules.py`(검증, 범위, 핫 리로드, 리터럴 대체),
`test_middlewares.py`(프로바이더 호출 없는 입력 차단, warn/replace 통과, LLM 게이팅과 실패 시 통과, 출력 대체),
`test_stream_guard.py`(보류 창, 래치, 전송 전 대체, 끝 검사). 설정 화면은
`client/app/pages/home/components/__tests__/SafetyFilterPanel.integration.test.ts` 가 고정합니다.
