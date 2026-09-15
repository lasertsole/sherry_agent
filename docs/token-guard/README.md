# 🛡️ 128K MAX_TOKEN Guard

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> How the agent enforces a hard 128K context-window floor on both LLMs: one shared predicate raises at four enforcement points (server boot, graph build, subagent spawn, and `.env` write), the frontend warns before it blocks, and `GET /model-config` exposes the live verdict.

Source of truth: `config/features/agent_side/token_guard.py`, `server/__main__.py`, `agent/core.py`, `agent/tools/subagent/spawn/core.py`, `server/service/env.py`, `server/trigger/http/model_config.py`, `client/app/composables/model-config.ts`, `client/app/composables/use-chat-stream.ts`, `client/app/pages/home/components/ConfigDialog.vue`, plus `.env.example`. Every line number and constant in this document was verified against that code.

## Table of Contents

- [Overview](#-overview)
- [Threshold & Configuration](#-threshold--configuration)
- [Enforcement Points](#-enforcement-points)
- [HTTP API: GET /model-config](#-http-api-get-model-config)
- [Frontend Behavior](#-frontend-behavior)
- [Operating Guide](#-operating-guide)
- [Testing](#-testing)
- [Limitations & Non-goals](#%EF%B8%8F-limitations--non-goals)
- [File Map](#-file-map)

## 🎯 Overview

The guard is one invariant, enforced in four independent places:

> **Both LLMs the agent runs on must reserve a context window of at least 128K tokens (`131_072`).**

- The **main LLM** (`MAIN_LLM_MAX_TOKEN`) drives the summarization trigger and the whole conversation budget.
- The **auxiliary LLM** (`AUXILIARY_LLM_MAX_TOKEN`) is used for compaction, memory work, tool-output summaries, and every non-main subagent role.

The threshold lives in exactly one module, `config/features/agent_side/token_guard.py`:

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

`assert_max_token_valid(key, value)` raises `TokenGuardError` in exactly two cases:

| Input | Result |
| :---- | :----- |
| `value is None` (env var unset or empty) | raise `"{key} is not set; must be >= 131072 (128K)"` |
| `0 <= value < 131072` | raise `"{key} = {value} is below the minimum 131072 (128K); agent startup is blocked"` |
| `value >= 131072` | pass (returns `None`) |

There is no upper bound and no way to lower the floor. `TokenGuardError` subclasses `RuntimeError`, and each caller decides how to surface it: a hard exit at boot, a WebSocket error chunk mid-run, or a `ValueError` on write.

`config/features/__init__.py` re-exports the three symbols, so every layer imports them from one place:

```python
from config.features import (
    MIN_REQUIRED_MAX_TOKEN,
    TokenGuardError,
    assert_max_token_valid,
)
```

## 📐 Threshold & Configuration

| Env var | Meaning | Required | `.env.example` default |
| :------ | :------ | :------- | :--------------------- |
| `MAIN_LLM_MAX_TOKEN` | Main model context window | `>= 131072` | `131072` (`.env.example` line 8) |
| `AUXILIARY_LLM_MAX_TOKEN` | Auxiliary model context window | `>= 131072` | `131072` (`.env.example` line 59) |

Both keys are also registered in the env-write allow-list `TOKEN_KEYS` (`server/service/env.py:27`):

```python
TOKEN_KEYS = frozenset({"MAIN_LLM_MAX_TOKEN", "AUXILIARY_LLM_MAX_TOKEN"})
```

### Effective-value resolution

All four enforcement points resolve the "effective" value the same way, mirroring the model builders:

```python
_main_val = int(_main_raw) if _main_raw else None
_aux_val = int(_aux_raw) if _aux_raw else LLM_CLIENT_DEFAULTS["aux_remote_max_tokens"]
```

Consequences worth memorizing:

- An **unset `MAIN_LLM_MAX_TOKEN`** resolves to `None` and is always rejected, even on a stock boot.
- An **unset `AUXILIARY_LLM_MAX_TOKEN`** resolves to `LLM_CLIENT_DEFAULTS["aux_remote_max_tokens"] = 121072`, which is below the floor, so it is rejected too. Leaving it out is not a shortcut.
- Local auxiliary mode (`AUXILIARY_LLM_MODEL_LOCAL=true`) does not change this, because no exemption exists (see [Limitations](#%EF%B8%8F-limitations--non-goals)).

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

## 🧭 Operating Guide

### Fix a failing config

1. Open `.env` at the repo root, or use System Config then the Environment tab in the UI.
2. Set both keys to `>= 131072`:

```bash
MAIN_LLM_MAX_TOKEN = 131072
AUXILIARY_LLM_MAX_TOKEN = 131072
```

3. Restart the backend so the boot gate re-runs. The shipped `.env.example` already has both at `131072`.

> If your local `.env` still holds `100000`, the server refuses to start with `STARTUP ABORTED: MAIN_LLM_MAX_TOKEN = 100000 is below the minimum 131072 (128K); agent startup is blocked`. Raise it to `>= 131072` before launching.

### Symptom, cause, fix

| Symptom | Cause | Fix |
| :------ | :---- | :-- |
| Server exits immediately; log shows `STARTUP ABORTED: ...` | A key is unset or below `131072` | Set both keys to `>= 131072`, then restart |
| Log shows `AUXILIARY_LLM_MAX_TOKEN = 121072 ...` | Aux key unset, so it fell back to `121072` | Set `AUXILIARY_LLM_MAX_TOKEN` explicitly |
| WebSocket error chunk instead of a reply | A mid-run env edit dropped a value below the floor | Fix `.env`, then retry the turn |
| Save in the env tab shows the maxToken error | Value is non-integer or `< 131072` | Enter an integer `>= 131072` |
| Send shows the "Model Config Below Minimum" toast | `GET /model-config` reports `valid: false` | Fix `.env`; the cache refreshes after a successful save |

## 🧪 Testing

| Suite | Marker | Covers |
| :---- | :----- | :----- |
| `tests/config/test_token_guard.py` | `unit` | Threshold is `131_072`; `None` and below-floor raise; the error names the key; at or above floor passes |
| `tests/agent/core/test_built_agent_guard.py` | `unit` | `built_agent()` rejects main-below, main-unset, and aux-unset-fallback; builds with a valid env |
| `tests/server/service/test_env_guard.py` | `unit` | `write_env_file()` rejects below-floor and non-integer without writing; a valid value and non-token keys pass |
| `tests/server/trigger/test_model_config.py` | `integration` | `GET /model-config` shape and `valid` for both-at-minimum, main-below, aux-unset, main-unset |
| `client/app/composables/__tests__/modelConfig.test.ts` | Vitest | fetch cache-busting, fetch-once cache, refetch after invalidate, default-invalid fallback |
| `client/app/composables/__tests__/use-chat-stream.token.test.ts` | Vitest | `handleSend` warns only when invalid; silent when valid and on fetch failure |
| `client/app/pages/home/components/__tests__/ConfigDialog.token.test.ts` | Vitest | Banner and pre-save rejection; a valid value persists and invalidates the cache |

```bash
# Python (unit + integration)
uv run pytest tests/config/test_token_guard.py tests/agent/core/test_built_agent_guard.py \
  tests/server/service/test_env_guard.py tests/server/trigger/test_model_config.py -q

# Frontend
cd client && pnpm test:unit -- modelConfig use-chat-stream.token ConfigDialog.token
```

## ⚠️ Limitations & Non-goals

- **No exemption switch.** Neither key can opt out of the floor. There is no env flag, no per-role override, and no bypass.
- **Local auxiliary mode is not exempt.** `AUXILIARY_LLM_MODEL_LOCAL=true` still must satisfy `>= 131072`. The local model path is hard-coded to `n_ctx=40960`, so a stock local setup is expected to be rejected until that window grows.
- **Unset aux is a trap, not a shortcut.** With no `AUXILIARY_LLM_MAX_TOKEN`, the resolved fallback is `LLM_CLIENT_DEFAULTS["aux_remote_max_tokens"] = 121072`, below the floor, so startup blocks anyway.
- **The floor is fixed at 128K.** There is no per-provider threshold, no upper cap, and no runtime hot-reload. A mid-run edit only takes effect on the next graph build (enforced by gate 2), never on an in-flight turn.
- **The frontend toast is advisory.** It does not prevent sending; enforcement is entirely server-side.
- **Unrelated to other guards.** This is independent of `ToolGuardrails` and CJK token estimation. It does not touch compression thresholds or `summarization` tuning. It fixes the minimum context window, not how that window is spent.

## 📁 File Map

| Layer | File | Role |
| :---- | :--- | :--- |
| Threshold | `config/features/agent_side/token_guard.py` | `MIN_REQUIRED_MAX_TOKEN`, `TokenGuardError`, `assert_max_token_valid` |
| Export | `config/features/__init__.py` | Re-exports the three symbols |
| Boot gate | `server/__main__.py:72` | Critical log then `SystemExit(1)` |
| Build gate | `agent/core.py:117` | `built_agent()` validates before building |
| Spawn gate | `agent/tools/subagent/spawn/core.py:762` | Child LLM construction validates |
| Write gate | `server/service/env.py:154` | `write_env_file()` rejects before writing |
| API | `server/trigger/http/model_config.py` | `GET /model-config` |
| Frontend cache | `client/app/composables/model-config.ts` | `fetchModelConfig`, `getModelConfigCached`, `invalidateModelConfigCache` |
| Frontend warn | `client/app/composables/use-chat-stream.ts:433` | Non-blocking toast on send |
| Frontend dialog | `client/app/pages/home/components/ConfigDialog.vue` | Banner, pre-save validation, cache invalidation |
| Defaults | `.env.example:8,59` | Both keys ship at `131072` |
