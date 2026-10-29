# 计划：启用 Anthropic Prompt Caching（`cache_control` 断点）

- 建档日期：2026-10-29
- 状态：**待评审**
- **文档范围**：为 Anthropic（Claude）provider 启用显式 prompt caching——补 `langchain-anthropic` 依赖声明、在请求层注入 `cache_control` 断点、接通 cache 读写计费。让 sherry 既有的"冻结系统提示词"架构在 Anthropic 上也产生缓存收益（目前完全空转）。
- **硬约束**：仅 Anthropic provider 生效（DeepSeek/OpenAI 自动缓存不受影响）；断点位置随 sherry 既有失效点走（系统提示词冻结 / agent-config 失效 / 压缩重建）；cache 读写费用必须计入 TokenBudget。
- 关联代码：
  - sherry 改造对象：
    - `models/LLMs/main_llm.py:243-276`（`_build_inner_chat_model_from`——Anthropic 客户端构建点，挂载 cache 包装器的位置）
    - `models/LLMs/family_llm.py:80-111`（`build_family_llm`——TaskFlow/compression 家族同样的构建点）
    - `models/LLMs/reasoning_normalizer.py:88-164`（`NormalizingChatModel`——既有包装层范式参考）
    - `agent/middlewares/system_prompt/core.py:60-77`（`system_prompt_injection`——系统提示词三级缓存 + 同内容跳过，BP1 的内容来源）
    - `agent/middlewares/thinking_control/core.py:305-353`（切换时失效 SYSTEM_PROMPT——与 cache 断点的交互）
    - `agent/middlewares/token_budget/`（`TokenBudget`——cache 读写费用累计）
    - `context_engine/store/core.py:51-201`（已读 `cache_read_input_tokens` / `cache_creation_input_tokens`，计费输入已就绪）
    - `config/features/agent_side/llm_retry.py`、`config/models_settings.py`（`.models` 配置面）
  - Pi 参考实现：
    - `pi/packages/ai/src/api/anthropic-messages.ts:79-93`（`getCacheControl`——`{type:"ephemeral", ttl?:"1h"}`）
    - `pi/packages/ai/src/api/anthropic-messages.ts:1157-1267`（`buildParams`——4 个断点放置）
    - `pi/packages/ai/src/api/anthropic-messages.ts:1519-1545`（最后一条消息打 cache_control）
    - `pi/packages/ai/src/api/anthropic-messages.ts:1649`（最后一个 tool 打 cache_control）
    - `pi/packages/ai/src/api/anthropic-messages.ts:197-209`（`DEFERRED_TOOL_PLACEHOLDER`——tool 变更不破缓存的占位符）

---

## 0. 背景（已核实）

### 0.1 sherry 现状：Anthropic 缓存从未启用

全仓 `grep cache_control` / `prompt_caching` / `ephemeral`（排除无关的 "ephemeral port/session"）在模型构建里**零命中**。`build_main_llm` 的 `model_config` 只装 `provider/model/api_key/base_url` + reasoning payload + `max_tokens`，经 `clean_client_kwargs` 直送 `init_chat_model(**config)`（`main_llm.py:100-138,271`）。

Anthropic 的 prompt caching **要求显式在 message block / system / tool 上打 `cache_control: {"type":"ephemeral"}` 断点**，不打就没有缓存。所以 sherry 对 Anthropic **从来没有前缀缓存可破坏**——`context_engine/store/core.py:66` 那行读 `cache_read_input_tokens` 的代码对 Anthropic 一直读到 ~0。

既有的"冻结系统提示词 / 冻结记忆快照 / 稳定内容系统提示词 / DynamicReminder 下沉到消息层"整套架构（详见 `workspace/prompt_builder.py:471-485` 注释）是为**自动缓存 provider**（DeepSeek server 端自动、OpenAI 2024-10 起自动）设计的。在 Anthropic 上这套保护完全空转。

### 0.2 更大的缺口：`langchain-anthropic` 依赖未声明

`pyproject.toml` 声明了 `langchain-deepseek`/`langchain-openai`/`langchain-classic`/`langchain-tavily`/`langchain-mcp-adapters`，**唯独没有 `langchain-anthropic`**。即使用户在 `.models` 配 `MAIN_LLM_PROVIDER: "anthropic"`，`init_chat_model(model_provider="anthropic")` 也会因找不到集成包而失败。但 `models/LLMs/reasoning_payload.py:47` 有 `anthropic -> thinking + budget_tokens` 分支，证明 Anthropic 是**预期支持**的 provider，只是依赖声明和缓存都没补。

**已验证**：`langchain_anthropic 1.7.2` 对 `cache_control` 有完整支持（32 处引用，覆盖 content blocks、tool_result 子块、hoisting）。集成层就绪，只差 sherry 侧挂载。

### 0.3 Pi 的做法（参考）

Pi 在 `anthropic-messages.ts` 里放 **4 个 `cache_control` 断点**（Anthropic 上限 4）：

| 断点 | 位置 | 缓存内容 |
|---|---|---|
| BP1 | 系统提示词 text block（`:1234-1243`）| 系统提示词（persona + 指令）|
| BP2 | 最后一个 tool 定义（`:1649`，`index === tools.length - 1`）| tool schemas |
| BP3 | 最后一条对话消息的末 content block（`:1519-1545`）| 整段对话历史 |
| BP4 | `DEFERRED_TOOL_PLACEHOLDER`（`:197-209,1261`）| tool 变更的 hidden scaffolding |

外加：
- **TTL 配置**：`PI_CACHE_RETENTION` env → `short`（5min，默认）/ `long`（1h，`ttl:"1h"`，写费 2x 但存活更久）；`getCacheControl()`（`:79-93`）
- **Session affinity header**：`x-session-affinity`（Anthropic）/ `x-session-id`（OpenRouter），把请求路由到同一缓存分片（`:1071-1075`）
- **计费读取**：`cache_read_input_tokens` / `cache_creation_input_tokens` / `cache_creation.ephemeral_1h_input_tokens`（`:715-717,868-880`）

### 0.4 sherry 与 Pi 的结构差异（决定断点放置）

| 维度 | Pi | sherry |
|---|---|---|
| 系统提示词稳定性 | 每次请求重算 | **冻结**（`_WORKSPACE_STATE_KEY` + `_system_prompt_snapshot`，session 内字节稳定）|
| tool 变更 | 原生 `tool_addition`/`tool_removal`（mid-convo）| `ToolSelectionMiddleware` 按 session 配置收窄，session 内稳定（agent-config 落地才变，且已失效 SYSTEM_PROMPT）|
| 消息前缀注入 | — | `DynamicReminderMiddleware` 每轮首轮在 user msg 前插 `<system-reminder>`（内容变才注）|

sherry 的系统提示词比 Pi **更适合做缓存锚**（冻结 vs 每次重算）。Pi 的 BP4（deferred-tool 占位符）是为原生 tool 变更设计的，sherry 没有 mid-convo 原生 tool 变更，**不需要 BP4**。

---

## 1. ⚠️ 硬约束

| 约束 | 含义 | 验收方式 |
|---|---|---|
| **C1**：仅 Anthropic 生效 | 非 Anthropic provider（DeepSeek/OpenAI/OpenRouter-非Claude）零行为变化 | DeepSeek/OpenAI 请求 body 无 `cache_control` 字段 |
| **C2**：断点随既有失效点走 | 系统提示词冻结失效（ThinkingControl 切换 / agent-config / 压缩）时，BP1 自然 miss，不额外保活 | 切换轮 `cache_read` 归零（符合既有"缓存本就冷"语义）|
| **C3**：cache 费用计入 TokenBudget | cache 读按折扣价、cache 写按溢价计入 `TOKEN_BUDGET_COST` | 启用缓存后 TokenBudget 成本反映 cache_read 折扣 |
| **C4**：不破坏既有消息流 | cache_control 注入不改消息内容、不改中间件链顺序、不改 state | 注入前后消息文本 byte-identical（仅追加 `cache_control` 元数据）|
| **C5**：fail-open | cache_control 注入失败不影响生成 | 注入异常时透传原始消息 |

---

## 2. 断点放置策略（核心设计决策）

Anthropic 上限 4 个 `cache_control` 断点，缓存前缀**累积**——每个断点缓存从开头到该点的全部内容。sherry 放 **3 个高价值断点**，第 4 个留储备：

```
[SystemMessage: 冻结 persona + memory + skills index]  ← BP1（最稳定，session 内字节不变）
[tools: 按 session 配置收窄的 tool 列表]                ← BP2（session 内稳定，agent-config 才变）
[对话历史: 历史 user/ai/tool 轮]                        ← BP3（历史尾部，稳定边界）
[DynamicReminder <system-reminder> 块]                  ← 不打断点（每轮可能变）
[当前 user message]                                     ← 不打断点（本轮新输入）
```

| 断点 | 打在 | 缓存价值 | 失效时机 |
|---|---|---|---|
| **BP1** | `SystemMessage` 转 content block，末 block 加 `cache_control` | **最高**——冻结 persona+memory+skills，session 内几乎不变 | ThinkingControl 切换重载记忆 / agent-config 改 skills / 压缩重建（均已有失效机制）|
| **BP2** | 最后一个 tool 定义加 `cache_control` | 高——tool schemas session 内稳定 | agent-config 改 tool 集合（已失效 SYSTEM_PROMPT，BP2 同步失效）|
| **BP3** | 上一轮最后一条 AI/tool 消息（当前轮注入前的稳定历史边界）加 `cache_control` | 高——缓存整段对话历史 | 每轮自然推进（新历史 = 旧历史 + 一轮，BP3 命中旧历史段）|

**为什么 BP3 打在"上一轮尾部"而不是"最后一条消息"**：sherry 的 `DynamicReminderMiddleware` 在 user msg 前插 reminder 块，reminder 内容每轮可能变。如果把 BP3 打在最后一条消息（reminder 或 user msg），每轮都 miss。打在**上一轮的稳定尾部**（最后一条 AI 或 tool_result），缓存的是"本轮之前已经定型的历史"，reminder 和 user msg 作为新输入自然 miss——这正是想要的边界。

---

## 3. 任务一：补 `langchain-anthropic` 依赖

### 3.1 目标
让 `init_chat_model(model_provider="anthropic")` 能解析到 `ChatAnthropic`。

### 3.2 改动清单
- [ ] **3.2.1** `pyproject.toml`：`dependencies` 加 `"langchain-anthropic>=1.7.2"`
- [ ] **3.2.2** `config/models_settings.py`：`ensure_models_config_file` 不需要改（provider 是 `.models` 用户配的），但 `.models.example` 的 main family 注释补一行 `// anthropic: 需 langchain-anthropic，启用 cache_control 后系统提示词缓存命中`
- [ ] **3.2.3** 验证：`uv run python -c "from langchain.chat_models import init_chat_model; m = init_chat_model(model='claude-sonnet-4-20250514', model_provider='anthropic', api_key='...')"` 能构造

**验收**：Anthropic provider 可构造，`isinstance(inner, ChatAnthropic)` 为真。

---

## 4. 任务二：cache_control 注入层（核心）

### 4.1 目标
新建一个 Anthropic 专用的 `BaseChatModel` 包装器，在 `_agenerate`/`_astream` 前给 messages 和 tools 注入 `cache_control` 断点，然后委托给 inner `ChatAnthropic`。仅 Anthropic provider 挂载。

### 4.2 挂载点选择

在 `_build_inner_chat_model_from`（`main_llm.py:243-271`）里，当 `effective_provider == "anthropic"` 时，把 `init_chat_model(**config)` 的结果再包一层。**不放在 `NormalizingChatModel` 里**——那层 provider 无关，不该耦合 Anthropic 逻辑。新建独立包装器，和 `NormalizingChatModel` 同级叠加：

```
build_main_llm → NormalizingChatModel(inner=AnthropicCacheControlChatModel(inner=ChatAnthropic))
```

`family_llm.py:80-111` 的 `build_family_llm` 同样在 provider 为 anthropic 时包一层。

### 4.3 改动清单

- [ ] **4.3.1** 新建 `models/LLMs/anthropic_cache.py`，定义 `AnthropicCacheControlChatModel(BaseChatModel)`：
  - `inner: BaseChatModel`（持有一个 `ChatAnthropic`）
  - `_agenerate`/`_astream`：克隆入参 messages，按 §2 策略注入 3 个 `cache_control` 断点，委托 inner
  - `_generate`/`_stream`：同步路径同理（production 走 async，但保持对称）
  - fail-open：注入异常时透传原始 messages
  - 通过 `inner._identifying_params` 透传身份（不破坏 `ThinkingControlMiddleware` 的 profile 指纹）
- [ ] **4.3.2** 注入逻辑（`_inject_cache_breakpoints(messages, tools)`）：
  - **BP1**：找到 `SystemMessage`，把 `content` 从 str 转成 `[{"type":"text","text":<str>,"cache_control":{"type":"ephemeral",...}}]`；若已是 content block 列表，给末 block 追加 `cache_control`
  - **BP2**：tools 列表非空时，给最后一个 tool 的 `additional_kwargs` 或 `args` 注入 `cache_control`（按 langchain-anthropic 1.7.2 的 tool 序列化格式——需实测确认 `bind(tools=...)` 传 dict 形式时 cache_control 透传）
  - **BP3**：从 messages 尾部向前扫描，跳过 `DynamicReminder`/`WorkspaceNotice` 注入的 carrier（用 `pub.func.message.system_reminder.is_system_reminder` / `workspace_notice.is_workspace_notice` 判定），找到第一条"真实历史"消息（AI 或 tool_result），给其末 content block 追加 `cache_control`
  - 断点上限保护：已注入 < 4 个才继续
- [ ] **4.3.3** `main_llm.py:243-271` `_build_inner_chat_model_from`：`effective_provider == "anthropic"` 时，`init_chat_model(**config)` 的返回值包一层 `AnthropicCacheControlChatModel`
- [ ] **4.3.4** `family_llm.py:80-111` `build_family_llm`：同上，provider 为 anthropic 时包一层
- [ ] **4.3.5** provider 判定：复用 `models/LLMs/reasoning_payload.py` 既有 provider 归一逻辑（注意 `openrouter` 路由 Claude 模型时也算——OpenRouter 接受 `cache_control` 透传给后端 Claude，Pi 的 `supportsCacheControlOnTools` compat flag 即此意）

**验收**：Anthropic 请求 body 里 system / 末 tool / 历史尾部消息带 `cache_control`；非 Anthropic 请求 body 无该字段。

---

## 5. 任务三：TTL 与开关配置

### 5.1 目标
让用户能选 5min（默认，便宜）或 1h（贵但存活久）TTL，并能整体关闭。

### 5.2 改动清单
- [ ] **5.2.1** `config/features/` 新增 `anthropic_cache.py`（TypedDict `ANTHROPIC_CACHE`）：
  - `enabled: bool = True`（总开关）
  - `ttl: "short" | "long" = "short"`（short=`{"type":"ephemeral"}`，long=`{"type":"ephemeral","ttl":"1h"}`）
  - `max_breakpoints: int = 4`（Anthropic 上限，防御性）
- [ ] **5.2.2** `.models` 不放此配置（它是 provider 行为开关，不是模型 profile 字段）——放 `sherry.jsonc`（tracked）或 `.env`（`SHERRY_ANTHROPIC_CACHE_TTL`）。**首选 `.env`**（和 `SANDBOX_*` 同类，非敏感）
- [ ] **5.2.3** `AnthropicCacheControlChatModel` 读 `ANTHROPIC_CACHE` 配置构造 `cache_control` 对象

**验收**：`SHERRY_ANTHROPIC_CACHE_TTL=long` 时断点带 `ttl:"1h"`；`enabled=false` 时包装器变透传。

---

## 6. 任务四：cache 读写计费接入 TokenBudget

### 6.1 目标
Anthropic cache 读按 ~0.1x、cache 写按 ~1.25x（5min）/ ~2x（1h）计费，`TokenBudget` 的 `TOKEN_BUDGET_COST` 反映真实成本。

### 6.2 现状
`context_engine/store/core.py:51-201` 已从 response 解析 `cache_read_input_tokens` / `cache_creation_input_tokens`（Anthropic 字段 `cache_read_input_tokens` / `cache_creation_input_tokens`，已在 `_cache_read_tokens_from_usage` 覆盖）。但 `TokenBudget` 的成本估算（`TOKEN_BUDGET_COST`）目前按 `usage_metadata` 的 input/output 统一价累加，**未区分 cache 读折扣 / 写溢价**。

### 6.3 改动清单
- [ ] **6.3.1** `.models` 的 `MODEL_PRICING_TABLE`（JSON string）schema 扩展：每条 model 条目加 `cache_read` / `cache_write` 单价（与 `input`/`output` 同级）；`.models.example` seed 补 Anthropic 模型的 cache 价目（参考 Anthropic 官网定价）
- [ ] **6.3.2** `agent/middlewares/token_budget/` 成本累计逻辑：从 `usage_metadata` 取 `cache_read_input_tokens` / `cache_creation_input_tokens`，分别按 `cache_read` / `cache_write` 单价计入 `TOKEN_BUDGET_COST`；非缓存 `input_tokens` 按 `input` 单价
- [ ] **6.3.3** `GET /sessions/budget` / `TokenBudgetPanel.vue` 展示：cost 拆分为 input / cache_read / cache_write / output 四列（可选，本期可只确保数值正确，展示拆分可后续）

**验收**：启用缓存后，一次 cache 命中的轮次 `TOKEN_BUDGET_COST` 显著低于无缓存轮次（cache_read 折扣生效）。

---

## 7. 任务五：与既有失效机制的交互核对

### 7.1 目标
确认启用 cache_control 后，既有的 3 个系统提示词失效点（§0.1）行为仍然正确，不产生"缓存与实际内容不一致"。

### 7.2 核对清单（不改代码，验证语义）

- [ ] **7.2.1 ThinkingControlMiddleware 切换**（`thinking_control/core.py:305-353`）：切换时重载记忆快照 + 删 SYSTEM_PROMPT → 下一轮系统提示词可能字节变化 → BP1 miss。**正确**——Anthropic cache 锚的是字节前缀，提示词变了就该 miss。无需改。
  - 注意：Anthropic 的 prompt cache 键是 system+tools+messages 的**字节前缀**，不含 thinking budget 等 request 级参数。所以 thinking 切换本身**不**使 Anthropic 缓存失效（与 DeepSeek 不同）——只有 SYSTEM_PROMPT 重建导致字节变化才失效。既有失效逻辑反而更保守（重载了快照），对 Anthropic 是安全方向。
- [ ] **7.2.2 agent_config 落地**（`agent_config_service.py:103-119`）：改 skills/tools → 失效 SYSTEM_PROMPT → BP1（系统提示词里的 skills 索引）和 BP2（tools）同步失效。**正确**。
- [ ] **7.2.3 压缩重建**（`summarization/core.py:349-361`）：`load_from_disk()` 拾起记忆写入 → 系统提示词可能变 → BP1 miss；同时消息列表被压缩重写 → BP3 miss。**正确**——压缩本就是缓存断裂点。
- [ ] **7.2.4 DynamicReminderMiddleware**：每轮首轮在 user msg 前插 reminder，BP3 打在 reminder **之前**的历史尾部，所以 reminder 变化不破 BP3 缓存。**正确**——这是 §2 选"上一轮尾部"而非"最后一条消息"的原因。

**验收**：无代码改动；在核对记录里确认 4 个失效点语义自洽。

---

## 8. 任务六：测试

### 8.1 目标
对照 Pi 的 `anthropic-cache-write-1h-cost.test.ts` / `cache-retention.test.ts`，pin 断点放置和 provider gating。

### 8.2 改动清单
- [ ] **8.2.1** `tests/models/LLMs/test_anthropic_cache.py`：
  - `test_injects_breakpoints_on_anthropic`：构造 Anthropic 包装器，喂一组 messages，断言 system/末tool/历史尾部带 `cache_control`
  - `test_noop_on_non_anthropic`：DeepSeek/OpenAI provider 不挂包装器，请求 body 无 `cache_control`
  - `test_breakpoint_skips_injected_carriers`：messages 含 `DynamicReminder`/`WorkspaceNotice` carrier 时，BP3 跳过它们打在真实历史尾部
  - `test_max_four_breakpoints`：构造超长 messages，断言断点数 ≤ 4
  - `test_failopen_on_injection_error`：注入抛异常时透传原始 messages
  - `test_ttl_long_adds_1h`：`SHERRY_ANTHROPIC_CACHE_TTL=long` 时 `cache_control` 带 `ttl:"1h"`
  - `test_disabled_is_passthrough`：`enabled=false` 时包装器等价透传
- [ ] **8.2.2** `tests/agent/middlewares/test_token_budget.py`：加 cache 读写计费 case（mock `usage_metadata` 带 `cache_read_input_tokens`，断言 cost 按 cache_read 单价）
- [ ] **8.2.3** 不写 llm_e2e（标记 `not llm_e2e`），断点验证用 mock inner

**验收**：`uv run pytest tests/models/LLMs/test_anthropic_cache.py -q` 全绿。

---

## 9. 风险与开放问题

| 项 | 说明 | 处置 |
|---|---|---|
| **langchain-anthropic tool cache_control 透传** | 1.7.2 支持 tool 上的 `cache_control`，但 sherry 用 `init_chat_model(...).bind(tools=...)` 传 tool——需实测 dict 形式 tool 的 cache_control 是否透传到 wire | 任务二 4.3.2 实测；若 `bind(tools=)` 不透传，改用 `additional_kwargs` 或在 `_format_messages` 前介入 |
| **OpenRouter 路由 Claude** | OpenRouter 对 Claude 模型接受 `cache_control` 透传，但行为与原生 Anthropic 不完全一致 | provider 判定把 `openrouter` + Claude 模型也纳入；compat flag `supportsCacheControlOnTools` 跟 Pi 学 |
| **BP3 定位"历史尾部"的扫描成本** | 每轮 model call 从尾部向前扫消息找第一条真实历史 | 消息列表通常 < 数百条，扫描 O(n) 可接受；可缓存上次扫到的 index |
| **Bedrock Claude** | `langchain-anthropic` 不覆盖 Bedrock；Bedrock 走 `langchain-aws`，其 cache_control 支持不同 | 本期不覆盖 Bedrock；provider 判定排除 `bedrock`，后续单独立项 |
| **cache 写费用突增** | 首次写 cache 溢价（1.25x/2x），短会话可能比无缓存更贵 | 默认 `ttl=short`（5min，1.25x）；`TokenBudget` 正确计费后用户可见成本，自行决定 |

---

## 10. 实施顺序

1. 任务一（补依赖）——独立、无风险，先落地
2. 任务二（注入层）——核心，需联调 langchain-anthropic 1.7.2 的实际序列化
3. 任务五（语义核对）——不改代码，确认任务二不与既有失效冲突
4. 任务三（TTL/开关）——任务二验证通过后加配置面
5. 任务四（计费）——可与任务三并行
6. 任务六（测试）——贯穿，每步落地即补对应 case
