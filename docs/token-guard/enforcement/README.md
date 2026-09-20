# 🛡️ Enforcement, API & Frontend

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> Part of [Token Guard](../README.md): the four gates that enforce the 128K floor, the read-only `GET /model-config` endpoint, and the advisory frontend behavior.

---

## 🚧 Enforcement Points

Four gates, one shared predicate. The table is the contract; details follow.

| # | Site | Failure behavior | Blocking? |
| :- | :--- | :--------------- | :-------- |
| 1 | `server/__main__.py` boot gate | `logger.critical("STARTUP ABORTED: ...")` then `SystemExit(1)` | Process exits, nothing serves |
| 2 | `agent/core.py::built_agent()` | `TokenGuardError` propagates to the caller (a WebSocket error chunk) | Graph not built for that turn |
| 3 | `agent/tools/subagent/spawn/core.py` spawn | `TokenGuardError` propagates out of the spawn call | Child agent never constructed |
| 4 | `server/service/env.py::write_env_file()` | `ValueError` before the file is touched | Save rejected, `.env` unchanged |

### 1. Server boot gate

`server/__main__.py` (gate starts at line 72) fails fast before any agent-core work, so a misconfigured `.env` can never serve a crippled agent:

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

A bad value produces exactly this line and the process does not start:

```text
CRITICAL  STARTUP ABORTED: MAIN_LLM_MAX_TOKEN = 100000 is below the minimum 131072 (128K); agent startup is blocked
```

### 2. Graph build gate

`agent/core.py` (gate at line 117) is the runtime second line of defense. Even if the server booted with a valid `.env`, a later edit that drops either value below 128K still refuses to build the graph:

```python
_main_raw = os.getenv("MAIN_LLM_MAX_TOKEN", "").strip()
_aux_raw = os.getenv("AUXILIARY_LLM_MAX_TOKEN", "").strip()
_main_val = int(_main_raw) if _main_raw else None
_aux_val = int(_aux_raw) if _aux_raw else LLM_CLIENT_DEFAULTS["aux_remote_max_tokens"]
assert_max_token_valid("MAIN_LLM_MAX_TOKEN", _main_val)
assert_max_token_valid("AUXILIARY_LLM_MAX_TOKEN", _aux_val)
```

`TokenGuardError` propagates to the caller; `server/service/messages.py` surfaces it to the client as a WebSocket error chunk.

### 3. Subagent spawn gate

`agent/tools/subagent/spawn/core.py` (gate at line 762) validates child LLMs because spawn bypasses `built_agent()`, so the floor is not inherited implicitly:

```python
# Subagent spawn bypasses built_agent(), so child LLM construction validates
# the same 128K MAX_TOKEN floor here instead of inheriting it implicitly.
...
assert_max_token_valid("MAIN_LLM_MAX_TOKEN", _main_val)
assert_max_token_valid("AUXILIARY_LLM_MAX_TOKEN", _aux_val)
```

### 4. Env write gate

`server/service/env.py` (`write_env_file()`, gate at line 154) makes it impossible to persist a sub-128K value from the UI. A non-integer or below-floor value raises `ValueError` before the `.env` is touched, so the backup and the write both stay clean:

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

The HTTP handler turns that into `{"success": False}` for the client, and the on-disk `.env` stays byte-identical.

## 🌐 HTTP API: GET /model-config

`server/trigger/http/model_config.py` registers a read-only endpoint the frontend consults. It reports both resolved values, the threshold, and the verdict:

```json
{
  "main_max_token": 131072,
  "aux_max_token": 131072,
  "min_required": 131072,
  "valid": true
}
```

`valid` is `true` only when all three conditions hold:

```python
"valid": (
    main_val is not None
    and main_val >= MIN_REQUIRED_MAX_TOKEN
    and aux_val >= MIN_REQUIRED_MAX_TOKEN
),
```

| Situation | `main_max_token` | `aux_max_token` | `valid` |
| :-------- | :--------------- | :-------------- | :------ |
| Both set to `131072` | `131072` | `131072` | `true` |
| Main below floor | e.g. `65536` | `131072` | `false` |
| Aux unset | `131072` | `121072` (fallback) | `false` |
| Main unset | `null` | `131072` | `false` |

There is no write endpoint for this resource (env writes go through the separate `/env` route); `GET /model-config` never mutates state.

## 💻 Frontend Behavior

The client warns early but never blocks the user. The backend remains the enforcer.

### Cached fetch

`client/app/composables/model-config.ts` owns the cache:

- `fetchModelConfig()` calls `GET /model-config` with a cache-busting `_ts` query param and replaces an empty body with a default-invalid object.
- `getModelConfigCached()` fetches once and caches the result module-wide; on fetch failure it caches a default-invalid object (`valid: false`) so the send path is never broken by a backend hiccup.
- `invalidateModelConfigCache()` clears the cache. `ConfigDialog` calls it after a successful env save, so the next `getModelConfigCached()` refetches.

```ts
function invalidModelConfig(): ModelConfig {
  return { main_max_token: null, aux_max_token: null, min_required: 131072, valid: false };
}
```

### Non-blocking warn on send

`client/app/composables/use-chat-stream.ts` (`handleSend()`, toast at line 433) reads the cached config and shows a 6-second warning toast when `valid` is `false`. It does not abort the send:

```ts
const cfg = await getModelConfigCached();
if (!cfg.valid) {
  toastWarn(t('chat.tokenGuard.title'), t('chat.tokenGuard.detail'), 6000);
}
```

When the config is valid, or when the fetch fails, the toast stays silent. The send still proceeds; if the backend rejects the build, it returns a WebSocket error chunk.

### Env tab banner and pre-save validation

`client/app/pages/home/components/ConfigDialog.vue`:

- When the env tab exposes any `*_MAX_TOKEN` key (`hasMaxTokenKeys`, line 344), an amber banner renders `config.env.maxTokenHint` (line 211).
- On save, `persistEnvChanges()` pre-validates both token keys (lines 379 to 389). A non-integer or sub-`131072` value sets `config.env.maxTokenError` and returns `false`, so the PUT is never sent:

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

- After a successful write (line 756), `invalidateModelConfigCache()` runs so the toast guard re-reads fresh values.

### i18n keys

| Key | Where | Purpose |
| :-- | :---- | :------ |
| `chat.tokenGuard.title` | `client/app/i18n/locales/{en,zh,ja,ko}.json` | Toast title |
| `chat.tokenGuard.detail` | same locale files | Toast body (points to System Config then Environment) |
| `config.env.maxTokenHint` | `ConfigDialog.vue` `<i18n>` block | Amber banner text |
| `config.env.maxTokenError` | `ConfigDialog.vue` `<i18n>` block | Pre-save rejection message; `{key}` interpolates the offending var |
