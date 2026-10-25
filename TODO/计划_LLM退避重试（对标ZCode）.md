# 计划：LLM 退避重试机制（对标 ZCode）

- 建档日期：2026-10-25
- 状态：**待执行**
- 目标：把 ZCode 的模型调用退避重试语义移植到 Sherry：统一重试预算、Retry-After、可配置退避曲线、可中止等待、可观测的重试状态，并避免重试造成重复输出。
- 非目标：不改 WS 断线重连的 `msg_id` 幂等（已完成）；不改用户输入的队列投递语义；不引入 ZCode 的 off-peak 排队协议。

---

## 0. 结论先行

1. **ZCode 的重试是“模型/API 调用级”的，不是重新提交用户消息。** 默认 `maxRetries = 10`（总尝试 11 次）、基础延迟 2s、指数因子 2、上限 60s、带 jitter，并且优先使用服务端 `Retry-After`。鉴权刷新、thinking 签名修复、空补全和 off-peak 排队各有独立的额外尝试/不占预算规则。
2. **Sherry 已有 `LLMRetryMiddleware` 和错误分类器，但仍是简化的单层退避。** 当前默认 `max_retries = 3`、延迟 2s→60s、对称 jitter 0.3；SDK 客户端自己还会重试 2 次，实际尝试次数叠加；`adaptive_rate_limit_backoff` 已存在但没有进入生产重试路径。
3. **本计划分阶段落地**：先补策略工具、配置和测试（不改变现有默认行为），再接入 Retry-After、可中止等待和可观测事件，最后统一下调 SDK 重试、加入空补全/鉴权刷新等特殊分支。一次全量替换的风险太高，按阶段验收。

---

## 1. ZCode 的实现基线

参考源码（父目录 `../ZCode`）：

- `apps/zcode-cli/packages/adapters/src/model/retry-policy.ts`
- `apps/zcode-cli/packages/adapters/src/model/retry-budget.ts`
- `apps/zcode-cli/packages/adapters/src/model/runner-retry.ts`
- `apps/zcode-cli/packages/adapters/src/model/failure-classifier.ts`
- `apps/zcode-cli/packages/adapters/src/model/runner-generate.ts`
- `apps/zcode-cli/packages/adapters/src/model/runner-stream.ts`
- `apps/zcode-cli/packages/adapters/src/model/empty-completion-retry.ts`
- `apps/zcode-cli/packages/adapters/src/model/offpeak-retry.ts`

### 1.1 默认参数与退避曲线

`retry-policy.ts`：

| 参数 | 默认值 | 备注 |
|---|---|---|
| `maxRetries` | 10 | 环境变量名按“重试次数”暴露；内部 `maxAttempts = maxRetries + 1 = 11` |
| `baseDelayMs` | 2,000 | 可被 `ZCODE_MODEL_RETRY_BASE_DELAY_MS` 覆盖 |
| `backoffFactor` | 2 | 可被环境变量覆盖 |
| `maxDelayMs` | 60,000 | 可被环境变量覆盖 |
| `jitter` | true | 最终延迟取 `capped × (0.5 + random×0.5)`（full-jitter 的一半区间） |

`calculateRetryDelay()` 的顺序：

1. `uncapped = baseDelay × backoffFactor^(attempt-1)`；
2. `capped = min(uncapped, maxDelay)`；
3. 若服务端 `Retry-After` 合理（≤ 5 分钟，或小于当前指数延迟），**直接采用 Retry-After**，不叠加 jitter；
4. 否则对 capped 做 0.5–1.0 的随机抖动。

等待使用带 `AbortSignal` 的 `sleep()`，用户停止/取消会立刻打断。

### 1.2 重试预算

`retry-budget.ts` 定义三档：

- **bounded**：普通请求，按 `maxAttempts` 计数；
- **Unbounded**：只放宽“瞬态失败是否还能再试”，退避曲线、分类、`Retry-After` 和“已产生可见输出后不再重试”的边界一律不变；
- **SingleAttempt**：本次请求总尝试数收敛为 1（首次即上抛，鉴权刷新/签名修复不占预算的额外尝试语义保留）。

计数只对“会消耗预算的瞬态失败”递增；鉴权刷新、reasoning signature 修复、off-peak 排队重试**不消耗**普通 attempt 预算（off-peak 排队甚至原地回退 attempt 计数）。

### 1.3 失败分类

`failure-classifier.ts` 把异常映射为 `{code, reason, retryReason, retryable, retryAfterMs, statusCode}`：

- 可重试：429（rate limited）、529 / provider overloaded、5xx、网络/代理错误、超时、stream idle timeout、空补全（单独处理）；
- 不重试：400 请求格式错误、401/403 鉴权失败（另有凭据刷新路径）、404 模型不存在、TLS 校验失败、上下文超限、内容策略拒绝、取消；
- 对 provider 业务码（如 429 内部的 3105、票据 3102）做映射，不能把所有 4xx 一律当格式错误；
- 读取并保留 `Retry-After` / `retry-after-ms` 响应头，带入状态事件与错误上下文。

### 1.4 尝试循环与可见输出边界

`runner-generate.ts` / `runner-stream.ts` 的循环：

1. 每次尝试前检查预算与 abort；
2. 失败后分类；不可重试 → 归一化错误并上抛；
3. 可重试 → 发布 `model_request_failed`（`retryable: true`），再发布 `model_retry_scheduled`（含 `delayMs`、`nextAttempt`、`reason`、`retryAfterMs`）；
4. **释放准入票据** → 等待退避 → 重新准入 → 下一次尝试；
5. 一旦已经向 UI 产出可见输出（`emittedRetryBoundaryEvent`），不再自动重试整段回答，避免重复文本；
6. 鉴权刷新和 thinking signature 修复是“不占普通预算的额外尝试”；空补全最多额外重试 1 次（受 `attempt < maxAttempts` 限制）；
7. off-peak 排队 429 用 `min(Retry-After, 5min)` 无限探测，票据过期则用稳定标记失败交给上层续跑。

### 1.5 可观测性

状态中暴露：

```text
apiRetry: { attempt, maxAttempts, nextRetryAt, reasonCode }
retryNotice timeline event: { attempt, reasonCode }
```

日志额外记录安全响应头摘要：`retry-after`、`retry-after-ms`、`x-should-retry`、最终 `delayMs` 与 `retryReason`，方便复现限流问题。

---

## 2. Sherry 的现状

### 2.1 已有能力

- `agent/middlewares/llm_retry/core.py`：`LLMRetryMiddleware`，包住每次 `wrap_model_call`，处理 timeout / rate_limit / overloaded / server_error / invalid_response / unknown 的退避重试，并保留 fallback 链、内容过滤、partial-stream stub、stale-streak 熔断。
- `pub/func/retry_utils.py`：`jittered_backoff()`（对称 jitter）与 `adaptive_rate_limit_backoff()`（会读 `Retry-After`，但**生产代码未使用**）。
- `pub/func/message/llm_error_classifier.py`：成熟的状态码 / 异常类型 / 文本分类表，输出 `retryable`、`should_fallback`、`should_compress`。
- `config/features/agent_side/llm_retry.py`：`max_retries=3`、`base_delay=2.0`、`max_delay=60.0`、`jitter=0.3`、`stale_giveup_threshold=5`。
- `config/features/agent_side/llm_client_defaults.py`：各模型客户端 `max_retries=2`。

### 2.2 与 ZCode 的差距

| 能力 | ZCode | Sherry 当前 | 计划 |
|---|---|---|---|
| 重试层数 | 一层 adapter 循环 | Middleware 3 次 + SDK 2 次，叠加最多 12 次 | 统一为一层；SDK `max_retries=0` |
| 默认预算 | 10 次重试（11 attempts） | 3 次重试（4 attempts） | 可配置，默认对齐 10；保留 env 覆盖 |
| 退避曲线 | 2s×2^n，封顶 60s，full-jitter | 2s×2^n，封顶 60s，对称 jitter 0.3 | 新增 `backoff_factor` 与 `jitter_mode` |
| Retry-After | 优先使用，`retry-after-ms` 也支持 | 工具函数存在，生产未使用 | 从 classifier/异常头解析并在 429/503 使用 |
| 可中止等待 | abortable sleep | async `asyncio.sleep` 可被任务取消；sync `time.sleep` 不可 | 统一 abort 感知的 sleep |
| 预算档位 | bounded / unbounded / single | 仅 bounded | 按调用传入 `RetryBudget`，默认 bounded |
| 空补全 | 额外重试 1 次 | 无专门分支 | 增加 `empty_completion_retries` |
| 鉴权刷新/签名修复 | 不占普通预算的额外尝试 | 部分由 provider/fallback 处理 | 明确额外尝试与计数边界 |
| 可见输出边界 | 已输出后不再整段重试 | partial-stream stub 会重建（需核对是否重复） | 加 `visible_output_boundary` 门禁 |
| 状态事件 | `apiRetry` + `retryNotice` | 只有日志 | 轨迹事件 + 可选 WS `model_retry` |
| 错误归因 | provider/runtime/network 三源 | `error_context` 部分记录 | 统一安全字段，不带密钥/正文 |

---

## 3. 实施设计

### 3.1 配置（`config/features/agent_side/llm_retry.py`）

扩展现有 TypedDict（保留旧键）：

```python
class LlmRetryConfig(TypedDict):
    max_retries: int
    base_delay: float
    backoff_factor: float
    max_delay: float
    jitter_mode: Literal["full", "symmetric"]
    jitter: float              # symmetric 模式的抖动比例
    respect_retry_after: bool
    retry_after_cap_s: float
    empty_completion_retries: int
    emit_retry_events: bool
    stale_giveup_threshold: int
```

默认建议：`max_retries=10`、`base_delay=2.0`、`backoff_factor=2.0`、`max_delay=60.0`、`jitter_mode="full"`、`respect_retry_after=True`、`retry_after_cap_s=300`、`empty_completion_retries=1`。允许环境变量覆盖，沿用 ZCode 的命名习惯（`SHERRY_LLM_RETRY_*`），但读取集中在配置模块，不在中间件散落。

同时把 `LLM_CLIENT_DEFAULTS[*_max_retries]` 调为 0，或在 `models/LLMs/*` 构建时显式关闭 SDK 重试；否则预算永远不是单层。保留 fallback 客户端自身的“首次连接”行为不变。

### 3.2 新增策略工具（`pub/func/retry_policy.py`）

把“算延迟”和“等”从中间件中抽出，便于单测与复用：

- `parse_retry_after(headers_or_value) -> float | None`：支持 `retry-after` 秒数、`retry-after-ms`、HTTP-date（可选）；
- `compute_retry_delay(attempt, cfg, retry_after_s=None) -> float`：指数、封顶、full/symmetric jitter、Retry-After 优先与上限；
- `sleep_with_abort(delay_s, abort_event) -> None`：async；sync 路径使用可轮询的 `Event.wait` 或去掉 sync 重试路径；
- `is_retry_after_reasonable(value, exponential)`：避免异常大的响应头把回合挂死。

`pub/func/retry_utils.py` 保留兼容；新代码只走 `retry_policy`，旧 `jittered_backoff` 在迁移完成后删除或标记 deprecated。

### 3.3 `LLMRetryMiddleware` 改造

1. **单层预算**：`retry_budget_attempt` 从 0 开始，只在准备重试一次瞬态失败时 +1；`attempt` 仅用于日志/退避指数。
2. **分类接入**：`classify_api_error()` 的结果映射到 `retryable`、`retry_after_s`、`reason`；不可重试立即走 fallback/上抛。
3. **Retry-After**：429、503、529 和带 `Retry-After` 的异常使用服务端值；超过 `retry_after_cap_s` 时按上限处理并记录日志。
4. **abort 感知**：`awrap_model_call` 的等待用 abortable sleep；用户停止时立即退出，不再睡满。
5. **空补全**：当返回的 AI 消息既无文本、又无 tool calls、也无 usage，且未超过 `empty_completion_retries` 时重试一次；这不是普通 attempt 预算的一部分，单独计数并发布状态。
6. **额外尝试**：鉴权刷新、thinking signature 修复各自最多一次，不占普通预算；失败后仍按普通分类决定是否继续。
7. **可见输出边界**：记录本次 attempt 是否已通过 stream callback 产出 chunk。若已产出，绝不再整段重试；partial-stream stub 作为**唯一例外**保留现有专用恢复路径，并在实现时补测试确认不会重复文本。若无法保证，先关闭该例外的自动重试，只展示已产出的部分。
8. **fallback**：fallback 切换后重置 `retry_budget_attempt`（新模型新预算），但保留总尝试上限，避免 A→B→A 的无限循环。
9. **stale streak**：保留现有 timeout 连续失败熔断，阈值仍可配置。

### 3.4 可观测性

- 日志：保留现有 `LLM call failed ... retrying`，新增结构化字段 `attempt`、`max_attempts`、`delay_ms`、`retry_after_ms`、`reason`、`provider/source`；不记录请求正文、密钥和完整响应体。
- 轨迹：每次重试经 `runtime.hooks.RECORD_TRAJECTORY_EVENT` 写一条 `model_retry`（含 `turn_id`、`reason`、`attempt`、`delay_ms`、`next_attempt`），复用批量写/失败开放接口。
- WS（可选第二阶段）：节流推送 `model_retry` 状态，客户端在模型切换/思考控件旁显示“重试中 2/11”；本轮计划先做轨迹和日志，UI 后置。
- 预算交互：重试等待不消耗 token 预算；模型返回的 usage 仍只累计一次（成功的那次）。

### 3.5 off-peak 排队

ZCode 的 off-peak 协议是专用 provider 语义，Sherry 没有对应网关契约，**不在本计划内实现**；如果未来接入，按同样方式把排队等待从普通 attempt 预算中剥离，并用稳定标记交给上层续跑。

---

## 4. 分阶段执行

### Phase 1：策略工具与配置（无行为变化）

- 新增 `pub/func/retry_policy.py` + 单测（Retry-After 解析、指数、封顶、jitter 边界、abort）。
- 扩展 `LLM_RETRY` 配置；默认值先保持现有 3/2/60/0.3，确保零行为变化。
- 把 `adaptive_rate_limit_backoff` 标记为旧路径，不接入生产。
- 验收：现有 `tests/agent/middlewares`、`tests/pub/func/test_retry_utils.py` 全绿。

### Phase 2：接入 Retry-After、abort 与退避曲线

- `LLMRetryMiddleware` 使用 `retry_policy.compute_retry_delay`；429/503/529 读取 Retry-After。
- async 等待可中止；sync 路径改为可中断等待或明确标记为测试兼容路径。
- 配置默认仍保持 3 次；新增 `backoff_factor` / `jitter_mode` 可配置。
- 验收：新增中间件测试证明 429 按 Retry-After 等待、abort 立即返回、jitter 在范围内。

### Phase 3：单层预算、空补全与可见输出边界

- SDK `max_retries` 降为 0；所有重试由中间件负责。
- 默认 `max_retries` 调整到 10（可 env 覆盖），补 `empty_completion_retries`。
- 加可见输出边界与 partial-stub 例外测试；确认流式文本不重复。
- 加鉴权刷新/签名修复的额外尝试语义（若 provider 客户端已有刷新入口，复用而不是新写一套）。
- 验收：模拟失败处理器精确断言调用次数；流式重复文本回归；预算不被空补全/额外尝试误耗。

### Phase 4：可观测性与 e2e

- 轨迹 `model_retry` 事件；日志字段统一；可选 WS 状态帧。
- 用本地 mock provider 构造 429 → 成功、500 → 成功、Retry-After → 成功、abort、空补全 → 成功五条 e2e。
- 跑 `tests/run_tests_split.py` 与 `-m llm_e2e` 的受影响主链用例。
- 验收：一次真实回合在重试期间不产生重复消息/重复上下文块；轨迹可回读每次 attempt。

---

## 5. 测试矩阵

| 层 | 用例 | 断言 |
|---|---|---|
| 策略单测 | 指数/封顶/full-jitter/对称 jitter | 延迟落在边界内，attempt 递增 |
| 策略单测 | Retry-After 秒 / ms / 非法值 / 超上限 | 采用合法值，非法值回退指数，超上限被夹紧 |
| 分类单测 | 429/500/503/529/401/400/404/TLS/timeout/取消 | 与 ZCode 矩阵一致；不可重试不消耗预算 |
| 中间件 | 可重试失败 N 次后成功 | handler 调用次数准确，最终结果正确 |
| 中间件 | 不可重试错误 | 只调用一次，fallback 行为不变 |
| 中间件 | Retry-After | 等待值等于预期，不叠加 jitter |
| 中间件 | abort | sleep 立即退出，抛出取消而非继续尝试 |
| 中间件 | 空补全 | 最多额外 1 次，预算不误耗 |
| 中间件 | 已产出可见输出 | 不整段重试；partial-stub 例外单独验证 |
| 中间件 | fallback 切换 | 预算重置、总上限仍生效 |
| e2e | mock provider 429→200 | 一次用户消息、一次最终回答、轨迹含 retry 事件 |
| 冒烟 | 真实后端一回合 | 无重复消息、提醒块顺序不变 |

---

## 6. 接受标准

1. 一次用户回合最多只有一个最终回答；重试不产生重复流式文本或重复消息行。
2. 429/503 带 `Retry-After` 时按服务端等待（上限 300s），无头时按指数退避；等待可被停止打断。
3. 默认总尝试 11 次，实际尝试次数可从日志/轨迹核对；SDK 不再叠加第二层重试。
4. 空补全、鉴权刷新、签名修复的额外尝试不计入普通预算，且有独立计数。
5. 原有 `LLMRetryMiddleware` 的 fallback、内容过滤、压缩溢出、stale-streak 行为不回归。
6. `tests/run_tests_split.py` 全绿；受影响的 `-m llm_e2e` 用例全绿。

---

## 7. 风险与回滚

- **长回合**：11 次 × 60s 上限可能让回合挂很久。保留 `SHERRY_LLM_RETRY_MAX_RETRIES` 环境覆盖，默认先按 ZCode 10 次，观测后可按模型族下调。
- **重复流**：可见输出边界是硬门禁；partial-stub 例外若验证不了无重复，先关闭自动重生，保留已产出内容并显示错误。
- **双层重试**：Phase 3 才关 SDK 重试；Phase 2 之前不能声称“预算准确”，测试需按实际层数断言。
- **回滚**：配置默认回退到旧值（3/2/60/0.3），SDK `max_retries` 恢复 2，新增工具函数无副作用。
