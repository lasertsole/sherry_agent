# 🛡️ 128K MAX_TOKEN 强制闸门

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> Agent 如何对两个 LLM 强制 128K 上下文窗口硬下限：一个共享断言在四个执法点抛出（服务启动、图构建、子 Agent 派生、`.env` 写入），前端在被拦截之前先告警，`GET /model-config` 对外暴露实时判定。

事实来源：`config/features/agent_side/token_guard.py`、`server/__main__.py`、`agent/core.py`、`agent/tools/subagent/spawn/core.py`、`server/service/env.py`、`server/trigger/http/model_config.py`、`client/app/composables/model-config.ts`、`client/app/composables/use-chat-stream.ts`、`client/app/pages/home/components/ConfigDialog.vue`，以及 `.env.example`。本文档中的每一个行号与常量都已对照该代码核对。

## 目录

- [概述](#-概述)
- [阈值与配置](#-阈值与配置)
- [执法、接口与前端](enforcement/README.zh.md)
  - [🚧 执法点](enforcement/README.zh.md#-执法点)
  - [🌐 接口：GET /model-config](enforcement/README.zh.md#-接口get-model-config)
  - [💻 前端行为](enforcement/README.zh.md#-前端行为)
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
| 启动闸门 | `server/__main__.py:95-100` | CRITICAL 日志后 `SystemExit(1)` |
| 构建闸门 | `agent/core.py:123` | `built_agent()` 构建前校验 |
| 派生闸门 | `agent/tools/subagent/spawn/core.py:815` | 子 LLM 构造时校验 |
| 写入闸门 | `server/service/env.py:157` | `write_env_file()` 写盘前拒绝 |
| API | `server/trigger/http/model_config.py` | `GET /model-config` |
| 前端缓存 | `client/app/composables/model-config.ts` | `fetchModelConfig`、`getModelConfigCached`、`invalidateModelConfigCache` |
| 前端告警 | `client/app/composables/use-chat-stream.ts:435` | 发送时的非阻断 toast |
| 前端对话框 | `client/app/pages/home/components/ConfigDialog.vue` | 横幅、保存前校验、缓存失效 |
| 默认值 | `.env.example:8,59` | 两个键默认均为 `131072` |
