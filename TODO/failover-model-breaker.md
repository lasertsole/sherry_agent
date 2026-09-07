# 模型调用断路器 + 三方案联动架构

> 方案 5（模型调用断路器）+ 三方案联动 + 实现步骤 + 配置汇总

## 4. 方案 5：模型调用断路器

### 4.1 新文件：`agent/middlewares/model_circuit_breaker.py`

```python
"""模型调用断路器中间件。

跟踪连续模型调用失败次数，到阈值后断路。
参考 openclaw session-observer.ts：MAX_CONSECUTIVE_FAILURES = 2。

与现有中间件的关系：
- ToolGuardrails 检测工具循环 → 本中间件检测模型循环
- IterationBudget 硬上限 → 本中间件是更细粒度的模型级检测
- 两者互补，不冲突
"""
from __future__ import annotations
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from langchain.agents.middleware import AgentMiddleware
from runtime.state_register import state_register_mem

logger = logging.getLogger(__name__)

_MODEL_CB_STATE_KEY = "model_circuit_breaker_state"


@dataclass
class ModelCircuitBreakerConfig:
    max_consecutive_failures: int = 2
    reset_on_success: bool = True
    cooldown_s: int = 30


@dataclass
class _ModelCircuitBreakerState:
    consecutive_failures: int = 0
    is_tripped: bool = False
    tripped_at: float = 0.0
    tripped_reason: str = ""
    blocked_providers: set[str] = field(default_factory=set)


class ModelCircuitBreaker(AgentMiddleware):
    """
    模型调用断路器中间件。

    工作原理：
    1. 每次 LLM 调用后检查结果
    2. 成功 → 重置失败计数
    3. 失败 → 递增失败计数
    4. 达到 max_consecutive_failures → 断路
       - 断路后，后续模型调用直接返回错误消息
       - 与方案 1 的冷却探针联动：跳过冷却中的 provider
    5. 每个 turn 开始时重置（before_agent）

    状态存储：state_register_mem（与 ToolGuardrails 相同的内存 KV 存储）
    重置时机：before_agent 钩子（每个 turn 开始时）
    """

    def __init__(self, config: ModelCircuitBreakerConfig | None = None):
        self.config = config or ModelCircuitBreakerConfig()

    def _get_session_id(self, state: dict) -> str:
        session_id = state.get("session_id", "")
        if not session_id.strip():
            raise RuntimeError("ModelCircuitBreaker: session_id is required")
        return session_id

    def _get_state(self, session_id: str) -> _ModelCircuitBreakerState:
        return state_register_mem.get_state(
            session_id, _MODEL_CB_STATE_KEY, _ModelCircuitBreakerState()
        )

    def _save_state(self, session_id: str, st: _ModelCircuitBreakerState) -> None:
        state_register_mem.set_state(session_id, _MODEL_CB_STATE_KEY, st)

    def _before_agent_impl(self, state: dict) -> None:
        session_id = self._get_session_id(state)
        state_register_mem.set_state(
            session_id, _MODEL_CB_STATE_KEY, _ModelCircuitBreakerState()
        )
        logger.debug("ModelCircuitBreaker: 状态已重置 (session=%s)", session_id)

    def before_agent(self, state, runtime) -> dict | None:
        self._before_agent_impl(state)
        return None

    def abefore_agent(self, state, runtime) -> dict | None:
        self._before_agent_impl(state)
        return None

    def record_success(self, session_id: str, provider: str = "") -> None:
        if not self.config.reset_on_success:
            return
        st = self._get_state(session_id)
        st.consecutive_failures = 0
        st.is_tripped = False
        st.tripped_reason = ""
        st.blocked_providers.discard(provider)
        self._save_state(session_id, st)

    def record_failure(
        self, session_id: str, provider: str = "", reason: str = "",
    ) -> bool:
        """记录模型调用失败，递增失败计数。返回 True 表示已跳闸。"""
        st = self._get_state(session_id)

        if st.is_tripped:
            if provider:
                st.blocked_providers.add(provider)
            self._save_state(session_id, st)
            return True

        st.consecutive_failures += 1

        if st.consecutive_failures >= self.config.max_consecutive_failures:
            st.is_tripped = True
            st.tripped_at = time.time()
            st.tripped_reason = reason
            if provider:
                st.blocked_providers.add(provider)
            logger.warning(
                "ModelCircuitBreaker: 断路器跳闸 (failures=%d, reason=%s)",
                st.consecutive_failures, reason,
            )
            self._save_state(session_id, st)
            return True

        logger.warning(
            "ModelCircuitBreaker: 连续失败 %d/%d (provider=%s, reason=%s)",
            st.consecutive_failures, self.config.max_consecutive_failures,
            provider, reason,
        )
        self._save_state(session_id, st)
        return False

    def is_blocked(self, session_id: str, provider: str = "") -> bool:
        st = self._get_state(session_id)
        if not st.is_tripped:
            return False
        if provider and provider in st.blocked_providers:
            return True
        return not provider
```

### 4.2 修改文件：`agent/core.py`

在 `middleware` 列表中注册 `ModelCircuitBreaker`：

```python
# 修改 agent/core.py 第 116-137 行
# 在 IterationBudget 之后、ToolGuardrails 之前插入

middleware=[
    ContextEngineHook(),
    MultimodalProcessor(),
    IterationBudget(90),
    ModelCircuitBreaker(),                    # 【新增】模型断路器
    ToolGuardrails(),
    ToolCallNormalize(),
    SubagentCompletionDrainMiddleware(),
    OutputRepetitionGuard(),
    HeartbeatStaleness(),
    HumanInTheLoop(HITLConfig()),
    Summarization(...),
],
```

### 4.3 与 FallbackChatModel 联动

在 `fallback.py` 的 `FallbackChatModel` 中新增断路器联动：

```python
class FallbackChatModel(BaseChatModel):
    # ... 已有字段 ...
    circuit_breaker: Optional["ModelCircuitBreaker"] = None

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        session_id = kwargs.pop("_session_id", "")
        provider = getattr(self.primary, "model_provider", "primary")

        # 前置检查：断路器是否已跳闸
        if self.circuit_breaker and session_id:
            if self.circuit_breaker.is_blocked(session_id, provider):
                return ChatResult(generations=[ChatGeneration(
                    message=AIMessage(content="模型断路器已跳闸，请稍后重试")
                )])

        try:
            result = self._try_fallback_chain(messages, stop, run_manager, **kwargs)
            if self.circuit_breaker and session_id:
                self.circuit_breaker.record_success(session_id, provider)
            return result
        except FailoverError as e:
            if self.circuit_breaker and session_id:
                self.circuit_breaker.record_failure(
                    session_id, provider=provider,
                    reason=str(e.primary_reason or "all_candidates_exhausted"),
                )
            raise
```

### 4.4 与现有中间件的协调

| 中间件                    | 与 ModelCircuitBreaker 的关系                         |
| ------------------------- | ----------------------------------------------------- |
| `IterationBudget(90)`     | 互补：预算是总迭代上限，断路器是连续模型失败检测      |
| `ToolGuardrails()`        | 互补：ToolGuardrails 检测工具循环，断路器检测模型循环 |
| `OutputRepetitionGuard()` | 独立：检测文本输出循环                                |
| `HeartbeatStaleness()`    | 互补：心跳检测无进展Agent，断路器是更细粒度模型级     |

**执行顺序**：

```
before_agent: [所有中间件重置状态]
    ↓
模型调用: ModelCircuitBreaker 前置检查 → FallbackChatModel 回退链 → 后置记录
    ↓ (模型成功)
工具调用: ToolGuardrails 拦截
    ↓ (模型失败 N 次)
ModelCircuitBreaker 断路 → 返回错误 → ToolGuardrails 可能 BLOCK 相关工具
```

---

## 5. 三方案联动架构

### 5.1 调用链路图

```
用户消息 → agent.astream()
  │
  ├─ before_agent: ModelCircuitBreaker 重置 + ToolGuardrails 重置
  │
  ├─ 模型调用 (LangGraph 调用 model.invoke)
  │    │
  │    ├─ ⑤ ModelCircuitBreaker 前置检查
  │    │    ├─ 已断路 → 返回错误 AIMessage（不执行调用）
  │    │    └─ 未断路 → 放行
  │    │
  │    ├─ ① FallbackChatModel._generate/_agenerate
  │    │    │
  │    │    ├─ 尝试 primary model
  │    │    │    ├─ 成功 → 重置断路器计数 → 返回结果
  │    │    │    └─ 失败 → ② classify_error() 分类
  │    │    │         │
  │    │    │         ├─ retryable=True (rate_limit/timeout/server_error)
  │    │    │         │    └─ 退避重试 primary (max_retries=2)
  │    │    │         │         ├─ 重试成功 → 重置断路器 → 返回
  │    │    │         │         └─ 重试耗尽 → 标记冷却 → 切换候选
  │    │    │         │
  │    │    │         ├─ retryable=False (auth/billing/model_not_found)
  │    │    │         │    └─ 标记冷却 → 立即切换候选
  │    │    │         │
  │    │    │         └─ reason=context_overflow
  │    │    │              └─ 不切换模型 → 触发上下文压缩 → 重试
  │    │    │
  │    │    ├─ 尝试 candidate 1 (跳过冷却中的)
  │    │    ├─ 尝试 candidate 2 (跳过冷却中的)
  │    │    └─ 全部耗尽 → FailoverError
  │    │
  │    ├─ ⑤ ModelCircuitBreaker 后置记录
  │    │    ├─ 成功 → record_success (重置计数)
  │    │    └─ 失败 → record_failure (递增计数)
  │    │         └─ 达阈值 → 断路 (is_tripped=True)
  │    │
  │    └─ 返回 AIMessage
  │
  ├─ 工具调用 (如果有)
  │    └─ ToolGuardrails 拦截（独立工作）
  │
  └─ ... (后续模型+工具循环)
```

### 5.2 场景走查

#### 场景 A：rate_limit（可重试）

```
1. primary (openai) 调用 → 429 RateLimitError
2. classifier → reason=rate_limit, retryable=True, cooldown=30s
3. 退避 1s → 重试 primary → 仍然 429
4. 退避 2s → 重试 primary → 仍然 429
5. 重试耗尽 → cooldown_registry.mark_cooldown("openai", 30)
6. 切换 candidate 1 (deepseek) → 成功
7. record_success → 断路器计数重置
8. 返回结果
```

#### 场景 B：auth_permanent（不可重试）

```
1. primary (openai) 调用 → 403 Forbidden
2. classifier → reason=auth_permanent, retryable=False, cooldown=3600s
3. 不重试 → cooldown_registry.mark_cooldown("openai", 3600)
4. 切换 candidate 1 (deepseek) → 成功
5. record_success → 断路器计数重置
6. 返回结果
```

#### 场景 C：连续 2 次完全失败

```
1. Turn 1: primary 失败 → candidate 1 失败 → candidate 2 失败 → FailoverError
   record_failure → consecutive_failures=1 (未断路)

2. Turn 2: primary 失败 → candidate 1 失败 → candidate 2 失败 → FailoverError
   record_failure → consecutive_failures=2
   → is_tripped=True → 断路！

3. Turn 3: ModelCircuitBreaker 前置检查 → is_blocked=True
   → 直接返回错误 AIMessage（不执行任何 LLM 调用）
```

#### 场景 D：所有候选耗尽

```
1. primary 失败 → candidate 1 失败 → candidate 2 失败
2. FailoverError(
     message="所有模型候选均已耗尽",
     attempts=[attempt1, attempt2, attempt3],
     primary_reason=FailoverReason.RATE_LIMIT,
   )
3. record_failure → consecutive_failures += 1
4. 上层 (messages.py) 捕获 FailoverError → 返回用户错误消息
```

#### 场景 E：context_overflow

```
1. primary (openai) 调用 → "context_length_exceeded" 错误
2. classifier → reason=context_overflow, retryable=False, cooldown=0
3. 不标记冷却（不是模型的问题）
4. 不切换候选（换模型也溢出）
5. 触发上下文压缩（Summarization 中间件或裁剪消息历史）
6. 压缩后重试 primary → 成功
7. record_success
```

### 5.3 中间件注册顺序

```
中间件注册顺序（agent/core.py）:
1. ContextEngineHook         — 上下文引擎
2. MultimodalProcessor       — 多模态处理
3. IterationBudget(90)       — 迭代预算硬上限
4. ModelCircuitBreaker       — 【新增】模型断路器
5. ToolGuardrails            — 工具循环检测
6. ToolCallNormalize         — 工具调用规范化
7. SubagentCompletionDrainMiddleware — 子代理完成排空
8. OutputRepetitionGuard     — 输出重复防护
9. HeartbeatStaleness        — 心跳过期检测
10. HumanInTheLoop           — 人在回路
11. Summarization            — 摘要压缩

外层包装:
RepetitionGuardWrapper(agent) — 流式实时重复切割
```

---

## 6. 实现步骤

### 6.1 实现阶段

```
第一阶段：分类引擎（方案 2，无外部依赖）
  ├── agent/failover/__init__.py
  ├── agent/failover/signal.py        (FailoverReason, FailoverClassification, FailoverSignal)
  ├── agent/failover/patterns.py      (消息/异常/HTTP/Provider 模式)
  ├── agent/failover/classifier.py    (classify_error 函数)
  ├── agent/failover/error.py         (FailoverError)
  └── 单元测试：各异常类型 → 正确分类

第二阶段：冷却探针（方案 1 依赖方案 2）
  ├── agent/failover/cooldown.py      (CooldownRegistry)
  └── 单元测试：冷却/过期/LRU淘汰

第三阶段：回退包装器（方案 1 依赖冷却）
  ├── models/LLMs/fallback.py         (FallbackChatModel)
  ├── 修改 models/LLMs/main_llm.py   (集成回退链)
  ├── 修改 .env.example               (新增 FALLBACK_LLM_* 变量)
  └── 集成测试：主模型失败 → 自动切换

第四阶段：模型断路器（方案 5 依赖方案 1+2）
  ├── agent/middlewares/model_circuit_breaker.py (ModelCircuitBreaker)
  ├── 修改 agent/core.py              (注册中间件)
  ├── 修改 models/LLMs/fallback.py    (联动断路器)
  └── 集成测试：连续失败 → 断路 → 恢复
```

### 6.2 测试计划

| 测试                               | 类型 | 验证内容                                           |
| ---------------------------------- | ---- | -------------------------------------------------- |
| `test_classifier_openai_429`       | 单元 | openai RateLimitError → rate_limit, retryable=True |
| `test_classifier_auth_403`         | 单元 | 403 → auth_permanent, retryable=False              |
| `test_classifier_context_overflow` | 单元 | context_length_exceeded → context_overflow         |
| `test_cooldown_basic`              | 单元 | mark → is_cooled_down=True → 过期 → False          |
| `test_cooldown_lru_eviction`       | 单元 | 超过 256 key → 淘汰最旧                            |
| `test_fallback_success`            | 集成 | primary 成功 → 不切换                              |
| `test_fallback_retryable`          | 集成 | 429 → 退避重试 → 成功                              |
| `test_fallback_switch`             | 集成 | auth_permanent → 立即切换候选                      |
| `test_fallback_all_exhausted`      | 集成 | 全部失败 → FailoverError                           |
| `test_circuit_breaker_trip`        | 集成 | 连续 2 次失败 → 断路                               |
| `test_circuit_breaker_reset`       | 集成 | 断路后成功 → 重置                                  |

---

## 7. 配置项汇总

### 7.1 环境变量

```env
# --- 方案 1：模型回退 ---
FALLBACK_LLM_1_PROVIDER=deepseek
FALLBACK_LLM_1_NAME=deepseek-chat
FALLBACK_LLM_1_API_KEY=sk-...
FALLBACK_LLM_1_API_BASE=https://api.deepseek.com/v1

FALLBACK_LLM_2_PROVIDER=zhipu
FALLBACK_LLM_2_NAME=glm-4-flash
FALLBACK_LLM_2_API_KEY=...
FALLBACK_LLM_2_API_BASE=https://open.bigmodel.cn/api/paas/v4

FALLBACK_LLM_3_PROVIDER=openrouter
FALLBACK_LLM_3_NAME=meta-llama/llama-3.1-70b-instruct
FALLBACK_LLM_3_API_KEY=sk-or-...
FALLBACK_LLM_3_API_BASE=https://openrouter.ai/api/v1
```

### 7.2 默认值表

| 参数                       | 默认值 | 来源                | 说明                 |
| -------------------------- | ------ | ------------------- | -------------------- |
| `max_retries`              | 2      | FallbackChatModel   | 同模型退避重试次数   |
| `backoff_base_ms`          | 1000   | FallbackChatModel   | 退避基础延迟         |
| `backoff_max_ms`           | 30000  | FallbackChatModel   | 退避最大延迟         |
| `MIN_INTERVAL_S`           | 30     | CooldownRegistry    | 冷却探针最小间隔     |
| `MAX_KEYS`                 | 256    | CooldownRegistry    | 最大跟踪 provider 数 |
| `TTL_S`                    | 86400  | CooldownRegistry    | 条目 TTL (24h)       |
| `max_consecutive_failures` | 2      | ModelCircuitBreaker | 连续失败断路阈值     |
| `reset_on_success`         | True   | ModelCircuitBreaker | 成功时重置计数       |
| `cooldown_s`               | 30     | ModelCircuitBreaker | 断路后冷却时间       |

### 7.3 新增文件清单

```
agent/failover/
├── __init__.py              # 子系统导出
├── signal.py                # FailoverReason / FailoverClassification / FailoverSignal
├── patterns.py              # 消息/异常/HTTP/Provider 模式匹配
├── classifier.py            # classify_error() 分类引擎
├── error.py                 # FailoverError 异常类
└── cooldown.py              # CooldownRegistry 冷却探针

models/LLMs/
└── fallback.py              # FallbackChatModel 回退包装器

agent/middlewares/
└── model_circuit_breaker.py # ModelCircuitBreaker 模型断路器中间件
```

### 7.4 修改文件清单

```
models/LLMs/main_llm.py       # 集成 FallbackChatModel
agent/core.py                 # 注册 ModelCircuitBreaker 中间件
.env.example                  # 新增 FALLBACK_LLM_* 变量
agent/middlewares/__init__.py  # 导出 ModelCircuitBreaker
```
