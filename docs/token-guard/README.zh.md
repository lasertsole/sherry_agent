# 🛡️ 128K MAX_TOKEN 强制闸门

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> Agent 如何对两个 LLM 强制 128K 上下文窗口硬下限：一个共享断言在四个执法点抛出（服务启动、图构建、子 Agent 派生、`.env` 写入），前端在被拦截之前先告警，`GET /model-config` 对外暴露实时判定。

事实来源：`config/features/agent_side/token_guard.py`、`server/__main__.py`、`agent/core.py`、`agent/tools/subagent/spawn/core.py`、`server/service/env.py`、`server/trigger/http/model_config.py`、`client/app/composables/model-config.ts`、`client/app/composables/use-chat-stream.ts`、`client/app/pages/home/components/ConfigDialog.vue`，以及 `.env.example`。本文档中的每一个行号与常量都已对照该代码核对。

## 目录

- [概述](#-概述)
- [阈值与配置](#-阈值与配置)
- [执法点](#-执法点)
- [接口：GET /model-config](#-接口get-model-config)
- [前端行为](#-前端行为)
- [运维指南](#-运维指南)
- [测试](#-测试)
- [局限与非目标](#%EF%B8%8F-局限与非目标)
- [文件地图](#-文件地图)

## 🎯 概述

这条闸门只有一条不变量，但在四个彼此独立的位置强制执行：

> **Agent 所运行的两个 LLM 都必须预留至少 128K token（`131_072`）的上下文窗口。**

- **主 LLM**（`MAIN_LLM_MAX_TOKEN`）驱动摘要触发阈值与整段对话的预算。
- **辅助 LLM**（`AUXILIARY_LLM_MAX_TOKEN`）用于压缩、记忆整理、工具输出摘要，以及所有非主 Agent 的子 Agent 角色。

阈值只存在于一个模块 `config/features/agent_side/token_guard.py`：

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

`assert_max_token_valid(key, value)` 只在两种情况下抛出 `TokenGuardError`：

| 输入 | 结果 |
| :---- | :----- |
| `value is None`（环境变量未设或为空） | 抛出 `"{key} is not set; must be >= 131072 (128K)"` |
| `0 <= value < 131072` | 抛出 `"{key} = {value} is below the minimum 131072 (128K); agent startup is blocked"` |
| `value >= 131072` | 通过（返回 `None`） |

没有上限，也没有任何办法把下限调低。`TokenGuardError` 继承自 `RuntimeError`，由各调用点自行决定如何呈现：启动时硬退出、运行中回一个 WebSocket error chunk，或写入时抛 `ValueError`。

`config/features/__init__.py` 重新导出这三个符号，因此每一层都从同一个地方导入：

```python
from config.features import (
    MIN_REQUIRED_MAX_TOKEN,
    TokenGuardError,
    assert_max_token_valid,
)
```

## 📐 阈值与配置

| 环境变量 | 含义 | 要求 | `.env.example` 默认值 |
| :------ | :------ | :------- | :--------------------- |
| `MAIN_LLM_MAX_TOKEN` | 主模型上下文窗口 | `>= 131072` | `131072`（`.env.example` 第 8 行） |
| `AUXILIARY_LLM_MAX_TOKEN` | 辅助模型上下文窗口 | `>= 131072` | `131072`（`.env.example` 第 59 行） |

两个键同时登记在环境写入白名单 `TOKEN_KEYS` 中（`server/service/env.py:27`）：

```python
TOKEN_KEYS = frozenset({"MAIN_LLM_MAX_TOKEN", "AUXILIARY_LLM_MAX_TOKEN"})
```

### 有效值解析

四个执法点用完全相同的方式解析"有效值"，与模型构建器保持一致：

```python
_main_val = int(_main_raw) if _main_raw else None
_aux_val = int(_aux_raw) if _aux_raw else LLM_CLIENT_DEFAULTS["aux_remote_max_tokens"]
```

几个务必记住的推论：

- **`MAIN_LLM_MAX_TOKEN` 未设**时解析为 `None`，会被无条件拒绝，即便是全新部署也一样。
- **`AUXILIARY_LLM_MAX_TOKEN` 未设**时解析为 `LLM_CLIENT_DEFAULTS["aux_remote_max_tokens"] = 121072`，低于下限，同样被拒绝。省略它并不是捷径。
- 本地辅助模式（`AUXILIARY_LLM_MODEL_LOCAL=true`）不会改变这一点，因为不存在豁免（见[局限](#%EF%B8%8F-局限与非目标)）。

## 🚧 执法点

四道闸门，共用一个断言。下表即契约，细节见后文。

| # | 位置 | 失败行为 | 是否阻断 |
| :- | :--- | :--------------- | :-------- |
| 1 | `server/__main__.py` 启动闸门 | `logger.critical("STARTUP ABORTED: ...")` 后 `SystemExit(1)` | 进程退出，不对外服务 |
| 2 | `agent/core.py::built_agent()` | `TokenGuardError` 传播给调用方（WebSocket error chunk） | 该回合不构建图 |
| 3 | `agent/tools/subagent/spawn/core.py` 派生 | `TokenGuardError` 从 spawn 调用向外传播 | 子 Agent 永不构造 |
| 4 | `server/service/env.py::write_env_file()` | 在触碰文件之前抛出 `ValueError` | 保存被拒，`.env` 不变 |

### 1. 服务启动闸门

`server/__main__.py`（闸门从第 72 行开始）在任何 agent-core 工作之前快速失败，因此配置错误的 `.env` 永远不可能服务一个残废的 Agent：

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

一个坏值会产出下面这一行，进程不会启动：

```text
CRITICAL  STARTUP ABORTED: MAIN_LLM_MAX_TOKEN = 100000 is below the minimum 131072 (128K); agent startup is blocked
```

### 2. 图构建闸门

`agent/core.py`（闸门在第 117 行）是运行时的第二道防线。即便服务带着合法 `.env` 启动，之后某次改动把任一值降到 128K 以下，仍然会拒绝构建图：

```python
_main_raw = os.getenv("MAIN_LLM_MAX_TOKEN", "").strip()
_aux_raw = os.getenv("AUXILIARY_LLM_MAX_TOKEN", "").strip()
_main_val = int(_main_raw) if _main_raw else None
_aux_val = int(_aux_raw) if _aux_raw else LLM_CLIENT_DEFAULTS["aux_remote_max_tokens"]
assert_max_token_valid("MAIN_LLM_MAX_TOKEN", _main_val)
assert_max_token_valid("AUXILIARY_LLM_MAX_TOKEN", _aux_val)
```

`TokenGuardError` 会传播给调用方；`server/service/messages.py` 将其作为 WebSocket error chunk 呈现给客户端。

### 3. 子 Agent 派生闸门

`agent/tools/subagent/spawn/core.py`（闸门在第 762 行）会校验子 LLM，因为派生绕过了 `built_agent()`，不能隐式继承那条下限：

```python
# Subagent spawn bypasses built_agent(), so child LLM construction validates
# the same 128K MAX_TOKEN floor here instead of inheriting it implicitly.
...
assert_max_token_valid("MAIN_LLM_MAX_TOKEN", _main_val)
assert_max_token_valid("AUXILIARY_LLM_MAX_TOKEN", _aux_val)
```

### 4. 环境写入闸门

`server/service/env.py`（`write_env_file()`，闸门在第 154 行）让 UI 无法持久化低于 128K 的值。非整数或低于下限的值会在触碰 `.env` 之前抛出 `ValueError`，因此备份与写入都保持干净：

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

HTTP 处理器会把它变成 `{"success": False}` 返回客户端，磁盘上的 `.env` 保持逐字节不变。

## 🌐 接口：GET /model-config

`server/trigger/http/model_config.py` 注册了一个只读端点供前端查询。它报告两个解析后的值、阈值，以及判定：

```json
{
  "main_max_token": 131072,
  "aux_max_token": 131072,
  "min_required": 131072,
  "valid": true
}
```

只有三个条件全部成立时 `valid` 才为 `true`：

```python
"valid": (
    main_val is not None
    and main_val >= MIN_REQUIRED_MAX_TOKEN
    and aux_val >= MIN_REQUIRED_MAX_TOKEN
),
```

| 情况 | `main_max_token` | `aux_max_token` | `valid` |
| :-------- | :--------------- | :-------------- | :------ |
| 两者都设为 `131072` | `131072` | `131072` | `true` |
| 主值低于下限 | 例如 `65536` | `131072` | `false` |
| 辅助值未设 | `131072` | `121072`（回退） | `false` |
| 主值未设 | `null` | `131072` | `false` |

该资源没有写端点（环境写入走单独的 `/env` 路由）；`GET /model-config` 从不修改状态。

## 💻 前端行为

客户端只提前告警，绝不阻断用户。真正的执法方始终是后端。

### 带缓存拉取

`client/app/composables/model-config.ts` 负责缓存：

- `fetchModelConfig()` 调用 `GET /model-config`，附带打破缓存的 `_ts` 查询参数，并把空响应体替换为默认无效对象。
- `getModelConfigCached()` 只拉取一次并在模块级缓存结果；拉取失败时缓存一个默认无效对象（`valid: false`），因此发送链路永远不会被后端的抖动打断。
- `invalidateModelConfigCache()` 清空缓存。`ConfigDialog` 在环境保存成功后调用它，于是下一次 `getModelConfigCached()` 会重新拉取。

```ts
function invalidModelConfig(): ModelConfig {
  return { main_max_token: null, aux_max_token: null, min_required: 131072, valid: false };
}
```

### 发送时的非阻断告警

`client/app/composables/use-chat-stream.ts`（`handleSend()`，toast 在第 433 行）读取缓存配置，当 `valid` 为 `false` 时弹出一个 6 秒的告警 toast。它不会中止发送：

```ts
const cfg = await getModelConfigCached();
if (!cfg.valid) {
  toastWarn(t('chat.tokenGuard.title'), t('chat.tokenGuard.detail'), 6000);
}
```

配置有效时，或拉取失败时，toast 保持静默。发送照常进行；若后端拒绝构建，它会返回一个 WebSocket error chunk。

### 环境标签横幅与保存前校验

`client/app/pages/home/components/ConfigDialog.vue`：

- 当环境标签暴露任意 `*_MAX_TOKEN` 键（`hasMaxTokenKeys`，第 344 行）时，渲染一条琥珀色横幅显示 `config.env.maxTokenHint`（第 211 行）。
- 保存时，`persistEnvChanges()` 会预校验两个 token 键（第 379 至 389 行）。非整数或低于 `131072` 的值会设置 `config.env.maxTokenError` 并返回 `false`，因此 PUT 永远不会发出：

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

- 写入成功后（第 756 行）会调用 `invalidateModelConfigCache()`，让 toast 守卫重新读取最新值。

### i18n 键位

| 键 | 位置 | 用途 |
| :-- | :---- | :------ |
| `chat.tokenGuard.title` | `client/app/i18n/locales/{en,zh,ja,ko}.json` | Toast 标题 |
| `chat.tokenGuard.detail` | 同上语言文件 | Toast 正文（指向 系统配置 然后 环境配置） |
| `config.env.maxTokenHint` | `ConfigDialog.vue` 的 `<i18n>` 块 | 琥珀色横幅文案 |
| `config.env.maxTokenError` | `ConfigDialog.vue` 的 `<i18n>` 块 | 保存前拒绝消息；`{key}` 插值出问题变量名 |

## 🧭 运维指南

### 修复不达标的配置

1. 打开仓库根目录的 `.env`，或使用 UI 的 系统配置 然后 环境配置 标签。
2. 把两个键都设为 `>= 131072`：

```bash
MAIN_LLM_MAX_TOKEN = 131072
AUXILIARY_LLM_MAX_TOKEN = 131072
```

3. 重启后端，让启动闸门重新运行。仓库自带的 `.env.example` 里两个键已经是 `131072`。

> 如果你的本地 `.env` 仍然是 `100000`，服务会拒绝启动并报 `STARTUP ABORTED: MAIN_LLM_MAX_TOKEN = 100000 is below the minimum 131072 (128K); agent startup is blocked`。启动前把它调到 `>= 131072`。

### 症状、原因、修复

| 症状 | 原因 | 修复 |
| :------ | :---- | :-- |
| 服务立即退出；日志出现 `STARTUP ABORTED: ...` | 某个键未设或低于 `131072` | 把两个键都设为 `>= 131072`，然后重启 |
| 日志出现 `AUXILIARY_LLM_MAX_TOKEN = 121072 ...` | 辅助键未设，回退到了 `121072` | 显式设置 `AUXILIARY_LLM_MAX_TOKEN` |
| 没有回复，而是收到 WebSocket error chunk | 运行途中改环境，把某个值降到下限以下 | 修好 `.env`，然后重试该回合 |
| 环境标签保存时报 maxToken 错误 | 值不是整数或 `< 131072` | 输入 `>= 131072` 的整数 |
| 发送时出现 "模型配置不达标" toast | `GET /model-config` 报告 `valid: false` | 修好 `.env`；保存成功后缓存会自动刷新 |

## 🧪 测试

| 套件 | 标记 | 覆盖 |
| :---- | :----- | :----- |
| `tests/config/test_token_guard.py` | `unit` | 阈值为 `131_072`；`None` 与低于下限抛错；错误信息点出键名；等于或高于下限通过 |
| `tests/agent/core/test_built_agent_guard.py` | `unit` | `built_agent()` 拒绝主值过低、主值未设、辅助值未设回退；合法环境可构建 |
| `tests/server/service/test_env_guard.py` | `unit` | `write_env_file()` 在**不写盘**的前提下拒绝低于下限与非整数；合法值与非 token 键通过 |
| `tests/server/trigger/test_model_config.py` | `integration` | `GET /model-config` 的响应形状与 `valid`：两者达最小值、主值过低、辅助未设、主值未设 |
| `client/app/composables/__tests__/modelConfig.test.ts` | Vitest | 拉取的破缓存、只拉一次缓存、失效后重拉、默认无效回退 |
| `client/app/composables/__tests__/use-chat-stream.token.test.ts` | Vitest | `handleSend` 仅在不达标时告警；有效与拉取失败时静默 |
| `client/app/pages/home/components/__tests__/ConfigDialog.token.test.ts` | Vitest | 横幅与保存前拒绝；合法值持久化并让缓存失效 |

```bash
# Python (unit + integration)
uv run pytest tests/config/test_token_guard.py tests/agent/core/test_built_agent_guard.py \
  tests/server/service/test_env_guard.py tests/server/trigger/test_model_config.py -q

# Frontend
cd client && pnpm test:unit -- modelConfig use-chat-stream.token ConfigDialog.token
```

## ⚠️ 局限与非目标

- **没有豁免开关。** 两个键都无法退出这条下限。没有环境变量开关、没有按角色覆盖、没有旁路。
- **本地辅助模式不豁免。** `AUXILIARY_LLM_MODEL_LOCAL=true` 仍须满足 `>= 131072`。本地模型路径硬编码为 `n_ctx=40960`，因此在窗口变大之前，标准的本地配置*预期*会被拒绝。
- **辅助值未设是陷阱，不是捷径。** 不设 `AUXILIARY_LLM_MAX_TOKEN` 时解析出的回退值是 `LLM_CLIENT_DEFAULTS["aux_remote_max_tokens"] = 121072`，低于下限，启动照样被阻断。
- **下限固定在 128K。** 没有 per-provider 阈值、没有上限、没有运行时热重载。运行途中的改动只对下一次图构建生效（由闸门 2 强制），对进行中的回合永不生效。
- **前端 toast 只是提醒。** 它不阻止发送；执法完全在后端。
- **与其他护栏无关。** 它与 `ToolGuardrails` 和 CJK token 估算相互独立，不触碰压缩阈值或 `summarization` 调参。它只固定上下文窗口的*最小值*，而非这个窗口如何被花掉。

## 📁 文件地图

| 层 | 文件 | 职责 |
| :---- | :--- | :--- |
| 阈值 | `config/features/agent_side/token_guard.py` | `MIN_REQUIRED_MAX_TOKEN`、`TokenGuardError`、`assert_max_token_valid` |
| 导出 | `config/features/__init__.py` | 重新导出三个符号 |
| 启动闸门 | `server/__main__.py:72` | CRITICAL 日志后 `SystemExit(1)` |
| 构建闸门 | `agent/core.py:117` | `built_agent()` 构建前校验 |
| 派生闸门 | `agent/tools/subagent/spawn/core.py:762` | 子 LLM 构造时校验 |
| 写入闸门 | `server/service/env.py:154` | `write_env_file()` 写盘前拒绝 |
| API | `server/trigger/http/model_config.py` | `GET /model-config` |
| 前端缓存 | `client/app/composables/model-config.ts` | `fetchModelConfig`、`getModelConfigCached`、`invalidateModelConfigCache` |
| 前端告警 | `client/app/composables/use-chat-stream.ts:433` | 发送时的非阻断 toast |
| 前端对话框 | `client/app/pages/home/components/ConfigDialog.vue` | 横幅、保存前校验、缓存失效 |
| 默认值 | `.env.example:8,59` | 两个键默认均为 `131072` |
