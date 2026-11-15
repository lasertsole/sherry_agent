# DeepSWE 本地测试失败原因分析

- 建档日期：2026-11-02（2026-11-15 更新）
- 状态：**2026-11-16 httpx 已 resolved（1/1，108/108）；cattrs 仍 0/1（refine 语义缺口）；交接链、DeepSeek reasoning、硬超时遥测、provider 分类/客户端重建均已落地**
- 最新运行：`20261115_233700`，DeepSWE 默认切片，`--local --timeout 1200`
- 历史运行：`20261101_233217`（0/2 硬超时）、`20261102_083002`（cattrs 0/1）
- 原始记录：[evals/deepswe/RESULTS.md](../evals/deepswe/RESULTS.md)
- 最新报告（gitignored）：`evals/results/deepswe-local/20261115_233700/`


## 2026-11-16 httpx 复跑（最新）

- 命令：`uv run --no-sync python evals/deepswe/suite.py --local --timeout 1200 httpx-streaming-json-iteration`
- 结果：**1/1 resolved**；108/108 F2P、1404/1404 P2P；70 次工具调用 / 808.6s；
  tokens 2,919,410 / 18,972。
- 卡住根因：上一轮在第 37 次调用后连续 5 次流停滞。用该会话最后的 73 条消息 +
  **新建客户端**复现，2.5s 就完成，说明不是对话/payload；是 agent 中间执行了
  `sleep 60` 的 terminal 命令，长时间空闲后复用了 provider/代理已关闭的
  keep-alive 连接。5 次重试都在同一个坏连接池上等满 60s 读超时，才看起来像
  “provider 连续挂 5 次”。
- 修复：`NormalizingChatModel.reset_async_client()` 在超时后关闭并重建 inner 的
  HTTP 客户端（fresh connection pool）；`LLMRetryMiddleware` 在下一次重试前调用它。
  stale-streak 熔断保留，用于真正无响应的 provider。
- 验证：新增客户端重建单测 + retry 路径单测；httpx 复跑 70 次调用、808.6s，
  未再触发熔断，最终 108/108。

## 2026-11-15 复跑（上一轮）

首次复跑在 worker 启动前就失败：`bind_external_session` 仍读
`payload["tools"]`，但 preset 自 2026-11-05 起只携带 `skills`（工具是构建期
契约）。两个任务都得到 `KeyError: 'tools'`、`tool_calls=0`，被误记为 harness
error。该读取已移除并加回归测试，随后重跑。

`20261115_233700` 结果：

| 任务 | Resolved | F2P | P2P | 调用 | 耗时 | 判定 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `httpx-streaming-json-iteration` | ✗ | **107/108** | 1404/1404 | 37 | 346.3s | provider stall；按修复后的分类是 invalid |
| `cattrs-partial-structuring-recovery` | ✗ | **62/69** | 7/7 | 51 | 411.8s | 有效运行，未解决 |

关键结论：

1. **交接链问题已不再是本轮失败原因**：两个任务都在 1200s 前自然结束，没有
   出现“反复 spawn/yield 直到硬超时”；completion 注入/回退链路由既有回归测试
   覆盖，本轮未再触发空读。
2. **P0-3 遥测已在真实运行中生效**：任务进行中的 `agent-result.json` 为
   `partial: true`、`tool_calls: 37`、`tokens={input:1047513, output:8320}`；
   即使硬超时被杀，父进程也不会再报 0。
3. **httpx 只差 1 个 F2P**：
   `test_iter_json_document_streaming_chunk_boundaries[chunks2-expected2]`。
   它在 37 次调用后连续 5 次流停滞，`LLMRetryMiddleware` 以
   `Provider unresponsive` 中止；补丁当时已 107/108 F2P、1404/1404 P2P。
   该消息原先不在 provider 标记里，被误分类为 `harness`；已加入
   `provider unresponsive` 并测试，之后这类运行记为 `provider` / invalid，
   不计入 resolve rate。
4. **cattrs 是真实能力缺口**：7 个 F2P 失败集中在
   `PartialResult.refine`——没有把 refine 后的新值合并回原始输入并重新结构化，
   必填字段失败时返回部分 dict 而不是 `value=None`；P2P 7/7 无回归。
5. **剩余工作**：修 cattrs 的 refine/必填字段语义；httpx 的 BOM-after-leading-
   space 用例（需 provider 稳定后重跑确认）；`models/runtime` 等 wiki 覆盖问题
   不属于 DeepSWE。

## DeepSWE 错误原因与修复方法（汇总）

| 问题 | 现象 | 根因 | 修复方法 | 状态 |
| :--- | :--- | :--- | :--- | :--- |
| `KeyError: 'tools'` | worker 启动即失败，`tool_calls=0`，两任务都成 harness error | preset 自 2026-11-05 起只携带 `skills`，工具是构建期契约；`bind_external_session` 仍读 `payload["tools"]` | 删除该读取，写入 skills-only payload；加 `test_bind_external_session_accepts_the_skills_only_payload` | 已修（2026-11-15） |
| 硬超时遥测丢失 | 1200s 被杀后 `tool_calls=0`、`tokens={}` | worker 在 SIGKILL 前没有写出最终 `agent-result.json` | `_drive` 逐帧回调；worker 每秒原子写 partial、token 每 10s 刷新；父进程超时后读 partial | 已修；实测 `partial: true`、`tool_calls=37`、tokens 非空 |
| 子代理完成消息不可见 | 日志显示 delivered，主 transcript 没有结果；只能反复 spawn | EventBus 只投递到 channel/WebSocket，本地 eval 没有 WS | 方案 C：busy 主 session 写入 `SteeringQueue`，`SubagentCompletionDrainMiddleware` 下一模型调用前注入 | 已修（2026-11-02）+ 回归测试 |
| `sessions_messages` 返回空 | 子代理结束后读不到 transcript，主 agent 没有兜底 | checkpoint 可能被 prune/cleanup，或子代理与读取方不是同一个 saver/DB | `_capture_child_transcript` 在释放前抓最后 30 条消息到 `completion.transcript_tail` 并立即持久化；`sessions_messages` 空 checkpoint 时回退 | 已修（2026-11-15）+ 7 个测试 |
| 子代理 DeepSeek thinking 400 | `reasoning_content must be passed back`，子代理空转 | 辅助/per-role 家族用普通 `ChatDeepSeek`，工具调用重放时不带 `reasoning_content` | 统一走 `build_reasoning_aware_chat_model`：DeepSeek→`ReasoningDeepSeekChat`，GLM→`ReasoningChatOpenAI` | 已修（2026-11-15）；真实 thinking + tool-call replay 冒烟通过 |
| provider stall 误分类 | `Provider unresponsive` 被记为 `harness`，污染能力分母 | provider 错误标记缺少这条文本 | 加入 `provider unresponsive` → `failure_kind=provider`、`invalid_run=True`，不计入 resolve rate；另加超时后客户端重建 | 已修 + 单测；2026-11-16 复跑未再触发 |
| 压力门放大故障 | 16 次读后读写被禁用，主 agent 只能继续 spawn | 旧 gate 是硬阻断，且完成消息不可见 | gate 改为 advisory（7/12 次读只发 reminder，不阻断工具） | 已修（历史） |
| `cattrs` 未解决 | 62/69 F2P，7/7 P2P | `PartialResult.refine` 没有把新值合并回输入并重新结构化；必填字段失败应返回 `value=None` | 需改任务实现（`partial.py` / `converters.py` / 顶层导出） | **未解决**（真实能力缺口） |
| `httpx`（已解决） | **108/108 F2P**，1404/1404 P2P | 上一轮卡在坏 keep-alive 连接；唯一剩余 BOM 用例在客户端重建后补上 | 超时后重建 provider HTTP 客户端；2026-11-16 单任务复跑 1/1 | **已解决**（2026-11-16） |

### 修复后的验收口径

- provider abort / stall 记为 `invalid_run`，不进入 resolve rate；
- 硬超时不再丢 `tool_calls`/tokens；
- 任务“没跑起来”（`KeyError`、worker 启动失败）与“模型没修好”（F2P 未过）分开记录；
- 最新复跑：`httpx` 107/108 F2P（provider invalid），`cattrs` 62/69 F2P（有效未解决）。

## 进度（2026-11-02）

方案 C 已实现：没有 WebSocket 但主 session busy 时，完成消息进入
`SteeringQueue`；`SubagentCompletionDrainMiddleware` 在下一模型调用前注入。
`cattrs` 本地复跑日志已出现 `completion drain: injecting 1 subagent completion
message(s)`，主 agent 的 transcript 也确实看到了子代理结果。

但同一次复跑仍以 0/69 F2P 结束（537.5s，无超时）：第一个实现子代理以
DeepSeek `reasoning_content` 400 失败；之后主 agent 被当时的压力门设计限制在“不能读写、
只能 spawn”的状态，只能反复派只读状态检查，无法修改 `src/cattrs/partial.py`。
所以 C 解决的是“结果不可见”，**没有解决压力门让主 agent 无法修复的问题**。
下一步优先处理 P1 的压力门恢复窗口和 P1 的子代理 thinking 400。


## 0. 结论（2026-11-01 历史；最新状态见上节）

这次 **0/2 不是模型完全没有做出来，也不是 GOLD/verifier 坏了**：

- GOLD 自检两任务均通过（`httpx` 108/108 F2P、`cattrs` 69/69 F2P 的参照补丁）。
- 两个任务的 agent 都在 1200 秒硬期限被杀；被杀前补丁已经接近通过：
  - `httpx`：104/108 F2P，1404/1404 P2P
  - `cattrs`：62/69 F2P，7/7 P2P
- 真正的失败链是：
  1. 当时的 `DelegationPressureGate` 在 16 次读后拦截主 agent 的工具调用；当前实现只在 7/12 次读时发 `<system-reminder>` 提示，不阻断工具；
  2. 主 agent 按指令 `sessions_spawn`，只把验证/清理交给子代理；
  3. 子代理正常完成，但 **完成消息没有进入主 agent 的 transcript**；
  4. `sessions_messages` 读子代理 checkpoint 也返回空，主 agent 没有任何回读路径；
  5. 主 agent 只能一轮轮重新 spawn/yield；每次 spawn 约 3–5 分钟，4–6 轮耗尽 1200 秒；
  6. 其中 2 个子代理还以 DeepSeek `reasoning_content` 400 结束，进一步浪费一轮。

因此这是 **评测 harness + delegation 交接路径 + 时间预算** 的组合问题；任务本身的实现缺口是次要原因。

## 1. 证据

### 1.1 主 agent 进入了“被阻断后反复 spawn”的循环

`httpx` 主 transcript：

- 第 47 条注入 `[SYSTEM DIRECTIVE: DELEGATION PRESSURE — READ/WRITE DISABLED]`；
- 第 48 条主 agent 说 “my read/write tools are paused”，随后 spawn；
- 第 51 条 `sessions_yield` 返回 “All subagents have completed. Their results have been delivered to you.”；
- 第 52–53 条主 agent 调 `sessions_messages`，返回
  `No messages found for session ... (the checkpoint may be empty or the session was purged).`；
- 第 56、66、75 条又连续 spawn；第 77 条再次 `sessions_yield` 时 worker 被硬超时杀掉。

`cattrs` 主 transcript 更直接：

- 第 133 条注入读/写禁用；
- 第 144 条：“The subagent's result was delivered but I don't see the content.”
- 第 147 条：“Tools are gated this turn. I'll spawn an executor to run the verification and report back.”
- 第 155 条：`sessions_messages` 仍为空；
- 第 175 条主 agent 自己总结：“I'm stuck in a loop where subagent results aren't surfacing and tools are gated.”
- 第 180、187 条继续 spawn；第 189 条 `sessions_yield` 后硬超时。

### 1.2 子代理本身大多正常完成

`subagent_registry.db` 中 10 个 run：

| 项目 | 数量 |
| :--- | :--- |
| `outcome=ok` | 8 |
| `outcome=error`（DeepSeek 400） | 2 |
| 单子代理耗时 | 约 12.5s–329s；多数 193–329s |
| 被主 agent 用于验证/清理的 executor | 6 个 |
| researcher | 2 个 |
| 仍 pending 的 run | 1 个（httpx 第 4 次 spawn 后硬超时） |

完成 run 的 `completion.result_text` 通常非空，例如
`All steps complete. Final verification runs are consistent.`，说明子代理确实跑了测试/命令，
不是空转。问题是主 agent 看不到这些文本。

### 1.3 完成消息的“投递成功”在评测进程里是假成功

关键代码路径：

- `agent/tools/subagent/announce/core.py`：子代理完成后调用
  `deliver_subagent_announcement`，日志写 `Announce delivered ... via direct-primary`。
- `agent/tools/subagent/announce/delivery.py::_deliver_completion_message`：
  把结果发布到进程内 `EventBus`。
- `agent/tools/subagent/events/bridge.py`：EventBus 的唯一消费者只把消息发到
  **channel 或 WebSocket**；没有目标时 log-and-drop。
- 同一文件的 `_route_completion_injection`（third path）第一道门是：
  `if _get_bound_websocket(bare) is None: return`。

`evals/coding_capability/suite.py::_drive` 直接调用 `async_generate`，没有绑定 WebSocket，
也没有启动 `server.trigger.subagent.start()` 的 EventBus 消费者。因此：

- `publish_internal` 返回成功；
- `Announce delivered` 日志成立；
- 但消息没有回到主 agent 的 turn pipeline；
- 主 transcript 中没有任何 `subagent_result` completion 消息（只有 spawn/yield 工具结果）。

这是本次失败的第一根因，优先级最高。

### 1.4 `sessions_messages` 回退路径也失效

主 agent 在第 52/60/70/142/155/174 条调用 `sessions_messages`，全部返回
“No messages found”。该工具本应是完成消息丢失后的兜底
（`agent/tools/subagent/tools/sessions_messages.py`），但：

- 子代理 checkpoint 在 run 结束后仍存在（本次 checkpoint DB 约 155 MB，包含大量子线程）；
- 读取按 `session_key` 推导 thread id；
- 评测环境中返回空，需要单独复现是 **thread id 映射不一致**、**checkpoint 被清理**
  还是 **`agent.state_port` 读到的主/子 saver 不是同一个**。

在 1.3 修好之前，sessions_messages 是唯一可行的兜底；它现在不能兜底，导致循环没有出口。

### 1.5 压力门的设计放大了故障

`agent/middlewares/delegation_pressure/core.py` 当时在 16 次读后阻断
`read_file` / `search_files` / `terminal` / `write_file` / `patch_file` 等。
当前实现还有两个交互问题：

1. 阻断后主 agent 仍需要确认子代理结果、跑最终测试、清理 scratch 文件；这些都被
   `terminal`/`read_file` 阻断，只能再 spawn，形成 **“读不到结果 → 再 spawn”** 的放大回路。
2. 子代理完成消息不可见时，门没有任何“完成已交付”的确认信号，主 agent 误以为还要继续
   等待或重试。`sessions_yield` 的 “All subagents have completed” 反而掩盖了投递丢失。

这不是说门应该恢复“spawn 后解锁”（首版软门正是因此出现 patch 循环），而是门与
completion delivery 必须一起设计：**至少允许读取已经投递的子代理结果/文件**，或者让
完成消息真正进入下一轮模型上下文。

### 1.6 DeepSeek 子代理 400：thinking 重放问题仍在

本次 2/10 子代理以：

```
Error code: 400 - The `reasoning_content` in the thinking mode must be passed back to the API.
```

结束。主 agent 的 `LLM_THINKING_ENABLED` 被评测设为 false，但子代理没有稳定继承这个
设置，或子代理的 DeepSeek thinking 请求在工具调用重放时没有带 `reasoning_content`。
两个 400 run 各浪费约 205–240 秒，且结果为空；主 agent 只能再 spawn。

### 1.7 硬超时与预算

- 每个正常 executor 约 200–330 秒；
- 主 agent 在 1200 秒内最多只能完成 3–4 轮“spawn → yield → 再 spawn”；
- 两个任务分别做了 4 次和 6 次 spawn（含 pending / 400 retry），预算在完成前耗尽；
- `tool_calls=0`、`tokens={}` 是硬超时杀掉 worker 后没有写出 result JSON 的
  **采集缺口**，不是主 agent 没有调用工具。

### 1.8 任务本身的剩余实现缺口（2026-11-01 历史；最新数量见上节）

这些是超时前仍存在的真实代码问题，修完交接链后预计仍需处理：

- `httpx`：UTF-32/UTF-32-LE charset 检测、前导空格后的 BOM、JSON-seq UTF-32-LE
  共 4 个 F2P；
- `cattrs`：顶层 `partial_structure` 未导出（2 个）、`PartialResult.refine`
  没有合并修复值（4 个）、TypedDict 必填字段失败时返回部分 dict 而非 `None`（1 个）；
- 两个 patch 都带了 agent 自建的 scratch 文件（`test`、`VERIFY_REPORT.txt`、
  `scratch_check.py`），应在收尾时清理，评测报告也应把它们列为 out-of-scope 提示。

## 2. 修复 TODO

### P0 — 让完成消息真正到达 agent

- [x] **方案 C 已落地（2026-11-02）**：`_route_completion_injection` 不再要求
  WebSocket 才入队；主 session busy 时统一写 `SteeringQueue`，由
  `SubagentCompletionDrainMiddleware` 在下一模型调用前注入；只有 idle 且无 WS
  时才跳过 auto-turn。本地 `cattrs` 复跑已看到
  `completion drain: injecting 1 subagent completion message(s)`。
- [x] 添加回归测试：`test_no_ws_busy_routes_to_steering` +
  `test_busy_parent_without_websocket_reaches_steering`；现有 WS busy / idle /
  consumed / error 路径仍通过。
- [x] 验证生产路径没有被测试 harness 的改动绕过：third path 的 WS 检查、auto-turn
  deferral、steering queue 仍按原语义工作（C 改动已保留 idle+WS→auto-turn）。
  2026-11-15 复跑未再出现 completion 丢失或 spawn 循环。

### P0 — 修复 `sessions_messages` 的兜底读取

- [x] **已修复（2026-11-15）**：`_capture_child_transcript` 在 checkpoint
  关闭/裁剪前把最后 30 条消息写入 run 的 `completion.transcript_tail`，
  `sessions_messages` 在 checkpoint 为空时回退到该快照；不再依赖 thread-id
  映射或 checkpoint 是否还在。
- [x] 回退快照在 `_release_child_resources` 之前抓取并立即持久化，覆盖
  cleanup/prune/跨 DB 三种情况。
- [x] 测试：`tests/agent/tools/subagent/spawn/test_transcript_capture.py` +
  `tests/agent/tools/subagent/test_sessions_messages.py` 覆盖角色/工具调用、
  截断、读失败 fail-open、空 checkpoint 回退。

### P1 — 压力门与交接语义

- [x] 门的阻断集合：当前 gate 只发 advisory `<system-reminder>`，不阻断任何工具。
- [x] directive 已说明 completion 消息与 `sessions_messages` 兜底（当前 gate advisory）。
- [x] 完成交付/结果消费由 completion drain 与 registry 状态观测。
- [x] 成功 accepted/pending 的 `sessions_spawn` 解除阻断并清零重新计数；
  失败 spawn 和写本身不清零。后续需观察这是否重新引入 patch 循环，必要时加
  spawn 次数/无进展上限。

### P1 — DeepSeek thinking 重放

- [x] **已修复（2026-11-15）**：辅助模型和 per-role/taskflow/compression 家族
  统一走 `build_reasoning_aware_chat_model`；DeepSeek 使用
  `ReasoningDeepSeekChat` 回传 `reasoning_content`，GLM 使用
  `ReasoningChatOpenAI`。真实 thinking + tool-call replay 冒烟通过。
- [x] provider stall/400 的分类：`provider unresponsive` 现归入 provider 标记，
  `invalid_run=True`，不计入 resolve rate。
- [ ] 加 DeepSeek thinking + tool-call replay 的回归用例（见记忆中的
  `deepseek-thinking-replay`）。

### P1 — 预算与编排

- [ ] 统计“子代理固定开销”（spawn 到完成约 3–5 分钟）并决定 eval 的 spawn 上限/
  并行策略；对于验证类工作，允许主 agent 在可用时直接执行，或把多个验证命令合并为
  一个子代理任务。
- [ ] 区分“任务预算”和“spawn 轮次预算”：在 1200 秒内不应该允许 4–6 次串行
  spawn。
- [x] **已修复（2026-11-15）**：worker 每秒原子写 partial result，token 每 10s
  刷新；父进程在硬超时 SIGKILL 后读取 partial，`tool_calls`/tokens 不再归零。
  真实运行中已观察到 `partial: true`、`tool_calls=37`、tokens 非空。
- [x] **已修复（2026-11-16）**：超时后重建 provider HTTP 客户端，避免长空闲后的
  坏 keep-alive 连接被后续重试复用；httpx 复跑 1/1 resolved。

### P2 — 产物与任务实现

- [ ] 收尾清理 scratch 文件；在 eval registry/report 中记录 out-of-scope 文件。
- [ ] 修 `httpx` 的 BOM-after-leading-space 用例（最新 run 只剩 1 个 F2P；
  旧 run 为 4 个）。
- [ ] 修 `cattrs` 的顶层导出、`refine` 合并/必填字段语义，覆盖 7 个失败 F2P。
- [ ] 修完后用同一命令复跑，确认是 resolved 而不是“又接近了”。

## 3. 验收标准（2026-11-15 状态）

1. [x] 默认两条 DeepSWE 任务在无 WebSocket 的本地 eval harness 中，主 agent 的
   transcript 能看到每个子代理的 completion 文本（方案 C + 回归测试；最新复跑
   未再出现超时循环）。
2. [x] `sessions_messages` 对已完成子代理返回非空 transcript，或明确说明 checkpoint
   已清理（`transcript_tail` 回退，2026-11-15）。
3. [x] 子代理不再因 DeepSeek `reasoning_content` 400 空转（所有模型家族统一走
   reasoning-aware 客户端；真实回放冒烟通过）。
4. [x] 同一 slice 在 1200 秒内不再出现“反复 spawn/yield 直到硬超时”（最新复跑
   346.3s / 411.8s 自然结束）。
5. [ ] 以 `resolve_rate` 和 F2P/P2P 数字验收：DeepSWE **1/2**；`httpx` 已
   **1/1 resolved**（108/108 F2P、1404/1404 P2P）；`cattrs` 仍 0/1（62/69 F2P、
   7/7 P2P），剩余是任务实现缺口，不是交接链或 provider。
