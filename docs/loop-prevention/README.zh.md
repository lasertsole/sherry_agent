# 🔁 失控循环防护：护栏、熔断器与崩溃门控

[English](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> 一个常驻 Agent 如何阻止自己陷入死循环：针对模型调用与工具调用的每回合病理护栏，后台服务上的指数退避熔断器，子 Agent 之间可靠的完成通知投递，启动阶段的进程级崩溃门控，以及所有手段都失效之后的 REST / 手动逃生口。

失控循环（runaway loop）指任何系统自身无法跳出的循环：一次模型调用永远重复同一段文字、一次工具调用用同样失败的参数反复重试、心跳或 cron 任务朝着虚空空转、一次子 Agent 的完成通知永远到不了父 Agent（或者到达了两次）、又或者一个进程崩溃后被重启进同一次崩溃。这套 harness 无人值守地运行（cron 计划、心跳唤醒、子 Agent 清扫、漫长的对话回合），所以每一条循环都必须有一个由*系统本身*强制执行的边界，而不是一句"请你乖一点"的提示词。

以下每一条护栏都遵循两条设计规则：

1. **只降级，绝不崩溃。** 防护永远不会拖垮进程：后台服务*停止*，回合*优雅结束*，启动门控把进程*收缩*到纯 HTTP 模式。
2. **永远留一个逃生口。** 每个熔断器都有成文的手动重置方式（REST 端点、删除状态文件、或重启进程）。

**事实来源：** `agent/middlewares/tool_guardrails.py`、`agent/middlewares/iteration_budget.py`、`agent/middlewares/output_repetition_guard.py`、`agent/stream_repetition_guard_wrapper.py`、`agent/middlewares/heartbeat_staleness.py`、`agent/middlewares/subagent_completion_drain.py`、`agent/tools/subagent/announce/delivery.py`、`agent/tools/subagent/announce/idempotency.py`、`runtime/periodic_backoff.py`、`runtime/crash_loop_breaker.py`、`skills/builtin/core/cron/scripts/base.py`、`skills/builtin/core/heartbeat/scripts/base.py`、`agent/tools/subagent/registry/sweeper.py`、`server/__main__.py`、`server/trigger/http/cron.py`、`server/trigger/__init__.py`、`server/trigger/channels/core.py`。

## 🎯 总览与威胁模型

| 循环高度 | 表现形式 | 防御手段 |
|---|---|---|
| **文字死亡循环**（单次模型调用） | 同一句话 / 同一串字符被永远流式输出 | `OutputRepetitionGuard`（worker）+ `RepetitionGuardWrapper`（主 Agent 流式层） |
| **工具病理循环**（单回合） | 同一个失败的工具调用、乒乓配对、参数翻新 | `ToolGuardrails`：5 种病理 → WARN → BLOCK → HALT，带恢复模式 |
| **无界回合** | 模型 / 工具调用永不停止 | `IterationBudget`（主 Agent 90 次 / worker 60 次，合并计数） |
| **卡死回合** | 连续数分钟毫无进展（工具挂起、循环楔死） | `HeartbeatStaleness` 看门狗 → `HeartbeatTimeoutError` |
| **后台服务循环** | 心跳 / 清扫器 / cron tick 永远失败 | `PeriodicBackoff`（耗尽 = 服务停止）/ cron 退化 → 自动停用 |
| **完成通知丢失或重复** | 子 Agent 已完成，但父 Agent 永远收不到通知，或者收到两次 | 完成通知 drain（只注入一次）+ announce 重试阶梯 + 幂等键 |
| **崩溃重启循环** | 进程启动即崩溃，守护进程重启后又崩进同一个坑 | `CrashLoopBreaker` → 纯 HTTP 模式 |
| **其余一切** | 某个熔断器停用了任务，或触发了启动门控 | Cron REST `failure-state` / `reset-failures`，状态文件重置 |

防御按高度分层，一层漏掉的失败会落进下一层：

1. **回合级**：每回合护栏约束单次对话回合（文字、工具、迭代次数、心跳时效）。
2. **恢复间歇**：一次 `ToolGuardrails` BLOCK 会先获得一个*受管重试窗口*，之后才升级为 HALT。
3. **后台级**：周期性服务按指数退避，连续失败 5 次后*停止*。
4. **进程级**：崩溃重启循环会触发启动熔断器，以最小化的纯 HTTP 模式启动。
5. **运维层**：REST 端点与成文的手动重置。

## ⚙️ 实现与架构

### 回合级：`ToolGuardrails`，工具调用病理检测

作用于主 Agent、worker Agent 和 nudge 子 Agent。每次工具调用的记录都会在评估*之前*追加进每回合状态（`state_register_mem` 中的 `tool_guardrail_state`），所以阈值是自包含的："第 2 次之后"意味着当前调用就是第 2 次，而第 5 次相同的无进展调用本身就达到 np=5 并被拦截。升级阶梯：ALLOW → WARN（向对话记录追加一条 nudge）→ BLOCK（拒绝该调用并附上说明消息）→ HALT（回合以一条终止消息优雅结束，与迭代预算同一模式）。

| 病理 | 信号 | WARN 阈值 | BLOCK 阈值 | hard-stop 模式 |
|---|---|---|---|---|
| 完全相同的失败重复 | 同一工具 + 相同（哈希后的）参数持续失败 | 2 | 5 | 5 次时 HALT |
| 同工具失败风暴 | 同一工具持续失败，参数可以不同 | 3 | 8 | 8 次时 HALT |
| 幂等无进展 | 幂等工具返回完全相同（哈希后）的结果 | 2 | 5 | 5 次时 HALT |
| 乒乓 | 不间断的只读 A → B → A → B 往返 | 4 | 6 | 6 次时 HALT |
| 参数翻新 | 同一幂等工具轮换不同参数变体 | 3 种变体 | 5 种变体 | 5 种时 HALT |

值得注意的细节：

- **恢复模式**（`recovery_mode_enabled=True` 默认开启）：第一次 BLOCK 不会把回合打入死牢。回合进入恢复状态，*precheck* 路径会放行被拦的工具，让重试得到全新评估。此后每次 BLOCK 都会递增违规计数器；一旦计数超过 `recovery_max_violations`（默认 1），动作升级为 HALT。实际效果：一个受管的重试窗口，而不是一堵立即竖起的墙。想要旧的严格行为就设 `recovery_mode_enabled=False`，或者设 `hard_stop_enabled=True` 把每个 BLOCK 阈值都变成 HALT（见上表）。
- **乒乓配对**对相邻两次调用的工具名做哈希，且只在*连续两次*调用都是成功的幂等调用（两条记录都带结果哈希）时才累加。任何错误，或任何一次成功的非幂等（有副作用）调用，都会把所有已累计的配对连击清零。结果内容从不参与比较：不间断的只读往返本身就是循环信号。非幂等工具的成功同样会重置参数翻新状态。
- 护栏状态严格**回合作用域**：`before_agent` 会重置它，新回合从干净状态开始。

### 回合级：`IterationBudget`

每回合把模型调用和工具调用**合并**计数。主 Agent 90 次，worker Agent 60 次（基础默认 50）。预算耗尽时，模型调用返回一条终止 AIMessage；工具调用返回一条错误 ToolMessage，让模型得以收尾而不是死在循环中间。内部完成通知回合豁免（不消耗迭代次数）。计数器（`iteration_budget` / `iteration_budget_used`）每回合重置。

### 回合级：文字死亡循环，`OutputRepetitionGuard` + `RepetitionGuardWrapper`

**跨调用检测**（`OutputRepetitionGuard`，worker 管线中的中间件）：

- 内容先归一化（NFKC → 去空白 → 去标点），再以首尾各 500 字符做双 `head|tail` MD5 哈希，能抓住长输出任意一端的重复。每个会话保留 30 个哈希的滚动历史。
- 连续 2 次相同输出时 WARN（追加一条 nudge）；3 次时 HALT（终止消息；halt 标志在本回合内粘滞）。跨调用匹配只需 1 个字符的内容，所以哪怕只是一个短句在连续调用间反复出现，也是有效的死亡循环信号。
- 单次输出内的**内部检测**：重复片段占比 > 0.6（按标点 / 换行切分，至少 6 个片段）、字符连跑 ≥ 8、短句（2-10 字符）连续重复 ≥ 5 次。少于 20 字符的内容直接跳过以避免误报。内部警告每个标签每会话最多触发一次。
- **推理内容独立追踪**：`reasoning_content` / `reasoning` / `reasoning_text` kwargs 以及内联的 `<think>` / `<thinking>` / `<reasoning>` 包裹各自维护独立的历史与 warned 标志。
- 该中间件恰好持有六个会话状态键（`SESSION_STATE_KEYS`），子 Agent 派生 Agent 被拆除时一并释放；中间件之间没有状态泄漏。

**流式层**（`RepetitionGuardWrapper`，包裹主 Agent）：

- 在*流中途*切除内部重复，赶在 chunk 到达客户端之前：先注入一条警告，然后压制该次调用剩余的流。
- HALT 短路：本回合一旦记录过 halt，后续模型调用直接返回 halt 消息。
- **幽灵流防护**（可选，生产环境已启用）：当一次新的 dict 输入调用取代一个仍在运行中的 run 时，丢弃旧的更新前模型文本。

### 回合级：`HeartbeatStaleness`，卡死回合看门狗

每回合注册一个 1 分钟定时器（`timer_call_register`），比较 `heartbeat_iter` / `heartbeat_tool` 计数器与上次观测值。任何进展都会重置陈旧计数；空闲 Agent 连续 **7** 个周期（约 7 分钟）无进展，或处于工具内部时连续 **20** 个周期（约 20 分钟）无进展，就设置 killed 标志，下一次进入 agent 循环时抛出 `HeartbeatTimeoutError`，让回合优雅结束。主 Agent 与 worker Agent 都有注册；每回合状态在 `before_agent` 重置，定时器在 `after_agent` 注销。

### 管线级：子 Agent 完成 drain + announce 重试

**注入 drain**（`agent/middlewares/subagent_completion_drain.py`）：一个 `before_model` 中间件，负责把会话的 `SteeringQueue` 重新水合并排空，在下一次模型调用之前注入排队的完成载体。SQLite 行在 drain 时标记为 `CONSUMED`，因此检查点回放（HITL 恢复）绝不可能再次注入同一条完成通知。该中间件完全 fail-open：所有失败都记日志后吞掉，父回合在没有注入的情况下继续。它封死了"子 Agent 早已完成、父 Agent 却永远等待"这条循环。

**投递重试 + 幂等**（`agent/tools/subagent/announce/delivery.py`、`idempotency.py`）：忙会话的完成通知按固定阶梯重试瞬态失败（5s / 10s / 20s，上限 `announce_retry_max=3`；压缩错误用 1s / 2s / 4s / 8s）。永久失败从不重试。每次投递都以 `subagent_announce:{run_id}:gen:{generation}` 为键存入有界的内存幂等集合，所以重试的 announce 无法二次注入。重试耗尽 → run 置为 FAILED；软重试上限 → SUSPENDED；命中 `max_announce_retry_count`（10）次重试或超过 24 小时时限的 run 会被丢弃。加上清扫器的孤儿恢复，子 Agent 生命周期的投递侧就此有了边界。

### 后台级：`PeriodicBackoff`，一个熔断器，三个服务

`runtime/periodic_backoff.py` 是一台纯状态机（无线程、无 I/O）：

- `record_failure()`：`consecutive_failures += 1`；`current_interval = min(base × factor^n, max_interval)`；`consecutive_failures >= max_consecutive_failures` 时耗尽。
- `record_success()`：完全重置。默认值：`factor=2.0`、`max_interval=7200s`、`max_consecutive_failures=5`。

| 服务 | 基础间隔 | 失败间隔 | 耗尽后 |
|---|---|---|---|
| 心跳（`skills/builtin/core/heartbeat/scripts/base.py`） | 1800s（与 `HeartbeatConfig.interval_s` 一致） | 3600s → 7200s → 7200s → 7200s | CRITICAL 日志（"paused ... manual recovery required"）；循环直接返回，服务停止而进程继续活着 |
| 子 Agent 清扫器（`agent/tools/subagent/registry/sweeper.py`） | 60s（`sweeper_interval_seconds`） | 120s → 240s → 480s → 960s | CRITICAL 日志；`_running=False` 结束清扫任务 |
| Cron 任务熔断器（见下） | 按任务，5s 基础 | 退化 → 停用 | 任务自动停用 |

值得了解的语义：

- 心跳在其 tick *内部*记录成功（tick 自己吞掉错误），所以只有真正的 tick 失败才计数。暂停之后 `trigger_now()` 依然有效：手动戳一下就能绕过休眠中的循环。
- `stop_sweeper()` 会丢弃退避对象（`_backoff=None`），所以手动重启的清扫从全新状态开始。退避对象是懒创建的（`_get_backoff`），从不在 import 时创建。
- 生产环境中清扫器由 `server/trigger/channels/core.py` 的 `_schedule_sweeper` 启动，它把协程跳转到主事件循环上（`run_coroutine_threadsafe`）；该接线由 `tests/unit/server/test_sweeper_wiring.py` 覆盖。
- 退避状态存放在 Python 对象里：重启进程会重置心跳与清扫器的熔断器。

### 后台级：cron 任务失败熔断器

`skills/builtin/core/cron/scripts/base.py` 中的按任务状态机（`CronJobFailureState`，仅存内存；除 `enabled` 标志外从不写入 `cron_jobs.json`）：

| 连续失败次数 | 效果 |
|---|---|
| 1-4 | 任务正常失败：状态标记为 error，WS 铃铛通知 |
| ≥ 5（退化） | 退避窗口内跳过触发：距上次失败 `min(5000ms × 2^(n-5), 300000ms)` |
| ≥ 10 | 持久化 `enabled=False`；尽力而为的通知发往任务的 payload 频道 |

- **先记录再抛出：** 失败先被记录，然后异常原样重新抛出，状态 / 错误报告因此完好无损。
- **一次性 `at` 任务豁免**（它们不可能触发两次，所以单次失败不成其为循环）。
- 成功会彻底重置状态。手动 `enable_job` 会清除它；REST `reset-failures` 端点只在*熔断器自己*执行了停用时才重新启用，运维人员的主动停用因此得以保留。

### 进程级：`CrashLoopBreaker` + 启动门控

`runtime/crash_loop_breaker.py` 把启动日志持久化到 `src/data/boot_lifecycle.json`（键：`boots`，含 `{ts, clean, reason}` 条目，reason 上限 200 字符；`last_exit_clean` 一次性标记）：

| 参数 | 值 | 含义 |
|---|---|---|
| `TRIP_THRESHOLD` | 3 | 触发所需的不洁启动次数 |
| `WINDOW_S` | 300 | 5 分钟窗口内 |
| `RETENTION_S` | 3600 | 启动记录 1 小时后清除 |

启动序列（`server/__main__.py`），按顺序：

1. `was_last_exit_clean()` 在 `record_boot(clean=..., reason="startup")` 消费它**之前**读取一次性标记。
2. `atexit.register(mark_clean_exit)`：*优雅*关闭会把下一次启动标记为干净。这就是自愈机制；只要干净退出一次，旧的不洁记录就会淡出 5 分钟窗口。
3. 若已触发（5 分钟内 3 次以上不洁启动）：设置 `SHERRY_HTTP_ONLY=1`，记录 CRITICAL 日志，并以**纯 HTTP 模式**启动：
   - `init_agent_core()` 照常运行，聊天功能保持可用。
   - 跳过 curator 与 cron 后台初始化；`server/trigger/__init__` 跳过频道管理器与子 Agent 的 import，所以心跳服务和清扫器也永远不会启动。
   - HTTP/WS 路由与 cron REST API 保持在线。

手动重置：删除 `src/data/boot_lifecycle.json`，或者干脆干净退出一次，让窗口自然衰减。

### 分层矩阵：哪一层接住什么

| 层 | 机制 | 接住什么 |
|---|---|---|
| 中间件（图内，每回合） | `ToolGuardrails`、`IterationBudget`、`OutputRepetitionGuard` / `RepetitionGuardWrapper`、`HeartbeatStaleness`、`SubagentCompletionDrain` | 工具病理、无界回合、文字死亡循环、卡死回合、丢失的完成注入 |
| 进程（后台服务） | `PeriodicBackoff`（心跳、清扫器）、cron 失败熔断器、announce 重试阶梯 + 幂等 | 服务重试风暴、失败的定时任务、重复的完成投递 |
| 启动（进程生命周期） | `CrashLoopBreaker`、`server/__main__` 门控、`trigger.__init__` 提前退出 | 崩溃重启循环 |
| 基础设施 / 运维 | cron REST 逃生口、HTTP-only 环境变量、状态文件删除 | 需要运维介入才能解开的熔断状态 |

## 📊 优先级矩阵

| 护栏 | 高度 | 状态所在 | 重置时机 |
|---|---|---|---|
| `ToolGuardrails` | 回合（工具调用） | `state_register_mem`（`tool_guardrail_state`） | 每回合（`before_agent`） |
| `IterationBudget` | 回合（调用计数） | `state_register_mem` | 每回合 |
| `OutputRepetitionGuard` | 回合 + 会话（文本） | 6 个会话键 | halt 标志按回合；哈希历史按会话（子 Agent 拆除时释放） |
| `RepetitionGuardWrapper` | 流式调用（文本） | in-flight + halt 键 | 每次模型调用 |
| `HeartbeatStaleness` | 回合（墙上时钟） | `heartbeat_*` 键 + 1 分钟定时器 | 每回合 |
| `SubagentCompletionDrain` | 回合（注入） | SteeringQueue 行（SQLite） | drain 时行标记为 CONSUMED |
| Announce 重试 + 幂等 | Run（投递） | 内存幂等集合 + run 记录 | 成功 / 重试上限 / 24 小时过期 |
| `PeriodicBackoff`（心跳 / 清扫器） | 服务（tick） | Python 对象 | 成功 / 进程重启 / `stop_sweeper` |
| Cron 失败熔断器 | 任务（触发） | 内存中 `CronJobFailureState` | 成功 / `reset-failures` / 手动 `enable_job` |
| `CrashLoopBreaker` | 进程（启动） | `src/data/boot_lifecycle.json` | 干净退出衰减 / 文件删除 |

在一个回合之内，各护栏彼此正交、并行生效：`OutputRepetitionGuard` / `RepetitionGuardWrapper` 看守*文本*，`ToolGuardrails` 看守*工具调用*，`IterationBudget` 看守*次数*，`HeartbeatStaleness` 看守*墙上时钟*。谁先触发谁结束回合，互不阻塞。如果它们全部漏网，后台熔断器约束*下一次*触发，启动熔断器约束*下一个*进程。

## 🛠️ 配置与使用

- **所有阈值都是代码默认值**（dataclass / 构造函数参数）；有意不为它们提供环境变量。值得注意的是，`config/schema.py` 的 `max_tool_iterations = 40` *并未*被中间件消费（预算是显式传入的：90 / 60），`HeartbeatConfig.interval_s = 1800` 与心跳服务默认值一致但服务是以默认参数构造的。
- `TOOL_CALL_TIMEOUT_MINUTES`（`.env.example` 中默认 5）目前**只存在于文档里**：没有任何代码消费它。实际生效的每工具边界是常量（web search 15s，terminal 30s，python REPL 30s）。不要把它当作循环边界。
- Worker 以中间件形式获得 `OutputRepetitionGuard`；主 Agent 由 `RepetitionGuardWrapper` 包裹（中间件钩子看不到原始流式 chunk）。
- `ToolGuardrails` 旋钮：`warnings_enabled`（默认 True）、`hard_stop_enabled`（默认 False，BLOCK 仍是拦截）、`recovery_mode_enabled`（默认 True）、`recovery_max_violations`（默认 1）。
- Announce 投递旋钮（子 Agent announce 配置）：`announce_retry_max=3` 配 5s / 10s / 20s 瞬态延迟，另有 `max_announce_retry_count=10` 和 24 小时 run 过期。

手动恢复速查表：

| 情形 | 动作 |
|---|---|
| Cron 任务被熔断器自动停用 | `POST /cron/reset-failures {"id": ...}`（只重新启用被熔断器停用的任务） |
| 查看某个 cron 任务的熔断状态 | `POST /cron/failure-state {"id": ...}`（未知任务 → 404，从未失败的任务 → 全零状态） |
| 心跳已暂停（5 次 tick 失败） | 重启进程；`trigger_now` 仍能触发一次性 tick |
| 清扫器已停止（退避耗尽） | 重启进程；新的清扫从全新退避开始 |
| 启动门控已触发（HTTP-only） | 干净退出一次，或删除 `src/data/boot_lifecycle.json` |

## 🧪 测试

| 测试套件 | 覆盖内容 |
|---|---|
| `tests/unit/middlewares/test_tool_guardrails.py` | 病理检测、升级阶梯、恢复模式 |
| `tests/unit/runtime/test_periodic_backoff.py` | 间隔数学、耗尽、成功重置 |
| `tests/unit/runtime/test_crash_loop_breaker.py` | 触发窗口 / 保留期、干净标记、损坏状态 |
| `tests/unit/cron/test_cron_failure_breaker.py` | 退化 → 停用、重置语义 |
| `tests/unit/heartbeat/test_heartbeat_backoff.py` | 服务退避接线、耗尽暂停 |
| `tests/unit/subagent/test_sweeper_backoff.py` | 清扫器退避接线、循环停止 |
| `tests/unit/server/test_sweeper_wiring.py` | 清扫器启动接线 |
| `tests/unit/server/test_crash_gating.py` | 启动门控、HTTP-only 模式 |
| `tests/unit/server/test_cron_api.py` | Cron REST，含 failure-state / reset-failures |

## ⚠️ 诚实与局限

- **回合护栏按设计就是回合作用域**：新回合从全新的护栏状态开始。跨回合重复检测是 `OutputRepetitionGuard` 的领域（会话作用域历史），不是工具护栏的。
- **内存中的熔断状态活不过重启**：护栏 / 重复 / 迭代状态本来就是回合或会话作用域；cron 失败计数器在进程重启时丢失（但持久化的 `enabled` 标志不会）；心跳 / 清扫器退避存在于 Python 对象中。因此重启永远是一次重置，有时是一次过于慷慨的重置。
- **耗尽后服务停止，直到重启**：暂停的心跳或停止的清扫器没有运行时重新武装的 API（"manual recovery required" 是字面意思）。进程本身继续提供服务。
- **崩溃门控基于窗口**：间隔超过 5 分钟的崩溃循环永远不会触发它，任何删掉状态文件的守护进程都会把它重置。该文件既是熔断器的记忆，也是手动逃生口。
- **`hard_stop_enabled` 默认为 False**：严格模式下，只有同工具失败和被转换成 hard-stop 的 BLOCK 能到达 HALT；其他病理停在 BLOCK（受恢复模式约束）。
- **内容归一化是双刃剑**：去空白 / 去标点让哈希对格式噪声免疫，但每次*换一种说法*重复循环的模型能躲开基于哈希的检测。内部片段 / 连跑检测器部分覆盖了这一点；完全改写的循环不在范围内。
- **恢复模式给了模型失败的空间**：顽固的病理在 HALT 之前要付出一次受管重试的代价。想要立即竖墙的运维应设 `recovery_mode_enabled=False`。
- **`TOOL_CALL_TIMEOUT_MINUTES` 有声明但无人读取**：它存在于 `.env.example`（根 README 也有描述），但今天没有任何代码消费它；上文列出的每工具常量才是真正的边界。
- **HTTP-only 模式是收缩后的足迹，不是封锁**：聊天、HTTP/WS 路由和 cron REST 按设计保持在线；目标是打断*崩溃循环*，不是把进程与世隔绝。
