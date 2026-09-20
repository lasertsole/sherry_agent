# 🛡️ 적용, 엔드포인트, 프런트엔드

[English](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> [Token Guard](../README.ko.md)의 일부: 128K 하한을 강제하는 네 개의 게이트, 읽기 전용 `GET /model-config` 엔드포인트, 경고만 하고 차단하지 않는 프런트엔드 동작.

---

## 🚧 적용 지점

네 개의 게이트, 하나의 공유 술어. 표가 계약이며, 세부는 뒤따릅니다.

| # | 위치 | 실패 동작 | 차단 여부 |
| :- | :--- | :--------------- | :-------- |
| 1 | `server/__main__.py` 부팅 게이트 | `logger.critical("STARTUP ABORTED: ...")` 후 `SystemExit(1)` | 프로세스 종료, 아무것도 서비스하지 않음 |
| 2 | `agent/core.py::built_agent()` | `TokenGuardError`가 호출자에게 전파(WebSocket 에러 청크) | 해당 턴의 그래프가 빌드되지 않음 |
| 3 | `agent/tools/subagent/spawn/core.py` 스폰 | `TokenGuardError`가 스폰 호출 밖으로 전파 | 자식 에이전트가 결코 구성되지 않음 |
| 4 | `server/service/env.py::write_env_file()` | 파일을 건드리기 전에 `ValueError` | 저장 거부, `.env` 불변 |

### 1. 서버 부팅 게이트

`server/__main__.py`(게이트는 72행에서 시작)는 어떤 agent-core 작업보다 먼저 빠르게 실패하므로, 잘못 구성된 `.env`가 불구가 된 에이전트를 서비스하는 일은 결코 없습니다:

```python
from config.features import LLM_CLIENT_DEFAULTS, assert_max_token_valid

_main_raw = os.getenv("MAIN_LLM_MAX_TOKEN", "").strip()
_aux_raw = os.getenv("AUXILIARY_LLM_MAX_TOKEN", "").strip()

# an unset AUXILIARY_LLM_MAX_TOKEN falls back to
# LLM_CLIENT_DEFAULTS["aux_remote_max_tokens"] = 121072 (< 128K), so leaving
# it unset blocks startup too. Local mode (AUXILIARY_LLM_MODEL_LOCAL=true)
# is NOT exempt.
_main_val = int(_main_raw) if _main_raw else None
_aux_val = int(_aux_raw) if _aux_raw else LLM_CLIENT_DEFAULTS["aux_remote_max_tokens"]

try:
    assert_max_token_valid("MAIN_LLM_MAX_TOKEN", _main_val)
    assert_max_token_valid("AUXILIARY_LLM_MAX_TOKEN", _aux_val)
except Exception as e:
    logger.critical("STARTUP ABORTED: {}", e)
    raise SystemExit(1) from e
```

잘못된 값은 정확히 이 줄을 만들어내고 프로세스는 시작하지 않습니다:

```text
CRITICAL  STARTUP ABORTED: MAIN_LLM_MAX_TOKEN = 100000 is below the minimum 131072 (128K); agent startup is blocked
```

### 2. 그래프 빌드 게이트

`agent/core.py`(게이트는 117행)는 런타임 두 번째 방어선입니다. 서버가 유효한 `.env`로 부팅했더라도, 이후 편집으로 어느 한 값이 128K 미만으로 떨어지면 그래프 빌드를 거부합니다:

```python
_main_raw = os.getenv("MAIN_LLM_MAX_TOKEN", "").strip()
_aux_raw = os.getenv("AUXILIARY_LLM_MAX_TOKEN", "").strip()
_main_val = int(_main_raw) if _main_raw else None
_aux_val = int(_aux_raw) if _aux_raw else LLM_CLIENT_DEFAULTS["aux_remote_max_tokens"]
assert_max_token_valid("MAIN_LLM_MAX_TOKEN", _main_val)
assert_max_token_valid("AUXILIARY_LLM_MAX_TOKEN", _aux_val)
```

`TokenGuardError`는 호출자에게 전파되며, `server/service/messages.py`가 이를 WebSocket 에러 청크로 클라이언트에 노출합니다.

### 3. 서브에이전트 스폰 게이트

`agent/tools/subagent/spawn/core.py`(게이트는 762행)는 자식 LLM을 검증합니다. 스폰은 `built_agent()`를 우회하므로 하한을 암묵적으로 상속하지 않기 때문입니다:

```python
# Subagent spawn bypasses built_agent(), so child LLM construction validates
# the same 128K MAX_TOKEN floor here instead of inheriting it implicitly.
...
assert_max_token_valid("MAIN_LLM_MAX_TOKEN", _main_val)
assert_max_token_valid("AUXILIARY_LLM_MAX_TOKEN", _aux_val)
```

### 4. 환경 쓰기 게이트

`server/service/env.py`(`write_env_file()`, 게이트는 154행)는 UI에서 128K 미만 값을 영속화할 수 없게 만듭니다. 비정수 또는 하한 미만 값은 `.env`를 건드리기 전에 `ValueError`를 던지므로, 백업과 쓰기가 모두 깨끗하게 유지됩니다:

```python
if key in TOKEN_KEYS:
    try:
        num = int(value)
    except ValueError:
        raise ValueError(f"{key} must be an integer, got: {value}")
    if num < MIN_REQUIRED_MAX_TOKEN:
        raise ValueError(
            f"{key} must be >= {MIN_REQUIRED_MAX_TOKEN} (128K); got {num}. "
            f"Refusing to save."
        )
```

HTTP 핸들러는 이를 `{"success": False}`로 바꿔 클라이언트에 반환하고, 디스크의 `.env`는 바이트 단위로 동일하게 남습니다.

## 🌐 엔드포인트: GET /model-config

`server/trigger/http/model_config.py`는 프런트엔드가 조회하는 읽기 전용 엔드포인트를 등록합니다. 해석된 두 값, 임계값, 판정을 보고합니다:

```json
{
  "main_max_token": 131072,
  "aux_max_token": 131072,
  "min_required": 131072,
  "valid": true
}
```

`valid`는 세 조건이 모두 성립할 때만 `true`입니다:

```python
"valid": (
    main_val is not None
    and main_val >= MIN_REQUIRED_MAX_TOKEN
    and aux_val >= MIN_REQUIRED_MAX_TOKEN
),
```

| 상황 | `main_max_token` | `aux_max_token` | `valid` |
| :-------- | :--------------- | :-------------- | :------ |
| 둘 다 `131072` | `131072` | `131072` | `true` |
| 메인이 하한 미만 | 예: `65536` | `131072` | `false` |
| 보조 미설정 | `131072` | `121072` (폴백) | `false` |
| 메인 미설정 | `null` | `131072` | `false` |

이 리소스에는 쓰기 엔드포인트가 없습니다(환경 쓰기는 별도의 `/env` 라우트를 통합니다). `GET /model-config`는 상태를 결코 변경하지 않습니다.

## 💻 프런트엔드 동작

클라이언트는 미리 경고하지만 사용자를 결코 차단하지 않습니다. 강제하는 쪽은 항상 백엔드입니다.

### 캐시된 조회

`client/app/composables/model-config.ts`가 캐시를 소유합니다:

- `fetchModelConfig()`는 캐시 무효화용 `_ts` 쿼리 파라미터를 붙여 `GET /model-config`를 호출하고, 빈 본문을 기본 무효 객체로 대체합니다.
- `getModelConfigCached()`는 한 번만 조회해 모듈 전역에 캐시합니다. 조회 실패 시 기본 무효 객체(`valid: false`)를 캐시하므로, 백엔드의 일시적 문제가 전송 경로를 깨뜨리는 일은 없습니다.
- `invalidateModelConfigCache()`는 캐시를 비웁니다. `ConfigDialog`가 환경 저장 성공 후 이를 호출하므로, 다음 `getModelConfigCached()`는 다시 조회합니다.

```ts
function invalidModelConfig(): ModelConfig {
  return { main_max_token: null, aux_max_token: null, min_required: 131072, valid: false };
}
```

### 전송 시 비차단 경고

`client/app/composables/use-chat-stream.ts`(`handleSend()`, toast는 433행)는 캐시된 설정을 읽고 `valid`가 `false`일 때 6초 경고 toast를 띄웁니다. 전송을 중단하지는 않습니다:

```ts
const cfg = await getModelConfigCached();
if (!cfg.valid) {
  toastWarn(t('chat.tokenGuard.title'), t('chat.tokenGuard.detail'), 6000);
}
```

설정이 유효하거나 조회가 실패하면 toast는 조용합니다. 전송은 그대로 진행되며, 백엔드가 빌드를 거부하면 WebSocket 에러 청크를 반환합니다.

### 환경 탭 배너와 저장 전 검증

`client/app/pages/home/components/ConfigDialog.vue`:

- 환경 탭이 `*_MAX_TOKEN` 키를 하나라도 노출하면(`hasMaxTokenKeys`, 344행) 호박색 배너가 `config.env.maxTokenHint`(211행)를 렌더링합니다.
- 저장 시 `persistEnvChanges()`가 두 토큰 키를 사전 검증합니다(379행부터 389행). 비정수 또는 `131072` 미만 값은 `config.env.maxTokenError`를 설정하고 `false`를 반환하므로, PUT은 결코 전송되지 않습니다:

```ts
const TOKEN_KEYS = ['MAIN_LLM_MAX_TOKEN', 'AUXILIARY_LLM_MAX_TOKEN'];
const MIN_TOKEN = 131072;
for (const [key, value] of Object.entries(changes)) {
  if (TOKEN_KEYS.includes(key)) {
    const num = parseInt(value, 10);
    if (Number.isNaN(num) || num < MIN_TOKEN) {
      envLoadError.value = t('config.env.maxTokenError', { key });
      return false;
    }
  }
}
```

- 쓰기 성공 후(756행) `invalidateModelConfigCache()`가 실행되어 toast 가드가 최신 값을 다시 읽습니다.

### i18n 키

| 키 | 위치 | 용도 |
| :-- | :---- | :------ |
| `chat.tokenGuard.title` | `client/app/i18n/locales/{en,zh,ja,ko}.json` | toast 제목 |
| `chat.tokenGuard.detail` | 같은 로케일 파일 | toast 본문(시스템 설정 → 환경 설정을 가리킴) |
| `config.env.maxTokenHint` | `ConfigDialog.vue`의 `<i18n>` 블록 | 호박색 배너 문구 |
| `config.env.maxTokenError` | `ConfigDialog.vue`의 `<i18n>` 블록 | 저장 전 거부 메시지. `{key}`가 문제 변수명으로 보간됨 |
