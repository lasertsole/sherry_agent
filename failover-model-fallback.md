# 模型级回退 + 故障转移分类引擎

> 方案 1（模型级回退 + 冷却探针）、方案 2（结构化故障转移分类引擎）

## 1. 背景与目标

### 1.1 sherry 现状

sherry 的 LLM 层目前存在以下防御空白：

| 防御层     | 现状                                                                           | 问题                                  |
| ---------- | ------------------------------------------------------------------------------ | ------------------------------------- |
| LLM 客户端 | `model_config` 中 `max_retries=2`（OpenAI SDK 内建 HTTP 重试）                 | 仅处理 429/5xx 网络错误，无应用级回退 |
| 错误分类   | 子代理交付层有 `classify_delivery_error()`（permanent/transient/unknown 三类） | LLM 层无错误分类                      |
| 模型回退   | 无                                                                             | 主模型挂了不会切换备用模型            |
| 模型断路器 | 无                                                                             | 模型连续返回错误仍会反复调用          |

**关键发现**：`skills/builtin/core/multimodal_rag/scripts/graph_rag/vendored_raganything/resilience.py` 已有完整的 `retry()` / `async_retry()` 装饰器和 `CircuitBreaker` 类，但仅在 vendored skill 内部使用，未接入主 LLM 层。

**调用链路现状**：

```
agent/core.py: built_agent()
  → build_main_llm()  → NormalizingChatModel(inner=init_chat_model(...))
  → create_agent(model=main_llm.bind(temperature), middleware=[...])

调用点（5处）:
  - server/service/messages.py:243   agent.astream()     流式聊天
  - server/service/messages.py:249   agent.ainvoke()     非流式
  - server/service/messages.py:676   agent.astream()     HITL恢复
  - server/service/heartbeat.py:82   agent.invoke()      心跳任务
  - agent/middlewares/context_engine/nudge.py:255  ainvoke()  记忆nudge
```

错误处理现状：`messages.py:538` 的 `except Exception` 直接 `raise`，无 retry 无 fallback。

### 1.2 目标

建立三层联动的 LLM 韧性体系：

```
⑤ ModelCircuitBreaker (前置断路: 连续失败 2 次断路)
  ↓
① FallbackChatModel (回退链: 主模型 → 候选1 → 候选2)
  ↓
② FailoverClassifier (分类: 15 种原因, 决定重试 or 切换)
```

### 1.3 openclaw 参考来源

| 方案 | openclaw 文件                                              | 核心机制                                          |
| ---- | ---------------------------------------------------------- | ------------------------------------------------- |
| 1    | `model-fallback-attempt.ts` + `model-fallback-cooldown.ts` | 多候选回退 + 跨运行冷却探针（30s/256key/24h TTL） |
| 2    | `src/agents/failover/` 整个子系统（~30文件）               | 15 种故障转移分类原因 + 多维度分类                |
| 5    | `session-observer.ts`                                      | `MAX_CONSECUTIVE_FAILURES=2` 连续失败断路         |

---

## 2. 方案 1：模型级回退 + 冷却探针

### 2.1 架构设计

```
build_main_llm(temperature)
  │
  ├─ 构建 primary_model (现有逻辑不变)
  │     └─ NormalizingChatModel(inner=ReasoningChatOpenAI/init_chat_model(...))
  │
  ├─ 构建 fallback_candidates[]
  │     ├─ NormalizingChatModel(inner=init_chat_model(provider=FALLBACK_LLM_PROVIDER, ...))
  │     └─ (可配置多个候选)
  │
  └─ 返回 FallbackChatModel(primary, candidates, cooldown_registry)
        │
        ├─ .bind(temperature=...)        → 委托给 primary.bind()
        ├─ .invoke() / .ainvoke()       → 先试 primary，失败走回退链
        ├─ .bind_tools(...)              → 委托给 primary.bind_tools()
        └─ .with_structured_output(...)  → 委托给 primary.with_structured_output()
```

### 2.2 新文件：`models/LLMs/fallback.py`

```python
"""模型级回退包装器，在主模型失败时自动切换备用模型。

参考 openclaw model-fallback-attempt.ts + model-fallback-cooldown.ts。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

logger = logging.getLogger(__name__)


@dataclass
class FallbackAttempt:
    """单次回退尝试记录。"""
    provider: str
    model: str
    error: Optional[Exception] = None
    reason: str = ""
    timestamp: float = field(default_factory=time.time)
    cooldown_until: float = 0.0


class FallbackChatModel(BaseChatModel):
    """多 provider 回退链包装器。

    调用流程：
    1. 尝试 primary model
    2. 失败 → 调用 FailoverClassifier 分类原因
    3. 可重试 → 退避重试 primary（最多 max_retries 次）
    4. 不可重试或重试耗尽 → 标记冷却 → 切换下一个候选
    5. 所有候选耗尽 → 抛出 FailoverError（携带所有尝试记录）
    """

    primary: BaseChatModel
    candidates: list[BaseChatModel]
    cooldown_registry: "CooldownRegistry"
    max_retries: int = 2
    backoff_base_ms: int = 1000
    backoff_max_ms: int = 30000

    @property
    def _llm_type(self) -> str:
        return "fallback"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        attempts: list[FallbackAttempt] = []
        all_models = [self.primary, *self.candidates]

        for idx, model in enumerate(all_models):
            provider = getattr(model, "model_provider", f"candidate-{idx}")

            if self.cooldown_registry.is_cooled_down(provider):
                logger.warning("模型 %s 处于冷却中，跳过", provider)
                attempts.append(FallbackAttempt(
                    provider=provider,
                    model=getattr(model, "model", "unknown"),
                    reason="cooldown",
                ))
                continue

            for attempt in range(self.max_retries + 1):
                try:
                    result = model._generate(messages, stop, run_manager, **kwargs)
                    self.cooldown_registry.clear(provider)
                    return result
                except Exception as e:
                    from agent.failover.classifier import classify_error
                    classification = classify_error(e, provider=provider)

                    attempts.append(FallbackAttempt(
                        provider=provider,
                        model=getattr(model, "model", "unknown"),
                        error=e,
                        reason=classification.reason.value,
                        cooldown_until=time.time() + classification.cooldown_suggested,
                    ))

                    if not classification.retryable:
                        self.cooldown_registry.mark_cooldown(
                            provider, classification.cooldown_suggested
                        )
                        break

                    if attempt < self.max_retries:
                        delay = min(
                            self.backoff_base_ms * (2 ** attempt),
                            self.backoff_max_ms,
                        ) / 1000
                        logger.warning(
                            "模型 %s 第 %d 次失败 (%s), %.1fs 后重试",
                            provider, attempt + 1, classification.reason.value, delay,
                        )
                        time.sleep(delay)
                    else:
                        self.cooldown_registry.mark_cooldown(
                            provider, classification.cooldown_suggested
                        )

        from agent.failover.error import FailoverError
        raise FailoverError("所有模型候选均已耗尽", attempts=attempts)

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        attempts: list[FallbackAttempt] = []
        all_models = [self.primary, *self.candidates]

        for idx, model in enumerate(all_models):
            provider = getattr(model, "model_provider", f"candidate-{idx}")

            if self.cooldown_registry.is_cooled_down(provider):
                logger.warning("模型 %s 处于冷却中，跳过", provider)
                attempts.append(FallbackAttempt(
                    provider=provider,
                    model=getattr(model, "model", "unknown"),
                    reason="cooldown",
                ))
                continue

            for attempt in range(self.max_retries + 1):
                try:
                    result = await model._agenerate(messages, stop, run_manager, **kwargs)
                    self.cooldown_registry.clear(provider)
                    return result
                except Exception as e:
                    from agent.failover.classifier import classify_error
                    classification = classify_error(e, provider=provider)

                    attempts.append(FallbackAttempt(
                        provider=provider,
                        model=getattr(model, "model", "unknown"),
                        error=e,
                        reason=classification.reason.value,
                        cooldown_until=time.time() + classification.cooldown_suggested,
                    ))

                    if not classification.retryable:
                        self.cooldown_registry.mark_cooldown(
                            provider, classification.cooldown_suggested
                        )
                        break

                    if attempt < self.max_retries:
                        delay = min(
                            self.backoff_base_ms * (2 ** attempt),
                            self.backoff_max_ms,
                        ) / 1000
                        logger.warning(
                            "模型 %s 第 %d 次失败 (%s), %.1fs 后重试",
                            provider, attempt + 1, classification.reason.value, delay,
                        )
                        await asyncio.sleep(delay)
                    else:
                        self.cooldown_registry.mark_cooldown(
                            provider, classification.cooldown_suggested
                        )

        from agent.failover.error import FailoverError
        raise FailoverError("所有模型候选均已耗尽", attempts=attempts)

    def bind_tools(self, tools, **kwargs):
        return self.primary.bind_tools(tools, **kwargs)

    def with_structured_output(self, schema, **kwargs):
        return self.primary.with_structured_output(schema, **kwargs)
```

### 2.3 新文件：`agent/failover/cooldown.py`

```python
"""跨进程冷却探针注册器。

防止在已失败的 provider 上频繁探测。
参考 openclaw model-fallback-cooldown.ts：30s 最小间隔、256 key 上限、24h TTL。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class _CooldownEntry:
    provider: str
    cooldown_until: float
    failure_count: int = 1


class CooldownRegistry:
    """线程安全的 provider 冷却注册器。

    - mark_cooldown(provider, seconds)：标记 provider 冷却 N 秒
    - is_cooled_down(provider)：是否仍在冷却中
    - clear(provider)：清除冷却（成功时调用）
    - 自动 LRU 淘汰：超过 MAX_KEYS 时淘汰最旧的条目
    - 自动 TTL 过期：超过 TTL_S 的条目自动清理
    """

    MIN_INTERVAL_S = 30
    MAX_KEYS = 256
    TTL_S = 86400

    def __init__(self) -> None:
        self._store: dict[str, _CooldownEntry] = {}
        self._lock = threading.Lock()

    def mark_cooldown(self, provider: str, cooldown_s: float) -> None:
        cooldown_s = max(cooldown_s, self.MIN_INTERVAL_S)
        with self._lock:
            self._evict_expired()
            self._evict_overflow()
            if provider in self._store:
                self._store[provider].failure_count += 1
                self._store[provider].cooldown_until = time.time() + cooldown_s
            else:
                self._store[provider] = _CooldownEntry(
                    provider=provider,
                    cooldown_until=time.time() + cooldown_s,
                )

    def is_cooled_down(self, provider: str) -> bool:
        with self._lock:
            entry = self._store.get(provider)
            if entry is None:
                return False
            if time.time() >= entry.cooldown_until:
                del self._store[provider]
                return False
            return True

    def clear(self, provider: str) -> None:
        with self._lock:
            self._store.pop(provider, None)

    def get_failure_count(self, provider: str) -> int:
        with self._lock:
            entry = self._store.get(provider)
            return entry.failure_count if entry else 0

    def _evict_expired(self) -> None:
        now = time.time()
        expired = [
            key for key, entry in self._store.items()
            if now - entry.cooldown_until > self.TTL_S
        ]
        for key in expired:
            del self._store[key]

    def _evict_overflow(self) -> None:
        if len(self._store) <= self.MAX_KEYS:
            return
        sorted_keys = sorted(
            self._store.keys(),
            key=lambda k: self._store[k].cooldown_until,
        )
        while len(self._store) > self.MAX_KEYS:
            del self._store[sorted_keys.pop(0)]


_global_cooldown_registry = CooldownRegistry()


def get_cooldown_registry() -> CooldownRegistry:
    return _global_cooldown_registry
```

### 2.4 修改文件：`models/LLMs/main_llm.py`

现有 `build_main_llm()` 在 `main_llm.py:76-94`，返回 `NormalizingChatModel`。

修改方式：在末尾用 `FallbackChatModel` 包裹。

```python
# --- 新增函数 ---
def _build_fallback_candidates() -> list[BaseChatModel]:
    """从环境变量构建回退候选模型列表。支持 FALLBACK_LLM_1_*, 2_*, 3_*。"""
    candidates = []
    for i in range(1, 4):
        prefix = f"FALLBACK_LLM_{i}_"
        provider = os.getenv(f"{prefix}PROVIDER", "")
        model_name = os.getenv(f"{prefix}NAME", "")
        if not provider or not model_name:
            break
        api_key = os.getenv(f"{prefix}API_KEY", "")
        api_base = os.getenv(f"{prefix}API_BASE", "")
        config = {
            "model_provider": provider,
            "model": model_name,
            "api_key": api_key or None,
            "base_url": api_base or None,
            "max_retries": 2,
            "timeout": 120,
        }
        config = {k: v for k, v in config.items() if v is not None and v != ""}
        try:
            inner = init_chat_model(**config)
            candidates.append(NormalizingChatModel(inner=inner))
            logger.info("已加载回退候选 %d: %s/%s", i, provider, model_name)
        except Exception as e:
            logger.warning("回退候选 %d 加载失败: %s", i, e)
    return candidates


# --- 修改 build_main_llm 末尾 ---
def build_main_llm(temperature: float | None = None) -> BaseChatModel:
    # ... 现有构建 primary 逻辑不变 ...
    primary = NormalizingChatModel(inner=inner)

    # 新增：包装回退链
    candidates = _build_fallback_candidates()
    if candidates:
        from models.LLMs.fallback import FallbackChatModel
        from agent.failover.cooldown import get_cooldown_registry
        primary = FallbackChatModel(
            primary=primary,
            candidates=candidates,
            cooldown_registry=get_cooldown_registry(),
            max_retries=2,
        )
        logger.info("主 LLM 已启用回退链 (%d 个候选)", len(candidates))

    if temperature is not None:
        return primary.bind(temperature=temperature)
    return primary
```

### 2.5 配置：`.env` 新增变量

```env
# --- 方案 1：模型回退配置 ---
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

### 2.6 数据结构关系

```
FallbackChatModel
├── primary: NormalizingChatModel          # 主模型（现有）
├── candidates: list[NormalizingChatModel]  # 回退候选（新增）
├── cooldown_registry: CooldownRegistry    # 冷却探针（新增）
├── max_retries: int = 2                   # 同模型退避重试
├── backoff_base_ms: int = 1000            # 退避基础延迟
└── backoff_max_ms: int = 30000             # 退避最大延迟

每次调用产生:
  FallbackAttempt
  ├── provider, model, error, reason, timestamp, cooldown_until

CooldownRegistry (全局单例)
├── _store: dict[str, _CooldownEntry]
│   └── _CooldownEntry {provider, cooldown_until, failure_count}
├── MIN_INTERVAL_S = 30
├── MAX_KEYS = 256
└── TTL_S = 86400
```

---

## 3. 方案 2：结构化故障转移分类引擎

### 3.1 15 种分类原因

```python
from enum import Enum

class FailoverReason(str, Enum):
    AUTH = "auth"                          # 认证失败（可重试：token 过期）
    AUTH_PERMANENT = "auth_permanent"      # 永久认证失败（不可重试）
    BILLING = "billing"                    # 计费问题（不可重试）
    RATE_LIMIT = "rate_limit"              # 速率限制（可重试）
    OVERLOADED = "overloaded"              # 服务过载（可重试）
    TIMEOUT = "timeout"                    # 超时（可重试）
    SERVER_ERROR = "server_error"          # 服务器内部错误（可重试）
    TLS_CERTIFICATE = "tls_certificate"    # TLS 证书问题（不可重试）
    CONTEXT_OVERFLOW = "context_overflow"  # 上下文溢出（不切换模型，压缩上下文）
    MODEL_NOT_FOUND = "model_not_found"    # 模型不存在（不可重试）
    SESSION_EXPIRED = "session_expired"    # 会话过期（可重试）
    EMPTY_RESPONSE = "empty_response"     # 空响应（可重试）
    NO_ERROR_DETAILS = "no_error_details"  # 无错误详情（不可重试）
    UNCLASSIFIED = "unclassified"           # 未分类（不可重试）
    UNKNOWN = "unknown"                     # 未知错误（不可重试）
```

### 3.2 可重试性矩阵

| 原因             | 可重试  | 建议冷却(秒) | 动作                        |
| ---------------- | ------- | ------------ | --------------------------- |
| auth             | Yes     | 60           | 退避重试（可能 token 刷新） |
| auth_permanent   | No      | 3600         | 切换候选 + 长冷却           |
| billing          | No      | 86400        | 切换候选 + 超长冷却         |
| rate_limit       | Yes     | 30           | 退避重试（读 Retry-After）  |
| overloaded       | Yes     | 15           | 退避重试                    |
| timeout          | Yes     | 10           | 退避重试                    |
| server_error     | Yes     | 30           | 退避重试                    |
| tls_certificate  | No      | 3600         | 切换候选                    |
| context_overflow | Special | 0            | 压缩上下文（不切换模型）    |
| model_not_found  | No      | 86400        | 切换候选                    |
| session_expired  | Yes     | 5            | 退避重试                    |
| empty_response   | Yes     | 5            | 退避重试                    |
| no_error_details | No      | 60           | 切换候选                    |
| unclassified     | No      | 60           | 切换候选                    |
| unknown          | No      | 60           | 切换候选                    |

### 3.3 新文件：`agent/failover/__init__.py`

```python
"""故障转移子系统。"""
from agent.failover.classifier import classify_error, FailoverClassification
from agent.failover.error import FailoverError
from agent.failover.signal import FailoverSignal, FailoverReason

__all__ = [
    "classify_error", "FailoverClassification", "FailoverError",
    "FailoverSignal", "FailoverReason",
]
```

### 3.4 新文件：`agent/failover/signal.py`

```python
"""故障转移信号类型定义。"""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class FailoverReason(str, Enum):
    AUTH = "auth"
    AUTH_PERMANENT = "auth_permanent"
    BILLING = "billing"
    RATE_LIMIT = "rate_limit"
    OVERLOADED = "overloaded"
    TIMEOUT = "timeout"
    SERVER_ERROR = "server_error"
    TLS_CERTIFICATE = "tls_certificate"
    CONTEXT_OVERFLOW = "context_overflow"
    MODEL_NOT_FOUND = "model_not_found"
    SESSION_EXPIRED = "session_expired"
    EMPTY_RESPONSE = "empty_response"
    NO_ERROR_DETAILS = "no_error_details"
    UNCLASSIFIED = "unclassified"
    UNKNOWN = "unknown"


@dataclass
class FailoverClassification:
    reason: FailoverReason
    retryable: bool
    cooldown_suggested: float
    evidence: str
    http_status: Optional[int] = None
    provider: Optional[str] = None


@dataclass
class FailoverSignal:
    reason: FailoverReason
    evidence: str
    http_status: Optional[int] = None
    error_message: str = ""
    error_type: str = ""
```

### 3.5 新文件：`agent/failover/patterns.py`

```python
"""错误消息模式匹配。

参考 openclaw failover/message-patterns.ts + provider-patterns.ts。
用字符串 key 避免循环导入，classifier.py 在使用时做转换。
"""
from __future__ import annotations
import re

MESSAGE_PATTERNS = {
    "rate_limit": [
        re.compile(r"rate.?limit", re.IGNORECASE),
        re.compile(r"too many requests", re.IGNORECASE),
        re.compile(r"429", re.IGNORECASE),
        re.compile(r"quota.*exceed", re.IGNORECASE),
    ],
    "billing": [
        re.compile(r"billing", re.IGNORECASE),
        re.compile(r"payment", re.IGNORECASE),
        re.compile(r"insufficient.*balance", re.IGNORECASE),
        re.compile(r"402", re.IGNORECASE),
    ],
    "timeout": [
        re.compile(r"timeout", re.IGNORECASE),
        re.compile(r"timed?\s*out", re.IGNORECASE),
        re.compile(r"deadline.*exceed", re.IGNORECASE),
        re.compile(r"ETIMEDOUT", re.IGNORECASE),
    ],
    "server_error": [
        re.compile(r"internal server error", re.IGNORECASE),
        re.compile(r"\b500\b", re.IGNORECASE),
        re.compile(r"\b502\b|\b503\b|\b504\b", re.IGNORECASE),
        re.compile(r"bad gateway", re.IGNORECASE),
        re.compile(r"service unavail", re.IGNORECASE),
        re.compile(r"gateway timeout", re.IGNORECASE),
    ],
    "overloaded": [
        re.compile(r"overload", re.IGNORECASE),
        re.compile(r"capacity", re.IGNORECASE),
        re.compile(r"busy", re.IGNORECASE),
        re.compile(r"\b503\b", re.IGNORECASE),
    ],
    "auth": [
        re.compile(r"unauthor", re.IGNORECASE),
        re.compile(r"\b401\b", re.IGNORECASE),
        re.compile(r"invalid.*api.?key", re.IGNORECASE),
        re.compile(r"expired.*token", re.IGNORECASE),
    ],
    "auth_permanent": [
        re.compile(r"forbidden", re.IGNORECASE),
        re.compile(r"\b403\b", re.IGNORECASE),
        re.compile(r"permission.*denied", re.IGNORECASE),
    ],
    "tls_certificate": [
        re.compile(r"ssl|tls", re.IGNORECASE),
        re.compile(r"certificate", re.IGNORECASE),
        re.compile(r"CERTIFICATE_VERIFY_FAILED", re.IGNORECASE),
    ],
    "context_overflow": [
        re.compile(r"context.*length.*exceed", re.IGNORECASE),
        re.compile(r"maximum.*context", re.IGNORECASE),
        re.compile(r"token.*limit", re.IGNORECASE),
        re.compile(r"too many tokens", re.IGNORECASE),
        re.compile(r"context_window", re.IGNORECASE),
    ],
    "model_not_found": [
        re.compile(r"model.*not.*found", re.IGNORECASE),
        re.compile(r"model.*not.*available", re.IGNORECASE),
        re.compile(r"unknown.*model", re.IGNORECASE),
        re.compile(r"\b404\b", re.IGNORECASE),
    ],
    "session_expired": [
        re.compile(r"session.*expired", re.IGNORECASE),
        re.compile(r"invalid.*session", re.IGNORECASE),
    ],
    "empty_response": [
        re.compile(r"empty.*response", re.IGNORECASE),
        re.compile(r"no.*content", re.IGNORECASE),
        re.compile(r"null.*response", re.IGNORECASE),
    ],
}

PROVIDER_PATTERNS = {
    "openai": {
        "rate_limit": [re.compile(r"RateLimitError", re.IGNORECASE)],
        "server_error": [re.compile(r"InternalServerError|APIError", re.IGNORECASE)],
        "auth": [re.compile(r"AuthenticationError", re.IGNORECASE)],
    },
    "anthropic": {
        "overloaded": [re.compile(r"overloaded_error", re.IGNORECASE)],
        "rate_limit": [re.compile(r"rate_limit_error", re.IGNORECASE)],
    },
    "deepseek": {
        "rate_limit": [re.compile(r"Rate limit reached", re.IGNORECASE)],
    },
    "zhipu": {
        "rate_limit": [re.compile(r"1301|1306", re.IGNORECASE)],
    },
    "dashscope": {
        "rate_limit": [re.compile(r"Throttling", re.IGNORECASE)],
    },
}

EXCEPTION_TYPE_MAP = {
    "ConnectionError": "timeout",
    "TimeoutError": "timeout",
    "asyncio.TimeoutError": "timeout",
    "ConnectionRefusedError": "timeout",
    "ConnectionResetError": "timeout",
    "ssl.SSLError": "tls_certificate",
    "ssl.CertificateError": "tls_certificate",
    "openai.AuthenticationError": "auth",
    "openai.PermissionDeniedError": "auth_permanent",
    "openai.RateLimitError": "rate_limit",
    "openai.APITimeoutError": "timeout",
    "openai.APIConnectionError": "timeout",
    "openai.InternalServerError": "server_error",
    "openai.NotFoundError": "model_not_found",
    "openai.UnprocessableEntityError": "context_overflow",
}

HTTP_STATUS_MAP = {
    401: "auth",
    402: "billing",
    403: "auth_permanent",
    404: "model_not_found",
    408: "timeout",
    429: "rate_limit",
    500: "server_error",
    502: "server_error",
    503: "overloaded",
    504: "timeout",
}

RETRYABILITY = {
    "auth": (True, 60),
    "auth_permanent": (False, 3600),
    "billing": (False, 86400),
    "rate_limit": (True, 30),
    "overloaded": (True, 15),
    "timeout": (True, 10),
    "server_error": (True, 30),
    "tls_certificate": (False, 3600),
    "context_overflow": (False, 0),
    "model_not_found": (False, 86400),
    "session_expired": (True, 5),
    "empty_response": (True, 5),
    "no_error_details": (False, 60),
    "unclassified": (False, 60),
    "unknown": (False, 60),
}
```

### 3.6 新文件：`agent/failover/classifier.py`

```python
"""故障转移分类引擎。

多维度分类：HTTP 状态码 + 异常类型 + Provider 特定模式 + 通用消息模式。
参考 openclaw src/agents/failover/classify.ts。
"""
from __future__ import annotations
import logging
from typing import Optional

from agent.failover.patterns import (
    MESSAGE_PATTERNS, PROVIDER_PATTERNS, EXCEPTION_TYPE_MAP,
    HTTP_STATUS_MAP, RETRYABILITY,
)
from agent.failover.signal import (
    FailoverClassification, FailoverReason, FailoverSignal,
)

logger = logging.getLogger(__name__)


def classify_error(
    error: Exception,
    provider: Optional[str] = None,
    http_status: Optional[int] = None,
) -> FailoverClassification:
    """
    对 LLM 调用错误进行多维度分类。

    分类优先级：
    1. HTTP 状态码（最精确）
    2. 异常类型（Python 异常类名）
    3. Provider 特定模式（提供商专属错误码）
    4. 通用消息模式（正则匹配错误文本）
    5. 兜底 → UNKNOWN
    """
    error_message = str(error)
    error_type = type(error).__name__
    full_type = f"{type(error).__module__}.{type(error).__name__}"

    signal = FailoverSignal(
        reason=FailoverReason.UNKNOWN,
        evidence="",
        http_status=http_status,
        error_message=error_message,
        error_type=full_type,
    )

    # 1. HTTP 状态码映射
    if http_status and http_status in HTTP_STATUS_MAP:
        reason_str = HTTP_STATUS_MAP[http_status]
        signal.reason = FailoverReason(reason_str)
        signal.evidence = f"HTTP {http_status} -> {reason_str}"

    # 2. 异常类型映射
    if signal.reason == FailoverReason.UNKNOWN:
        for exc_type, reason_str in EXCEPTION_TYPE_MAP.items():
            if exc_type in full_type or exc_type == error_type:
                signal.reason = FailoverReason(reason_str)
                signal.evidence = f"异常类型 {full_type} -> {reason_str}"
                break

    # 3. Provider 特定模式
    if signal.reason == FailoverReason.UNKNOWN and provider:
        provider_patterns = PROVIDER_PATTERNS.get(provider, {})
        for reason_str, patterns in provider_patterns.items():
            for pattern in patterns:
                if pattern.search(error_message):
                    signal.reason = FailoverReason(reason_str)
                    signal.evidence = f"Provider({provider}) 模式匹配 -> {reason_str}"
                    break
            if signal.reason != FailoverReason.UNKNOWN:
                break

    # 4. 通用消息模式
    if signal.reason == FailoverReason.UNKNOWN:
        for reason_str, patterns in MESSAGE_PATTERNS.items():
            for pattern in patterns:
                if pattern.search(error_message):
                    signal.reason = FailoverReason(reason_str)
                    signal.evidence = f"消息模式匹配 -> {reason_str}"
                    break
            if signal.reason != FailoverReason.UNKNOWN:
                break

    # 5. 兜底
    if signal.reason == FailoverReason.UNKNOWN:
        signal.evidence = f"无法分类: {error_type}: {error_message[:200]}"

    retryable, cooldown_suggested = RETRYABILITY.get(
        signal.reason.value, (False, 60)
    )

    if signal.reason == FailoverReason.CONTEXT_OVERFLOW:
        retryable = False
        cooldown_suggested = 0

    return FailoverClassification(
        reason=signal.reason,
        retryable=retryable,
        cooldown_suggested=cooldown_suggested,
        evidence=signal.evidence,
        http_status=http_status,
        provider=provider,
    )
```

### 3.7 新文件：`agent/failover/error.py`

```python
"""FailoverError 异常类。携带结构化元数据。"""
from __future__ import annotations
from typing import Optional

from agent.failover.signal import FailoverReason


class FailoverError(Exception):
    """所有模型候选均已耗尽时抛出的异常。"""

    def __init__(
        self,
        message: str,
        attempts: Optional[list] = None,
        primary_reason: Optional[FailoverReason] = None,
    ):
        super().__init__(message)
        self.attempts = attempts or []
        self.primary_reason = primary_reason

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.attempts:
            parts.append(f"attempts={len(self.attempts)}")
            for i, att in enumerate(self.attempts):
                parts.append(
                    f"  [{i}] {att.provider}/{att.model}: reason={att.reason}"
                )
        return "\n".join(parts)
```

### 3.8 复用现有 resilience 模块

`skills/builtin/core/multimodal_rag/scripts/graph_rag/vendored_raganything/resilience.py` 已有：

| 已有组件                           | 复用策略                                                    |
| ---------------------------------- | ----------------------------------------------------------- |
| `retry()` / `async_retry()` 装饰器 | 不直接复用（FallbackChatModel 内部已实现退避重试）          |
| `CircuitBreaker` 类                | 可提升到 `agent/failover/circuit_breaker.py`，由方案 5 复用 |
| 可重试异常列表                     | 已整合到 `patterns.py` 的 `EXCEPTION_TYPE_MAP`              |

---
