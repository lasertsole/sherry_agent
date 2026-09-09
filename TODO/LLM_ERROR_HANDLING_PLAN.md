# LLM API 错误处理计划

> 基于 hermes-agent-main 生产验证的框架模式，适配中间件 + 流式双层架构。
> 模块 A-G：错误分类、重试循环、内容安全过滤、断路器、断流诊断、退避、模型回退链。
> 截断增强（模块 H）+ 流式上下文守卫（模块 I）+ 集成/配置/测试见 `LLM_TRUNCATION_CONTEXT_PLAN.md`。

## 目录

1. [现状审计](#1-现状审计)
2. [目标架构（双层：中间件层 + 流式层）](#2-目标架构双层中间件层--流式层)
3. [模块 A：错误分类引擎](#模块-a错误分类引擎)
4. [模块 B：LLM API 重试循环](#模块-bllm-api-重试循环)
5. [模块 C：内容安全过滤检测](#模块-c内容安全过滤检测)
6. [模块 D：跨轮次连续失败断路器](#模块-d跨轮次连续失败断路器)
7. [模块 E：流式断流诊断](#模块-e流式断流诊断)
8. [模块 F：抖动退避工具](#模块-f抖动退避工具)
9. [模块 G：模型回退链](#模块-g模型回退链)
10. 截断增强 + 流式上下文守卫 → `LLM_TRUNCATION_CONTEXT_PLAN.md`

---

## 1. 现状审计

### 1.1 sherry_agent 已有

| 能力                                 | 位置                                                       | 覆盖范围                                                                                           |
| ------------------------------------ | ---------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| T4/T5 溢出恢复循环                   | `agent/middlewares/summarization.py:1105-1176`             | 仅 `payload_too_large` (413) + `context_overflow` 两类                                             |
| 2 类错误分类                         | `pub_func/message/llm_error_classifier.py`                 | 仅 2 分类，无恢复动作提示                                                                          |
| `finish_reason` 跟踪                 | `server/service/stream_dispatch.py:177`, `messages.py:273` | 跟踪 `length`/`max_tokens`，无 `content_filter` 分支                                               |
| `MaxTokensBoostMiddleware`           | `agent/middlewares/max_tokens_boost.py`                    | `finish_reason=length` + tool_calls 截断 boost（模块 H 保持不改动，H.3/H.5 由 LLMRetry 内层拦截）  |
| `StreamTurn._should_text_continue()` | `stream_dispatch.py`                                       | `finish_reason=length` 文本续传                                                                    |
| OpenAI SDK 内建重试                  | `model_config` `max_retries=2`                             | 仅 429/5xx HTTP 层，无应用级分类                                                                   |
| `PeriodicBackoff`                    | `runtime/periodic_backoff.py`                              | 用于 cron/sweeper，未接入 LLM 层                                                                   |
| `RepetitionGuardWrapper`             | `agent/stream_repetition_guard_wrapper.py`                 | 流式层重复检测 + 截断，已证明 wrapper 模式可行                                                     |
| Summarization T1-T3                  | `agent/middlewares/summarization.py`                       | T1(before_agent) + T2(wrap_model_call) + T3(post-response)，但 T3 修改被丢弃 + T2 anti-thrash gate |

### 1.2 sherry_agent 缺失（对照 hermes-agent + TTBP + CLGW）

| 缺口                               | 参考来源                                              | 影响                                            |
| ---------------------------------- | ----------------------------------------------------- | ----------------------------------------------- |
| LLM API 重试循环                   | hermes `conversation_loop.py:1122-3300`               | 网络抖动/超时直接冒泡终止 turn                  |
| 完整错误分类 (~20 种)              | hermes `error_classifier.py` `FailoverReason`         | 无法区分可重试/应回退/应压缩                    |
| `ClassifiedError` 动作提示         | hermes `error_classifier.py:78`                       | 调用方需自行判断恢复策略                        |
| `jittered_backoff()`               | hermes `retry_utils.py`                               | 退避无抖动，多 session 并发时雷鸣               |
| 内容安全过滤检测                   | hermes `chat_completion_helpers.py:3195-3228`         | `content_filter` 误入空响应重试循环             |
| 跨轮次连续失败断路器               | hermes `chat_completion_helpers.py:193-237`           | 提供者持续不可达时无限等待                      |
| 流式断流诊断                       | hermes `stream_diag.py`                               | 无法定位断流原因                                |
| 模型回退链                         | hermes `conversation_loop.py:1498-1573`               | 主模型挂了不切换备用                            |
| 异常链展开                         | hermes `stream_diag.py:89`                            | LangChain 包装的异常看不到根因                  |
| Thinking-budget 主动膨胀           | TTBP Phase A, hermes `anthropic_adapter.py:2663`      | thinking 启用时 max_tokens 不足                 |
| Reasoning-only 截断检测 + 专用重试 | TTBP Phase B, hermes `conversation_loop.py:1820-1867` | 纯思考截断走文本续写 → 空转                     |
| Subagent boost 对齐                | TTBP Phase C                                          | subagent 工具截断无法自动 boost                 |
| reasoning_tokens 追踪              | TTBP Phase D, hermes `normalize_usage()`              | thinking token 消耗不可观测                     |
| 网络断流 vs 输出截断区分           | hermes `PARTIAL_STREAM_STUB_ID`                       | 网络断流误为 max_tokens 截断                    |
| Dropped tool names 提示            | hermes `chat_completion_helpers.py:2473-2480`         | 流中断时静默丢弃未完成的 tool-call              |
| 流中断 content-filter → 回退       | hermes `chat_completion_helpers.py:3195-3228`         | 中流安全过滤走续传循环                          |
| 流式层真实 token 检测              | CLGW 防御一                                           | T3 检测溢出但修改被丢弃，T2 被 anti-thrash 挡住 |
| 流式层 output 预算截断             | CLGW 防御二                                           | 超长输出未被截断，加剧下轮 context 膨胀         |

---

## 2. 目标架构（双层：中间件层 + 流式层）

```
┌──────────────────────────────────────────────────────────────────────────┐
│                        LLM 调用韧性体系                                   │
│                                                                          │
│  ┌─ 流式层（Wrapper，agent.astream 外层拦截）─────────────────────────┐ │
│  │                                                                    │ │
│  │  RepetitionGuardWrapper (内层: 重复检测 + 截断)                     │ │
│  │    ↓ 透传 usage_metadata                                            │ │
│  │  模块 I: ContextLimitGuardWrapper (外层: 流式上下文守卫)             │ │
│  │    ├─ 防御一: model-call 边界 force-compress (真实 input_tokens)    │ │
│  │    │   → 写 _FORCE_RECOVERY_KEY → T2 绕过 anti-thrash gate           │ │
│  │    ├─ 防御二: mid-stream output 预算截断 (超 20% 窗口停止 forward)   │ │
│  │    └─ 模块 E: per-attempt HTTP 元数据 + 异常链展开                   │ │
│  │    ↓                                                                │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  ┌─ 中间件层（per model call）──────────────────────────────────────┐   │
│  │                                                                  │   │
│  │  模块 D: StaleStreakCircuitBreaker (跨 turn 连续失败断路)        │   │
│  │    ↓                                                              │   │
│  │  模块 B: LLMRetryMiddleware (while retry_count < max_retries)    │   │
│  │    │   ├─ 模块 A: classify_api_error(exc) → ClassifiedError     │   │
│  │    │   ├─ 模块 F: jittered_backoff(retry_count)                 │   │
│  │    │   ├─ 模块 C: content_filter 检测 (finish_reason)             │   │
│  │    │   ├─ 现有: T4/T5 溢出恢复 (Summarization)                   │   │
│  │    │   ├─ 模块 H: MaxTokensBoost 扩展                             │   │
│  │    │   │   ├─ H.1 thinking budget 主动膨胀 (TTBP Phase A)        │   │
│  │    │   │   ├─ H.2 thinking-budget 耗尽检测 (TTBP Phase B)        │   │
│  │    │   │   ├─ H.3 网络断流 vs 输出截断区分                        │   │
│  │    │   │   ├─ H.4 dropped tool names 提示                        │   │
│  │    │   │   ├─ H.5 流中断 content-filter → 回退                   │   │
│  │    │   │   ├─ H.6 reasoning-only 专用重试 prompt                 │   │
│  │    │   │   ├─ H.7 subagent boost 对齐 (TTBP Phase C)             │   │
│  │    │   │   └─ H.8 reasoning_tokens 追踪 (TTBP Phase D)          │   │
│  │    │   └─ 模块 G: 模型回退链 (可选，候选配置时启用)               │   │
│  │    ↓                                                              │   │
│  └──────────────────────────────────────────────────────────────────┘   │
│                                                                          │
│  ┌─ 流式 chunk 处理（StreamTurn，per model call 内部）──────────────┐  │
│  │                                                                  │  │
│  │  模块 C: content_filter finish_reason 分支                      │  │
│  │  模块 H.2: _has_reasoning / _has_visible_text flag 设置          │  │
│  │  模块 H.3: _is_partial_stream_stub 标记                          │  │
│  │  模块 H.4: _partial_tool_names 跟踪                               │  │
│  │  模块 H.5: _content_filter_terminated 标记                       │  │
│  │  模块 H.8: meta_reasoning_tokens 提取                            │  │
│  │  模块 E: stream_diag_init/capture/summary                         │  │
│  │                                                                  │  │
│  └──────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────┘
```

**设计原则**：

- **双层分离**：流式层（Wrapper）负责 chunk 级监控和真实 token 检测；中间件层负责 per-call 错误分类和重试
- **流式层先于中间件层**：Wrapper 在 `agent.astream()` 外层拦截，能看到中间件看不到的 chunk 流和 `usage_metadata`
- **分类与恢复分离**：`classify_api_error` 只分类返回动作提示，重试循环根据提示决策
- **兼容现有 T1-T5**：新重试循环包裹在 `Summarization` 的 `_execute_with_recovery` 外层
- **增量启用**：模块 G（模型回退链）仅配置了 `FALLBACK_LLM_*` 环境变量时激活

---

## 模块 A：错误分类引擎

### A.1 目标

将 `pub_func/message/llm_error_classifier.py` 从 2 类扩展到 hermes-agent 的完整分类体系，新增 `ClassifiedError` 动作提示。

### A.2 新 `FailoverReason` 枚举

```python
class FailoverReason(enum.Enum):
    """LLM API 错误原因分类 — 决定恢复策略。"""

    # 认证 / 授权
    auth = "auth"  # 瞬时 401/403 — 可刷新/轮转
    auth_permanent = "auth_permanent"  # 刷新后仍 401/403 — 不可恢复

    # 计费 / 配额
    billing = "billing"  # 402 或确认额度耗尽 — 立即切换
    rate_limit = "rate_limit"  # 429 或配额限流 — 退避后重试
    upstream_rate_limit = "upstream_rate_limit"  # 聚合器上游 429 — 切模型不切 key

    # 服务端
    overloaded = "overloaded"  # 503/529 — 退避重试
    server_error = "server_error"  # 500/502 — 退避重试

    # 传输
    timeout = "timeout"  # 连接/读取超时 — 重建 client + 重试
    ssl_cert_verification = "ssl_cert_verification"  # TLS 证书验证失败 — 快速失败

    # 上下文 / 负载
    context_overflow = "context_overflow"  # 上下文过大 — 压缩
    payload_too_large = "payload_too_large"  # 413 — 压缩负载
    image_too_large = "image_too_large"  # 单图超限 — 缩小重试

    # 模型 / 提供商策略
    model_not_found = "model_not_found"  # 404 — 切换模型
    provider_policy_blocked = "provider_policy_blocked"  # 聚合器策略阻断
    content_policy_blocked = "content_policy_blocked"  # 安全过滤 — 不重试

    # 请求格式
    format_error = "format_error"  # 400 — 终止或裁剪重试
    invalid_response = "invalid_response"  # 空响应/格式错 — 重试或切换

    # 兜底
    unknown = "unknown"  # 无法分类 — 退避重试
```

### A.3 `ClassifiedError` 结果

```python
@dataclass
class ClassifiedError:
    """结构化错误分类 + 恢复动作提示。"""

    reason: FailoverReason
    status_code: Optional[int] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    message: str = ""
    error_context: Dict[str, Any] = field(default_factory=dict)

    retryable: bool = True
    should_compress: bool = False
    should_fallback: bool = False

    @property
    def is_auth(self) -> bool:
        return self.reason in {FailoverReason.auth, FailoverReason.auth_permanent}
```

### A.4 分类管道（8 步优先级排序）

参考 `hermes-agent-main/agent/error_classifier.py:599-850`。

```python
def classify_api_error(
    exc: BaseException,
    *,
    provider: str = "",
    model: str = "",
    approx_tokens: int = 0,
    context_length: int = 200000,
    num_messages: int = 0,
) -> ClassifiedError:
    """8 步优先级管道：
    1. 特殊 case 模式（content_filter, thinking_signature 等）
    2. HTTP 状态码映射
    3. 异常错误码映射
    4. 异常消息模式匹配
    5. SSL/TLS 告警模式
    6. 服务端断连模式
    7. 传输层启发式（httpx 异常类名）
    8. 兜底 → unknown
    """
```

### A.5 模式表

```python
_HTTP_STATUS_MAP = {
    400: FailoverReason.format_error,
    401: FailoverReason.auth,
    402: FailoverReason.billing,
    403: FailoverReason.auth,
    404: FailoverReason.model_not_found,
    408: FailoverReason.timeout,
    413: FailoverReason.payload_too_large,
    429: FailoverReason.rate_limit,
    500: FailoverReason.server_error,
    502: FailoverReason.server_error,
    503: FailoverReason.overloaded,
    504: FailoverReason.timeout,
    529: FailoverReason.overloaded,
}

_EXCEPTION_TYPE_MAP = {
    "TimeoutError": FailoverReason.timeout,
    "asyncio.TimeoutError": FailoverReason.timeout,
    "ConnectionError": FailoverReason.timeout,
    "ConnectionRefusedError": FailoverReason.timeout,
    "ConnectionResetError": FailoverReason.timeout,
    "ssl.SSLError": FailoverReason.ssl_cert_verification,
    "ssl.SSLCertVerificationError": FailoverReason.ssl_cert_verification,
    "openai.APITimeoutError": FailoverReason.timeout,
    "openai.APIConnectionError": FailoverReason.timeout,
    "openai.AuthenticationError": FailoverReason.auth,
    "openai.PermissionDeniedError": FailoverReason.auth_permanent,
    "openai.RateLimitError": FailoverReason.rate_limit,
    "openai.InternalServerError": FailoverReason.server_error,
    "openai.NotFoundError": FailoverReason.model_not_found,
    "httpx.ConnectTimeout": FailoverReason.timeout,
    "httpx.ReadTimeout": FailoverReason.timeout,
    "httpx.RemoteProtocolError": FailoverReason.server_error,
}

_MESSAGE_PATTERNS = {
    FailoverReason.context_overflow: [
        "context_length_exceeded",
        "maximum context length",
        "context length",
        "context window",
        "input length exceeds",
        "reduce the length",
        "too many tokens",
    ],
    FailoverReason.content_policy_blocked: [
        "content_policy_violation",
        "content filter",
        "safety",
        "refusal",
        "content_filter",
    ],
    FailoverReason.overloaded: ["overload", "capacity", "server is overloaded"],
    FailoverReason.billing: [
        "billing",
        "payment",
        "insufficient balance",
        "quota exceeded",
        "credit",
    ],
    FailoverReason.rate_limit: ["rate limit", "too many requests", "throttling", "rate_limit"],
    FailoverReason.timeout: ["timeout", "timed out", "deadline exceeded", "etimedout"],
    FailoverReason.ssl_cert_verification: [
        "ssl",
        "certificate",
        "certificate_verify_failed",
        "cert verification",
    ],
    FailoverReason.model_not_found: ["model not found", "model not available", "unknown model"],
}

_RECOVERY_MATRIX = {
    FailoverReason.auth: dict(retryable=True, should_compress=False, should_fallback=False),
    FailoverReason.auth_permanent: dict(
        retryable=False, should_compress=False, should_fallback=True
    ),
    FailoverReason.billing: dict(retryable=False, should_compress=False, should_fallback=True),
    FailoverReason.rate_limit: dict(retryable=True, should_compress=False, should_fallback=False),
    FailoverReason.upstream_rate_limit: dict(
        retryable=False, should_compress=False, should_fallback=True
    ),
    FailoverReason.overloaded: dict(retryable=True, should_compress=False, should_fallback=False),
    FailoverReason.server_error: dict(retryable=True, should_compress=False, should_fallback=False),
    FailoverReason.timeout: dict(retryable=True, should_compress=False, should_fallback=False),
    FailoverReason.ssl_cert_verification: dict(
        retryable=False, should_compress=False, should_fallback=True
    ),
    FailoverReason.context_overflow: dict(
        retryable=False, should_compress=True, should_fallback=False
    ),
    FailoverReason.payload_too_large: dict(
        retryable=False, should_compress=True, should_fallback=False
    ),
    FailoverReason.image_too_large: dict(
        retryable=True, should_compress=False, should_fallback=False
    ),
    FailoverReason.model_not_found: dict(
        retryable=False, should_compress=False, should_fallback=True
    ),
    FailoverReason.provider_policy_blocked: dict(
        retryable=False, should_compress=False, should_fallback=True
    ),
    FailoverReason.content_policy_blocked: dict(
        retryable=False, should_compress=False, should_fallback=True
    ),
    FailoverReason.format_error: dict(
        retryable=False, should_compress=False, should_fallback=False
    ),
    FailoverReason.invalid_response: dict(
        retryable=True, should_compress=False, should_fallback=False
    ),
    FailoverReason.unknown: dict(retryable=True, should_compress=False, should_fallback=False),
}
```

### A.6 向后兼容

```python
PAYLOAD_TOO_LARGE = FailoverReason.payload_too_large.value
CONTEXT_OVERFLOW = FailoverReason.context_overflow.value


def classify_provider_error(exc: BaseException) -> str | None:
    """向后兼容包装。"""
    classified = classify_api_error(exc)
    if classified.reason in (FailoverReason.payload_too_large, FailoverReason.context_overflow):
        return classified.reason.value
    return None
```

### A.7 流式层集成

**流式层不需要直接调用分类器** — 分类发生在异常 catch 时，而异常冒泡到中间件层的 `wrap_model_call`。但流式层需要：

1. **流中断时标记异常类型**（模块 E/H.3/H.5）：流中断时捕获异常，调用 `classify_api_error` 判断是否 `content_policy_blocked`，在 `response_metadata` 中打标
2. **`finish_reason=content_filter` 时通知中间件**（模块 C）：通过 `state_register_mem` 传递

### A.8 改动文件

| 文件                                       | 操作     | 说明                        |
| ------------------------------------------ | -------- | --------------------------- |
| `pub_func/message/llm_error_classifier.py` | **重写** | 从 103 行扩展到完整分类引擎 |

---

## 模块 B：LLM API 重试循环

### B.1 目标

在 `Summarization` 中间件的 T4/T5 恢复循环外层包一层通用重试循环，处理非溢出类错误。

### B.2 新文件：`agent/middlewares/llm_retry.py`

```python
"""LLM API 重试中间件 — 通用错误重试循环。

参考 hermes-agent conversation_loop.py:1122-3300。

与现有中间件的关系：
- 包裹在 Summarization 的 _execute_with_recovery 外层
- Summarization 继续处理 T4/T5 溢出类错误
- 本中间件处理：timeout, rate_limit, overloaded, server_error, invalid_response, unknown
- content_policy_blocked 不重试（确定性拒绝）
- ssl_cert_verification 不重试（确定性握手失败）
"""


class LLMRetryMiddleware(AgentMiddleware):
    def __init__(
        self,
        config: LLMRetryConfig | None = None,
        fallback_chain: list[FallbackCandidate] | None = None,
    ):
        self.config = config or LLMRetryConfig()
        self.fallback_chain = fallback_chain or []

    def wrap_model_call(self, request, handler):
        session_id = self._get_session_id(request)
        retry_count = 0
        while retry_count <= self.config.max_retries:
            # D: 断路器检查
            if self._check_stale_giveup(session_id):
                raise RuntimeError("Provider unresponsive — aborting to avoid indefinite stall.")
            try:
                result = handler(request)
                self._reset_stale_streak(session_id)
                return result
            except BaseException as exc:
                classified = classify_api_error(exc, ...)
                if classified.should_compress:
                    raise  # 委托给 Summarization T4/T5
                if not classified.retryable:
                    if classified.should_fallback and self._try_fallback(session_id):
                        retry_count = 0
                        continue
                    raise
                retry_count += 1
                if retry_count > self.config.max_retries:
                    raise
                if classified.reason == FailoverReason.timeout:
                    self._bump_stale_streak(session_id)
                delay = jittered_backoff(retry_count, ...)
                time.sleep(delay)

    async def awrap_model_call(self, request, handler):
        # 同构异步版本
        ...
```

### B.3 流式层集成

**中间件层的重试 vs 流式层的续传 — 边界划分**：

| 场景                                | 处理层                       | 机制                                                      |
| ----------------------------------- | ---------------------------- | --------------------------------------------------------- |
| 异常类错误（timeout, 429, 500）     | 中间件层 B                   | `wrap_model_call` catch → classify → retry/fallback       |
| `finish_reason=length` 文本截断     | 流式层 H.6                   | `StreamTurn._should_text_continue()` → 续传 HumanMessage  |
| `finish_reason=length` + tool_calls | 中间件层 H                   | `MaxTokensBoostMiddleware` boost max_tokens               |
| `finish_reason=content_filter`      | 流式层 C → 中间件层 B        | 流式层标记 → 中间件层检测 → fallback                      |
| 流中断（网络断开 mid-stream）       | 流式层 E → 中间件层 B        | 流式层标记 partial-stream-stub → 中间件层分类异常 → retry |
| 上下文溢出（provider 报错）         | 中间件层 Summarization T4/T5 | `classify_provider_error` → 强制压缩                      |
| 上下文溢出（预测性）                | 流式层 I                     | `usage_metadata.input_tokens` → force-compress            |

### B.4 改动文件

| 文件                             | 操作     | 说明                                                   |
| -------------------------------- | -------- | ------------------------------------------------------ |
| `agent/middlewares/llm_retry.py` | **新建** | 重试循环中间件                                         |
| `agent/core.py`                  | **修改** | 注册 `LLMRetryMiddleware`，位置在 `Summarization` 之后 |

---

## 模块 C：内容安全过滤检测

### C.1 目标

在 `finish_reason == "content_filter"` 时不走空响应重试循环，直接尝试模型回退或终止。

### C.2 中间件层处理

在 `LLMRetryMiddleware` 的 `wrap_model_call` 中，检测 `state_register_mem` 中由流式层设置的 `llm_content_filter_blocked` 标志：

```python
# llm_retry.py — wrap_model_call 中 handler 返回后检查
if state_register_mem.get_state(session_id, "llm_content_filter_blocked", False):
    state_register_mem.set_state(session_id, "llm_content_filter_blocked", False)
    if self._try_fallback(session_id):
        retry_count = 0
        continue
    raise ContentFilterError("Model declined to respond (safety refusal).")
```

### C.3 流式层处理

在 `stream_dispatch.py` 的 `_should_text_continue` 中增加 content_filter 分支：

```python
def _should_text_continue(self) -> bool:
    if self.meta_finish_reason == "content_filter":
        # 通知中间件层
        state_register_mem.set_state(self.session_id, "llm_content_filter_blocked", True)
        return False  # 不续传
    return self.meta_finish_reason in ("length", "max_tokens")
```

### C.4 中流（mid-stream）内容过滤检测

部分 provider（如 MiniMax）在流式输出中途触发安全过滤，表现为：流突然结束，`finish_reason` 可能不是 `content_filter` 而是空或 `stop`。

```python
# stream_dispatch.py — 流式 chunk 循环中，异常 catch 时
_content_filter_keywords = {"new_sensitive", "content_filter", "safety"}
_partial_text = "".join(self._text_chunks)

if (
    self.meta_finish_reason in (None, "stop", "")
    and len(self._text_chunks) > 0
    and any(kw in _partial_text.lower() for kw in _content_filter_keywords)
):
    self.meta_finish_reason = "content_filter"
    state_register_mem.set_state(self.session_id, "llm_content_filter_blocked", True)
```

### C.5 改动文件

| 文件                                | 操作     | 说明                                              |
| ----------------------------------- | -------- | ------------------------------------------------- |
| `server/service/stream_dispatch.py` | **修改** | `_should_text_continue` + 中流检测 + state 标记   |
| `agent/middlewares/llm_retry.py`    | **修改** | 读取 `llm_content_filter_blocked` 标志 → fallback |

---

## 模块 D：跨轮次连续失败断路器

### D.1 目标

跟踪跨 turn 的连续 stale（超时/断流）次数，超阈值后立即中止。

### D.2 中间件层逻辑

内嵌在 `LLMRetryMiddleware` 中：

```python
_LLM_STALE_STREAK_KEY = "llm_stale_streak"
STALE_GIVEUP_THRESHOLD = 5


def _bump_stale_streak(self, session_id): ...
def _reset_stale_streak(self, session_id): ...
def _check_stale_giveup(self, session_id) -> bool: ...
```

### D.3 流式层联动

流式层在以下场景 bump stale streak：

```python
# stream_dispatch.py — _on_stream_error 中
def _on_stream_error(self, error: Exception) -> None:
    classified = classify_api_error(error)
    if classified.reason == FailoverReason.timeout:
        # 递增 stale streak
        current = state_register_mem.get_state(self.session_id, "llm_stale_streak", 0) or 0
        state_register_mem.set_state(self.session_id, "llm_stale_streak", current + 1)
    elif classified.reason == FailoverReason.content_policy_blocked:
        # H.5: content-filter → 标记
        self._content_filter_terminated = True
```

### D.4 改动文件

无新增文件 — 逻辑内嵌在 `LLMRetryMiddleware` + `stream_dispatch.py`。

---

## 模块 E：流式断流诊断

### E.1 目标

流式响应中途断开时，收集诊断信息（HTTP 状态、响应头、chunk 计数、异常链）。

### E.2 新文件：`server/service/stream_diag.py`

```python
"""流式断流诊断 — 每次尝试的 HTTP 元数据 + 异常链展开。

参考 hermes-agent-main/agent/stream_diag.py。
"""

STREAM_DIAG_HEADERS = (
    "cf-ray",
    "cf-cache-status",
    "x-request-id",
    "x-openrouter-provider",
    "x-openrouter-model",
    "server",
    "via",
    "x-vercel-id",
)


def stream_diag_init() -> Dict[str, Any]:
    return {
        "started_at": time.time(),
        "first_chunk_at": None,
        "chunks": 0,
        "bytes": 0,
        "headers": {},
        "http_status": None,
    }


def stream_diag_capture_response(diag, http_response): ...
def flatten_exception_chain(error: BaseException) -> str: ...
def stream_diag_summary(diag, error=None) -> str: ...
```

### E.3 流式层集成

在 `StreamTurn` 的流式 chunk 循环中使用：

```python
# stream_dispatch.py — _run_stream() 中

diag = stream_diag_init()

try:
    # 获取 http_response 后：
    stream_diag_capture_response(diag, http_response)
    # 每个 chunk 后：
    diag["chunks"] += 1
    diag["bytes"] += len(chunk)
    if diag["first_chunk_at"] is None:
        diag["first_chunk_at"] = time.time()
    # ... 现有 yield 逻辑 ...
except Exception as e:
    summary = stream_diag_summary(diag, e)
    logger.error("Stream failed: {}", summary)
    raise type(e)(f"{e} | diag: {summary}") from e
```

### E.4 改动文件

| 文件                                | 操作     | 说明           |
| ----------------------------------- | -------- | -------------- |
| `server/service/stream_diag.py`     | **新建** | 诊断工具函数   |
| `server/service/stream_dispatch.py` | **修改** | 集成 diag 采集 |

---

## 模块 F：抖动退避工具

### F.1 新文件：`pub_func/retry_utils.py`

```python
"""重试退避工具 — 指数退避 + 抖动。参考 hermes-agent retry_utils.py。"""

import random


def jittered_backoff(
    attempt: int, *, base_delay: float = 2.0, max_delay: float = 60.0, jitter: float = 0.3
) -> float:
    exponential = base_delay * (2 ** (attempt - 1))
    capped = min(exponential, max_delay)
    jitter_amount = capped * jitter * (random.random() * 2 - 1)
    return max(0.1, capped + jitter_amount)


def adaptive_rate_limit_backoff(
    attempt: int,
    *,
    retry_after: float | None = None,
    base_delay: float = 5.0,
    max_delay: float = 120.0,
) -> float:
    if retry_after is not None and retry_after > 0:
        return min(retry_after, max_delay)
    return jittered_backoff(attempt, base_delay=base_delay, max_delay=max_delay)
```

### F.2 改动文件

| 文件                      | 操作     | 说明         |
| ------------------------- | -------- | ------------ |
| `pub_func/retry_utils.py` | **新建** | 退避工具函数 |

---

## 模块 G：模型回退链

### G.1 目标

主模型不可用（auth_permanent, billing, ssl_cert, model_not_found, content_policy 等）时自动切换备用模型。

参考 hermes `conversation_loop.py:1498-1573` 的 `_try_activate_fallback()`。复用 `failover-model-fallback.md` 中的 `CooldownRegistry` 设计。

### G.2 中间件层实现

在 `LLMRetryMiddleware` 中注入回退链：

```python
@dataclass
class FallbackCandidate:
    provider: str
    model_name: str
    model: Any


class LLMRetryMiddleware(AgentMiddleware):
    def __init__(self, config=None, fallback_chain=None):
        self.fallback_chain = fallback_chain or []

    def _try_fallback(self, session_id: str) -> bool:
        idx = state_register_mem.get_state(session_id, "llm_fallback_index", 0) or 0
        if idx >= len(self.fallback_chain):
            return False
        candidate = self.fallback_chain[idx]
        state_register_mem.set_state(session_id, "llm_fallback_index", idx + 1)
        # ... 切换模型 ...
        return True
```

### G.3 流式层联动

当流式层检测到 content_filter（模块 C/H.5）或网络断流（模块 H.3）时，通过 `state_register_mem` 标记，中间件层检测到后调用 `_try_fallback`。

### G.4 配置

```env
FALLBACK_LLM_1_PROVIDER=deepseek
FALLBACK_LLM_1_NAME=deepseek-chat
FALLBACK_LLM_1_API_KEY=sk-...
FALLBACK_LLM_1_API_BASE=https://api.deepseek.com/v1
```

### G.5 改动文件

| 文件                             | 操作     | 说明                                       |
| -------------------------------- | -------- | ------------------------------------------ |
| `agent/middlewares/llm_retry.py` | **修改** | 增加 `FallbackCandidate` + `_try_fallback` |
| `models/LLMs/main_llm.py`        | **修改** | `build_main_llm()` 构建回退候选列表        |
| `agent/core.py`                  | **修改** | 将回退链传入 `LLMRetryMiddleware`          |

---

> 模块 H（截断增强）、模块 I（流式上下文守卫）、集成注册、配置项、实施顺序、测试计划已拆分至 `LLM_TRUNCATION_CONTEXT_PLAN.md`。