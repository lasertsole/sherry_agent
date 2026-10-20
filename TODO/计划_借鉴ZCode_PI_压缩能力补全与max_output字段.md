# 计划：借鉴 ZCode 压缩能力补全 + 借鉴 PI per-provider max_output 字段

- 建档日期：2026-10-19
- 状态：**已落地（2026-10-20）**
  - §2 per-provider max_output（MAIN/AUX；空=不传，配置 >= 8192，boot/build/save 三处校验）、§3 output reserve（未配置时保持旧公式）、§4 microcompact、§5 compact_now（工具 + T2 用户请求分发，trigger=USER）。
  - §6 ModelDownshift：session model profile 新增可选 `context_window`（client descriptor + backend sanitizer）；压缩中间件按 session 窗口计算阈值，切到更小窗口后下一次 pre-request 自动压缩，trajectory `reason=model_downshift`。
  - M8：`CompactPhase`/`CompactReason` 已由 `计划_响应式压缩对齐ZCode_ReactivePhase.md` §2 落地，compact 事件统一带 `phase`/`reason`/`trigger`。
- **文档范围**：两个主题，五个任务，依赖关系明确：
  - **任务一（§2）**：借鉴 PI agent 加 per-provider `max_output_tokens` 字段（**基础设施，最先做**）
  - **任务二（§3）**：借鉴 ZCode 改进压缩阈值公式（用 §2 的 max_output 算 output reserve，对齐 ZCode `effective context window = ctx - outputReserve`）
  - **任务三（§4）**：借鉴 ZCode microcompact（本地清理旧 tool result，不调 LLM，预防性压缩）
  - **任务四（§5）**：借鉴 ZCode UserRequested（用户 `/compact` 命令）
  - **任务五（§6）**：借鉴 ZCode ModelDownshift（切模型时压缩，对 sherry per-session 切模型尤其关键）
- **硬约束**：**不破坏现有 T1-T5 压缩行为 + 不破坏现有 `MAIN_LLM_MAX_TOKEN` 配置**，详见 §1
- 关联代码：
  - sherry 现状（改造对象）：
    - `.models.example:18, 33, 40`（`MAIN_LLM_MAX_TOKEN` / `AUXILIARY_LLM_MAX_TOKEN` / `REASONER_LLM_MAX_TOKEN`——全部是 context window，无 max_output 字段）
    - `models/LLMs/main_llm.py:28-97, 338`（`max_tokens` 用法——把 context window 当 max_output 用）
    - `agent/middlewares/summarization/compression.py:122-127`（`_calculate_preserve_budget` 用 `ctx × 0.25`，不扣 output reserve）
    - `agent/middlewares/summarization/overflow.py:67-88`（overflow route 决策，threshold 也基于 ctx 不扣 output）
  - PI 参考（per-provider max_output 字段）：
    - `D:/selfProj/pi/packages/ai/src/api/openai-responses.ts:32-33, 93, 340-341`（`OPENAI_RESPONSES_MIN_OUTPUT_TOKENS=16` + `supportsMaxOutputTokens` 检查 + `max_output_tokens` 字段映射）
    - `D:/selfProj/pi/packages/ai/src/api/google-generative-ai.ts:382-383`（`generationConfig.maxOutputTokens` 字段映射）
    - `D:/selfProj/pi/packages/ai/src/api/azure-openai-responses.ts:18, 206-207`（Azure 同 OpenAI Responses）
    - `D:/selfProj/pi/packages/ai/src/providers/compat-schema.ts:218-219, 399-404`（`maxTokensField` 字段选择 + `supportsMaxOutputTokens` capability flag 定义）
  - ZCode 参考（压缩能力）：
    - `ZCode-main/.../packages/core/src/compact/policy.ts:6-14, 67-88, 90-155`（`outputReserve` 公式 + `shouldAutoCompact` 5 reason）
    - `ZCode-main/.../packages/core/src/compact/microcompact.ts:77-81, 176-200`（microcompact threshold + 2 trigger）
    - `ZCode-main/.../packages/contracts/src/compact/index.ts:41-48`（`CompactReason` 四值：UserRequested / ContextLimit / ModelDownshift / ProviderOverflow）
- 关联 TODO：
  - `TODO/计划_响应式压缩对齐ZCode_ReactivePhase.md`（Reactive phase 显式化，正交）
  - `TODO/计划_TaskFlow决策LLM独立配置.md`（任务 B 压缩 LLM 独立配置，正交）

---

## 0. 背景：sherry 现状 vs ZCode + PI（已核实）

### 0.1 sherry 当前没有 max_output 字段（已核实）

sherry `.models.example` 只有三个 `*_MAX_TOKEN` 字段：

```jsonc
"MAIN_LLM_MAX_TOKEN": 131072,        // 这是 context window（128K），不是 max_output
"AUXILIARY_LLM_MAX_TOKEN": 131072,   // 同上
"REASONER_LLM_MAX_TOKEN": "",        // 同上
```

`models/LLMs/main_llm.py:28` 直接 `max_tokens = os.getenv("MAIN_LLM_MAX_TOKEN")`，把 context window 当 max_output 用。**完全没区分**这两个概念。

后果：
- sherry 调 provider 时**不显式传 max_output_tokens**（或者传 131072 给 provider，很多 provider 会拒绝或截断）
- **provider 用默认值 4096**（OpenAI Chat Completions API 及大多数 OpenAI 兼容 API 的默认 max_tokens）——用户不配置 `MAIN_LLM_MAX_OUTPUT_TOKEN` 时，sherry 不传字段，provider 用 4096
- **thinking 模式下 4096 严重不够**：reasoning tokens + 可见回答共享 4096 budget，思考链可能挤掉整个答案，或答案在生成中途被截断
- **压缩阈值没法扣 output reserve**——sherry 不知道 output 上限是多少（实际是 4096，但 sherry 配置里没声明），只能用 `ctx × 0.80`

### 0.2 sherry 当前压缩阈值公式不扣 output reserve（已核实）

`compression.py:122-127` 的 `_calculate_preserve_budget`：

```python
def _calculate_preserve_budget(self) -> int:
    ctx = self._main_llm_context_window
    if ctx:
        budget = int(ctx * PRESERVE_RATIO)  # ctx × 0.25
        return min(MAX_PRESERVE_TOKENS, max(MIN_PRESERVE_TOKENS, budget))
    return MIN_PRESERVE_TOKENS
```

`overflow.py` 的 threshold 也是基于 ctx 直接算，不扣 output reserve。

对比 ZCode（`policy.ts:67-88`）：

```typescript
function getEffectiveContextWindowSize(config) {
  const contextWindow = config.contextWindow ?? 200_000;
  const reserve = Math.min(getAutoCompactOutputReserveTokens(config), contextWindow);
  return Math.max(0, contextWindow - reserve);  // ★ 先扣 output reserve
}
function getAutoCompactThreshold(config) {
  const effectiveContextWindow = getEffectiveContextWindowSize(config);
  const buffer = config.bufferTokens ?? 13_000;
  return Math.max(0, effectiveContextWindow - buffer);  // ★ 再扣 buffer
}
```

ZCode 的 docstring 明说：**"provider 的 context window 是 input + output 共享窗口，autoCompact 只能让出输入侧，因此阈值分母必须先扣掉当前模型允许的 output token"**。

sherry 不扣 → over-commit → 可能留不够 output 空间，触发 provider 报 `context_overflow`。

### 0.3 sherry 当前没有 microcompact / UserRequested / ModelDownshift（已核实）

| 能力 | sherry | ZCode | PI |
|---|---|---|---|
| context window 字段 | `MAIN_LLM_MAX_TOKEN` | `contextWindow` | — |
| **max_output 字段** | ❌ 没有（混在 MAX_TOKEN）→ provider 默认 **4096** | ❌ 硬编码默认（32K/21K） | ✅ per-provider 映射 |
| **per-provider 字段名映射** | — | — | ✅ `max_output_tokens` / `maxOutputTokens` / `max_completion_tokens` / `max_tokens` |
| **supportsMaxOutputTokens 检查** | — | — | ✅ capability flag |
| **压缩阈值扣 output reserve** | ❌ | ✅（用硬编码值） | — |
| **microcompact（本地清旧 tool result）** | ❌ | ✅（60 min idle + token pressure） | — |
| **UserRequested（/compact 命令）** | ❌ | ✅ StandaloneTurn | — |
| **ModelDownshift（切模型时压缩）** | ❌ | ✅ CompactReason.ModelDownshift | — |

sherry 在这四个维度全部缺失。本 TODO 补全。

---

## 1. ⚠️ 硬约束：不破坏现有 T1-T5 行为 + 不破坏 MAX_TOKEN 配置

| 约束 | 含义 | 验收方式 |
|---|---|---|
| **M1**：现有 `MAIN_LLM_MAX_TOKEN` 字段语义不变 | 保留为 context window 字段，不重命名；新字段 `MAIN_LLM_MAX_OUTPUT_TOKEN` 是追加 | 现有配置 byte-identical 读取 |
| **M2**：T1-T5 压缩行为不变（除阈值公式改进） | T1/T2/T3/T4/T5 的触发逻辑、route 决策、recovery loop 不动；只有 `_calculate_preserve_budget` 和 overflow threshold 的公式改进 | 现有压缩测试 byte-identical（在 max_output 未配置时退化到旧行为） |
| **M3**：未配置 max_output 时退化 | `MAIN_LLM_MAX_OUTPUT_TOKEN` 空 → 走现有 `ctx × 0.80` 公式（不扣 output reserve）；配置后才走新公式 | 未配置时行为 byte-identical |
| **M4**：provider 不支持 max_output 时跳过 | 借鉴 PI 的 `supportsMaxOutputTokens` 检查；不支持时不传字段，用 provider 默认值 | 给 Codex-protocol gateway 不传 max_output_tokens |
| **M5**：microcompact 不破坏现有非 LLM 策略 | `read_file` 切片 / media offload 仍跑；microcompact 是新加的一层，在它们之前或之后跑 | 现有非 LLM 策略测试 byte-identical |
| **M6**：UserRequested 不破坏自动压缩 | `/compact` 命令是用户主动触发，不影响 T1-T5 自动压缩逻辑；两条路径独立 | 自动压缩不受 `/compact` 影响 |
| **M7**：ModelDownshift 不破坏 ThinkingControl | 切模型时的压缩是辅助，不替换 ThinkingControl 的模型切换逻辑；切模型后压缩一次，不持续干扰 | ThinkingControl 行为 byte-identical |
| **M8**：新枚举/新字段是 derived | `CompactReason.ModelDownshift` / `CompactReason.UserRequested` 跟前面 TODO 的 `CompactPhase`/`CompactReason` 枚举一致；不修改现有 trigger | 跟 `TODO/计划_响应式压缩对齐ZCode_ReactivePhase.md` §2 的枚举对齐 |
| **M9**：`MAIN_LLM_MAX_OUTPUT_TOKEN` 最小不能低于 8192（8K） | 配置 < 8192 时启动 abort 并提示"output token too small (min 8192/8K, thinking 模式下 reasoning + answer 共享预算,小于 8K 会截断)"；跟 PI 的 `OPENAI_RESPONSES_MIN_OUTPUT_TOKENS=16` 是不同维度(16 是 provider API 硬下限,8K 是业务下限) | 配置 < 8192 时启动 abort；运行时 `build_*_llm` 解析到 < 8192 也 abort |

---

## 2. 任务一：借鉴 PI 加 per-provider `max_output_tokens` 字段（基础设施，最先做）

### 2.1 目标

新增 `MAIN_LLM_MAX_OUTPUT_TOKEN` / `AUXILIARY_LLM_MAX_OUTPUT_TOKEN` / `TASKFLOW_LLM_MAX_OUTPUT_TOKEN` / `COMPRESSION_LLM_MAX_OUTPUT_TOKEN` 配置字段，区分 context window 和 max_output。加 per-provider 字段名映射（借鉴 PI 的 compat schema）。

### 2.2 改动清单

- [ ] **2.2.1** `.models.example` 在每个 `*_MAX_TOKEN` 字段后追加 `*_MAX_OUTPUT_TOKEN`（**默认留空，不设默认值**——用户不配就不传给 provider，用 provider 默认 4096；**配置时最小不能低于 8192（8K）**，M9）：

  ```jsonc
  "MAIN_LLM_MAX_TOKEN": 131072,              // context window（保留，不动）
  "MAIN_LLM_MAX_OUTPUT_TOKEN": "",           // 新增：单次输出上限。留空 = 不传给 provider，用 provider 默认（OpenAI 兼容 API 默认 4096）。配置时最小 8192/8K（M9），低于 8K 启动 abort（thinking 模式下 reasoning + answer 共享预算，小于 8K 会截断）。借鉴 PI options.maxTokens 的 if-set-then-pass 模式
  ```

  对 MAIN / AUXILIARY / TASKFLOW（如果有）/ COMPRESSION（如果有）四个 LLM 都加，**全部默认留空**，**全部最小 8192/8K**（M9）。

- [ ] **2.2.2** `models/LLMs/main_llm.py` 改造：
  - 读 `MAIN_LLM_MAX_OUTPUT_TOKEN` env，作为 max_output
  - 调 provider 时**显式传**对应字段（per-provider 映射，借鉴 PI）
  - 保留 `MAIN_LLM_MAX_TOKEN` 作为 context window（`profile.max_input_tokens`）

- [ ] **2.2.3** 新建 `models/LLMs/provider_max_output.py`（per-provider 字段映射，借鉴 PI compat schema）：

  ```python
  """Per-provider max_output field mapping (借鉴 PI compat-schema).

  不同 provider 用不同字段名传 max_output：
    - OpenAI Responses / Azure OpenAI Responses: max_output_tokens (min 16)
    - Google Generative AI / Vertex: maxOutputTokens (camelCase, in generationConfig)
    - OpenAI Chat Completions (通用兼容): max_completion_tokens 或 max_tokens
    - Anthropic: max_tokens (跟 context window 共用字段名，但语义是 output 上限)

  有些 provider (Codex-protocol gateways) 不支持 max_output_tokens，要跳过。
  """

  PROVIDER_MAX_OUTPUT_FIELD = {
      "openai_responses": "max_output_tokens",        # OpenAI Responses API
      "azure_responses": "max_output_tokens",          # Azure OpenAI Responses
      "google": "maxOutputTokens",                      # Google Generative AI / Vertex (in generationConfig)
      "anthropic": "max_tokens",                        # Anthropic (跟 context window 共用，语义是 output)
      "openai_chat": "max_completion_tokens",           # OpenAI Chat Completions (o1+ 模型)
      "openai_chat_legacy": "max_tokens",               # OpenAI Chat Completions (legacy 模型)
  }

  OPENAI_RESPONSES_MIN_OUTPUT_TOKENS = 16  # 借鉴 PI: OpenAI Responses 拒绝 < 16

  # Provider capability flags (借鉴 PI supportsMaxOutputTokens)
  PROVIDER_SUPPORTS_MAX_OUTPUT = {
      "openai_responses": True,
      "azure_responses": True,
      "google": True,
      "anthropic": True,
      "openai_chat": True,
      "openai_chat_legacy": True,
      # Codex-protocol gateways 等: False (要在 .models 里显式配)
  }

  def resolve_max_output_field(provider: str, model: str) -> tuple[str | None, bool]:
      """返回 (field_name, supports) — field_name 是 API 字段名，supports 是是否支持传这字段。"""
      ...
  ```

- [ ] **2.2.4** `models/LLMs/main_llm.py` 调 provider 时（**借鉴 PI 的 if-set-then-pass 模式**，留空不传；**配置时校验 ≥ 8192（8K）**，M9）：
  ```python
  max_output = os.getenv("MAIN_LLM_MAX_OUTPUT_TOKEN", "").strip()
  if max_output:  # 留空 = 不传，用 provider 默认（4096）
      max_output_int = int(max_output)
      # M9: 业务下限 8192/8K（跟 PI 的 OPENAI_RESPONSES_MIN_OUTPUT_TOKENS=16 不同维度：
      # 16 是 provider API 硬下限，8K 是业务下限——thinking 模式 reasoning + answer 共享，
      # 8K 给 reasoning 5K + answer 3K 的最低可用组合，小于 8K 明显截断）
      if max_output_int < 8192:
          raise ValueError(
              f"MAIN_LLM_MAX_OUTPUT_TOKEN={max_output_int} too small (min 8192/8K, "
              "thinking 模式下 reasoning + answer 共享预算，小于 8K 会截断)"
          )
      field, supports = resolve_max_output_field(provider, model)
      if supports and field:
          if provider in ("openai_responses", "azure_responses"):
              # 借鉴 PI: OpenAI Responses 拒绝 < 16,Math.max 提到 16
              params[field] = max(max_output_int, OPENAI_RESPONSES_MIN_OUTPUT_TOKENS)
          elif provider == "google":
              generation_config[field] = max_output_int  # Google 走 generationConfig
          else:
              params[field] = max_output_int
      # provider 不支持时不传，用 provider 默认值
  # 留空时：不传任何字段，让 provider 用自己的默认（OpenAI 兼容 API 默认 4096）
  ```

  **两层下限**：
  - **业务下限 8192/8K**（M9）：在 sherry 启动时（boot 校验）+ 运行时 `build_*_llm` 解析时校验，< 8K abort
  - **API 硬下限 16**（借鉴 PI）：在调 OpenAI Responses provider 时 `Math.max(value, 16)`，避免被 API 拒绝

- [ ] **2.2.5** `.models` hydrate 扩展（跟现有 `*_MAX_TOKEN` 同模式）：注入 `os.environ`，显式 env 优先。

### 2.3 风险与约束

- **PI 的 compat schema 很复杂**：sherry 不需要全盘照搬，只需要 per-provider 字段名映射 + `supports` flag。第一版可以硬编码映射表，后续如果要像 PI 那样从 URL auto-detect，再扩展。
- **OpenAI Responses min 16**：借鉴 PI 的 `OPENAI_RESPONSES_MIN_OUTPUT_TOKENS = 16`，传 max_output < 16 时 Math.max 提到 16。
- **Google 的 maxOutputTokens 走 generationConfig**：不是顶层字段，要特殊处理。
- **Anthropic 的 max_tokens 语义是 output 上限**（不是 context window）——这点容易混淆，要在注释里写清楚。

**验收**：M1 + M3 + M4。新建 `tests/models/test_provider_max_output.py`，断言：
- OpenAI Responses provider 调用时传 `max_output_tokens`（min 16）
- Google provider 调用时传 `generationConfig.maxOutputTokens`
- 不支持的 provider 不传字段
- 未配置 `MAIN_LLM_MAX_OUTPUT_TOKEN` 时不传（用 provider 默认）

---

## 3. 任务二：借鉴 ZCode 改进压缩阈值公式（依赖 §2）

### 3.1 目标

用 §2 的 max_output 字段算 output reserve，对齐 ZCode 的 `effective context window = ctx - outputReserve` 公式。

### 3.2 改动清单

- [ ] **3.2.1** `agent/middlewares/summarization/compression.py:122-127` 的 `_calculate_preserve_budget` 改造：

  ```python
  def _calculate_preserve_budget(self) -> int:
      ctx = self._main_llm_context_window
      if ctx:
          output_reserve = self._resolve_output_reserve()  # 新增
          effective_ctx = max(0, ctx - output_reserve)     # 借鉴 ZCode: 先扣 output reserve
          budget = int(effective_ctx * PRESERVE_RATIO)     # 再 × 0.25
          return min(MAX_PRESERVE_TOKENS, max(MIN_PRESERVE_TOKENS, budget))
      return MIN_PRESERVE_TOKENS

  def _resolve_output_reserve(self) -> int:
      """借鉴 ZCode getAutoCompactOutputReserveTokens.
      优先读 MAIN_LLM_MAX_OUTPUT_TOKEN;未配置时退化到 0 (M3: 旧行为)。"""
      from models.LLMs.main_llm import get_max_output_tokens  # 新增 accessor
      max_output = get_max_output_tokens()
      if max_output and max_output > 0:
          return min(max_output, self._main_llm_context_window or max_output)
      return 0  # M3: 未配置时不扣,退化到 ctx × 0.25 的旧行为
  ```

- [ ] **3.2.2** `overflow.py` 的 threshold 计算同步改造（如果有类似 `ctx × ratio` 的地方）
- [ ] **3.2.3** 加 buffer 概念（借鉴 ZCode `AUTOCOMPACT_BUFFER_TOKENS = 13_000`）：threshold = effective_ctx × ratio - buffer。第一版可以不加 buffer，先只扣 output reserve。

### 3.3 风险与约束

- **M3 退化语义**：未配置 `MAIN_LLM_MAX_OUTPUT_TOKEN` 时，`_resolve_output_reserve` 返回 0，公式退化到 `ctx × 0.25`——现有行为不变。
- **MAX_PRESERVE_TOKENS = 15000 仍然 clamp**：即使 effective_ctx 很大，保留预算仍然 clamp 到 15000。这是 sherry 现有设计，不动。
- **overflow threshold 改动影响大**：overflow route 的 4 路决策都基于 threshold，改公式要跑全套 overflow 测试。

**验收**：M2 + M3。新建 `tests/agent/middlewares/summarization/test_preserve_budget_with_output_reserve.py`：
- 配置 `MAIN_LLM_MAX_OUTPUT_TOKEN=32000` + `MAIN_LLM_MAX_TOKEN=131072` → effective_ctx = 99K → budget = 24K（clamped to 15K）
- 未配置 → budget = 131072 × 0.25 = 32K（clamped to 15K）—— 跟现状一致

---

## 4. 任务三：借鉴 ZCode microcompact（本地清理旧 tool result，不调 LLM）

### 4.1 目标

在 autoCompact（LLM 摘要）之前加一层 microcompact：本地清理旧 tool result（替换成 `[Old tool result content cleared]`），不调 LLM。触发条件：时间（60 min idle）或 token 压力（达 autoCompact threshold × 0.9）。

### 4.2 改动清单

- [ ] **4.2.1** 新建 `agent/middlewares/summarization/microcompact.py`（借鉴 ZCode `microcompact.ts`）：
  - `maybe_local_microcompact(messages, config, last_assistant_completed_at_ms)` 函数
  - 找出对话里**旧的 tool result**（默认保留最近 5 个，`DEFAULT_MICROCOMPACT_KEEP_RECENT_TOOL_RESULTS = 5`）
  - 替换 content 成 `[Old tool result content cleared]`
  - 不调 LLM,纯本地操作
  - 节省 < 256 token 就不做(`DEFAULT_MICROCOMPACT_MIN_TOKEN_SAVINGS = 256`)

- [ ] **4.2.2** 触发条件(借鉴 ZCode `resolveMicrocompactTrigger`):
  - `TimeBased`:距上次 assistant 完成超过 60 min(`DEFAULT_MICROCOMPACT_IDLE_THRESHOLD_MINUTES = 60`)
  - `TokenPressure`:估算 token >= microcompact threshold
    - microcompact threshold = `min(autoCompact_threshold × 0.9, autoCompact_threshold - 2000)`

- [ ] **4.2.3** compactable tool 名单(借鉴 ZCode `DEFAULT_MICROCOMPACT_COMPACTABLE_TOOLS`):
  - sherry 的工具:`read_file` / `terminal` / `python_repl` / `web_search` / `web_fetch` / `grep` / `glob` / `patch_file` / `write_file`
  - **不 compact**:HITL 工具、question 工具、taskflow 工具(这些结果可能还需要)

- [ ] **4.2.4** 集成到 `_run_non_llm_strategies`(`compression.py`):在 read_file 切片 + media offload 之后,加 microcompact 这一层。

### 4.3 风险与约束

- **媒体内容保护**:借鉴 ZCode `hasMediaToolResultContent`,image/video/file 块不清理(`microcompact.ts:258-265`)
- **已经 cleared 的不重复清理**:借鉴 ZCode `isMicrocompactClearedToolResultContent`
- **M5 不破坏现有非 LLM 策略**:microcompact 是新加层,read_file 切片/media offload 仍跑

**验收**:M5。新建 `tests/agent/middlewares/summarization/test_microcompact.py`。

---

## 5. 任务四:借鉴 ZCode UserRequested(`/compact` 命令)

### 5.1 目标

加用户主动触发压缩的能力,对应 ZCode 的 `StandaloneTurn` + `UserRequested`。

### 5.2 改动清单

- [ ] **5.2.1** 新增 LangChain tool `compact_now`(或 `force_compact`):
  ```python
  @tool("compact_now")
  async def compact_now(session_id: str) -> str:
      """用户主动触发对话压缩。对应 ZCode StandaloneTurn + UserRequested。"""
      # 强制跑一次 _aapply_compression_under_lock,bypass anti-thrash
  ```
- [ ] **5.2.2** 或者作为 client 命令(`/compact` 输入)→ server endpoint → 调 compression middleware 的 force 方法
- [ ] **5.2.3** trajectory event 标 `phase=standalone_turn` + `reason=user_requested`(跟 §响应式压缩 TODO 的 CompactPhase 枚举对齐)
- [ ] **5.2.4** bypass anti-thrash(用户主动触发不计冷却、不计 turn attempt,跟 T4/T5 forced-recovery 同语义)

### 5.3 风险与约束

- **M6 不破坏自动压缩**:`/compact` 是独立路径,T1-T5 自动压缩不受影响
- **用户体验**:压缩完要给用户反馈(例如"已压缩 N 条消息,节省 X token")
- **bypass anti-thrash 的滥用风险**:用户频繁 `/compact` 可能抖动——加用户侧 cooldown(例如 30 秒内不能再次 `/compact`)

**验收**:M6。

---

## 6. 任务五:借鉴 ZCode ModelDownshift(切模型时压缩)

### 6.1 目标

用户通过 ThinkingControl 切到 ctx 更小的模型时,如果对话历史超过新 ctx,触发压缩把历史压到新 ctx 内。对应 ZCode 的 `CompactReason.ModelDownshift`。

**这对 sherry 尤其关键**——sherry 支持 per-session 切模型(`ThinkingControlMiddleware` + `PUT /sessions/model`),用户切到小 ctx 模型(比如从 128K Claude 切到 32K DeepSeek)时,历史可能超新 ctx,直接炸。

### 6.2 改动清单

- [ ] **6.2.1** `agent/middlewares/thinking_control/core.py` 切模型时检查:
  ```python
  new_ctx = new_model_profile.context_window
  current_tokens = estimate_messages_tokens(current_messages)
  if current_tokens > new_ctx * 0.80:  # 超新 ctx 80%
      # 触发 ModelDownshift 压缩
      await force_compact_for_downshift(session_id, new_ctx)
  ```
- [ ] **6.2.2** `force_compact_for_downshift` 调 `_aapply_compression_under_lock`,但 preserve_budget 用新模型的 ctx 算
- [ ] **6.2.3** trajectory event 标 `phase=pre_request` + `reason=model_downshift`
- [ ] **6.2.4** 切模型前给用户提示:"新模型上下文窗口较小,将压缩对话历史"(可选)

### 6.3 风险与约束

- **M7 不破坏 ThinkingControl**:压缩是辅助,不替换模型切换逻辑;切完后压缩一次,不持续干扰
- **新模型 ctx 未知**:如果新模型 profile 没声明 context_window,跳过 ModelDownshift(用旧 ctx)
- **切完模型后第一次 turn 可能立即触发 autoCompact**:ModelDownshift 压缩后如果还不够,第一次 turn 的 wrap_model_call 会再触发 T2——这是合理的兜底,不要拦截

**验收**:M7。新建 `tests/agent/middlewares/thinking_control/test_model_downshift_compact.py`。

---

## 7. 执行顺序与依赖

```
§2 借鉴 PI 加 max_output 字段(基础设施,0.5-1 天)——最先做
  │
  ↓ (§3 依赖 §2 的 max_output 字段)
§3 借鉴 ZCode 改进压缩阈值公式(0.5 天)
  │
  ↓ (§4 独立,但跟 §3 同主题)
§4 借鉴 ZCode microcompact(1-1.5 天)
  │
§5 借鉴 ZCode UserRequested /compact 命令(1 天,独立)
  │
§6 借鉴 ZCode ModelDownshift(0.5-1 天,依赖 §3 的阈值公式)

总工程量:3.5-5 天
```

**关键路径**:§2 是基础设施,必须最先做。§3 依赖 §2。§4/§5/§6 可以并行(§6 弱依赖 §3)。

**跟其他 TODO 的关系**:
- 跟 `TODO/计划_响应式压缩对齐ZCode_ReactivePhase.md` 正交(那个改 phase 标记,这个改能力)
- 跟 `TODO/计划_TaskFlow决策LLM独立配置.md` 任务 B(压缩 LLM 独立配置)正交——本 TODO 不动 LLM 选择,只动 max_output 字段和压缩能力
- 两个 TODO 可以并行做,但 §2 的 max_output 字段建议跟任务 B 的 `COMPRESSION_LLM_*` 字段一起加(都在 `.models.example` 改)

---

## 8. 验收标准

### 8.1 硬约束验收

- [ ] **M1**:现有 `MAIN_LLM_MAX_TOKEN` 字段语义不变(仍是 context window);新字段 `MAIN_LLM_MAX_OUTPUT_TOKEN` 是追加。
- [ ] **M2**:T1-T5 压缩行为在 max_output 未配置时 byte-identical(M3 退化)。
- [ ] **M3**:未配置 max_output 时,`_resolve_output_reserve` 返回 0,公式退化到 `ctx × 0.25`。
- [ ] **M4**:provider 不支持 max_output 时(如 Codex-protocol gateway)不传字段。
- [ ] **M5**:microcompact 不破坏现有非 LLM 策略(read_file 切片 / media offload 仍跑)。
- [ ] **M6**:`/compact` 命令不影响自动压缩。
- [ ] **M7**:ModelDownshift 不破坏 ThinkingControl。
- [ ] **M8**:新枚举跟 `TODO/计划_响应式压缩对齐ZCode_ReactivePhase.md` §2 的 CompactPhase/CompactReason 对齐。
- [ ] **M9**:`MAIN_LLM_MAX_OUTPUT_TOKEN` < 8192 时启动 abort;运行时 `build_*_llm` 解析到 < 8192 也 abort;调用 OpenAI Responses provider 时 `Math.max(value, 16)` 满足 API 硬下限。

### 8.2 功能验收

- [ ] 配置 `MAIN_LLM_MAX_OUTPUT_TOKEN=32768` 后,OpenAI Responses provider 调用时传 `max_output_tokens=32768`(≥ 16,API 硬下限满足;≥ 8192,业务下限满足)。
- [ ] 配置 `MAIN_LLM_MAX_OUTPUT_TOKEN=4096` 时启动 abort,报错信息含"min 8192"和"thinking 模式"说明(M9)。
- [ ] 配置 `MAIN_LLM_MAX_OUTPUT_TOKEN=8` 时也 abort(8 < 8192,业务下限优先于 API 硬下限——先在 sherry 层 abort,不到 provider 调用阶段)。
- [ ] Google provider 调用时传 `generationConfig.maxOutputTokens=32768`。
- [ ] 配置 max_output 后,`_calculate_preserve_budget` 用 `(ctx - output_reserve) × 0.25` 算 budget。
- [ ] microcompact 在 token 达到 autoCompact threshold × 0.9 时触发,清理旧 tool result。
- [ ] microcompact 保留最近 5 个 tool result,清理更早的。
- [ ] `/compact` 命令触发后,对话被压缩,trajectory event 标 `phase=standalone_turn reason=user_requested`。
- [ ] 切到 ctx 更小的模型时,如果历史超新 ctx 80%,触发 ModelDownshift 压缩。

### 8.3 回归验收

- [ ] 现有 `tests/agent/middlewares/summarization/` 全套通过。
- [ ] 现有 `tests/models/` 全套通过。
- [ ] ThinkingControl 测试 byte-identical(M7)。
- [ ] 现有 `.models` 配置(无 max_output 字段)升级后不报错(M1)。

---

## 9. 不做的事(边界)

| 项 | 不做的原因 |
|---|---|
| 全盘照搬 PI compat schema | PI 的 schema 很复杂(URL auto-detect / per-model override / session affinity),sherry 只需要 per-provider 字段名映射 + supports flag,第一版硬编码 |
| 流式过程中触发压缩 | sherry 和 ZCode 都不支持,体验代价大,另起 TODO 评估 |
| 区分 `legacy` / `preflight-v1` 两种 budget strategy | ZCode 有这两种模式,sherry 第一版只做 `preflight-v1`(扣 output reserve),不做 `legacy` 模式 |
| ZCode 的 `MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000` | 这是 ZCode 给摘要自身 output 的上限,sherry 摘要走 auxiliary_llm,有自己的 max_token,不需要单独限制 |
| ZCode 的 circuit_breaker(`MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3`) | sherry 已有 `MAX_OVERFLOW_RETRIES` 跟这个同构,不重复加 |
| ZCode 的 `hasEnoughMessagesToCompact` 检查 | sherry 用 token 估算判断,不需要消息数检查 |

---

## 10. 关键参考

### sherry 内部(改造对象)

- `.models.example:18, 33, 40`(`*_MAX_TOKEN` 字段,无 max_output)
- `models/LLMs/main_llm.py:28-97, 338`(`max_tokens` 用法)
- `agent/middlewares/summarization/compression.py:122-127`(`_calculate_preserve_budget`)
- `agent/middlewares/summarization/overflow.py:67-88`(overflow threshold)
- `agent/middlewares/summarization/compression.py:171-173`(`_run_non_llm_strategies`,microcompact 集成点)
- `agent/middlewares/thinking_control/core.py`(ModelDownshift 集成点)

### PI 参考(per-provider max_output)

- `D:/selfProj/pi/packages/ai/src/api/openai-responses.ts:32-33, 93, 340-341`(`OPENAI_RESPONSES_MIN_OUTPUT_TOKENS=16` + `supportsMaxOutputTokens` 检查 + `max_output_tokens` 字段映射)
- `D:/selfProj/pi/packages/ai/src/api/google-generative-ai.ts:382-383`(`generationConfig.maxOutputTokens`)
- `D:/selfProj/pi/packages/ai/src/api/azure-openai-responses.ts:18, 206-207`(Azure 同 OpenAI Responses)
- `D:/selfProj/pi/packages/ai/src/providers/compat-schema.ts:218-219, 399-404`(`maxTokensField` 字段选择 + `supportsMaxOutputTokens` capability flag)

### ZCode 参考(压缩能力)

- `ZCode-main/.../packages/core/src/compact/policy.ts:6-14, 67-88, 90-155`(`outputReserve` 公式 + `shouldAutoCompact` 5 reason)
- `ZCode-main/.../packages/core/src/compact/microcompact.ts:77-81, 176-200`(microcompact threshold + 2 trigger)
- `ZCode-main/.../packages/core/src/compact/microcompact.ts:12-29`(常量:keep_recent=5 / idle=60min / min_savings=256 / threshold_ratio=0.9 / buffer=2000)
- `ZCode-main/.../packages/contracts/src/compact/index.ts:41-48`(`CompactReason` 四值)

### 关联 TODO

- `TODO/计划_响应式压缩对齐ZCode_ReactivePhase.md`(Reactive phase 显式化,正交)
- `TODO/计划_TaskFlow决策LLM独立配置.md`(任务 B 压缩 LLM 独立配置,正交;§2 max_output 字段建议跟任务 B 的 COMPRESSION_LLM_* 一起加)
