# 计划：内部决策 LLM 独立配置系列（TaskFlow 决策 LLM + 压缩 LLM，均从 auxiliary 分离）

- 建档日期：2026-10-19
- 状态：**计划（未开工）**
- **文档范围**：本计划包含两个并行子任务，改造模式完全镜像：
  - **任务 A（§1-§11）**：`TASKFLOW_LLM_*` 独立配置，接手 taskflow 内部 4 个 LLM 调用（plan / replan / step_judge / finish）
  - **任务 B（§12，本次追加）**：`COMPRESSION_LLM_*` 独立配置，接手压缩路径的 LLM 摘要生成（`_acreate_summary` / `run_memory_flush`），**不动 memory review（用主 LLM）和 plan extraction（用 auxiliary）**
- **硬约束**：**不破坏现有 auxiliary_llm 行为**——非 taskflow / 非压缩的调用点（completion_judge / nudges memory review / subagent spawn）不动；两个新 LLM 不配置时均默认 fallback 到 auxiliary，保持向后兼容，详见 §1（任务 A 硬约束 T1-T9）和 §12.1（任务 B 硬约束 C1-C8）
- 关联代码：
  - **任务 A · 4 个 taskflow 调用点**（全部要改）：
    - `agent/tools/taskflow/tools/taskflow_plan.py:147`（初始规划）
    - `agent/tools/taskflow/tools/taskflow_replan.py:224`（失败后重规划）
    - `agent/tools/taskflow/step_judge.py:170`（步骤 PASS/RETRY/BLOCK 判定）
    - `agent/tools/taskflow/tools/taskflow_finish.py:98`（流程目标达成判定）
  - **任务 B · 压缩路径 LLM 调用点**（只改注入源，内部代码一行不动）：
    - `agent/core.py:387, 394`（启动时构建 + 注入 middleware chain）—— 把 `auxiliary_llm` 拆成 `auxiliary_llm` + `compression_llm` 两个实例
    - `agent/middlewares/summarization/core.py:278, 288`（`SummarizationMiddleware.__init__` 接收 `llm_factory` 参数）
    - `agent/middlewares/summarization/compression.py:191-208, 284-293`（两条压缩路径调 `self._llm_factory`）
    - `agent/middlewares/summarization/summary_generation.py:58-87, 110-123`（`_acreate_summary` / `_create_summary` 通过 `_build_llm(llm_factory)` 调用）
    - `agent/middlewares/summarization/memory_flush.py:70, 86, 110, 122`（`run_memory_flush` 接收 `llm_factory`）
  - **非改造范围**（**不动**，继续各自原有的 LLM）：
    - `agent/tools/subagent/spawn/completion_judge.py:159`（subagent 完成判定 → auxiliary）
    - `agent/middlewares/summarization/nudges.py:614-641`（**memory review → 主 LLM `build_main_llm()`**，cache-warm 设计，不在 auxiliary 调用点列表里）
    - `agent/middleways/summarization/nudges.py:901-931`（**plan extraction → auxiliary**，独立触发，跟压缩不同步）
    - `agent/tools/subagent/spawn/core.py:1455, 1492, 1495`（spawn/steer 分支 → auxiliary）
  - **配置/工厂层**（两个任务共享扩展点）：
    - `.models.example`（配置模板）
    - `config/models_settings.py`（hydrate 逻辑，294 行）
    - `models/LLMs/auxiliary_llm/core.py`（参考工厂模式）
    - `models/__init__.py`（导出 `build_taskflow_llm` + `build_compression_llm`）
  - **API 与前端**（两个任务共享扩展点）：
    - `server/trigger/http/models_config.py`（GET/PUT `/models-config`）
    - `server/service/models_config_service.py`（read/write_models_config 实现）
    - `client/app/pages/home/components/LlmModelManager.vue`（模型管理面板，加两张新卡片）
    - `client/app/composables/env.ts`（env 常量）
- 关联文档：`AGENTS.md` § Configuration Files（要追加 `TASKFLOW_LLM_*` 和 `COMPRESSION_LLM_*` 说明）

---

## 0. 背景：为什么需要独立的 TaskFlow 决策 LLM

### 0.1 当前 taskflow 内部 LLM 调用全用 auxiliary（已核实）

sherry 的 taskflow 子系统有 4 个内部 LLM 调用点，**全部使用 `build_auxiliary_llm(temperature=0)`**：

| 调用点 | 文件:行 | 用途 | temperature |
|---|---|---|---|
| 初始规划 | `taskflow_plan.py:147` | 把 goal 分解成 DAG steps | 0 |
| 失败重规划 | `taskflow_replan.py:224` | 保留 done 步骤，LLM 重新规划 non-done 部分 | 0 |
| 步骤验收 | `step_judge.py:170` | 子代理结果是否满足 validation_criteria，PASS/RETRY/BLOCK | 0 |
| 目标达成 | `taskflow_finish.py:98` | Gate E 流程目标是否达成 | 0 |

### 0.2 用户场景：为什么想把 taskflow 决策独立出来

当前所有 taskflow 内部决策都走 auxiliary_llm，但 auxiliary 还承担其他职责：

- `completion_judge.py:159`：subagent 完成判定（不属于 taskflow）
- `summarization/nudges.py:922`：摘要 memory review + plan extraction（不属于 taskflow）
- `subagent/spawn/core.py:1492, 1495`：subagent spawn/steer 分支

这意味着**用户没法单独给 taskflow 决策配更强的模型**——比如想让 step_judge 用 Qwen3-72B 提升判定精度，但 auxiliary 同时还跑摘要 review，配 72B 会拖慢摘要。或者反过来：auxiliary 配 9B 够摘要用了，但 taskflow 决策需要更强的模型。

### 0.3 设计目标

新增一个独立的 `TASKFLOW_LLM_*` 配置块，**接手所有 taskflow 内部 LLM 调用**（plan / replan / step_judge / finish），**留空时默认 fallback 到 auxiliary_llm**——保持向后兼容，老配置不需要改。

---

## 1. ⚠️ 硬约束：不破坏现有行为

| 约束 | 含义 | 验收方式 |
|---|---|---|
| **T1**：现有 `AUXILIARY_LLM_*` 配置不动 | `.models.example` / `.models` 现有字段保留；`build_auxiliary_llm` 工厂签名不变 | 现有 auxiliary 调用点（completion_judge / nudges / subagent spawn）行为 byte-identical |
| **T2**：非 taskflow 调用点不改 | `completion_judge.py:159` / `nudges.py:922` / `subagent/spawn/core.py:1492,1495` 继续用 `build_auxiliary_llm`，不切换到 `build_taskflow_llm` | grep 这些文件，不出现 `build_taskflow_llm` |
| **T3**：未配置时 fallback 到 auxiliary | `TASKFLOW_LLM_PROVIDER` 和 `TASKFLOW_LLM_API_BASE` 都为空 → `build_taskflow_llm()` 内部调 `build_auxiliary_llm()` | 未配置时 taskflow 4 个调用点行为 byte-identical（同 auxiliary） |
| **T4**：本地模式优先级一致 | `TASKFLOW_LLM_MODEL_LOCAL=true` 时走本地 GGUF（与 auxiliary 本地模式对齐）；本地开关优先于 provider 配置 | 本地模式 + provider 同时配置时，走本地 |
| **T5**：max_token 强制 ≥ 131072（128K） | 跟 `AUXILIARY_LLM_MAX_TOKEN` 一致（taskflow_replan prompt 可能很长，需要大上下文） | 配置 < 131072 启动时 abort 并提示（参考 auxiliary 的 boot gate） |
| **T6**：`.models` hydrate 行为不变 | hydrate 把 `TASKFLOW_LLM_*` key 注入 `os.environ`，跟 `AUXILIARY_LLM_*` 同模式；显式 env 变量优先级最高 | 卸载 `.models`，仅靠 env 变量也能跑 |
| **T7**：`GET/PUT /models-config` 协议兼容 | 现有 endpoint 返回的 JSON shape 不动；新字段追加；前端旧版本读不到新字段不报错 | 老前端版本访问新后端不崩 |
| **T8**：前端面板新条目不破坏现有 | `LlmModelManager.vue` 现有卡片（main/auxiliary/reasoner 等）行为不变；新卡片独立 | 现有卡片配置读写 byte-identical |
| **T9**：`AGENTS.md` 文档更新但不改协议 | § Configuration Files 章节追加 `TASKFLOW_LLM_*` 说明，其他部分不动 | 文档 diff 只增不删 |

---

## 2. 后端配置层改造（`.models` + hydrate）

### 2.1 `.models.example` 模板扩展

在 `AUXILIARY_LLM_*` 块之后（约第 33 行后）追加 `TASKFLOW_LLM_*` 块，**字段命名风格完全对齐 auxiliary**：

```jsonc
  // ── TaskFlow 决策模型 TaskFlow decision model ──────────────────────────
  // Used by: taskflow_plan / taskflow_replan / step_judge / taskflow_finish
  // Empty → falls back to AUXILIARY_LLM_* (backward compatible).
  // MUST be >= 131072 (128K): replan prompt carries every done step's result.
  "TASKFLOW_LLM_MODEL_LOCAL": false,
  "TASKFLOW_LLM_PROVIDER": "",
  "TASKFLOW_LLM_API_NAME": "",
  "TASKFLOW_LLM_API_BASE": "",
  "TASKFLOW_LLM_API_KEY": "",
  "TASKFLOW_LLM_MAX_TOKEN": 131072,
```

### 2.2 `config/models_settings.py` hydrate 扩展

`config/models_settings.py:294` 行的 hydrate 逻辑（参考 `AUXILIARY_LLM_*` 的处理）追加：

- [ ] **2.2.1** 在 models registry 里加 `TASKFLOW_LLM_*` 字段（跟 auxiliary 同 schema）
- [ ] **2.2.2** hydrate 时把 `TASKFLOW_LLM_*` 注入 `os.environ`，跟现有 key 同模式
- [ ] **2.2.3** seed 逻辑：`.models` 不存在时从 `.models.example` 复制（已经有这个机制，新字段会自动进来）
- [ ] **2.2.4** legacy `.env` 迁移：如果老 `.env` 里有 `TASKFLOW_LLM_*`（不应该有，但要防），按现有 `SPLIT_OUT_KEYS` 模式拒绝合并到 `.models`

### 2.3 boot 校验（128K floor）

`server/trigger/http/model_config.py:22` 已有 auxiliary 的 128K 校验：

```python
aux_raw = os.getenv("AUXILIARY_LLM_MAX_TOKEN", "").strip()
```

- [ ] **2.3.1** 同样加 `TASKFLOW_LLM_MAX_TOKEN` 的 128K 校验
- [ ] **2.3.2** 未配置（空字符串）时不校验，走 fallback 到 auxiliary（auxiliary 自己的校验会跑）

---

## 3. 后端工厂层改造（`models/LLMs/taskflow_llm/`）

### 3.1 新建 `models/LLMs/taskflow_llm/` 子包

**结构完全镜像 `auxiliary_llm/`**（参考 `models/LLMs/auxiliary_llm/core.py`）：

```
models/LLMs/taskflow_llm/
├── __init__.py
├── core.py            # build_taskflow_llm(temperature) 工厂
└── local_adapters.py  # 本地 GGUF 适配（复用 auxiliary 的 LocalStructuredOutput/LocalToolBinder）
```

### 3.2 `core.py::build_taskflow_llm` 实现

```python
"""TaskFlow decision LLM — auto-selects between TASKFLOW_LLM_* and AUXILIARY_LLM_*.

If TASKFLOW_LLM_PROVIDER / TASKFLOW_LLM_API_BASE is configured, uses that.
Otherwise falls back to build_auxiliary_llm() — backward compatible.

Usage:
    from models import build_taskflow_llm
    llm = build_taskflow_llm(temperature=0)
"""

import os
from typing import Any

from config.path import load_env
from models.LLMs.auxiliary_llm.core import build_auxiliary_llm


load_env()


def build_taskflow_llm(temperature: float | None = None):
    """Create a fresh TaskFlow decision LLM instance, or fall back to auxiliary.

    The factory intentionally mirrors build_auxiliary_llm's signature so the
    four taskflow call sites can switch with a one-line import change.

    Fallback rules (T3 / T4):
      1. TASKFLOW_LLM_MODEL_LOCAL=true → local GGUF (TODO: separate GGUF file
         or reuse auxiliary's? See §3.4)
      2. TASKFLOW_LLM_PROVIDER or TASKFLOW_LLM_API_BASE set → remote API
      3. Neither set → build_auxiliary_llm(temperature)  ← fallback
    """
    _provider = os.getenv("TASKFLOW_LLM_PROVIDER", "").strip()
    _api_base = os.getenv("TASKFLOW_LLM_API_BASE", "").strip()
    _is_local = os.getenv("TASKFLOW_LLM_MODEL_LOCAL", "").strip().lower() == "true"

    if not _is_local and not _provider and not _api_base:
        # Fallback to auxiliary (T3) — backward compatible.
        return build_auxiliary_llm(temperature=temperature)

    if _is_local:
        # Local GGUF branch (T4) — see §3.4 for the GGUF file decision.
        # First pass: reuse auxiliary's local adapter with a different model
        # path (TASKFLOW_LLM_LOCAL_MODEL_PATH, defaults to auxiliary's).
        # TODO: implement LocalLlamaChatBase subclass or share auxiliary's.
        raise NotImplementedError("Local GGUF branch — see §3.4")

    # Remote API branch — mirror auxiliary's remote branch
    from langchain.chat_models import init_chat_model
    from models.env_builder import ModelEnvBuilder, read_env

    _raw_max = os.getenv("TASKFLOW_LLM_MAX_TOKEN", "").strip()
    _max_tokens = int(_raw_max) if _raw_max else 131072

    _model_config: dict[str, Any] = ModelEnvBuilder(
        {
            "model_provider": "TASKFLOW_LLM_PROVIDER",
            "model": "TASKFLOW_LLM_API_NAME",
            "api_key": "TASKFLOW_LLM_API_KEY",
            "base_url": "TASKFLOW_LLM_API_BASE",
        }
    ).build()

    return init_chat_model(
        model_provider=_model_config["model_provider"],
        model=_model_config["model"],
        api_key=_model_config["api_key"],
        base_url=_model_config["base_url"],
        temperature=temperature if temperature is not None else 0,
        max_tokens=_max_tokens,
    )
```

### 3.3 `models/__init__.py` 导出

- [ ] **3.3.1** 在 `_LAZY_EXPORTS` 字典里加 `"build_taskflow_llm": ".LLMs"`（参考 `build_auxiliary_llm` 的导出位置，第 8 行附近）
- [ ] **3.3.2** `__all__` 同步追加

### 3.4 本地 GGUF 分支的设计决策（**待定**）

本地模式有个真问题:**taskflow 决策用什么 GGUF?**

| 选项 | 优点 | 缺点 |
|---|---|---|
| **A：复用 auxiliary 的本地 GGUF** | 零新依赖,零新模型文件 | 本地模式下 taskflow 跟 auxiliary 还是同一个模型,失去"独立配置"的意义 |
| **B：独立 GGUF 文件**(`TASKFLOW_LLM_LOCAL_MODEL_PATH`) | 真正独立,可配更强的本地模型 | 多一个 GGUF 文件下载,桌面打包体积上升 |
| **C：本地模式不支持,只支持远程 API** | 简单 | 桌面离线场景不能用更强的 taskflow 决策 |

**推荐 A**(第一版):
- 本地模式下 `build_taskflow_llm` 走 `build_auxiliary_llm`(跟 fallback 路径一样)
- 这样本地模式行为跟未配置一致——零新依赖,不破坏桌面打包体积红线
- 用户想用更强的 taskflow 决策 → 配远程 API
- 后续如果真有"本地也要独立"的需求,再升级到 B

- [ ] **3.4.1** 决策点:本地模式选 A/B/C(推荐 A,见 §3.4)
- [ ] **3.4.2** 如果选 A,`core.py::_is_local` 分支直接 `return build_auxiliary_llm(temperature)`
- [ ] **3.4.3** 如果选 B,需要扩展 `config/features/infra_side/model_downloads.py::MODEL_DOWNLOADS`,新增 taskflow_llm family(参考 auxiliary 的下载配置)

### 3.5 风险与约束

- **工厂函数必须每次创建新实例**(不能返回单例):跟 `build_auxiliary_llm` 一样,避免 subagent daemon thread 的 event loop binding 死锁(参考 `auxiliary_llm/core.py:48-61` 的 docstring)
- **fallback 路径不能 raise**:即使 taskflow 配置错(比如 API base 拼错),也要能 fallback 到 auxiliary,不能让 taskflow 整体停摆(fail-open 设计,跟 step_judge 一致)
- **temperature 默认 0**:工厂函数 `temperature=None` 时,远程 API 分支硬设 0(跟 auxiliary 一致,决策性任务要确定性)

**验收**:T1 + T3 + T4 + T5。新建 `tests/models/test_build_taskflow_llm.py`:
- 未配置时返回 auxiliary 实例(T3)
- 配置远程 API 时返回 init_chat_model 实例
- 本地模式行为(取决于 §3.4 决策)

---

## 4. 4 个 taskflow 调用点改造(改 import)

### 4.1 改造清单(满足 T2)

**只改这 4 个文件,每个文件改一行 import + 一行调用**:

- [ ] **4.1.1** `agent/tools/taskflow/tools/taskflow_plan.py:144-147`
  ```python
  # 改前
  from models import build_auxiliary_llm
  llm = build_auxiliary_llm(temperature=0)
  # 改后
  from models import build_taskflow_llm
  llm = build_taskflow_llm(temperature=0)
  ```

- [ ] **4.1.2** `agent/tools/taskflow/tools/taskflow_replan.py:221-224`
  ```python
  # 改前
  from models import build_auxiliary_llm
  llm = build_auxiliary_llm(temperature=0)
  # 改后
  from models import build_taskflow_llm
  llm = build_taskflow_llm(temperature=0)
  ```

- [ ] **4.1.3** `agent/tools/taskflow/step_judge.py:25, 170`
  ```python
  # 改前(modules-level)
  from models import build_auxiliary_llm
  # 改后
  from models import build_taskflow_llm
  # 改前(line 170)
  llm = build_auxiliary_llm(temperature=0)
  # 改后
  llm = build_taskflow_llm(temperature=0)
  ```

- [ ] **4.1.4** `agent/tools/taskflow/tools/taskflow_finish.py:96-98`
  ```python
  # 改前
  from models import build_auxiliary_llm
  llm = build_auxiliary_llm(temperature=0)
  # 改后
  from models import build_taskflow_llm
  llm = build_taskflow_llm(temperature=0)
  ```

### 4.2 验证非 taskflow 调用点不动(满足 T2)

- [ ] **4.2.1** grep 全仓库 `build_auxiliary_llm`,确认剩余调用点只剩:
  - `agent/core.py:12, 387`(主 agent 启动构建,给 middleware chain;其中 `auxiliary_llm` 实例注入到 `SummarizationMiddleware._llm_factory`,作为压缩时的 LLM 摘要生成器——**这是真正的"压缩"用 auxiliary 的地方**,`compression.py:191-208`)
  - `agent/middlewares/summarization/nudges.py:919, 922`(**plan extraction only**;memory review 在 `nudges.py:614-641::_create_nudge_agent` 用的是**主 LLM** `build_main_llm()`,cache-warm 设计,不在 auxiliary 调用点列表里)
  - `agent/tools/subagent/spawn/completion_judge.py:29, 159`(subagent 完成判定)
  - `agent/tools/subagent/spawn/core.py:1455, 1492, 1495`(spawn/steer)
- [ ] **4.2.2** 确认这 4 处全部保持 `build_auxiliary_llm`,不切换到 `build_taskflow_llm`

### 4.3 风险

- **import 顺序**:`models/__init__.py` 的 lazy export 必须在 `taskflow_plan` 第一次调用前生效——参考 `build_auxiliary_llm` 的 lazy 模式即可(`_LAZY_EXPORTS` dict + `__getattr__`)
- **测试 fixture**:`tests/agent/tools/taskflow/` 下现有测试可能 mock `build_auxiliary_llm`,需要改 mock `build_taskflow_llm`(或者 mock 两者,因为 fallback 路径会调 auxiliary)

**验收**:T2。跑 `tests/agent/tools/taskflow/` 全套,确认无 regression。

---

## 5. API endpoint 扩展(`GET/PUT /models-config`)

### 5.1 `server/service/models_config_service.py` 扩展

- [ ] **5.1.1** `read_models_config()` 返回的 dict 追加 `taskflow_llm` 块(跟 `auxiliary_llm` 块同 shape):
  ```json
  {
    "main_llm": {...},
    "auxiliary_llm": {...},
    "taskflow_llm": {
      "provider": "",
      "api_name": "",
      "api_base": "",
      "api_key": "<masked or omitted>",
      "max_token": 131072,
      "model_local": false,
      "is_fallback": true   // 新字段:当前是否走 fallback 到 auxiliary
    },
    "reasoner_llm": {...},
    ...
  }
  ```
- [ ] **5.1.2** `write_models_config(changes)` 接受 `taskflow_llm` 块的更新,写入 `.models`
- [ ] **5.1.3** `is_fallback` 字段是 derived(服务端实时计算),不接受写入——写入时忽略

### 5.2 `server/trigger/http/models_config.py` handler 扩展

现有 handler(`server/trigger/http/models_config.py:6-31`)不需要改——它只是 `read_models_config()` / `write_models_config(changes)` 的薄包装。新字段自动随 service 层扩展透传。

### 5.3 风险

- **API key 屏蔽**:跟 auxiliary 一样,`GET /models-config` 不返回明文 API key(返回空或 `***`)
- **并发写**:走现有 `expected_revision` 乐观锁(如果有,需要核实 service 层是否已有)

**验收**:T7。新建 `tests/server/test_models_config.py` 的 taskflow_llm 子用例。

---

## 6. 前端面板改造(`LlmModelManager.vue`)

### 6.1 新增"TaskFlow 决策模型"卡片

参考现有 auxiliary 卡片的 UI 模式,在 `LlmModelManager.vue` 加一个新卡片:

- [ ] **6.1.1** 卡片标题:**TaskFlow 决策模型**(副标题:用于 taskflow_plan / replan / step_judge / finish)
- [ ] **6.1.2** 字段(对齐 auxiliary 卡片):
  - Provider(下拉:openai / anthropic / azure / bedrock / deepseek / ...)
  - API Name(文本输入)
  - API Base(文本输入,可选)
  - API Key(password 输入)
  - Max Token(number,默认 131072,最小 131072)
  - Model Local(切换:本地 GGUF 模式)
- [ ] **6.1.3** **Fallback 提示徽章**:当 Provider + API Base 都空时,卡片显示"跟随辅助 LLM"徽章(对应 §3.2 的 fallback 路径)
- [ ] **6.1.4** 保存逻辑:写入走现有 `PUT /models-config`,载荷追加 `taskflow_llm` 块

### 6.2 `client/app/composables/env.ts` 常量扩展

- [ ] **6.2.1** `env.ts` 加 `TASKFLOW_LLM_*` 常量(对齐 `AUXILIARY_LLM_*` 的现有定义)
- [ ] **6.2.2** `client/app/constants/env.ts` 同步

### 6.3 风险

- **前端类型**:TypeScript 接口要扩展 `ModelsConfig` 类型,加 `taskflow_llm` 字段(可选,保证老前端版本读不到不崩——T7)
- **i18n**:四语言文案(EN/ZH/JA/KO),跟现有卡片一致
- **本地模式 UI**:`Model Local` 切换打开后,Provider/API 字段灰掉(跟 auxiliary 卡片行为一致)
- **默认值显示**:Provider 空 + API Base 空 → 卡片显示"跟随辅助 LLM"徽章 + 字段全灰(可编辑但提示等价于辅助)

**验收**:T8。新建 `client/app/pages/home/components/__tests__/LlmModelManager.taskflow.integration.test.ts`。

---

## 7. 文档更新(`AGENTS.md`)

### 7.1 § Configuration Files 章节追加

在 `AUXILIARY_LLM_*` 说明之后追加 `TASKFLOW_LLM_*` 说明:

- [ ] **7.1.1** 说明用途:"用于 taskflow 内部决策(plan / replan / step_judge / finish),留空 fallback 到 auxiliary"
- [ ] **7.1.2** 说明 max_token 强制 ≥ 131072
- [ ] **7.1.3** 说明本地模式:第一版复用 auxiliary 本地 GGUF(§3.4 决策 A)
- [ ] **7.1.4** 列出 4 个调用点(链接到代码位置)

### 7.2 § Per-Session Agent Config 章节不动

`TASKFLOW_LLM_*` 是**系统级配置**(写入 `.models`),不是 per-session 配置。不进 `AGENT_CONFIG` payload,不进"代理模型"tab。

**验收**:T9。`AGENTS.md` diff 只增不删。

---

## 8. 执行顺序与依赖

```
§2 后端配置层(.models + hydrate)         — 0.5 天
  │
  ↓
§3 后端工厂层(models/LLMs/taskflow_llm/)  — 1 天
  │
  ├─ §3.4 本地 GGUF 决策(选 A,0 工作量)
  │
  ↓
§4 4 个调用点改 import                    — 0.5 天
  │
  ↓ (后端能力就绪,可以本地测试)
§5 API endpoint 扩展                      — 0.5 天
  │
  ↓
§6 前端面板改造                           — 1-2 天
  │
  ↓
§7 文档更新                               — 0.5 天
```

**总工程量**:3.5-4.5 天。建议分批:

1. **第一批(1.5 天)**:§2 + §3 + §4——后端能力就绪,可以本地 curl 测试
2. **第二批(0.5 天)**:§5——API endpoint 暴露
3. **第三批(1-2 天)**:§6——前端面板
4. **第四批(0.5 天)**:§7——文档

**关键路径**:§3 工厂层是核心,§3.4 本地 GGUF 决策必须在动 §3 代码前定(推荐选 A,零新依赖)。

---

## 9. 验收标准

### 9.1 硬约束验收(每项必须过)

- [ ] **T1**:现有 `AUXILIARY_LLM_*` 配置不动;`build_auxiliary_llm` 工厂签名不变。
- [ ] **T2**:grep `build_auxiliary_llm` 全仓库,剩余调用点只剩 `agent/core.py` + `summarization/nudges.py` + `completion_judge.py` + `subagent/spawn/core.py`(4 处非 taskflow)。
- [ ] **T3**:`TASKFLOW_LLM_PROVIDER` + `TASKFLOW_LLM_API_BASE` 都空时,`build_taskflow_llm()` 返回 `build_auxiliary_llm()` 实例(可以用 `id()` 或类型断言验证)。
- [ ] **T4**:`TASKFLOW_LLM_MODEL_LOCAL=true` 时走本地分支(第一版本选 A → 走 auxiliary 本地路径)。
- [ ] **T5**:`TASKFLOW_LLM_MAX_TOKEN < 131072` 启动 abort,报错信息跟 auxiliary 一致。
- [ ] **T6**:hydrate 把 `TASKFLOW_LLM_*` 注入 `os.environ`;显式 env 变量优先级最高。
- [ ] **T7**:老前端版本访问新后端 `GET /models-config` 不崩(新字段 `taskflow_llm` 是可选)。
- [ ] **T8**:`LlmModelManager.vue` 现有卡片(main/auxiliary/reasoner)读写 byte-identical。
- [ ] **T9**:`AGENTS.md` diff 只增不删。

### 9.2 功能验收

- [ ] 前端配 `TASKFLOW_LLM_PROVIDER=openai` + `TASKFLOW_LLM_API_NAME=gpt-4o-mini` 保存后,后端 `taskflow_plan` 调用走 gpt-4o-mini(不走 auxiliary)。
- [ ] 前端清空 `TASKFLOW_LLM_*` 所有字段保存后,后端 `taskflow_plan` 调用走 auxiliary(fallback)。
- [ ] `GET /models-config` 返回的 `taskflow_llm.is_fallback` 字段在未配置时为 `true`,配置后为 `false`。
- [ ] `taskflow_replan` / `step_judge` / `taskflow_finish` 三个调用点同样切换到 taskflow_llm(不只 plan)。

### 9.3 回归验收

- [ ] 现有 `tests/agent/tools/taskflow/` 全套测试通过(可能需要 mock 改 `build_taskflow_llm`)。
- [ ] 现有 `tests/server/test_models_config.py` 通过(新字段追加不影响现有字段断言)。
- [ ] 现有 `LlmModelManager` 前端集成测试通过(可能需要扩展 fixture 加 `taskflow_llm` 字段)。
- [ ] `completion_judge` / `summarization/nudges` 行为 byte-identical(用同一输入跑前后对比)。

---

## 10. 不做的事(边界)

| 项 | 不做的原因 |
|---|---|
| 把 `completion_judge` / `nudges` 也切到 taskflow_llm | 这两个不属于 taskflow 子系统,继续用 auxiliary。如果要独立,另起 TODO |
| per-session 配置 taskflow_llm | taskflow_llm 是系统级配置,不进 `AGENT_CONFIG` payload。per-session 切换会让 taskflow 决策质量跟着 session 抖动,违反"决策稳定性"原则 |
| 本地 GGUF 独立文件(§3.4 选 B) | 第一版选 A(复用 auxiliary 本地 GGUF),零新依赖。如果用户反馈"本地也要独立"再升级到 B |
| 给 taskflow_llm 加 thinking control | taskflow 决策要确定性(temperature=0),不应该跟用户的 thinking 开关联动 |
| 把 main_llm 也改成可被 taskflow 接管 | taskflow 决策不应该用对话模型,设计上保持分工(对话 vs 决策) |

---

## 11. 关键参考

### sherry 内部(参考/修改)

- `.models.example:26-33`(`AUXILIARY_LLM_*` 配置块,新块参考这个)
- `config/models_settings.py`(294 行,hydrate 逻辑)
- `models/LLMs/auxiliary_llm/core.py:48-90`(`build_auxiliary_llm` 工厂,新工厂参考这个)
- `models/__init__.py:8`(`build_auxiliary_llm` lazy export,新导出参考这个)
- `server/trigger/http/models_config.py:6-31`(GET/PUT handler,薄包装不改)
- `server/service/models_config_service.py`(read/write_models_config 实现,扩展新字段)
- `client/app/pages/home/components/LlmModelManager.vue`(模型管理面板,加新卡片)
- `client/app/composables/env.ts` / `client/app/constants/env.ts`(env 常量)
- `server/trigger/http/model_config.py:22`(auxiliary 128K 校验,taskflow 同模式)

### 4 个 taskflow 调用点(改 import)

- `agent/tools/taskflow/tools/taskflow_plan.py:144-147`(plan)
- `agent/tools/taskflow/tools/taskflow_replan.py:221-224`(replan)
- `agent/tools/taskflow/step_judge.py:25, 170`(step_judge)
- `agent/tools/taskflow/tools/taskflow_finish.py:96-98`(finish goal judge)

### 非 taskflow 调用点(**不动**)

- `agent/core.py:12, 387`(主 agent 启动构建;`auxiliary_llm` 实例注入到 `SummarizationMiddleware._llm_factory`,这是任务 B 改造对象)
- `agent/middlewares/summarization/nudges.py:919, 922`(**plan extraction only**;memory review 在 `nudges.py:614-641::_create_nudge_agent` 用主 LLM `build_main_llm()`,cache-warm 设计)
- `agent/tools/subagent/spawn/completion_judge.py:29, 159`(subagent 完成判定)
- `agent/tools/subagent/spawn/core.py:1455, 1492, 1495`(spawn/steer)

### AGENTS.md 相关章节

- § Configuration Files(追加 `TASKFLOW_LLM_*` 和 `COMPRESSION_LLM_*` 说明)
- § Per-Session Agent Config(**不动**,两个新 LLM 都是系统级)

---

## 12. 任务 B：压缩 LLM 独立配置（`COMPRESSION_LLM_*`，从 auxiliary 分离）

### 12.0 背景：为什么需要独立的压缩 LLM（已核实代码）

sherry 当前所有压缩路径的 LLM 调用**通过同一个 `self._llm_factory`**，从 `agent/core.py:387` 启动时注入的 `auxiliary_llm` 实例而来。涉及调用点：

| 调用点 | 文件:行 | 用途 |
|---|---|---|
| LLM 摘要生成（同步） | `compression.py:191-208` | `run_memory_flush` + `_create_summary` |
| LLM 摘要生成（异步） | `compression.py:284-293` | `run_memory_flush` + `_acreate_summary` |
| 摘要 LLM 构造 | `summary_generation.py:58-87, 110-123` | `_build_llm(llm_factory)` → `llm.ainvoke(prompt)` |
| Memory flush | `memory_flush.py:70, 86, 110, 122` | `run_memory_flush` 接收 `llm_factory` 参数 |

**关键事实**：`SummarizationMiddleware.__init__`（`core.py:278-288`）接收 `llm_factory` 参数并存为 `self._llm_factory`——**所有下游调用都通过这一个字段**。这意味着**改造面非常小**：只改 `agent/core.py` 启动时注入的实例来源 + `SummarizationMiddleware.__init__` 参数命名,内部代码（compression.py / summary_generation.py / memory_flush.py）一行不动。

### 12.0.1 明确不动的范围（与"压缩"相关的另两个 LLM 调用）

压缩路径的"前后"还有两个 LLM 调用,但**不属于压缩本身**,**不动**：

| 调用点 | LLM | 不动的原因 |
|---|---|---|
| Memory review（`nudges.py:614-641::_create_nudge_agent`） | **主 LLM** `build_main_llm()` | cache-warm 设计——读主 turn 已 cache 的前缀，改 LLM 会丢 cache。用户已明确否定此前的双阶段 prefix cache 复用方案，memory review 的 LLM 选择保持现状 |
| Plan extraction（`nudges.py:901-931::_create_plan_extraction_agent`） | auxiliary_llm（temperature=0） | 独立触发（plan 完成时），跟压缩异步；结构化数据抽取，跟 step_judge 同性质，继续走 auxiliary 合理 |

任务 B 只动"压缩中"的 LLM 摘要生成 + memory_flush，**不动 memory review 和 plan extraction**。

### 12.1 ⚠️ 硬约束（任务 B 专属，C1-C8）

| 约束 | 含义 | 验收方式 |
|---|---|---|
| **C1**：`self._llm_factory` 字段命名保留 | `SummarizationMiddleware` 内部 `self._llm_factory` 字段名不变，只改注入源——下游 `compression.py` / `summary_generation.py` / `memory_flush.py` 一行不改 | grep `self._llm_factory`，调用点不变 |
| **C2**：未配置时 fallback 到 auxiliary | `COMPRESSION_LLM_PROVIDER` + `COMPRESSION_LLM_API_BASE` 都为空 → `build_compression_llm()` 内部调 `build_auxiliary_llm()` | 未配置时压缩路径行为 byte-identical（同 auxiliary） |
| **C3**：`agent/core.py:387` 不删 `auxiliary_llm` 构建 | 启动时仍构建 `auxiliary_llm`（给其他 middleware 用）+ 新构建 `compression_llm`；两者并存 | grep `build_auxiliary_llm`，`agent/core.py:387` 仍存在 |
| **C4**：memory review 不切换 LLM | `nudges.py:614-641` 继续用 `build_main_llm()`，**不动** | grep `build_main_llm` 在 `nudges.py` 中仍存在 |
| **C5**：plan extraction 不切换 LLM | `nudges.py:901-931` 继续用 `build_auxiliary_llm(temperature=0)`，**不动** | grep `build_auxiliary_llm` 在 `nudges.py:919-922` 仍存在 |
| **C6**：`COMPRESSION_LLM_MAX_TOKEN` 强制 ≥ 131072 | 跟 `AUXILIARY_LLM_MAX_TOKEN` 一致（压缩 prompt 可能很长） | 配置 < 131072 启动 abort |
| **C7**：本地模式第一版选 A（复用 auxiliary 本地 GGUF） | 跟任务 A §3.4 决策一致；零新依赖 | 同任务 A |
| **C8**：前端面板新卡片不破坏现有 | `LlmModelManager.vue` 现有卡片（main/auxiliary/reasoner/taskflow）行为不变；新卡片独立 | 现有卡片读写 byte-identical |

### 12.2 后端配置层改造（`.models` + hydrate）

- [ ] **12.2.1** `.models.example` 在 `TASKFLOW_LLM_*` 块之后追加 `COMPRESSION_LLM_*` 块（字段命名风格完全对齐 auxiliary / taskflow）：

  ```jsonc
  // ── 压缩模型 Compression model ──────────────────────────────────────────
  // Used by: SummarizationMiddleware (LLM summary generation + memory_flush)
  // Empty → falls back to AUXILIARY_LLM_* (backward compatible).
  // Memory review uses MAIN_LLM (cache-warm), NOT this one.
  // Plan extraction uses AUXILIARY_LLM, NOT this one.
  // MUST be >= 131072 (128K): compression prompt carries the discarded slice verbatim.
  "COMPRESSION_LLM_MODEL_LOCAL": false,
  "COMPRESSION_LLM_PROVIDER": "",
  "COMPRESSION_LLM_API_NAME": "",
  "COMPRESSION_LLM_API_BASE": "",
  "COMPRESSION_LLM_API_KEY": "",
  "COMPRESSION_LLM_MAX_TOKEN": 131072,
  ```

- [ ] **12.2.2** `config/models_settings.py` hydrate 扩展（跟 §2.2 同模式）：加 `COMPRESSION_LLM_*` 字段注册 + 注入 `os.environ` + seed 逻辑
- [ ] **12.2.3** boot 校验：`server/trigger/http/model_config.py` 加 `COMPRESSION_LLM_MAX_TOKEN` 的 128K 校验（跟 taskflow 同模式）

### 12.3 后端工厂层改造（`models/LLMs/compression_llm/`）

- [ ] **12.3.1** 新建 `models/LLMs/compression_llm/` 子包（结构镜像 `auxiliary_llm/` 和 `taskflow_llm/`）：

  ```
  models/LLMs/compression_llm/
  ├── __init__.py
  ├── core.py            # build_compression_llm(temperature) 工厂
  └── local_adapters.py  # 复用 auxiliary 的 LocalStructuredOutput/LocalToolBinder
  ```

- [ ] **12.3.2** `core.py::build_compression_llm` 实现（完全镜像 `build_taskflow_llm`）：

  ```python
  """Compression LLM — auto-selects between COMPRESSION_LLM_* and AUXILIARY_LLM_*.

  Fallback rules (C2 / C7):
    1. COMPRESSION_LLM_MODEL_LOCAL=true → 复用 auxiliary 本地 GGUF（任务 A §3.4 选 A）
    2. COMPRESSION_LLM_PROVIDER or COMPRESSION_LLM_API_BASE set → remote API
    3. Neither set → build_auxiliary_llm(temperature)  ← fallback
  """
  import os
  from models.LLMs.auxiliary_llm.core import build_auxiliary_llm

  def build_compression_llm(temperature: float | None = None):
      _provider = os.getenv("COMPRESSION_LLM_PROVIDER", "").strip()
      _api_base = os.getenv("COMPRESSION_LLM_API_BASE", "").strip()
      _is_local = os.getenv("COMPRESSION_LLM_MODEL_LOCAL", "").strip().lower() == "true"

      if not _is_local and not _provider and not _api_base:
          return build_auxiliary_llm(temperature=temperature)  # C2 fallback

      if _is_local:
          # C7: 复用 auxiliary 本地路径（第一版）
          return build_auxiliary_llm(temperature=temperature)

      # Remote API branch — mirror taskflow_llm 的远程分支
      from langchain.chat_models import init_chat_model
      from models.env_builder import ModelEnvBuilder

      _raw_max = os.getenv("COMPRESSION_LLM_MAX_TOKEN", "").strip()
      _max_tokens = int(_raw_max) if _raw_max else 131072

      _model_config = ModelEnvBuilder({
          "model_provider": "COMPRESSION_LLM_PROVIDER",
          "model": "COMPRESSION_LLM_API_NAME",
          "api_key": "COMPRESSION_LLM_API_KEY",
          "base_url": "COMPRESSION_LLM_API_BASE",
      }).build()

      return init_chat_model(
          model_provider=_model_config["model_provider"],
          model=_model_config["model"],
          api_key=_model_config["api_key"],
          base_url=_model_config["base_url"],
          temperature=temperature if temperature is not None else 0,
          max_tokens=_max_tokens,
      )
  ```

- [ ] **12.3.3** `models/__init__.py` 导出 `build_compression_llm`（跟 §3.3 同模式，lazy export）

### 12.4 调用点改造（**只改注入源，下游一行不动**）

这是任务 B 跟任务 A 最大的区别——任务 A 要改 4 个调用点的 import，任务 B **只改 1 处注入源**：

- [ ] **12.4.1** `agent/core.py:387, 394` 改造：

  ```python
  # 改前
  auxiliary_llm = build_auxiliary_llm()
  # ...
  agent_middleware = _build_middlewares(
      fallback_chain=fallback_chain,
      auxiliary_llm=auxiliary_llm,
      ...
  )

  # 改后
  auxiliary_llm = build_auxiliary_llm()
  compression_llm = build_compression_llm()   # 新增：独立压缩 LLM 实例
  # ...
  agent_middleware = _build_middlewares(
      fallback_chain=fallback_chain,
      auxiliary_llm=auxiliary_llm,             # 保留：给其他 middleware 用（如 LLMRetry 等）
      compression_llm=compression_llm,         # 新增：传给 SummarizationMiddleware
      ...
  )
  ```

- [ ] **12.4.2** `agent/middlewares/summarization/core.py:278, 288` 改造：

  ```python
  # 改前
  def __init__(
      self,
      ...,
      llm_factory: Any = None,    # ← 原参数
      ...,
  ):
      self._llm_factory = llm_factory   # C1: 字段名保留

  # 改后（C1 保证字段名不变）
  def __init__(
      self,
      ...,
      compression_llm: Any = None,   # ← 新参数命名
      ...,
  ):
      self._llm_factory = compression_llm   # 字段名保留 self._llm_factory
  ```

  **关键**：`self._llm_factory` 字段名不改（C1），所以 `compression.py` / `summary_generation.py` / `memory_flush.py` 里所有 `self._llm_factory` 引用**一行不改**——它们自动用新的 `compression_llm` 实例。

- [ ] **12.4.3** 找到 `_build_middlewares`（在 `agent/core.py` 或 middleware 工厂）里把 `auxiliary_llm` 传给 `SummarizationMiddleware(llm_factory=...)` 的地方，改成 `compression_llm=compression_llm`。

### 12.5 API endpoint 扩展

- [ ] **12.5.1** `server/service/models_config_service.py::read_models_config()` 返回的 dict 追加 `compression_llm` 块（shape 跟 `taskflow_llm` 一致，含 `is_fallback` derived 字段）
- [ ] **12.5.2** `write_models_config(changes)` 接受 `compression_llm` 块更新，写入 `.models`
- [ ] **12.5.3** `server/trigger/http/models_config.py` handler 不用改（薄包装）

### 12.6 前端面板改造

- [ ] **12.6.1** `LlmModelManager.vue` 在 TaskFlow 决策卡片之后加"**压缩模型**"卡片（副标题：用于 LLM 摘要生成 + memory_flush；标注"memory review 走主 LLM,不在此配置"）
- [ ] **12.6.2** 字段对齐其他卡片（Provider / API Name / API Base / API Key / Max Token / Model Local）
- [ ] **12.6.3** Fallback 提示徽章（跟 taskflow 卡片同模式）
- [ ] **12.6.4** `client/app/composables/env.ts` / `client/app/constants/env.ts` 加 `COMPRESSION_LLM_*` 常量

### 12.7 文档更新

- [ ] **12.7.1** `AGENTS.md` § Configuration Files 追加 `COMPRESSION_LLM_*` 说明，明确：
  - 用于：`SummarizationMiddleware`（LLM 摘要 + memory_flush）
  - **不用于**：memory review（走主 LLM）、plan extraction（走 auxiliary）
  - 留空 fallback 到 auxiliary
  - max_token ≥ 131072

### 12.8 风险与约束

- **改造面比任务 A 小很多**：任务 A 要改 4 个调用点的 import，任务 B 只改 1 处注入源 + 1 处参数命名。下游代码（compression.py / summary_generation.py / memory_flush.py）一行不动（C1）。
- **`auxiliary_llm` 实例仍要构建**（C3）：`agent/core.py:387` 的 `build_auxiliary_llm()` 不能删——可能还有其他 middleware 用（比如 LLMRetry、fallback_chain）。需要 grep 确认 `auxiliary_llm` 在 `_build_middlewares` 里传给了几个 middleware，只把 `SummarizationMiddleware` 的来源改掉，其他保留。
- **未配置时行为完全一致**（C2）：fallback 路径走 `build_auxiliary_llm()`，返回的实例跟原来一样，压缩路径 byte-identical。
- **memory review 不能误改**（C4）：用户明确否定此前的双阶段 prefix cache 复用方案。memory review 继续用主 LLM，本任务**不动** `nudges.py:614-641`。
- **plan extraction 不能误改**（C5）：`nudges.py:901-931` 继续用 auxiliary，本任务不动。

### 12.9 工程量与执行顺序

```
任务 B 总工程量：~1.5-2 天（远小于任务 A 的 3.5-4.5 天）
  ├─ §12.2 配置层（0.5 天）
  ├─ §12.3 工厂层（0.5 天，跟 taskflow_llm 工厂同模式）
  ├─ §12.4 注入源改造（0.25 天，只改 1 处）
  ├─ §12.5 API 扩展（0.25 天）
  ├─ §12.6 前端卡片（0.5-1 天）
  └─ §12.7 文档（0.25 天）
```

**关键路径**：§12.4 注入源改造是核心，但工程量极小（改 2 行）。大头在前端卡片和配置层。

**跟任务 A 的关系**：两个任务**完全并行**，互不依赖。可以同时做，或先后做。建议：
- 任务 A 先做（4 个调用点改 import + 工厂）——验证"独立 LLM 配置 + fallback"模式跑通
- 任务 B 后做（只改注入源）——复用任务 A 验证过的模式，工程量小
- 或者两任务合并一批做（配置层 + 工厂层 + API + 前端可以共用改造，省 1-2 天）

### 12.10 验收标准

- [ ] **C1**：`grep "self._llm_factory"` 在 `compression.py` / `summary_generation.py` / `memory_flush.py` 的调用点不变。
- [ ] **C2**：未配置 `COMPRESSION_LLM_*` 时，`build_compression_llm()` 返回 `build_auxiliary_llm()` 实例；压缩路径行为 byte-identical。
- [ ] **C3**：`grep "build_auxiliary_llm"` 在 `agent/core.py:387` 仍存在。
- [ ] **C4**：`grep "build_main_llm"` 在 `nudges.py:614-641` 仍存在。
- [ ] **C5**：`grep "build_auxiliary_llm"` 在 `nudges.py:919-922` 仍存在。
- [ ] **C6**：`COMPRESSION_LLM_MAX_TOKEN < 131072` 启动 abort。
- [ ] **C7**：`COMPRESSION_LLM_MODEL_LOCAL=true` 走 auxiliary 本地路径（第一版选 A）。
- [ ] **C8**：`LlmModelManager.vue` 现有卡片（含任务 A 加的 taskflow 卡片）读写 byte-identical。
- [ ] 前端配 `COMPRESSION_LLM_PROVIDER=openai` + `COMPRESSION_LLM_API_NAME=gpt-4o-mini` 后，压缩路径走 gpt-4o-mini（不走 auxiliary）。
- [ ] 前端清空 `COMPRESSION_LLM_*` 后，压缩路径走 auxiliary（fallback）。
- [ ] `GET /models-config` 返回的 `compression_llm.is_fallback` 字段在未配置时为 `true`。

### 12.11 不做的事（任务 B 边界，补充 §10）

| 项 | 不做的原因 |
|---|---|
| 改 memory review 用 compression_llm | 用户已否定双阶段 prefix cache 复用方案；memory review 用主 LLM 是 cache-warm 设计，改 LLM 会丢 cache（C4） |
| 改 plan extraction 用 compression_llm | plan extraction 跟压缩异步触发（plan 完成时），不共享调用路径；继续用 auxiliary 合理（C5） |
| 把 `self._llm_factory` 重命名为 `self._compression_llm_factory` | 违反 C1——下游代码一行不改是任务 B 工程量小的关键 |
| 给 compression_llm 加 thinking control | 压缩任务要确定性（temperature=0），不跟用户 thinking 开关联动 |
| per-session 配置 compression_llm | 系统级配置，不应跟 session 抖动 |

---

## 13. 总执行顺序（任务 A + 任务 B 合并视图）

```
任务 A（§1-§11，3.5-4.5 天）+ 任务 B（§12，1.5-2 天）

合并执行（共享配置层/工厂层/API/前端，省 1-2 天）：
  第一批（2 天）  §2 + §3 + §4（任务 A 4 个调用点）+ §12.2 + §12.3 + §12.4（任务 B 注入源）
  第二批（0.5 天）§5 + §12.5（API endpoint 共享扩展）
  第三批（1.5-2 天）§6 + §12.6（前端两张新卡片：TaskFlow + 压缩）
  第四批（0.5 天）§7 + §12.7（文档更新）

合并总工程量：~4.5-5 天（比两个任务分开做省 1-2 天）
```
