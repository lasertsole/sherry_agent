# 🛡️ 128K MAX_TOKEN 가드

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> 에이전트가 두 LLM 모두에 128K 컨텍스트 윈도우 하한을 강제하는 방식: 하나의 공유 술어가 네 개의 적용 지점(서버 부팅, 그래프 빌드, 서브에이전트 스폰, `.env` 쓰기)에서 예외를 던지고, 프런트엔드는 차단하기 전에 경고하며, `GET /model-config`가 현재 판정을 노출합니다.

사실 출처: `config/features/agent_side/token_guard.py`, `server/__main__.py`, `agent/core.py`, `agent/tools/subagent/spawn/core.py`, `server/service/env.py`, `server/trigger/http/model_config.py`, `client/app/composables/model-config.ts`, `client/app/composables/use-chat-stream.ts`, `client/app/pages/home/components/ConfigDialog.vue`, 그리고 `.env.example`. 이 문서의 모든 줄 번호와 상수는 해당 코드와 대조해 검증했습니다.

## 목차

- [개요](#-개요)
- [임계값과 설정](#-임계값과-설정)
- [적용, 엔드포인트, 프런트엔드](enforcement/README.ko.md)
  - [🚧 적용 지점](enforcement/README.ko.md#-적용-지점)
  - [🌐 엔드포인트: GET /model-config](enforcement/README.ko.md#-엔드포인트-get-model-config)
  - [💻 프런트엔드 동작](enforcement/README.ko.md#-프런트엔드-동작)
- [운영 가이드](#-운영-가이드)
- [테스트](#-테스트)
- [한계와 비목표](#%EF%B8%8F-한계와-비목표)
- [파일 맵](#-파일-맵)

## 🎯 개요

이 가드는 하나의 불변식이며, 서로 독립적인 네 곳에서 강제됩니다:

> **에이전트가 실행하는 두 LLM 모두 최소 128K 토큰(`131_072`)의 컨텍스트 윈도우를 확보해야 합니다.**

- **메인 LLM**(`MAIN_LLM_MAX_TOKEN`)은 요약 트리거 임계값과 대화 전체 예산을 좌우합니다.
- **보조 LLM**(`AUXILIARY_LLM_MAX_TOKEN`)은 압축, 메모리 작업, 도구 출력 요약, 그리고 메인이 아닌 모든 서브에이전트 역할에 쓰입니다.

임계값은 단 하나의 모듈 `config/features/agent_side/token_guard.py`에 존재합니다:

```python
MIN_REQUIRED_MAX_TOKEN: int = 131_072


class TokenGuardError(RuntimeError):
    """Raised when a MAX_TOKEN env value is below the minimum."""


def assert_max_token_valid(key: str, value: int | None) -> None:
    """Raise TokenGuardError if value is None or < MIN_REQUIRED_MAX_TOKEN."""
    if value is None:
        raise TokenGuardError(f"{key} is not set; must be >= {MIN_REQUIRED_MAX_TOKEN} (128K)")
    if value < MIN_REQUIRED_MAX_TOKEN:
        raise TokenGuardError(
            f"{key} = {value} is below the minimum {MIN_REQUIRED_MAX_TOKEN} (128K); "
            f"agent startup is blocked"
        )
```

`assert_max_token_valid(key, value)`가 `TokenGuardError`를 던지는 경우는 정확히 두 가지입니다:

| 입력 | 결과 |
| :---- | :----- |
| `value is None` (환경 변수 미설정 또는 빈 값) | `"{key} is not set; must be >= 131072 (128K)"` 발생 |
| `0 <= value < 131072` | `"{key} = {value} is below the minimum 131072 (128K); agent startup is blocked"` 발생 |
| `value >= 131072` | 통과 (`None` 반환) |

상한은 없고, 하한을 낮출 방법도 없습니다. `TokenGuardError`는 `RuntimeError`의 하위 클래스이며, 각 호출자가 노출 방식을 정합니다: 부팅 시 하드 종료, 실행 중 WebSocket 에러 청크, 쓰기 시 `ValueError`.

`config/features/__init__.py`가 이 세 심볼을 재내보내므로, 모든 계층이 같은 위치에서 임포트합니다:

```python
from config.features import (
    MIN_REQUIRED_MAX_TOKEN,
    TokenGuardError,
    assert_max_token_valid,
)
```

## 📐 임계값과 설정

| 환경 변수 | 의미 | 요구 | `.env.example` 기본값 |
| :------ | :------ | :------- | :--------------------- |
| `MAIN_LLM_MAX_TOKEN` | 메인 모델 컨텍스트 윈도우 | `>= 131072` | `131072` (`.env.example` 8행) |
| `AUXILIARY_LLM_MAX_TOKEN` | 보조 모델 컨텍스트 윈도우 | `>= 131072` | `131072` (`.env.example` 59행) |

두 키는 환경 쓰기 허용 목록 `TOKEN_KEYS`에도 등록되어 있습니다(`server/service/env.py:27`):

```python
TOKEN_KEYS = frozenset({"MAIN_LLM_MAX_TOKEN", "AUXILIARY_LLM_MAX_TOKEN"})
```

### 유효값 해석

네 적용 지점 모두 "유효값"을 동일한 방식으로 해석하며, 모델 빌더와 일치시킵니다:

```python
_main_val = int(_main_raw) if _main_raw else None
_aux_val = int(_aux_raw) if _aux_raw else LLM_CLIENT_DEFAULTS["aux_remote_max_tokens"]
```

기억해 둘 결과:

- **`MAIN_LLM_MAX_TOKEN` 미설정**은 `None`으로 해석되어, 갓 배포한 상태에서도 항상 거부됩니다.
- **`AUXILIARY_LLM_MAX_TOKEN` 미설정**은 `LLM_CLIENT_DEFAULTS["aux_remote_max_tokens"] = 121072`로 해석되어 하한 미만이므로 마찬가지로 거부됩니다. 생략은 지름길이 아닙니다.
- 로컬 보조 모드(`AUXILIARY_LLM_MODEL_LOCAL=true`)도 이를 바꾸지 않습니다. 면제가 존재하지 않기 때문입니다([한계](#%EF%B8%8F-한계와-비목표) 참조).

## 🧭 운영 가이드

### 미달 설정 고치기

1. 리포지토리 루트의 `.env`를 열거나, UI의 시스템 설정 → 환경 설정 탭을 사용합니다.
2. 두 키를 모두 `>= 131072`로 설정합니다:

```bash
MAIN_LLM_MAX_TOKEN = 131072
AUXILIARY_LLM_MAX_TOKEN = 131072
```

3. 부팅 게이트를 다시 실행하도록 백엔드를 재시작합니다. 동봉된 `.env.example`은 이미 두 키 모두 `131072`입니다.

> 로컬 `.env`가 아직 `100000`이라면, 서버는 `STARTUP ABORTED: MAIN_LLM_MAX_TOKEN = 100000 is below the minimum 131072 (128K); agent startup is blocked`와 함께 시작을 거부합니다. 실행 전에 `>= 131072`로 올리세요.

### 증상, 원인, 해결

| 증상 | 원인 | 해결 |
| :------ | :---- | :-- |
| 서버가 즉시 종료되고 로그에 `STARTUP ABORTED: ...` | 키가 미설정이거나 `131072` 미만 | 두 키를 `>= 131072`로 설정 후 재시작 |
| 로그에 `AUXILIARY_LLM_MAX_TOKEN = 121072 ...` | 보조 키 미설정으로 `121072`로 폴백 | `AUXILIARY_LLM_MAX_TOKEN`을 명시적으로 설정 |
| 응답 대신 WebSocket 에러 청크 | 실행 중 환경 편집으로 값이 하한 미만이 됨 | `.env`를 고친 뒤 해당 턴을 재시도 |
| 환경 탭 저장 시 maxToken 오류 | 값이 비정수이거나 `< 131072` | `>= 131072` 정수를 입력 |
| 전송 시 "모델 설정이 최소값 미만" toast | `GET /model-config`가 `valid: false` 보고 | `.env`를 고침. 저장 성공 후 캐시가 새로고침됨 |

## 🧪 테스트

| 스위트 | 마커 | 커버 |
| :---- | :----- | :----- |
| `tests/config/test_token_guard.py` | `unit` | 임계값은 `131_072`. `None`과 하한 미만은 예외. 오류가 키 이름을 명시. 하한 이상은 통과 |
| `tests/agent/core/test_built_agent_guard.py` | `unit` | `built_agent()`가 메인 미만, 메인 미설정, 보조 미설정 폴백을 거부. 유효 env로 빌드 |
| `tests/server/service/test_env_guard.py` | `unit` | `write_env_file()`가 **쓰지 않고** 하한 미만과 비정수를 거부. 유효값과 비토큰 키는 통과 |
| `tests/server/trigger/test_model_config.py` | `integration` | `GET /model-config` 형상과 `valid`: 둘 다 최소, 메인 미만, 보조 미설정, 메인 미설정 |
| `client/app/composables/__tests__/modelConfig.test.ts` | Vitest | 조회 캐시 무효화, 한 번만 캐시, invalidate 후 재조회, 기본 무효 폴백 |
| `client/app/composables/__tests__/use-chat-stream.token.test.ts` | Vitest | `handleSend`는 무효일 때만 경고. 유효할 때와 조회 실패 시 조용 |
| `client/app/pages/home/components/__tests__/ConfigDialog.token.test.ts` | Vitest | 배너와 저장 전 거부. 유효값은 영속화하고 캐시를 무효화 |

```bash
# Python (unit + integration)
uv run pytest tests/config/test_token_guard.py tests/agent/core/test_built_agent_guard.py \
  tests/server/service/test_env_guard.py tests/server/trigger/test_model_config.py -q

# Frontend
cd client && pnpm test:unit -- modelConfig use-chat-stream.token ConfigDialog.token
```

## ⚠️ 한계와 비목표

- **면제 스위치가 없습니다.** 어느 키도 하한에서 빠져나갈 수 없습니다. 환경 플래그도, 역할별 오버라이드도, 우회도 없습니다.
- **로컬 보조 모드는 면제되지 않습니다.** `AUXILIARY_LLM_MODEL_LOCAL=true`도 `>= 131072`를 만족해야 합니다. 로컬 모델 경로는 `n_ctx=40960`으로 하드코딩되어 있어, 표준 로컬 구성은 윈도우가 커지기 전까지 *거부될 것으로 예상됩니다*.
- **보조 미설정은 함정이지 지름길이 아닙니다.** `AUXILIARY_LLM_MAX_TOKEN`이 없으면 해석되는 폴백은 `LLM_CLIENT_DEFAULTS["aux_remote_max_tokens"] = 121072`로 하한 미만이라, 어차피 부팅이 차단됩니다.
- **하한은 128K로 고정됩니다.** 프로바이더별 임계값도, 상한도, 런타임 핫 리로드도 없습니다. 실행 중 편집은 다음 그래프 빌드(게이트 2가 강제)에만 적용되고, 진행 중인 턴에는 결코 적용되지 않습니다.
- **프런트엔드 toast는 권고일 뿐입니다.** 전송을 막지 않으며, 강제는 전적으로 서버 측입니다.
- **다른 가드와 무관합니다.** `ToolGuardrails`나 CJK 토큰 추정과 독립적이며, 압축 임계값이나 `summarization` 튜닝을 건드리지 않습니다. 고정하는 것은 컨텍스트 윈도우의 *최소값*이며, 그 윈도우를 어떻게 쓰는지가 아닙니다.

## 📁 파일 맵

| 계층 | 파일 | 역할 |
| :---- | :--- | :--- |
| 임계값 | `config/features/agent_side/token_guard.py` | `MIN_REQUIRED_MAX_TOKEN`, `TokenGuardError`, `assert_max_token_valid` |
| 내보내기 | `config/features/__init__.py` | 세 심볼 재내보내기 |
| 부팅 게이트 | `server/__main__.py:72` | CRITICAL 로그 후 `SystemExit(1)` |
| 빌드 게이트 | `agent/core.py:117` | `built_agent()`가 빌드 전 검증 |
| 스폰 게이트 | `agent/tools/subagent/spawn/core.py:762` | 자식 LLM 구성 시 검증 |
| 쓰기 게이트 | `server/service/env.py:154` | `write_env_file()`가 쓰기 전 거부 |
| API | `server/trigger/http/model_config.py` | `GET /model-config` |
| 프런트엔드 캐시 | `client/app/composables/model-config.ts` | `fetchModelConfig`, `getModelConfigCached`, `invalidateModelConfigCache` |
| 프런트엔드 경고 | `client/app/composables/use-chat-stream.ts:433` | 전송 시 비차단 toast |
| 프런트엔드 다이얼로그 | `client/app/pages/home/components/ConfigDialog.vue` | 배너, 저장 전 검증, 캐시 무효화 |
| 기본값 | `.env.example:8,59` | 두 키 모두 `131072` |
