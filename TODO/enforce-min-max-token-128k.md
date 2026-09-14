# TODO: 强制 main_agent 和 auxiliary_agent 的 MAX_TOKEN >= 128K

> 状态: **待执行**
> 创建时间: 2026-09-14
> 关联文件: `.env` · `agent/core.py` · `server/__main__.py` · `server/service/env.py` · `models/LLMs/main_llm.py` · `models/LLMs/auxiliary_llm/core.py` · `client/app/pages/home/components/ConfigDialog.vue` · `client/app/composables/use-chat-stream.ts`

---

## 目标

`MAIN_LLM_MAX_TOKEN` 和 `AUXILIARY_LLM_MAX_TOKEN` 都必须 >= 131,072 (128K tokens)。不满足时:

1. **后端**: 拒绝启动服务器、拒绝构建 agent graph、拒绝写入 .env
2. **前端**: ConfigDialog 环境配置 tab 阻止保存不达标的值; 每次发消息时如果配置不达标则弹 toast 警告
3. **本地模式** (`AUXILIARY_LLM_MODEL_LOCAL=true`) 也强制要求, 不豁免

---

## 阈值定义

```
128K = 131,072 tokens
常量名: MIN_REQUIRED_MAX_TOKEN
```

---

## Part 1: 后端 — 常量与验证函数

### 新建 `config/features/agent_side/token_guard.py`

```python
"""MIN_MAX_TOKEN guard: enforces both agents' context window >= 128K."""

MIN_REQUIRED_MAX_TOKEN: int = 131_072


class TokenGuardError(RuntimeError):
    """Raised when a MAX_TOKEN env value is below the minimum."""


def assert_max_token_valid(key: str, value: int | None) -> None:
    """Raise TokenGuardError if value is None or < MIN_REQUIRED_MAX_TOKEN.

    Args:
        key: The env var name (e.g. 'MAIN_LLM_MAX_TOKEN'), used in the error message.
        value: The parsed integer value, or None when unset.
    """
    if value is None:
        raise TokenGuardError(
            f"{key} is not set; must be >= {MIN_REQUIRED_MAX_TOKEN} (128K)"
        )
    if value < MIN_REQUIRED_MAX_TOKEN:
        raise TokenGuardError(
            f"{key} = {value} is below the minimum {MIN_REQUIRED_MAX_TOKEN} (128K); "
            f"agent startup is blocked"
        )
```

### 编辑 `config/features/agent_side/__init__.py`

在 re-export 列表中添加:

```python
from .token_guard import (
    MIN_REQUIRED_MAX_TOKEN as MIN_REQUIRED_MAX_TOKEN,
    TokenGuardError as TokenGuardError,
    assert_max_token_valid as assert_max_token_valid,
)
```

### 编辑 `config/features/__init__.py`

在 aggregator 中添加 re-export (跟随现有模式)。

---

## Part 2: 后端 — 服务器启动闸门

### 编辑 `server/__main__.py`

位置: `load_dotenv(ENV_PATH, override=True)` (第 26 行) 之后, `if __name__ == "__main__":` 块内, `init_agent_core()` 之前。

```python
from config.features import (
    LLM_CLIENT_DEFAULTS,
    assert_max_token_valid,
)

_main_raw = os.getenv("MAIN_LLM_MAX_TOKEN", "").strip()
_aux_raw = os.getenv("AUXILIARY_LLM_MAX_TOKEN", "").strip()

# Resolve effective values (mirrors main_llm.py / auxiliary_llm/core.py logic)
_main_val = int(_main_raw) if _main_raw else None
_aux_val = int(_aux_raw) if _aux_raw else LLM_CLIENT_DEFAULTS["aux_remote_max_tokens"]

try:
    assert_max_token_valid("MAIN_LLM_MAX_TOKEN", _main_val)
    assert_max_token_valid("AUXILIARY_LLM_MAX_TOKEN", _aux_val)
except Exception as e:
    logger.critical(f"STARTUP ABORTED: {e}")
    raise SystemExit(1)
```

**关键点:**

- `AUXILIARY_LLM_MAX_TOKEN` 为空时 fallback 到 `LLM_CLIENT_DEFAULTS["aux_remote_max_tokens"]` = 121,072 < 131,072, 所以不设也会被拦住
- 本地模式 (`AUXILIARY_LLM_MODEL_LOCAL=true`) 同样检查 `AUXILIARY_LLM_MAX_TOKEN` env var — 如果未设置或 < 128K, 启动被阻止

---

## Part 3: 后端 — Agent 构建闸门

### 编辑 `agent/core.py` -> `built_agent()`

位置: `built_agent()` 函数体开头, `global _agent, _agent_loop` 之后, `build_main_llm()` 之前。

```python
from config.features import LLM_CLIENT_DEFAULTS, assert_max_token_valid

import os as _os

_main_raw = _os.getenv("MAIN_LLM_MAX_TOKEN", "").strip()
_aux_raw = _os.getenv("AUXILIARY_LLM_MAX_TOKEN", "").strip()
_main_val = int(_main_raw) if _main_raw else None
_aux_val = int(_aux_raw) if _aux_raw else LLM_CLIENT_DEFAULTS["aux_remote_max_tokens"]

assert_max_token_valid("MAIN_LLM_MAX_TOKEN", _main_val)
assert_max_token_valid("AUXILIARY_LLM_MAX_TOKEN", _aux_val)
```

**作用:** 运行时第二道防线。即使启动时通过了检查 (.env 被运行中修改), `built_agent()` 仍会拒绝构建。

**调用链影响:**

- `server/service/messages.py:191` -> `built_agent(force_rebuild=True)` — `TokenGuardError` 会传播为 WS error chunk
- `server/service/messages.py:554` -> `built_agent()` — 同上
- `server/service/interrupt_marker.py:123` -> `agent_core.built_agent()` — 同上
- `agent/tools/subagent/spawn/core.py:740-785` — subagent daemon 中调用 `build_main_llm()` / `build_auxiliary_llm()`, 但 subagent spawn 不经过 `built_agent()`, 需额外在 spawn 路径加 guard (见 Part 3b)

### Part 3b: Subagent spawn 路径 (可选, 视需求)

**编辑** `agent/tools/subagent/spawn/core.py`

在 `build_main_llm()` / `build_auxiliary_llm()` 调用之前 (约第 765 行), 加入相同的 env 校验。由于 subagent 使用父 agent 的 LLM 配置, 如果主 agent 已通过校验, subagent 的 env 值相同, 理论上不会失败。但为防御性编程, 建议加上。

---

## Part 4: 后端 — .env 写入校验

### 编辑 `server/service/env.py` -> `write_env_file()`

位置: `by_key = {e.key: e for e in entries}` (第 149 行) 之后, 写入循环之前。

```python
from config.features import MIN_REQUIRED_MAX_TOKEN

_TOKEN_KEYS = frozenset({"MAIN_LLM_MAX_TOKEN", "AUXILIARY_LLM_MAX_TOKEN"})
for key, value in changes.items():
    if key in _TOKEN_KEYS:
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

**调用链:**

- `server/trigger/http/env.py:31-33` -> `write_env_file(changes)` -> `ValueError` -> 返回 `{"success": False, "message": str(e)}`
- 前端 `writeEnvConfig()` 返回 `false` -> ConfigDialog 显示错误

---

## Part 5: 后端 — 新增 GET /model-config 端点

### 新建 `server/trigger/http/model_config.py`

```python
"""Lightweight endpoint exposing current MAX_TOKEN config and validity."""

import os

from config.features import LLM_CLIENT_DEFAULTS, MIN_REQUIRED_MAX_TOKEN
from server.trigger.core import app


@app.get("/model-config")
async def model_config_handler(request) -> dict:
    """Return current MAX_TOKEN values, the minimum threshold, and validity flag.

    Response shape:
        {
            "main_max_token": int | null,
            "aux_max_token": int,
            "min_required": int,
            "valid": bool,
        }
    """
    main_raw = os.getenv("MAIN_LLM_MAX_TOKEN", "").strip()
    aux_raw = os.getenv("AUXILIARY_LLM_MAX_TOKEN", "").strip()
    main_val = int(main_raw) if main_raw else None
    aux_val = int(aux_raw) if aux_raw else LLM_CLIENT_DEFAULTS["aux_remote_max_tokens"]
    return {
        "main_max_token": main_val,
        "aux_max_token": aux_val,
        "min_required": MIN_REQUIRED_MAX_TOKEN,
        "valid": (
            main_val is not None
            and main_val >= MIN_REQUIRED_MAX_TOKEN
            and aux_val >= MIN_REQUIRED_MAX_TOKEN
        ),
    }
```

### 编辑 `server/trigger/http/__init__.py`

在末尾添加:

```python
import server.trigger.http.model_config  # noqa: F401  (side-effect route registration)
```

---

## Part 6: 前端 — modelConfig composable

### 新建 `client/app/composables/modelConfig.ts`

```typescript
/**
 * Lightweight model-config fetcher with module-level cache.
 *
 * Used by:
 *  - use-chat-stream.ts (toast on send when config is invalid)
 *  - ConfigDialog.vue (invalidate cache after env save)
 */

export interface ModelConfig {
  main_max_token: number | null;
  aux_max_token: number | null;
  min_required: number;
  valid: boolean;
}

let cached: ModelConfig | null = null;

/**
 * Fetch the current model config from GET /model-config.
 * Throws on request failure so callers can distinguish network errors.
 */
export async function fetchModelConfig(): Promise<ModelConfig> {
  const res = await fetchApi({
    url: "/model-config",
    opts: { _ts: Date.now() },
    method: "get",
  });
  return (
    (res as unknown as ModelConfig | undefined) ?? {
      main_max_token: null,
      aux_max_token: null,
      min_required: 131072,
      valid: false,
    }
  );
}

/**
 * Return the cached config, fetching once if needed.
 * Silently returns a default-invalid object on fetch failure
 * (so handleSend's toast guard never breaks the send chain).
 */
export async function getModelConfigCached(): Promise<ModelConfig> {
  if (cached) return cached;
  try {
    cached = await fetchModelConfig();
  } catch {
    cached = {
      main_max_token: null,
      aux_max_token: null,
      min_required: 131072,
      valid: false,
    };
  }
  return cached;
}

/**
 * Invalidate the cache. Called after ConfigDialog saves env changes
 * so the next getModelConfigCached() refetches from the backend.
 */
export function invalidateModelConfigCache(): void {
  cached = null;
}
```

---

## Part 7: 前端 — ConfigDialog 校验

### 编辑 `client/app/pages/home/components/ConfigDialog.vue`

#### 7a. Env tab 顶部提示横幅

在 env tab 的 `<template v-else>` (约第 206 行) 内部, `v-for="group in envGroups"` 循环之前:

```html
<div
  v-if="hasMaxTokenKeys"
  class="rounded-lg border border-amber-300 bg-amber-50 dark:border-amber-700 dark:bg-amber-900/20 p-3 mb-2"
>
  <p class="m-0 text-xs text-amber-700 dark:text-amber-300">
    {{ t('config.env.maxTokenHint') }}
  </p>
</div>
```

在 `<script>` 中添加 computed:

```typescript
const hasMaxTokenKeys = computed(() =>
  envGroups.value.some((group) =>
    group.entries.some((e) => e.key.endsWith("_MAX_TOKEN")),
  ),
);
```

#### 7b. persistEnvChanges() 前端预校验

在 `persistEnvChanges()` 函数中, `if (Object.keys(changes).length === 0) return true;` 之后:

```typescript
const TOKEN_KEYS = ["MAIN_LLM_MAX_TOKEN", "AUXILIARY_LLM_MAX_TOKEN"];
const MIN_TOKEN = 131072;
for (const [key, value] of Object.entries(changes)) {
  if (TOKEN_KEYS.includes(key)) {
    const num = parseInt(value, 10);
    if (isNaN(num) || num < MIN_TOKEN) {
      envLoadError.value = t("config.env.maxTokenError", { key });
      return false;
    }
  }
}
```

#### 7c. 保存成功后刷新缓存

在 `handleSave()` 函数中, env 保存成功之后:

```typescript
import { invalidateModelConfigCache } from "~/composables/modelConfig";

// ... after persistEnvChanges() success:
invalidateModelConfigCache();
```

#### 7d. i18n (四语言)

在 `<i18n lang="json">` 的 `config.env` 下添加:

**zh:**

```json
"maxTokenHint": "MAIN_LLM_MAX_TOKEN 和 AUXILIARY_LLM_MAX_TOKEN 必须 >= 131072 (128K)，否则 Agent 将拒绝启动。",
"maxTokenError": "{key} 必须 >= 131072 (128K)，当前值不满足要求，无法保存。"
```

**en:**

```json
"maxTokenHint": "MAIN_LLM_MAX_TOKEN and AUXILIARY_LLM_MAX_TOKEN must be >= 131072 (128K), otherwise the agent will refuse to start.",
"maxTokenError": "{key} must be >= 131072 (128K); current value does not meet the requirement, cannot save."
```

**ja:**

```json
"maxTokenHint": "MAIN_LLM_MAX_TOKEN と AUXILIARY_LLM_MAX_TOKEN は 131072 (128K) 以上である必要があります。そうでない場合、エージェントは起動を拒否します。",
"maxTokenError": "{key} は 131072 (128K) 以上である必要があります。現在の値は要件を満たしていません。保存できません。"
```

**ko:**

```json
"maxTokenHint": "MAIN_LLM_MAX_TOKEN 및 AUXILIARY_LLM_MAX_TOKEN은 131072 (128K) 이상이어야 합니다. 그렇지 않으면 에이전트가 시작을 거부합니다.",
"maxTokenError": "{key}는 131072 (128K) 이상이어야 합니다. 현재 값이 요구사항을 충족하지 않아 저장할 수 없습니다."
```

---

## Part 8: 前端 — 对话发送时 Toast 警告 (仅不达标时)

### 编辑 `client/app/composables/use-chat-stream.ts` -> `handleSend()`

位置: `handleSend` 函数体开头 (第 412 行), `const sid = ...` 之前。

```typescript
import { getModelConfigCached } from "~/composables/modelConfig";
import { toastWarn } from "~/composables/toast";

// 仅在配置不达标时弹 toast (合法配置下静默)
try {
  const cfg = await getModelConfigCached();
  if (!cfg.valid) {
    toastWarn(t("chat.tokenGuard.title"), t("chat.tokenGuard.detail"), 6000);
  }
} catch {
  // 缓存获取失败 (后端未启动等) — 静默, 不阻塞发送
}
```

### i18n

在 `use-chat-stream.ts` 所在页面或全局 locale 文件中添加 (四语言):

**zh:**

```json
"tokenGuard": {
  "title": "模型配置不达标",
  "detail": "MAIN_LLM_MAX_TOKEN 或 AUXILIARY_LLM_MAX_TOKEN 低于 128K (131072)，Agent 可能拒绝运行。请在系统配置 → 环境配置中修正。"
}
```

**en:**

```json
"tokenGuard": {
  "title": "Model Config Below Minimum",
  "detail": "MAIN_LLM_MAX_TOKEN or AUXILIARY_LLM_MAX_TOKEN is below 128K (131072). The agent may refuse to run. Fix in System Config → Environment."
}
```

**ja:**

```json
"tokenGuard": {
  "title": "モデル設定が最小値未満",
  "detail": "MAIN_LLM_MAX_TOKEN または AUXILIARY_LLM_MAX_TOKEN が 128K (131072) を下回っています。エージェントが実行を拒否する可能性があります。システム設定 → 環境設定で修正してください。"
}
```

**ko:**

```json
"tokenGuard": {
  "title": "모델 설정이 최소값 미만",
  "detail": "MAIN_LLM_MAX_TOKEN 또는 AUXILIARY_LLM_MAX_TOKEN이 128K (131072) 미만입니다. 에이전트가 실행을 거부할 수 있습니다. 시스템 설정 → 환경 설정에서 수정하세요."
}
```

**注意:**

- toast **不阻止发送** — 后端 `built_agent()` 会拒绝构建并返回 error chunk, 前端 WS error handler 已有 toast 兜底
- 前端 toast 是提前预警, 让用户在收到后端错误之前就知道问题

---

## Part 9: 测试

### 后端 (Python)

| #   | 测试文件                                     | Marker      | 描述                                                                                              |
| --- | -------------------------------------------- | ----------- | ------------------------------------------------------------------------------------------------- |
| 1   | `tests/config/test_token_guard.py`           | unit        | `assert_max_token_valid` 在 value=None / value<128K 时 raise; value>=128K 时不 raise              |
| 2   | `tests/agent/core/test_built_agent_guard.py` | unit        | `built_agent()` 在 env token <128K 时 raise `TokenGuardError`; monkeypatch env 到合法值后正常构建 |
| 3   | `tests/server/service/test_env_guard.py`     | unit        | `write_env_file()` 在 token key <128K 时 raise `ValueError`; 合法值正常写入                       |
| 4   | `tests/server/trigger/test_model_config.py`  | integration | `GET /model-config` 返回正确 shape + valid flag                                                   |

### 前端 (TypeScript)

| #   | 测试文件                                                                | 描述                                                                          |
| --- | ----------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| 5   | `client/app/composables/__tests__/modelConfig.test.ts`                  | `fetchModelConfig` mock + cache lifecycle (fetch once, invalidate refetch)    |
| 6   | `client/app/pages/home/components/__tests__/ConfigDialog.token.test.ts` | persistEnvChanges 在 token key <128K 时返回 false + 设置 error; >=128K 时正常 |
| 7   | `client/app/composables/__tests__/use-chat-stream.token.test.ts`        | handleSend 在 `valid=false` 时调用 `toastWarn`; `valid=true` 时不调用         |

### 测试注意事项

- Test 2: 需要使用 `tests/agent/conftest.py` 中的 monkeypatch 模式 (`monkeypatch.setattr(agent_core, "main_llm_max_tokens", context_window)`) 来避免 Summarization trigger 的 None 问题
- Test 3: 使用 tmp_path 创建临时 .env 文件, 避免修改项目根的 .env
- Test 7: mock `getModelConfigCached` 返回 `{ valid: false }` / `{ valid: true }`, 验证 `toastWarn` 调用次数

---

## 文件汇总

| 层       | 文件                                                | 操作                      |
| -------- | --------------------------------------------------- | ------------------------- |
| Config   | `config/features/agent_side/token_guard.py`         | **新建**                  |
| Config   | `config/features/agent_side/__init__.py`            | 编辑 (re-export)          |
| Config   | `config/features/__init__.py`                       | 编辑 (re-export)          |
| Backend  | `server/__main__.py`                                | 编辑 (启动闸门)           |
| Backend  | `agent/core.py`                                     | 编辑 (构建闸门)           |
| Backend  | `server/service/env.py`                             | 编辑 (写入校验)           |
| Backend  | `server/trigger/http/model_config.py`               | **新建**                  |
| Backend  | `server/trigger/http/__init__.py`                   | 编辑 (注册路由)           |
| Frontend | `client/app/composables/modelConfig.ts`             | **新建**                  |
| Frontend | `client/app/composables/use-chat-stream.ts`         | 编辑 (toast)              |
| Frontend | `client/app/pages/home/components/ConfigDialog.vue` | 编辑 (横幅 + 校验 + i18n) |
| Tests    | 4 Python + 3 TS files                               | **新建**                  |

---

## 执行顺序

```
1. config/features/agent_side/token_guard.py + re-exports
2. server/service/env.py 写入校验
3. agent/core.py 构建闸门
4. server/__main__.py 启动闸门
5. server/trigger/http/model_config.py + 注册
6. client/app/composables/modelConfig.ts
7. client/app/pages/home/components/ConfigDialog.vue (横幅 + 校验 + i18n)
8. client/app/composables/use-chat-stream.ts (toast + i18n)
9. 全部测试 (lint + typecheck + pytest + vitest)
```

---

## 现有关键代码路径参考

| 路径                         | 文件:行号                                              | 说明                                                                                                                   |
| ---------------------------- | ------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------- |
| MAIN_LLM_MAX_TOKEN 读取      | `models/LLMs/main_llm.py:22-24`                        | `max_tokens = int(os.getenv("MAIN_LLM_MAX_TOKEN"))`                                                                    |
| main_llm profile 设置        | `models/LLMs/main_llm.py:75`                           | `profile: {max_input_tokens: max_tokens}`                                                                              |
| AUXILIARY_LLM_MAX_TOKEN 读取 | `models/LLMs/auxiliary_llm/core.py:72-73`              | `_raw_max = os.getenv(...); _max_tokens = int(_raw_max) if _raw_max else LLM_CLIENT_DEFAULTS["aux_remote_max_tokens"]` |
| aux profile 设置             | `models/LLMs/auxiliary_llm/core.py:82`                 | `profile: {max_input_tokens: _max_tokens}`                                                                             |
| aux 本地模式                 | `models/LLMs/auxiliary_llm/core.py:156`                | `LocalLlamaChatModel(n_ctx=40960, ..., max_tokens=32768)` — 硬编码低于 128K                                            |
| LLM_CLIENT_DEFAULTS          | `config/features/agent_side/llm_client_defaults.py:35` | `aux_remote_max_tokens: 121072` (fallback, < 128K)                                                                     |
| built_agent() 入口           | `agent/core.py:103-198`                                | 构建 agent graph                                                                                                       |
| server 启动                  | `server/__main__.py:40-132`                            | init_agent_core() + start                                                                                              |
| .env 读写                    | `server/service/env.py`                                | read_env_file / write_env_file                                                                                         |
| /env HTTP handler            | `server/trigger/http/env.py`                           | GET + PUT                                                                                                              |
| env composable               | `client/app/composables/env.ts`                        | readEnvConfig / writeEnvConfig                                                                                         |
| ConfigDialog                 | `client/app/pages/home/components/ConfigDialog.vue`    | 4 tabs: character / background / env / sherry                                                                          |
| handleSend                   | `client/app/composables/use-chat-stream.ts:412`        | 发消息入口                                                                                                             |
| toast                        | `client/app/composables/toast.ts`                      | toastWarn / toastError / ...                                                                                           |
| 顶部工具栏菜单               | `client/app/pages/home/index.vue:98-132`               | 九宫格设置菜单                                                                                                         |
| headerTools 配置             | `client/app/pages/home/config.ts:24-88`                | systemConfig event -> ConfigDialog                                                                                     |
| HTTP 路由注册                | `server/trigger/http/__init__.py`                      | 所有 http trigger 在此 import 注册                                                                                     |

---

## 风险与注意事项

1. **aux_remote_max_tokens 默认值**: 当前 `LLM_CLIENT_DEFAULTS["aux_remote_max_tokens"]` = 121,072 < 131,072。这意味着如果用户不设置 `AUXILIARY_LLM_MAX_TOKEN`, agent 将拒绝启动。需要更新 `.env.example` 中的默认值为 131072 或更高, 并在 `.env.example` 中添加注释说明最低要求。

2. **本地模式**: `AUXILIARY_LLM_MODEL_LOCAL=true` 时, `auxiliary_llm/core.py:156` 硬编码 `n_ctx=40960` 和 `max_tokens=32768`, 远低于 128K。强制要求 128K 意味着本地模式实际上将被阻止, 除非同时修改代码让本地模式也从 env 读取 `n_ctx`。这需要额外工作项 (可选):
   - 将 `n_ctx=40960` 改为 `n_ctx=int(os.getenv("AUXILIARY_LLM_MAX_TOKEN", "131072"))`
   - 将 `max_tokens=32768` 改为从 env 读取或设为合理的输出上限

3. **CI 环境**: `tests/agent/conftest.py` 中 `monkeypatch.setattr(agent_core, "main_llm_max_tokens", context_window)` 用于绕过 None 问题。新增 guard 后, 测试中也需要 monkeypatch env vars 到合法值。

4. **已有 .env 用户**: 升级后如果现有 `MAIN_LLM_MAX_TOKEN=65536` (.env.example 默认值), 服务器将拒绝启动。需要在 release notes 中提示用户修改 `.env`。

5. **Subagent spawn 路径**: `agent/tools/subagent/spawn/core.py:765-771` 调用 `build_main_llm()` / `build_auxiliary_llm()` 但不经过 `built_agent()`。如果需要在 subagent 也强制, 需在 spawn 路径额外加 guard。
