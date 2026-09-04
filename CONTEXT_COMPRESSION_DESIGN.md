# 上下文压缩机制设计：多触发点 + 双轨路由

> 融合 hermes-agent 的 **5 触发点密集检测** 与 openclaw 的 **压缩+截断双轨路由**，形成一套「多时机触发 + 分级处理」的上下文管理系统。

---

## 一、机制总览

```
┌─────────────────────────────────────────────────────────────┐
│                    消息流 + 压缩触发点                        │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  用户发送 HumanMessage                                       │
│    │                                                        │
│    ├─ [T1] PREFLIGHT 预检压缩（生成前）                      │
│    │      └─ 双轨路由 → 截断 / 压缩 / 都做 / 放行            │
│    │                                                        │
│    ├─ 进入工具循环 while iter < max:                         │
│    │    │                                                    │
│    │    ├─ [T2] PRE-API 压缩（生成前，每次API调用前）         │
│    │    │      └─ 双轨路由                                   │
│    │    │                                                    │
│    │    ├─ ═════ 模型流式生成 ═════                          │
│    │    │                                                    │
│    │    ├─ 更新真实 token 计数 (provider 报告)               │
│    │    │                                                    │
│    │    ├─ [T3] POST-RESPONSE 压缩（生成后）                  │
│    │    │      └─ 双轨路由（用真实 token 数）                │
│    │    │                                                    │
│    │    ├─ [T4] 413 Payload-Too-Large 恢复（生成后错误）     │
│    │    │      └─ 强制压缩 + 重试                            │
│    │    │                                                    │
│    │    ├─ [T5] Context-Overflow 恢复（生成后错误）          │
│    │    │      └─ 强制压缩 + 重试                            │
│    │    │                                                    │
│    │    └─ 有工具调用 → 执行工具 → 追加结果 → continue       │
│    │       无工具调用 → break                                 │
│    │                                                        │
│    └─ 返回最终响应                                           │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## 二、双轨路由机制（核心创新点）

来自 openclaw 的设计：不是每次都做重量级 AI 压缩，而是先评估 **溢出类型**，路由到不同级别的处理。

```
评估当前 prompt 的 token 压力
    │
    ├─ overflow_tokens <= 0 ──────────────────→ "fits"                    → 直接放行
    │
    ├─ 有大工具结果 且 可截断量 >= overflow ──→ "truncate_tool_results_only" → 仅截断工具结果
    │
    ├─ 无大工具结果 或 可截断量 < overflow ──→ "compact_only"               → 仅 AI 压缩
    │
    └─ 截断后仍溢出 ─────────────────────────→ "compact_then_truncate"     → 先压缩再截断
```

---

## 三、五个触发点详解

### T1 — PREFLIGHT 预检压缩（生成前）

| 属性         | 值                                               |
| ------------ | ------------------------------------------------ |
| **时机**     | 用户发消息后、第一次 API 调用前                  |
| **检测依据** | 粗略估算 token 数（`len(text) // 4`）            |
| **目的**     | 防止历史过长直接导致首次调用失败                 |
| **来源**     | hermes-agent `turn_context.build_turn_context()` |

### T2 — PRE-API 压缩（生成前，工具循环内）

| 属性         | 值                                                       |
| ------------ | -------------------------------------------------------- |
| **时机**     | 工具循环每次迭代中，工具结果追加后、API 调用前           |
| **检测依据** | 含工具结果的估算 token 数                                |
| **目的**     | 防止单轮内工具输出（如大量 terminal 输出）导致上下文膨胀 |
| **来源**     | hermes-agent `conversation_loop.py` 第 1029-1036 行      |

### T3 — POST-RESPONSE 压缩（生成后）

| 属性         | 值                                                  |
| ------------ | --------------------------------------------------- |
| **时机**     | 模型响应完成、工具结果追加后                        |
| **检测依据** | provider 报告的真实 `prompt_tokens`                 |
| **目的**     | 用精确数据做二次校验，捕获估算遗漏的溢出            |
| **来源**     | hermes-agent `conversation_loop.py` 第 4838-4862 行 |

### T4 — 413 Payload-Too-Large 恢复（生成后错误）

| 属性         | 值                                                  |
| ------------ | --------------------------------------------------- |
| **时机**     | API 调用返回 HTTP 413 错误后                        |
| **动作**     | 强制压缩（跳过防抖动）+ 重试                        |
| **重试上限** | `MAX_OVERFLOW_RETRIES = 3`                          |
| **来源**     | hermes-agent `conversation_loop.py` 第 3401-3458 行 |

### T5 — Context-Overflow 恢复（生成后错误）

| 属性         | 值                                                       |
| ------------ | -------------------------------------------------------- |
| **时机**     | Provider 返回上下文溢出错误（`context_length_exceeded`） |
| **动作**     | 强制压缩（含截断兜底）+ 重试                             |
| **重试上限** | `MAX_OVERFLOW_RETRIES = 3`                               |
| **来源**     | hermes-agent `conversation_loop.py` 第 3550-3706 行      |

---

## 四、伪代码实现

```python
# ============================================================
# 常量与配置
# ============================================================

CONTEXT_WINDOW        = 65536          # 模型上下文窗口
RESERVE_TOKENS        = 8192           # 为输出预留的 token 数
THRESHOLD_PERCENT     = 0.50           # 触发压缩的阈值比例 (50%)
COMPACTION_COOLDOWN   = 3              # 压缩后冷却轮数（防抖动）
MAX_OVERFLOW_RETRIES  = 3             # 溢出恢复最大重试次数
MAX_COMPRESS_ATTEMPTS = 3             # 单轮内最大压缩尝试次数
PRUNE_TTL_SECONDS     = 300           # 工具结果 Cache-TTL（5分钟）
TRUNCATE_BUDGET_RATIO = 0.6           # 工具结果截断预算占上下文的比例

# 触发点枚举
T1_PREFLIGHT   = "preflight"
T2_PRE_API     = "pre_api"
T3_POST_RESP   = "post_response"
T4_ERROR_413   = "error_413"
T5_OVERFLOW    = "error_overflow"

# 路由决策枚举
ROUTE_FITS               = "fits"
ROUTE_TRUNCATE_ONLY      = "truncate_tool_results_only"
ROUTE_COMPACT_ONLY       = "compact_only"
ROUTE_COMPACT_THEN_TRUNC = "compact_then_truncate"


# ============================================================
# 主对话循环 — 5 触发点编排
# ============================================================

def conversation_loop(user_message: HumanMessage) -> Response:
    messages = load_history() + [user_message]
    system_prompt = build_system_prompt()

    compression_cooldown = 0      # 防抖动计数器
    compress_attempts    = 0      # 本轮压缩尝试计数
    overflow_retries     = 0      # 溢出重试计数

    # ──────────────────────────────────────────────
    # [T1] PREFLIGHT 预检压缩（生成前）
    # 时机：用户发消息后、第一次 API 调用前
    # 依据：粗略估算 token 数
    # ──────────────────────────────────────────────
    messages, system_prompt = trigger_dual_track(
        messages, system_prompt,
        trigger=T1_PREFLIGHT,
        cooldown_counter=compression_cooldown,
    )

    api_call_count = 0
    while api_call_count < MAX_ITERATIONS:

        # ──────────────────────────────────────────
        # [T2] PRE-API 压缩（生成前，工具循环内）
        # 时机：每次 API 调用前（含工具结果追加后）
        # 依据：含工具结果的估算 token 数
        # ──────────────────────────────────────────
        if compression_cooldown == 0 and compress_attempts < MAX_COMPRESS_ATTEMPTS:
            messages, system_prompt = trigger_dual_track(
                messages, system_prompt,
                trigger=T2_PRE_API,
                cooldown_counter=compression_cooldown,
            )
            if was_compressed:
                compress_attempts += 1
                compression_cooldown = COMPACTION_COOLDOWN

        # ──────────────────────────────────────────
        # 模型流式生成
        # ──────────────────────────────────────────
        try:
            response, usage = call_model_stream(
                messages=messages,
                system_prompt=system_prompt,
                context_window=CONTEXT_WINDOW,
            )
        except HTTP413Error as e:
            # ──────────────────────────────────────
            # [T4] 413 Payload-Too-Large 恢复
            # 时机：API 返回 413 错误后
            # 动作：强制压缩 + 重试
            # ──────────────────────────────────────
            if overflow_retries >= MAX_OVERFLOW_RETRIES:
                raise FatalError("Max 413 retries exceeded")

            overflow_retries += 1
            messages, system_prompt = force_compact(
                messages, system_prompt,
                trigger=T4_ERROR_413,
            )
            continue  # 重试 API 调用

        except ContextOverflowError as e:
            # ──────────────────────────────────────
            # [T5] Context-Overflow 恢复
            # 时机：Provider 返回上下文溢出错误
            # 动作：强制压缩（含截断兜底）+ 重试
            # ──────────────────────────────────────
            if overflow_retries >= MAX_OVERFLOW_RETRIES:
                raise FatalError("Max overflow retries exceeded")

            overflow_retries += 1
            messages, system_prompt = force_compact(
                messages, system_prompt,
                trigger=T5_OVERFLOW,
            )
            continue

        # 更新真实 token 计数（provider 报告）
        update_real_token_count(usage.prompt_tokens)
        if compression_cooldown > 0:
            compression_cooldown -= 1

        # ──────────────────────────────────────────
        # [T3] POST-RESPONSE 压缩（生成后）
        # 时机：模型响应完成、工具结果追加后
        # 依据：provider 报告的真实 prompt_tokens
        # ──────────────────────────────────────────
        if response.has_tool_calls():
            tool_results = execute_tools(response.tool_calls)
            messages.append(response)
            messages.extend(tool_results)

            real_tokens = get_last_real_token_count()
            if should_compress_real(real_tokens) and compression_cooldown == 0:
                messages, system_prompt = trigger_dual_track(
                    messages, system_prompt,
                    trigger=T3_POST_RESP,
                    real_token_count=real_tokens,
                    cooldown_counter=compression_cooldown,
                )
                if was_compressed:
                    compress_attempts += 1
                    compression_cooldown = COMPACTION_COOLDOWN
            continue
        else:
            break  # 无工具调用，最终响应

    return response


# ============================================================
# 双轨路由核心 — openclaw 风格的分级处理
# ============================================================

def trigger_dual_track(
    messages: list[Message],
    system_prompt: str,
    trigger: str,
    real_token_count: int | None = None,
    cooldown_counter: int = 0,
) -> tuple[list[Message], str]:
    """
    双轨路由：根据溢出类型选择「截断」/「压缩」/「都做」/「放行」
    """
    # 1. 估算 token 压力
    estimated_tokens = estimate_tokens(messages, system_prompt)
    # 后生成场景优先使用真实 token 数
    if real_token_count is not None and real_token_count > 0:
        pressure_tokens = real_token_count
    else:
        pressure_tokens = estimated_tokens

    # 2. 计算可用预算
    usable_budget = CONTEXT_WINDOW - RESERVE_TOKENS
    threshold = int(usable_budget * THRESHOLD_PERCENT)

    # 3. 防抖动检查
    if cooldown_counter > 0:
        return messages, system_prompt  # 冷却中，跳过

    # 4. 未超阈值 → 放行
    if pressure_tokens < threshold:
        return messages, system_prompt

    overflow_tokens = pressure_tokens - usable_budget
    if overflow_tokens <= 0:
        # 超阈值但未超可用窗口 → 软触发，可选截断
        overflow_tokens = pressure_tokens - threshold

    # 5. 评估可截断的工具结果
    truncatable = find_truncatable_tool_results(messages)
    truncatable_tokens = sum(estimate_tokens(r.content) for r in truncatable)

    # 6. 路由决策
    route = decide_route(
        overflow_tokens=overflow_tokens,
        truncatable_tokens=truncatable_tokens,
        has_truncatable=len(truncatable) > 0,
    )

    # 7. 执行路由
    match route:
        case ROUTE_FITS:
            return messages, system_prompt

        case ROUTE_TRUNCATE_ONLY:
            # 轻量级：仅截断大工具结果
            messages = truncate_tool_results(
                messages,
                budget=int(usable_budget * TRUNCATE_BUDGET_RATIO),
                ttl_seconds=PRUNE_TTL_SECONDS,
            )
            return messages, system_prompt

        case ROUTE_COMPACT_ONLY:
            # 重量级：AI 压缩整个对话历史
            return compact_messages(messages, system_prompt, trigger)

        case ROUTE_COMPACT_THEN_TRUNC:
            # 先压缩
            messages, system_prompt = compact_messages(
                messages, system_prompt, trigger,
            )
            # 再检查是否仍溢出 → 截断兜底
            post_tokens = estimate_tokens(messages, system_prompt)
            if post_tokens > usable_budget:
                messages = truncate_tool_results(
                    messages,
                    budget=int(usable_budget * TRUNCATE_BUDGET_RATIO),
                    ttl_seconds=PRUNE_TTL_SECONDS,
                )
            return messages, system_prompt


def decide_route(
    overflow_tokens: int,
    truncatable_tokens: int,
    has_truncatable: bool,
) -> str:
    """openclaw 风格的路由决策"""
    if overflow_tokens <= 0:
        return ROUTE_FITS

    if has_truncatable and truncatable_tokens >= overflow_tokens:
        return ROUTE_TRUNCATE_ONLY  # 截断就够

    if not has_truncatable or truncatable_tokens < overflow_tokens:
        # 截断不够 → 需要压缩
        # 如果有大工具结果，压缩后可能仍需截断兜底
        if has_truncatable:
            return ROUTE_COMPACT_THEN_TRUNC
        else:
            return ROUTE_COMPACT_ONLY


# ============================================================
# 工具结果截断 — openclaw 风格的轻量级处理
# ============================================================

def truncate_tool_results(
    messages: list[Message],
    budget: int,
    ttl_seconds: int,
) -> list[Message]:
    """
    两级截断策略：
    1. Cache-TTL 过期截断：超过 TTL 的工具结果替换为占位符
    2. 预算截断：从最旧的工具结果开始截断，直到 token 数降到预算内
    """
    now = time.time()
    freed_tokens = 0

    # 阶段1: TTL 过期截断
    for msg in reverse(messages):
        if not is_tool_result(msg):
            continue
        if msg.timestamp + ttl_seconds < now:
            freed_tokens += estimate_tokens(msg.content)
            msg.content = f"[Tool result expired (freed ~{freed_tokens} tokens)]"
            msg.metadata.truncated = True

    # 阶段2: 预算截断（如果 TTL 截断后仍超预算）
    current_tokens = estimate_tokens(flatten(messages))
    if current_tokens <= budget:
        return messages

    for msg in reverse(messages):  # 从最旧的开始
        if not is_tool_result(msg) or msg.metadata.truncated:
            continue
        content_tokens = estimate_tokens(msg.content)
        if content_tokens < 100:  # 太小的跳过
            continue

        # head+tail 截断：保留头部 30% + 尾部 30%
        head_len = int(len(msg.content) * 0.3)
        tail_len = int(len(msg.content) * 0.3)
        msg.content = (
            msg.content[:head_len]
            + f"\n[...truncated {content_tokens} tokens...]\n"
            + msg.content[-tail_len:]
        )
        msg.metadata.truncated = True
        freed_tokens += int(content_tokens * 0.4)  # 截断后省约 40%

        if estimate_tokens(flatten(messages)) <= budget:
            break

    return messages


# ============================================================
# AI 压缩 — hermes-agent 风格的重量级处理
# ============================================================

def compact_messages(
    messages: list[Message],
    system_prompt: str,
    trigger: str,
) -> tuple[list[Message], str]:
    """
    调用辅助 LLM 生成对话摘要，
    用摘要替换旧消息，保留最近 N 条消息不动
    """
    KEEP_RECENT = 10  # 保留最近 10 条消息

    if len(messages) <= KEEP_RECENT:
        return messages, system_prompt  # 消息太少，不压缩

    # 分割：旧消息（待压缩）+ 最近消息（保留）
    old_messages = messages[:-KEEP_RECENT]
    recent_messages = messages[-KEEP_RECENT:]

    # 调用辅助 LLM 生成摘要
    summary = call_auxiliary_llm(
        prompt=build_compaction_prompt(old_messages),
        max_tokens=4096,
    )

    # 构建压缩后的消息列表
    summary_message = HumanMessage(
        content=f"[Conversation Summary]\n{summary}",
        metadata={"source": "summarization", "trigger": trigger},
    )

    # 截断摘要本身（防止摘要过长）
    if len(summary_message.content) > 8000:
        summary_message.content = truncate_head_tail(
            summary_message.content, max_chars=8000,
        )

    # 合并连续 HumanMessage（防止格式异常）
    compressed = [summary_message] + recent_messages
    compressed = merge_consecutive_human_messages(compressed)

    # 记录压缩效果（用于 anti-thrashing）
    old_tokens = estimate_tokens(flatten(old_messages))
    new_tokens = estimate_tokens(summary_message.content)
    record_compression(
        trigger=trigger,
        old_tokens=old_tokens,
        new_tokens=new_tokens,
        reduction_ratio=1 - (new_tokens / old_tokens),
    )

    # 如果压缩无效（缩减率 < 20%），增加无效计数
    if new_tokens / old_tokens > 0.8:
        increment_ineffective_count()

    return compressed, system_prompt


# ============================================================
# 强制压缩 — 错误恢复路径（T4/T5）
# ============================================================

def force_compact(
    messages: list[Message],
    system_prompt: str,
    trigger: str,
) -> tuple[list[Message], str]:
    """
    错误恢复专用：跳过防抖动检查，强制执行最激进的双轨处理
    先压缩 → 再截断 → 确保降到安全线以下
    """
    # 强制压缩（忽略冷却期和尝试计数）
    messages, system_prompt = compact_messages(
        messages, system_prompt, trigger,
    )

    # 强制截断所有可截断的工具结果
    messages = truncate_tool_results(
        messages,
        budget=int(CONTEXT_WINDOW * 0.3),  # 更激进的预算
        ttl_seconds=0,  # 忽略 TTL，全部截断
    )

    # 最终检查
    final_tokens = estimate_tokens(messages, system_prompt)
    if final_tokens > CONTEXT_WINDOW - RESERVE_TOKENS:
        # 仍然超限 → 截断保留的最近消息中的工具结果
        messages = emergency_truncate_all_tool_results(messages)

    return messages, system_prompt


# ============================================================
# 辅助函数
# ============================================================

def should_compress_real(real_tokens: int) -> bool:
    """后生成检测：使用 provider 报告的真实 token 数"""
    threshold = int((CONTEXT_WINDOW - RESERVE_TOKENS) * THRESHOLD_PERCENT)
    return real_tokens >= threshold

def estimate_tokens(messages, system_prompt) -> int:
    """粗略估算：每 4 字符 ≈ 1 token"""
    text = system_prompt + "".join(m.content for m in messages)
    return len(text) // 4

def find_truncatable_tool_results(messages) -> list[Message]:
    """找出可截断的工具结果（非最近 2 轮的）"""
    result = []
    for msg in messages[:-6]:  # 跳过最近 6 条（~2轮）
        if is_tool_result(msg) and not msg.metadata.truncated:
            if estimate_tokens(msg.content) > 200:  # 超过 200 token 的才值得截断
                result.append(msg)
    return result

def record_compression(trigger, old_tokens, new_tokens, reduction_ratio):
    """记录压缩日志和效果，用于 anti-thrashing 决策"""
    logger.info(
        f"Compression [{trigger}]: {old_tokens} -> {new_tokens} tokens "
        f"({reduction_ratio:.1%} reduction)"
    )
```

---

## 五、设计要点总结

| 机制                        | 来源         | 作用                                                                              |
| --------------------------- | ------------ | --------------------------------------------------------------------------------- |
| **5 触发点**                | hermes-agent | 覆盖所有时机：回合开始(T1)、工具循环内(T2)、响应后(T3)、413错误(T4)、溢出错误(T5) |
| **双轨路由**                | openclaw     | 根据溢出类型选择轻量截断 or 重量压缩 or 两者兼做，避免无脑 AI 压缩                |
| **真实 token + 估算 token** | hermes-agent | T1/T2 用估算值提前预防，T3 用 provider 真实值精确判断                             |
| **TTL 截断 + 预算截断**     | openclaw     | 两级截断：先按时间过期清理，再按 token 预算从旧到新截断                           |
| **head+tail 截断**          | openclaw     | 保留工具结果头尾各 30%，中间用占位符替换                                          |
| **防抖动**                  | hermes-agent | 压缩后冷却 N 轮 + 无效压缩计数器，防止反复无效压缩                                |
| **强制恢复路径**            | hermes-agent | T4/T5 跳过所有保护机制，执行最激进压缩+截断                                       |
| **anti-thrashing**          | sherry_agent | 最多 3 次压缩尝试，2 次无效后放弃                                                 |

---

## 六、与现有项目的对比

| 维度                | opencode-dev | hermes-agent | openclaw | sherry_agent       | **本方案** |
| ------------------- | ------------ | ------------ | -------- | ------------------ | ---------- |
| **语言/框架**       | TS / Effect  | Python       | TS       | Python / LangGraph | Python     |
| **生成前压缩**      | ✅           | ✅           | ✅       | ✅                 | ✅         |
| **生成后压缩**      | ✅           | ✅           | ✅       | ❌                 | ✅         |
| **触发点数量**      | 2            | 5            | 3        | 1                  | **5**      |
| **双轨路由**        | ❌           | ❌           | ✅       | ❌                 | ✅         |
| **TTL 截断**        | ❌           | ❌           | ✅       | ❌                 | ✅         |
| **防抖动**          | ❌           | ✅           | ✅       | ✅                 | ✅         |
| **错误恢复**        | ✅           | ✅           | ✅       | ❌                 | ✅         |
| **流式 token 监控** | ❌           | ❌           | ❌       | ❌                 | ❌         |
