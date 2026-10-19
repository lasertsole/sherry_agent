# 🧭 摘要压缩触发 — 生命周期 T1–T5 与溢出路由

[English](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> [Summarization](../README.zh.md) 的一部分：五个生命周期触发点（T1–T5）与四路溢出决策。

---

## 🧭 生命周期：五个触发点（T1–T5）

```
回合开始
│
├─ T1  before_agent 预检  (_t1_preflight overflow.py:795 / _at1_preflight overflow.py:820)
│      ├─ _reset_turn_state (thrash.py:157) 重置 11 个每回合计数器
│      ├─ _decide_overflow_route (overflow.py:185) → None / "fits" → 直接放行
│      ├─ 冷却期 > 0 时封锁 COMPACT 路由；截断轨道仍然运行
│      │  （它本身就是最廉价的恢复机制）
│      └─ 派发（trigger="T1"）+ _t1_state_update (overflow.py:773) 把结果提交进图：
│         [RemoveMessage(id=REMOVE_ALL_MESSAGES), *new_messages]
│         （add_messages reducer 自己从不删除消息 —— RemoveMessage
│         哨兵是被压缩掉的前缀真正离开状态的唯一途径）
│
├─ T2  wrap_model_call，handler 之前（core.py:413 同步 / core.py:495 异步）
│      ├─ 先读 force 标志（core.py:426）再过跳过闸门 —— 跳过闸门
│      │  （_should_skip_compression thrash.py:110）会消费该标志
│      ├─ _tick_cooldown（thrash.py:87）：每次调用都递减冷却计数
│      ├─ 防抖闸门（core.py:437–439）：
│      │    if not forced and (cooldown_active or
│      │               attempts >= MAX_COMPRESS_ATTEMPTS_PER_TURN):
│      │      直接放行（若刚发生过 compact 则重建系统提示词，
│      │      core.py:441–459）→ handler → 监控 → T3
│      ├─ 否则：四路决策（core.py:472）→ _dispatch_overflow_route；
│      │  若遗留触发子句命中（_check_trigger overflow.py:135，例如
│      │  ("messages", 40)）→ ROUTE_COMPACT_ONLY（core.py:480）
│      └─ handler 调用本身运行在
│         _execute_with_recovery（overflow.py:703）之内 —— 即 T4/T5 恢复环
│
├─ T3  响应后复检  (_post_response_check overflow.py:511 / 异步 overflow.py:543)
│      ├─ 若 T2 在本次 wrap 调用中已压缩则跳过（t2_compressed
│      │  标志，core.py:484–486）—— 每次模型调用至多一次压缩
│      ├─ extract_reported_input_tokens(response)（overflow.py:66）；None → 返回
│      ├─ 闸门：回合尝试上限、冷却期、可用预算
│      ├─ pressure = max(估算 + 系统提示词, 上报值) —— provider
│      │  上报的输入 token 数优先（compute_pressure）
│      ├─ pressure < usable × 0.80 → 返回；路由为 "fits" → 返回
│      └─ 派发（trigger="T3"）并始终返回原始响应；整个函数体
│         fail-open（任何异常 → 记日志，原始响应原样保留）
│
└─ T4/T5  provider 报错恢复环
       (_execute_with_recovery overflow.py:675 / _aexecute_with_recovery overflow.py:734)
       ├─ handler 抛异常 → classify_provider_error
       │  （pub/func/message/llm_error_classifier.py）：
       │  payload_too_large → T4，context_overflow → T5
       │  （_TRIGGER_BY_ERROR_CLASS overflow.py:105，_RETRY_KEY_BY_ERROR_CLASS overflow.py:111）
       ├─ 非目标类 / 未知类 → 原始异常原样重新抛出（零重试、
       │  零状态写入、绝不吞掉）
       ├─ 重试次数 < MAX_OVERFLOW_RETRIES (3) → _forced_recovery_request
       │  （overflow.py:625 / 异步 overflow.py:662）：不调 LLM 的尾部裁剪先运行 —— 当它
       │  单独就把估算压到可用预算以下时，处理器带着 stub 后的尾部
       │  结果重试、完全不做 compact；已是 stub 的请求会让裁剪变成
       │  no-op，于是下一次尝试退化为「compact + 预算截断」这一步，
       │  它构造上绕过全部防抖闸门（冷却期、每回合上限与
       │  _should_skip_compression 均不被咨询）；它不武装冷却期、
       │  不计回合尝试，但会经过 _record_compression 保证会话统计
       │  真实；每类重试计数器在成功之后才递增（overflow.py:614）
       ├─ 重试耗尽 → 原始异常重新抛出（错误帧经 messages.py →
       │  turn_runner.py 链路向上传播 —— 绝不返回空响应）
       └─ 强制压缩步骤自身失败 → 原始异常重新抛出
          （raise exc from compression_exc）。_monitor_degradation
          只在恢复环返回之后的最终成功响应上运行一次。
```

遗留触发子句仍然作为 T2 的兜底存在（`_check_trigger`，overflow.py:135）：`("messages", N)` 按历史长度触发，`("tokens", N)` 按 `max(本地估算, 最后一条 AIMessage 上报的 usage_metadata.total_tokens)` ≥ N 触发。子句列表是 OR 关系。

## 🚦 四路溢出路由决策

`pub/func/message/overflow_router.py` 是一个**纯决策层** —— 不截断、不压缩、无 I/O、无状态。中间件从它导入三个函数：

- `compute_pressure`（:50）= `max(estimated_tokens + system_prompt_tokens, reported_tokens)` —— 有 API 上报值时以上报值为准；
- `find_truncatable_tool_results`（:68）—— **只有** `ToolMessage` 有资格（工具输出可再生）；最近 `TRUNCATABLE_RECENT_SKIP (6)` 条消息永远排除在外，保证最新的 tool/ai 配对完好；候选必须值回票价：估算 token ≥ `MIN_TOOL_RESULT_TOKENS_TO_TRUNCATE (200)`；结果按 token 数降序排列，执行器先切最大的赢家；
- `decide_route`（:103）—— 派发契约（字符串稳定）：

| 压力（`p`）与 `usable` 的关系 | 无截断候选 | 有截断候选 | 候选 token 总和 vs 溢出量（`p − usable`） |
| :------------------------- | :------------------------ | :--------------- | :--------------------------------------------- |
| `p < 0.70 × usable` | `fits` | `fits` | — |
| 软溢出 `0.70 × usable ≤ p < 0.80 × usable` | `fits` | `truncate_tool_results_only` | —（软溢出**绝不会**单独触发压缩） |
| 硬溢出 `p ≥ 0.80 × usable` | `compact_only` | 总和 ≥ 溢出量 → `truncate_tool_results_only`；总和 < 溢出量 → `compact_then_truncate` | 溢出量 = `p − usable` |

所有阈值输入都从**可用预算**推导，而不是原始窗口：

```
usable_budget  = max(context_window − COMPRESSION_RESERVE_TOKENS(16_000), 0)   # _usable_budget overflow.py:168
system_est     = estimate_text_tokens(system_prompt)   # _estimate_system_prompt_tokens overflow.py:179
truncate line  = usable × PREEMPTIVE_TRUNCATE_RATIO (0.70)
compact line   = usable × COMPRESSION_TRIGGER_RATIO (0.80)
truncate budget= usable × TRUNCATE_BUDGET_RATIO (0.60)
```

唯一的执行器 `_dispatch_overflow_route`（overflow.py:408 同步 / overflow.py:430 异步）同时服务 T1、T2 **和** T3 —— 绝不复制第二份：

- `truncate_tool_results_only` → `_run_budget_truncation`（overflow.py:219）—— 第 1 步截断超大的工具调用参数（返回新消息，见 [截断轨道](../internals/README.zh.md#-截断轨道预算截断与-ttl-模块)），第 2 步原地截断工具输出 —— 然后进行**复检**：如果释放的 token 不够（`new_tokens ≥ usable × 0.80`，按返回的列表估算），升级为 `compact_then_truncate`；否则直接放行、不做压缩；
- `compact_only` / `compact_then_truncate` → `_execute_compact`（overflow.py:326 / 异步 overflow.py:344）→ `_apply_compression`（异常记日志、请求原样返回）→ `_record_compaction_bookkeeping`（core.py:345：武装冷却期、计一次回合尝试）→ `compact_then_truncate` 还会对压缩结果再跑一次预算截断兜底 → 按新旧 token 与压力比记录路由日志。

**P1-2 快速路径 —— 不调 LLM 的尾部裁剪。** 在任一上游路由执行之前，`_fast_tail_clip` 会先运行 `clip_overflow_tail`（`pub/func/message/overflow_clip.py`）：尾部连续的 `ToolMessage` 批次被替换为紧凑 stub，替换经 `ToolMessage.model_copy` 完成，因此没有任何消息被删除或注入 —— `id`、`tool_call_id`、`name` 与 `additional_kwargs` 全部保留，配对净化与持久水位因此依然满足。预算：`target = max(estimate_messages_tokens(messages, reported_tokens=0) − usable × ratio, 0)`；`target` 为 `0` 表示"取最大可裁剪批次"（上限 `overflow_clip_max_remove`，下限 `overflow_clip_min_keep`）。`ratio` 在上游路由路径为 `COMPRESSION_TRIGGER_RATIO (0.80)`，在 T4/T5 强制步骤为 `1.0`（低于 usable 预算）。只有**单独**把估算压到线下方的裁剪才会被接受：请求带着 stub 列表直接返回，上游路由根本不执行（不做预算截断、不调辅助 LLM 压缩）。裁剪不够时整份结果被丢弃，既有路由在原始列表上照常运行。在 T4/T5 上裁剪读取 `request.messages`；已 stub 的请求会让裁剪变成 no-op，因此重试预算不会被相同裁剪烧掉，下一次尝试退化为压缩。

**为什么丢弃尾部内容是安全的：** 每条工具结果在返回的瞬间就已刷入 MesMemory（`MessagePersistenceMiddleware`），并可通过 `message_search` 工具继续取回。被 P0-2 驱逐的结果在 stub 中保留自己的 `[evicted to: …]` 指针（因此 `read_file` 依然可用），被 P2-4 切片的 `read_file` 结果原样保留切片通知；而一条已 stub 的消息会终止可扫描批次 —— 第二次裁剪是 no-op，绝不会破坏这些标记。配置：`overflow_clip_enabled` / `overflow_clip_max_remove` / `overflow_clip_min_keep`。

窗口算术（测试契约）：窗口 `41 600` → usable `25 600`，两条线 `17 920` / `20 480`，截断预算 `15 360`。当测试固定值 `MAIN_LLM_MAX_TOKEN = 65536` 时（运行时 `.env` 值必须 >= 131072 / 128K），注册的 T2 子句落在 `52 428`。
