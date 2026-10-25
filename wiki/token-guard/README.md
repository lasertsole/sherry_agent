# 🛡️ 200K MAX_TOKEN Guard

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> How the agent enforces a hard 200K context-window floor on both LLMs: one shared predicate raises at four enforcement points (server boot, graph build, subagent spawn, and the `.models` write), the frontend warns before it blocks, and `GET /model-config` exposes the live verdict.

Source of truth: `config/features/agent_side/token_guard.py`, `server/__main__.py`, `agent/core.py`, `agent/tools/subagent/spawn/core.py`, `server/service/env.py`, `server/trigger/http/model_config.py`, `client/app/composables/model-config.ts`, `client/app/composables/use-chat-stream.ts`, `client/app/pages/home/components/ConfigPanel.vue`, `server/service/models_config.py`, plus `.models`. Every line number and constant in this document was verified against that code.

## Table of Contents

- [Overview](#-overview)
- [Threshold & Configuration](#-threshold--configuration)
- [Enforcement, API & Frontend](enforcement/README.md)
  - [🚧 Enforcement Points](enforcement/README.md#-enforcement-points)
  - [🌐 HTTP API: GET /model-config](enforcement/README.md#-http-api-get-model-config)
  - [💻 Frontend Behavior](enforcement/README.md#-frontend-behavior)
- [Operating Guide](#-operating-guide)
- [Testing](#-testing)
- [Limitations & Non-goals](#%EF%B8%8F-limitations--non-goals)
- [File Map](#-file-map)

## 🎯 Overview

The guard is one invariant, enforced in four independent places:

> **Both LLMs the agent runs on must reserve a context window of at least 200K tokens (`200_000`).**

- The **main LLM** (`MAIN_LLM_MAX_TOKEN`) drives the summarization trigger and the whole conversation budget.
- The **auxiliary LLM** (`AUXILIARY_LLM_MAX_TOKEN`) is the fallback for compaction, memory work, tool-output summaries, and every non-main subagent role. Two optional families can take over part of that work: `COMPRESSION_LLM_*` (the summarization middleware's own model) and `TASKFLOW_LLM_*` (plan / replan / step judge / goal gate). Both follow the auxiliary model when unconfigured.

The threshold lives in exactly one module, `config/features/agent_side/token_guard.py`:

```python
MIN_REQUIRED_MAX_TOKEN: int = 200_000


class TokenGuardError(RuntimeError):
    """Raised when a MAX_TOKEN env value is below the minimum."""


def assert_max_token_valid(key: str, value: int | None) -> None:
    """Raise TokenGuardError if value is None or < MIN_REQUIRED_MAX_TOKEN."""
    if value is None:
        raise TokenGuardError(f"{key} is not set; must be >= {MIN_REQUIRED_MAX_TOKEN} (200K)")
    if value < MIN_REQUIRED_MAX_TOKEN:
        raise TokenGuardError(
            f"{key} = {value} is below the minimum {MIN_REQUIRED_MAX_TOKEN} (200K); "
            f"agent startup is blocked"
        )
```

`assert_max_token_valid(key, value)` raises `TokenGuardError` in exactly two cases:

| Input | Result |
| :---- | :----- |
| `value is None` (env var unset or empty) | raise `"{key} is not set; must be >= 200000 (200K)"` |
| `0 <= value < 200000` | raise `"{key} = {value} is below the minimum 200000 (200K); agent startup is blocked"` |
| `value >= 200000` | pass (returns `None`) |

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

| Env var | Meaning | Required | `.models` default |
| :------ | :------ | :------- | :--------------------- |
| `MAIN_LLM_MAX_TOKEN` | Main model context window | `>= 200000` | `200000` |
| `AUXILIARY_LLM_MAX_TOKEN` | Auxiliary model context window | `>= 200000` | `200000` |
| `TASKFLOW_LLM_MAX_TOKEN` | TaskFlow decision model window (optional) | unset = follows auxiliary; set `>= 200000` | `200000` |
| `COMPRESSION_LLM_MAX_TOKEN` | Compression model window (optional) | unset = follows auxiliary; set `>= 200000` | `200000` |

Both keys are also registered in the config-write allow-list `TOKEN_KEYS` (`server/service/models_config.py`; `.env` itself no longer accepts the model keys):

```python
TOKEN_KEYS = frozenset({
    "MAIN_LLM_MAX_TOKEN",
    "AUXILIARY_LLM_MAX_TOKEN",
    "TASKFLOW_LLM_MAX_TOKEN",
    "COMPRESSION_LLM_MAX_TOKEN",
})
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
- The two **optional** keys are validated only when set: an unset family follows the auxiliary model (whose own check already ran). A set value below 200K is refused by the save path and aborts the next boot.
- Local auxiliary mode (`AUXILIARY_LLM_MODEL_LOCAL=true`) does not change this, because no exemption exists (see [Limitations](#%EF%B8%8F-limitations--non-goals)).

## 🧭 Operating Guide

### Fix a failing config

1. Open `.models` at the repo root, or use System Config then the 模型配置 (Model Config) tab in the UI.
2. Set both keys to `>= 200000`:

```bash
MAIN_LLM_MAX_TOKEN = 200000
AUXILIARY_LLM_MAX_TOKEN = 200000
```

3. Restart the backend so the boot gate re-runs. The shipped `.models.example` already has both at `200000`.

An existing `.models` that still carries the previous `131072` default is migrated to `200000` when the file is loaded; any other below-floor value is left for the boot gate to reject.

> If your local `.models` still holds `100000`, the server refuses to start with `STARTUP ABORTED: MAIN_LLM_MAX_TOKEN = 100000 is below the minimum 200000 (200K); agent startup is blocked`. Raise it to `>= 200000` before launching.

### Symptom, cause, fix

| Symptom | Cause | Fix |
| :------ | :---- | :-- |
| Server exits immediately; log shows `STARTUP ABORTED: ...` | A key is unset or below `200000` | Set both keys to `>= 200000`, then restart |
| Log shows `AUXILIARY_LLM_MAX_TOKEN = 121072 ...` | Aux key unset, so it fell back to `121072` | Set `AUXILIARY_LLM_MAX_TOKEN` explicitly |
| WebSocket error chunk instead of a reply | A mid-run env edit dropped a value below the floor | Fix `.models`, then retry the turn |
| Save in the env tab shows the maxToken error | Value is non-integer or `< 200000` | Enter an integer `>= 200000` |
| Send shows the "Model Config Below Minimum" toast | `GET /model-config` reports `valid: false` | Fix `.models`; the cache refreshes after a successful save |

## 🧪 Testing

| Suite | Marker | Covers |
| :---- | :----- | :----- |
| `tests/config/test_token_guard.py` | `unit` | Threshold is `200_000`; `None` and below-floor raise; the error names the key; at or above floor passes |
| `tests/agent/core/test_built_agent_guard.py` | `unit` | `built_agent()` rejects main-below, main-unset, and aux-unset-fallback; builds with a valid env |
| `tests/server/service/test_env_guard.py` | `unit` | `write_env_file()` rejects below-floor and non-integer without writing; a valid value and non-token keys pass |
| `tests/server/trigger/test_model_config.py` | `integration` | `GET /model-config` shape and `valid` for both-at-minimum, main-below, aux-unset, main-unset |
| `client/app/composables/__tests__/modelConfig.test.ts` | Vitest | fetch cache-busting, fetch-once cache, refetch after invalidate, default-invalid fallback |
| `client/app/composables/__tests__/use-chat-stream.token.test.ts` | Vitest | `handleSend` warns only when invalid; silent when valid and on fetch failure |
| `client/app/pages/home/components/__tests__/ConfigPanel.token.test.ts` | Vitest | Banner and pre-save rejection; a valid value persists and invalidates the cache |

```bash
# Python (unit + integration)
uv run pytest tests/config/test_token_guard.py tests/agent/core/test_built_agent_guard.py \
  tests/server/service/test_env_guard.py tests/server/trigger/test_model_config.py -q

# Frontend
cd client && pnpm test:unit -- modelConfig use-chat-stream.token ConfigPanel.token
```

## ⚠️ Limitations & Non-goals

- **No exemption switch.** Neither key can opt out of the floor. There is no env flag, no per-role override, and no bypass.
- **Local auxiliary mode is not exempt.** `AUXILIARY_LLM_MODEL_LOCAL=true` still must satisfy `>= 200000`. The local model path is hard-coded to `n_ctx=40960`, so a stock local setup is expected to be rejected until that window grows.
- **Unset aux is a trap, not a shortcut.** With no `AUXILIARY_LLM_MAX_TOKEN`, the resolved fallback is `LLM_CLIENT_DEFAULTS["aux_remote_max_tokens"] = 121072`, below the floor, so startup blocks anyway.
- **The floor is fixed at 200K.** There is no per-provider threshold, no upper cap, and no runtime hot-reload. A mid-run edit only takes effect on the next graph build (enforced by gate 2), never on an in-flight turn.
- **The frontend toast is advisory.** It does not prevent sending; enforcement is entirely server-side.
- **Unrelated to other guards.** This is independent of `ToolGuardrails` and CJK token estimation. It does not touch compression thresholds or `summarization` tuning. It fixes the minimum context window, not how that window is spent.

## 📁 File Map

| Layer | File | Role |
| :---- | :--- | :--- |
| Threshold | `config/features/agent_side/token_guard.py` | `MIN_REQUIRED_MAX_TOKEN`, `TokenGuardError`, `assert_max_token_valid` |
| Export | `config/features/__init__.py` | Re-exports the three symbols |
| Boot gate | `server/__main__.py:95-100` | Critical log then `SystemExit(1)` |
| Build gate | `agent/core.py:124` | `built_agent()` validates before building |
| Spawn gate | `agent/tools/subagent/spawn/core.py:877` | Child LLM construction validates |
| Write gate | `server/service/models_config.py` | `write_models_config()` rejects before writing |
| API | `server/trigger/http/model_config.py` | `GET /model-config` |
| Frontend cache | `client/app/composables/model-config.ts` | `fetchModelConfig`, `getModelConfigCached`, `invalidateModelConfigCache` |
| Frontend warn | `client/app/composables/use-chat-stream.ts:435` | Non-blocking toast on send |
| Frontend panel | `client/app/pages/home/components/ConfigPanel.vue` | Banner, pre-save validation, cache invalidation |
| Defaults | `.models.example` | Both keys ship at `200000` |
