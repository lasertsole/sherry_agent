# 计划：借鉴 PI provider 溢出报错对应表（扩展 patterns + NON_OVERFLOW 排除 + silent/length-stop 检测）

- 建档日期：2026-10-19
- 状态：**计划（未开工）**
- **文档范围**：把 sherry 的 7 个字符串 substring patterns 升级到 PI 的 25 个 regex patterns + NON_OVERFLOW 排除表 + silent overflow / length-stop 检测。**是 `TODO/计划_响应式压缩对齐ZCode_ReactivePhase.md` 的前置依赖**——Reactive phase 显式化前必须先补全 classifier，不然 T4/T5 在某些 provider 上根本不触发。
- **硬约束**：**不破坏现有 7 个 patterns 匹配行为 + 不破坏 `classify_provider_error` 签名**，详见 §1
- 关联代码：
  - sherry 现状（改造对象）：
    - `pub/func/message/llm_error_classifier.py:103-111`（`_CONTEXT_OVERFLOW_PATTERNS` 7 个字符串 substring）
    - `pub/func/message/llm_error_classifier.py:389-411`（`classify_provider_error(exc)` 入口）
    - `pub/func/message/llm_error_classifier.py:414-426`（`_exception_chain_texts` 遍历 `__cause__`/`__context__` 链）
    - `pub/func/message/llm_error_classifier.py:429-443`（`_match_special_cases` 现有逻辑：含 `approx_tokens >= context_length` 简化 silent 检测）
    - `pub/func/message/llm_error_classifier.py:99-129`（HTTP 状态码映射 + exception type 映射）
  - PI 参考（对齐目标）：
    - `D:/selfProj/pi/packages/ai/src/utils/overflow.ts:37-63`（25 个 regex `OVERFLOW_PATTERNS`）
    - `D:/selfProj/pi/packages/ai/src/utils/overflow.ts:65`（`CEREBRAS_BODYLESS_OVERFLOW_PATTERN` 特殊处理）
    - `D:/selfProj/pi/packages/ai/src/utils/overflow.ts:76-80`（`NON_OVERFLOW_PATTERNS` 排除表）
    - `D:/selfProj/pi/packages/ai/src/utils/overflow.ts:137-171`（`isContextOverflow` 三种检测方式）
  - 关联 TODO：
    - `TODO/计划_响应式压缩对齐ZCode_ReactivePhase.md`（**前置依赖**——本 TODO 完成后那个才有意义）
    - `TODO/计划_借鉴ZCode_PI_压缩能力补全与max_output字段.md`（正交，但 §3 output reserve 公式依赖本 TODO 的 context_window 检测准确性）

---

## 0. 背景：sherry 现状 vs PI（已核实）

### 0.1 sherry 当前 7 个字符串 substring patterns

`pub/func/message/llm_error_classifier.py:103-111`：

```python
_CONTEXT_OVERFLOW_PATTERNS = (
    "context_length_exceeded",
    "maximum context length",
    "context length",
    "context window",
    "input length exceeds",
    "reduce the length",
    "too many tokens",
)
```

匹配方式：`pattern in lowered`（纯 substring，`lowered` 是 exception 文本小写）。**不是 regex**。

### 0.2 sherry 已有的隐式 silent 检测（简化版）

`_match_special_cases`（`llm_error_classifier.py:437-438`）：

```python
if approx_tokens > 0 and context_length > 0 and approx_tokens >= context_length:
    return FailoverReason.context_overflow
```

`approx_tokens` 是从 exception 文本里**正则提取的数字**（如 "exceeds 131072 tokens" 提取出 131072）。只在 exception 文本含明确 token 数时工作。

### 0.3 sherry 缺什么（vs PI）

| 维度 | sherry | PI |
|---|---|---|
| patterns 数量 | 7 个字符串 substring | 25 个 regex |
| provider 显式覆盖 | 通用（不区分） | 20+ provider 显式 |
| 匹配方式 | `pattern in lowered` | `regex.test(errorMessage)` |
| **silent overflow 检测** | 简化版（exception 文本提取 token 数 vs context_length） | 完整版（`stopReason="stop"` + `usage.input + cacheRead > contextWindow`） |
| **length-stop 检测** | ❌ 没有 | ✅ `stopReason="length"` + `output=0` + `input >= ctx × 0.99`（Xiaomi MiMo） |
| **NON_OVERFLOW 排除表** | ❌ 没有 | ✅ 防 Bedrock throttling 误判 |
| **Cerebras 特殊处理** | ❌ 没有 | ✅ `400/413 (no body)` 当溢出 |
| HTTP 状态码映射 | ✅ 413→payload_too_large | 不靠状态码 |
| exception chain 遍历 | ✅ `_exception_chain_texts`（3 层深） | — |

### 0.4 入口差异：exception vs message（关键设计约束）

PI 的 `isContextOverflow(message, contextWindow)` 接收**完整 assistant message**（有 `stopReason` / `usage` 字段直接访问）。

sherry 的 `classify_provider_error(exc)` 接收**exception 对象**——走 LangChain，exception 不一定带 `stopReason` / `usage` 字段。`_exception_chain_texts` 遍历 `__cause__`/`__context__` 链提取文本，但**不访问 response 对象的 usage**。

**后果**：
- PI 的 silent overflow（`stopReason="stop"` + `usage.input > ctx`）和 length-stop（`stopReason="length"` + `output=0`）检测**不能直接照搬**——sherry 入口没有 stopReason/usage
- 要做完整 silent/length-stop 检测，需要先从 exception 里提取 response 对象（langchain/openai SDK 可能把 response 包在 `APIStatusError.response` 里），再读 usage——工程量比纯 patterns 扩展大
- 第一版可以**只扩展 patterns + 加 NON_OVERFLOW 排除表**，silent/length-stop 完整检测作为后续任务（§4/§5 标"可选"）

### 0.5 后果：T4/T5 在某些 provider 上根本不触发

sherry 7 个 patterns 只能命中 PI 25 个里的约 5-6 个——**70%+ 的 provider 特定报错 sherry 检测不到**：

- 用 z.ai 模型，silent overflow → sherry 不识别 → **不触发 T5** → 后续 turn 继续累积 → 最终真炸
- 用 Xiaomi MiMo，length-stop overflow → sherry 不识别 → **不触发 T5** → 同上
- 用 Bedrock，throttling 报错 → sherry 误判为 T5（`too many tokens` 命中）→ **触发不必要的压缩** → 浪费 LLM 调用

这影响 `TODO/计划_响应式压缩对齐ZCode_ReactivePhase.md` 的可行性——Reactive phase 显式化前必须先补全 classifier。

---

## 1. ⚠️ 硬约束：不破坏现有 classifier 行为

| 约束 | 含义 | 验收方式 |
|---|---|---|
| **P1**：现有 7 个 patterns 匹配行为不变 | 新 patterns 是追加；7 个 substring 保留（不删除，不改 regex 化）——保证老 exception 文本仍命中 | 现有 classifier 测试 byte-identical |
| **P2**：`classify_provider_error(exc)` 签名不变 | 入口仍是 `exc: BaseException -> str \| None`，调用方（`overflow.py:_aexecute_with_recovery`）一行不改 | grep 调用点不变 |
| **P3**：`_exception_chain_texts` 行为不变 | 3 层 `__cause__`/`__context__` 遍历不动；新检测方式如果需要 response 对象，从 exception 的 attribute 里提取（如 `getattr(exc, "response", None)`），不改 chain 遍历 | chain 遍历测试 byte-identical |
| **P4**：HTTP 状态码映射不变 | `_HTTP_STATUS_MAP`（413→payload_too_large 等）不动；新检测方式不替换状态码路径，是补充 | 状态码映射测试 byte-identical |
| **P5**：NON_OVERFLOW 排除表不破坏现有误判逻辑 | sherry 当前没排除表，加排除表后**只可能让原本命中的变成不命中**（更严格），不会让原本不命中的变成命中——这是收紧，不是放宽 | 现有命中 context_overflow 的 case 仍命中（除非匹配 NON_OVERFLOW） |
| **P6**：regex patterns 失败时退化 | 如果 regex 编译失败或匹配异常，退化到 substring 匹配（不抛异常）——classifier 必须 fail-open | regex 编译失败时 classifier 仍工作 |
| **P7**：不破坏 max_output 字段 TODO | 跟 `TODO/计划_借鉴ZCode_PI_压缩能力补全与max_output字段.md` 正交——本 TODO 不动 max_output 字段，只动 error classifier | 两个 TODO 可并行 |

---

## 2. 任务一：扩展 patterns 到 25 个 regex（借鉴 PI `OVERFLOW_PATTERNS`）

### 2.1 目标

把 `_CONTEXT_OVERFLOW_PATTERNS` 从 7 个字符串 substring 升级到 PI 的 25 个 regex + 保留原 7 个 substring（P1 向后兼容）。

### 2.2 改动清单

- [ ] **2.2.1** `pub/func/message/llm_error_classifier.py` 把 `_CONTEXT_OVERFLOW_PATTERNS` 拆成两部分：

  ```python
  # 保留原 7 个字符串 substring（P1 向后兼容，不动）
  _CONTEXT_OVERFLOW_SUBSTRINGS = (
      "context_length_exceeded",
      "maximum context length",
      "context length",
      "context window",
      "input length exceeds",
      "reduce the length",
      "too many tokens",
  )

  # 新增：借鉴 PI 的 25 个 regex patterns
  import re
  _CONTEXT_OVERFLOW_REGEXES: tuple[re.Pattern[str], ...] = (
      re.compile(r"prompt (?:is )?too long", re.IGNORECASE),                    # Anthropic / z.ai
      re.compile(r"prompt exceeds max length", re.IGNORECASE),                  # z.ai CN
      re.compile(r"request_too_large", re.IGNORECASE),                           # Anthropic 413
      re.compile(r"input is too long for requested model", re.IGNORECASE),      # Amazon Bedrock
      re.compile(r"exceeds the context window", re.IGNORECASE),                  # OpenAI
      re.compile(r"exceeds (?:the )?(?:model'?s )?maximum context length(?: of [\d,]+ tokens?|\s*\([\d,]+\))", re.IGNORECASE),  # OpenAI/LiteLLM
      re.compile(r"input token count.*exceeds the maximum", re.IGNORECASE),     # Google Gemini
      re.compile(r"maximum prompt length is \d+", re.IGNORECASE),               # xAI Grok
      re.compile(r"reduce the length of the messages", re.IGNORECASE),          # Groq
      re.compile(r"maximum context length is \d+ tokens", re.IGNORECASE),       # OpenRouter
      re.compile(r"exceeds (?:the )?maximum allowed input length of [\d,]+ tokens?", re.IGNORECASE),  # OpenRouter/Poolside
      re.compile(r"input \(\d+ tokens\) is longer than the model'?s context length \(\d+ tokens\)", re.IGNORECASE),  # Together AI
      re.compile(r"exceeds the limit of \d+", re.IGNORECASE),                   # GitHub Copilot
      re.compile(r"exceeds the available context size", re.IGNORECASE),         # llama.cpp
      re.compile(r"greater than the context length", re.IGNORECASE),            # LM Studio
      re.compile(r"context window exceeds limit", re.IGNORECASE),               # MiniMax
      re.compile(r"exceeded model token limit", re.IGNORECASE),                 # Kimi For Coding
      re.compile(r"too large for model with \d+ maximum context length", re.IGNORECASE),  # Mistral
      re.compile(r"prompt has [\d,]+ tokens?, but the configured context size is [\d,]+ tokens?", re.IGNORECASE),  # DS4
      re.compile(r"model_context_window_exceeded", re.IGNORECASE),             # z.ai non-standard finish_reason
      re.compile(r"prompt too long; exceeded (?:max )?context length", re.IGNORECASE),  # Ollama
      re.compile(r"range of input length should be", re.IGNORECASE),           # DashScope/Qwen
      re.compile(r"context[_ ]length[_ ]exceeded", re.IGNORECASE),             # Generic
      re.compile(r"too many tokens", re.IGNORECASE),                            # Generic（P1：原 substring 也有这条，regex 重复无害）
      re.compile(r"token limit exceeded", re.IGNORECASE),                       # Generic
  )
  ```

- [ ] **2.2.2** `_match_special_cases` 改造匹配逻辑（P1 保留 substring 匹配，追加 regex 匹配）：

  ```python
  def _match_special_cases(status_code, lowered, approx_tokens, context_length):
      if _match_payload_too_large(status_code, lowered):
          return FailoverReason.payload_too_large
      # P1: 保留原 substring 匹配（向后兼容）
      if any(pattern in lowered for pattern in _CONTEXT_OVERFLOW_SUBSTRINGS):
          return FailoverReason.context_overflow
      # 新增:借鉴 PI 的 regex patterns
      try:
          if any(regex.search(lowered) for regex in _CONTEXT_OVERFLOW_REGEXES):
              return FailoverReason.context_overflow
      except Exception:
          # P6: regex 失败时退化到 substring(已跑过),不抛异常
          logger.debug("regex overflow patterns failed, fallback to substring only")
      if approx_tokens > 0 and context_length > 0 and approx_tokens >= context_length:
          return FailoverReason.context_overflow
      # ... 现有 content_filter / multimodal 检测不动
  ```

- [ ] **2.2.3** `re.compile` 在模块加载时执行（不是每次调用 classify 时）——避免重复编译开销。但 P6 要求编译失败时不阻塞 import：用 try/except 包裹整个 `_CONTEXT_OVERFLOW_REGEXES` 构造，失败时退化成空 tuple。

### 2.3 风险与约束

- **P1 向后兼容**：原 7 个 substring 保留，新 regex 是追加——老 exception 文本仍命中
- **P6 fail-open**：regex 编译/匹配失败时退化到 substring，不抛异常
- **重复 patterns**：`too many tokens` 同时在 substring 和 regex 里，无害（重复匹配不影响结果）
- **正则大小写**：全部 `re.IGNORECASE`，跟 PI 一致
- **性能**：25 个 regex 对每次 exception 文本搜索——exception 不频繁，性能不是问题

**验收**：P1 + P6。新建 `tests/pub/func/message/test_overflow_patterns.py`，断言：
- 原 7 个 substring 命中的 case 仍命中（向后兼容）
- PI 文档列的 20+ provider 报错样本都能命中（新增 regex）
- regex 编译失败时退化到 substring

---

## 3. 任务二：加 NON_OVERFLOW 排除表（借鉴 PI `NON_OVERFLOW_PATTERNS`）

### 3.1 目标

防止非溢出错误（如 Bedrock throttling）被误判为 context_overflow。借鉴 PI `NON_OVERFLOW_PATTERNS`。

### 3.2 改动清单

- [ ] **3.2.1** 新增 `_NON_OVERFLOW_REGEXES`（借鉴 PI `utils/overflow.ts:76-80`）：

  ```python
  _NON_OVERFLOW_REGEXES: tuple[re.Pattern[str], ...] = (
      re.compile(r"^(Throttling error|Service unavailable):", re.IGNORECASE),  # AWS Bedrock 非溢出
      re.compile(r"rate limit", re.IGNORECASE),                                # 通用限流
      re.compile(r"too many requests", re.IGNORECASE),                         # HTTP 429 风格
  )
  ```

- [ ] **3.2.2** `_match_special_cases` 加排除逻辑（在 overflow 检测**之前**）：

  ```python
  def _match_special_cases(status_code, lowered, approx_tokens, context_length):
      # 新增:NON_OVERFLOW 排除(在 overflow 检测之前)
      try:
          if any(regex.search(lowered) for regex in _NON_OVERFLOW_REGEXES):
              # 命中排除表:不是 overflow,继续走其他检测(content_filter / multimodal 等)
              pass  # 不 return,让后续检测决定
          else:
              # 不命中排除表,正常走 overflow 检测
              if _match_payload_too_large(status_code, lowered):
                  return FailoverReason.payload_too_large
              if any(pattern in lowered for pattern in _CONTEXT_OVERFLOW_SUBSTRINGS):
                  return FailoverReason.context_overflow
              if any(regex.search(lowered) for regex in _CONTEXT_OVERFLOW_REGEXES):
                  return FailoverReason.context_overflow
      except Exception:
          pass  # P6: regex 失败时退化到原逻辑
      # ... 现有逻辑(approx_tokens / content_filter / multimodal)
  ```

  **关键设计**（借鉴 PI `utils/overflow.ts:140-149`）：NON_OVERFLOW 命中时**不直接 return**，而是跳过 overflow 检测，继续走 content_filter / multimodal 等其他检测——因为一个错误可能既是 throttling 又是 content_filter（虽然罕见）。

### 3.3 风险与约束

- **P5 收紧语义**：NON_OVERFLOW 排除表只会让原本命中的变成不命中（更严格），不会让原本不命中的变成命中——这是收紧，不是放宽
- **Bedrock throttling 测试**：构造 "ThrottlingException: Too many tokens, please wait" 文本，断言不命中 context_overflow

**验收**：P5。新建 `tests/pub/func/message/test_non_overflow_exclusion.py`。

---

## 4. 任务三（可选）：加 silent overflow 完整检测（借鉴 PI Case 2）

### 4.1 目标

借鉴 PI `utils/overflow.ts:152-158` 的 silent overflow 检测：`stopReason="stop"` + `usage.input + cacheRead > contextWindow`。

### 4.2 改动清单

- [ ] **4.2.1** sherry 入口是 exception 不是 message，需要先从 exception 提取 response 对象：

  ```python
  def _extract_usage_from_exception(exc: BaseException) -> dict[str, int] | None:
      """Try to extract usage info from an APIStatusError-like exception.

      langchain/openai SDK 把 response 包在 APIStatusError.response 里,
      response.json() 可能有 'usage' 字段。
      """
      response = getattr(exc, "response", None)
      if response is None:
          return None
      try:
          body = response.json() if hasattr(response, "json") else {}
          usage = body.get("usage") or {}
          if usage:
              return {
                  "input": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
                  "output": int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
                  "cache_read": int(usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)),
              }
      except Exception:
          return None
      return None
  ```

- [ ] **4.2.2** `classify_provider_error` 加 silent overflow 检测分支（在 patterns 检测之后）：

  ```python
  def classify_provider_error(exc: BaseException) -> str | None:
      # ... 现有 classify_api_error 路径
      classified = classify_api_error(exc)
      if classified.reason in (FailoverReason.payload_too_large, FailoverReason.context_overflow):
          return classified.reason.value

      # 新增:silent overflow 检测(provider 接受请求但 input 超过 ctx)
      usage = _extract_usage_from_exception(exc)
      if usage and usage["input"] > 0:
          context_length = _resolve_context_length(exc)  # 从 exc 里提取 model context length
          if context_length and (usage["input"] + usage["cache_read"]) > context_length:
              return FailoverReason.context_overflow.value

      return None
  ```

### 4.3 风险与约束

- **exception 不一定带 response**：langchain 包装的 exception 可能丢失原始 response 对象——`_extract_usage_from_exception` 返回 None 时退化
- **context_length 解析**：需要从 exception 或 model config 里提取 context_length（sherry 的 `MAIN_LLM_MAX_TOKEN`）——这是个独立的解析任务
- **PI 的 silent overflow 是 z.ai 专属**：z.ai 有时接受溢出请求但不报错。sherry 要不要支持 z.ai？要看用户是否用 z.ai provider
- **优先级**：可选，第一版可以只做 §2/§3，silent overflow 完整检测作为后续任务

**验收**：可选。如果做，新建 `tests/pub/func/message/test_silent_overflow.py`。

---

## 5. 任务四（可选）：加 length-stop 检测（借鉴 PI Case 3）

### 5.1 目标

借鉴 PI `utils/overflow.ts:160-168`：`stopReason="length"` + `output=0` + `input >= ctx × 0.99`（Xiaomi MiMo 风格）。

### 5.2 改动清单

- [ ] **5.2.1** 跟 §4.2.1 共用 `_extract_usage_from_exception`，加 finish_reason 检测：

  ```python
  def _extract_finish_reason_from_exception(exc: BaseException) -> str | None:
      """Try to extract finish_reason from exception's response body."""
      response = getattr(exc, "response", None)
      if response is None:
          return None
      try:
          body = response.json() if hasattr(response, "json") else {}
          # OpenAI 风格:choices[0].finish_reason
          choices = body.get("choices") or []
          if choices and isinstance(choices, list):
              return choices[0].get("finish_reason")
          # 其他 provider 的 finish_reason 字段
          return body.get("finish_reason") or body.get("stopReason")
      except Exception:
          return None
  ```

- [ ] **5.2.2** `classify_provider_error` 加 length-stop 检测分支：

  ```python
  # 新增:length-stop overflow 检测(Xiaomi MiMo 风格)
  usage = _extract_usage_from_exception(exc)
  finish_reason = _extract_finish_reason_from_exception(exc)
  if (usage and finish_reason == "length" and usage["output"] == 0
      and context_length and (usage["input"] + usage["cache_read"]) >= context_length * 0.99):
      return FailoverReason.context_overflow.value
  ```

### 5.3 风险与约束

- **finish_reason 字段位置因 provider 而异**：OpenAI 在 `choices[0].finish_reason`，其他 provider 可能不同
- **优先级**：可选，第一版可以只做 §2/§3
- **跟 §4 共享基础设施**：`_extract_usage_from_exception` 同时服务 silent 和 length-stop 检测

**验收**：可选。如果做，新建 `tests/pub/func/message/test_length_stop_overflow.py`。

---

## 6. 任务五：加 Cerebras 特殊处理（借鉴 PI `CEREBRAS_BODYLESS_OVERFLOW_PATTERN`）

### 6.1 目标

借鉴 PI `utils/overflow.ts:65`：Cerebras 返回 `400/413 (no body)` 时当溢出。

### 6.2 改动清单

- [ ] **6.2.1** 新增 `_CEREBRAS_BODYLESS_OVERFLOW_PATTERN`：

  ```python
  _CEREBRAS_BODYLESS_OVERFLOW_PATTERN = re.compile(
      r"^4(?:00|13)\s*(?:status code)?\s*\(no body\)", re.IGNORECASE
  )
  ```

- [ ] **6.2.2** `_match_special_cases` 加 Cerebras 分支（借鉴 PI `utils/overflow.ts:146-148`）：

  ```python
  # Cerebras:无 body 的 400/413 当溢出(借鉴 PI)
  if _CEREBRAS_BODYLESS_OVERFLOW_PATTERN.search(lowered):
      return FailoverReason.context_overflow
  ```

### 6.3 风险与约束

- **provider 识别**：sherry 怎么知道是 Cerebras？需要从 exception 里提取 provider 信息——但 sherry 当前 classifier 不带 provider 上下文。第一版可以**不做 provider 区分**，直接对所有 "400/413 (no body)" 文本匹配——风险是其他 provider 也可能返回这种文本，但概率低。

**验收**：可选。如果做，新建 `tests/pub/func/message/test_cerebras_bodyless.py`。

---

## 7. 执行顺序与依赖

```
§2 扩展 patterns 到 25 个 regex(必做,0.5 天)——基础设施
  │
  ↓
§3 加 NON_OVERFLOW 排除表(必做,0.25 天)
  │
  ↓
§4 silent overflow 完整检测(可选,0.5-1 天,需要 exception response 提取)
§5 length-stop 检测(可选,0.5 天,跟 §4 共享基础设施)
§6 Cerebras 特殊处理(可选,0.25 天)

总工程量:0.75 天(必做)/ 1.75-2.5 天(含可选)
```

**关键路径**：§2 是基础，§3 依赖 §2（排除表要先有 patterns 才有意义）。§4/§5/§6 独立可选。

**跟其他 TODO 的关系**：
- **是 `TODO/计划_响应式压缩对齐ZCode_ReactivePhase.md` 的前置依赖**——Reactive phase 显式化前必须先补全 classifier，不然 T4/T5 在某些 provider 上不触发
- 跟 `TODO/计划_借鉴ZCode_PI_压缩能力补全与max_output字段.md` 正交——本 TODO 不动 max_output 字段，只动 classifier

---

## 8. 验收标准

### 8.1 硬约束验收

- [ ] **P1**：现有 7 个 substring patterns 命中的 case 仍命中（向后兼容）。
- [ ] **P2**：`classify_provider_error(exc)` 签名不变。
- [ ] **P3**：`_exception_chain_texts` 行为不变。
- [ ] **P4**：HTTP 状态码映射不变。
- [ ] **P5**：NON_OVERFLOW 排除表只收紧不放宽。
- [ ] **P6**：regex 编译/匹配失败时退化到 substring。
- [ ] **P7**：跟 max_output 字段 TODO 正交。

### 8.2 功能验收

- [ ] PI 文档列的 20+ provider 报错样本（Anthropic "prompt is too long"、OpenAI "exceeds the context window"、Google "input token count exceeds the maximum"、xAI "maximum prompt length is X"、Groq "reduce the length of the messages"、Together AI "input (X tokens) is longer than the model's context length"、llama.cpp "exceeds the available context size"、Mistral "too large for model with X maximum context length"、DS4 "Prompt has X tokens, but the configured context size is Y tokens"、DashScope "Range of input length should be" 等）都能命中 `context_overflow`。
- [ ] Bedrock throttling 报错 "ThrottlingException: Too many tokens, please wait" 不命中 `context_overflow`（NON_OVERFLOW 排除）。
- [ ] regex 编译失败时（手动破坏 regex）classifier 仍工作（退化到 substring）。

### 8.3 回归验收

- [ ] 现有 `tests/pub/func/message/test_llm_error_classifier.py`（如果有）全套通过。
- [ ] 现有 `tests/agent/middlewares/summarization/` T4/T5 测试 byte-identical（在 patterns 扩展后，原本命中的仍命中）。
- [ ] overflow route 决策测试 byte-identical。

---

## 9. 不做的事（边界）

| 项 | 不做的原因 |
|---|---|
| 全盘照搬 PI `isContextOverflow(message, contextWindow)` 接口 | sherry 入口是 exception 不是 message（§0.4），改入口要动 langchain 集成层，工程量太大 |
| silent overflow / length-stop 完整检测作为必做 | §4/§5 需要从 exception 提取 response 对象，工程量大且不一定可靠（exception 不一定带 response）；第一版只做 §2/§3 |
| 把 7 个 substring 改成 regex | P1 向后兼容——substring 保留，regex 是追加 |
| 区分 `payload_too_large`（T4）和 `context_overflow`（T5）的 patterns | sherry 现状只有 `_CONTEXT_OVERFLOW_PATTERNS` 一个列表，T4 走 HTTP 413 状态码——保留这个分工不动 |
| 给 classifier 加 provider 上下文 | sherry 的 `classify_provider_error(exc)` 入口不带 provider 信息，加 provider 上下文要改入口签名（违反 P2）；第一版靠 patterns 文本匹配，不靠 provider 区分 |
| 上 silent overflow / length-stop 检测的 e2e 测试 | 这俩检测是可选的（§4/§5），e2e 测试需要真 provider 报错样本，太重——第一版只做 unit test |

---

## 10. 关键参考

### sherry 内部（改造对象）

- `pub/func/message/llm_error_classifier.py:103-111`（`_CONTEXT_OVERFLOW_PATTERNS` 7 个 substring）
- `pub/func/message/llm_error_classifier.py:389-411`（`classify_provider_error` 入口）
- `pub/func/message/llm_error_classifier.py:414-426`（`_exception_chain_texts` 遍历）
- `pub/func/message/llm_error_classifier.py:429-443`（`_match_special_cases` 现有逻辑）
- `pub/func/message/llm_error_classifier.py:99-129`（HTTP 状态码 + exception type 映射）
- `agent/middlewares/summarization/overflow.py:700-774`（T4/T5 forced-recovery loop 调用 classifier）

### PI 参考（对齐目标）

- `D:/selfProj/pi/packages/ai/src/utils/overflow.ts:37-63`（25 个 regex `OVERFLOW_PATTERNS`）
- `D:/selfProj/pi/packages/ai/src/utils/overflow.ts:65`（`CEREBRAS_BODYLESS_OVERFLOW_PATTERN`）
- `D:/selfProj/pi/packages/ai/src/utils/overflow.ts:76-80`（`NON_OVERFLOW_PATTERNS` 排除表）
- `D:/selfProj/pi/packages/ai/src/utils/overflow.ts:137-171`（`isContextOverflow` 三种检测方式）
- `D:/selfProj/pi/packages/ai/src/utils/overflow.ts:9-36`（PI docstring 列的 20+ provider 报错样本，作为测试 fixture 来源）

### 关联 TODO

- `TODO/计划_响应式压缩对齐ZCode_ReactivePhase.md`（**前置依赖**——本 TODO 完成后那个才有意义）
- `TODO/计划_借鉴ZCode_PI_压缩能力补全与max_output字段.md`（正交）
