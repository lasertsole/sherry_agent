# DeepSWE 本地测试失败原因分析

- 建档日期：2026-11-02
- 状态：**原因已定位，修复待做**
- 运行：`20261101_233217`，DeepSWE 默认切片，`--local --timeout 1200`
- 原始记录：[evals/deepswe/RESULTS.md](../evals/deepswe/RESULTS.md)
- 原始报告（gitignored）：`evals/results/deepswe-local/20261101_233217/`

## 0. 结论

这次 **0/2 不是模型完全没有做出来，也不是 GOLD/verifier 坏了**：

- GOLD 自检两任务均通过（`httpx` 108/108 F2P、`cattrs` 69/69 F2P 的参照补丁）。
- 两个任务的 agent 都在 1200 秒硬期限被杀；被杀前补丁已经接近通过：
  - `httpx`：104/108 F2P，1404/1404 P2P
  - `cattrs`：62/69 F2P，7/7 P2P
- 真正的失败链是：
  1. `DelegationPressureGate` 在 16 次读后拦截主 agent 的 `read_file` / `terminal` / `write_file` / `patch_file`；
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

`agent/middlewares/delegation_pressure/core.py` 在 16 次读后阻断
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

### 1.8 任务本身的剩余实现缺口

这些是超时前仍存在的真实代码问题，修完交接链后预计仍需处理：

- `httpx`：UTF-32/UTF-32-LE charset 检测、前导空格后的 BOM、JSON-seq UTF-32-LE
  共 4 个 F2P；
- `cattrs`：顶层 `partial_structure` 未导出（2 个）、`PartialResult.refine`
  没有合并修复值（4 个）、TypedDict 必填字段失败时返回部分 dict 而非 `None`（1 个）；
- 两个 patch 都带了 agent 自建的 scratch 文件（`test`、`VERIFY_REPORT.txt`、
  `scratch_check.py`），应在收尾时清理，评测报告也应把它们列为 out-of-scope 提示。

## 2. 修复 TODO

### P0 — 让完成消息真正到达 agent

- [ ] 在 eval harness（`evals/coding_capability/suite.py::_drive` 或
  `evals/deepswe/agent_worker.py`）启动一个与生产等价的完成消息消费者：
  - 方案 A：绑定一个假 WebSocket，让 `_route_completion_injection` 的 third path
    走 auto-turn/steering；
  - 方案 B：消费 `EventBus`，把 `subagent_result` 转成主 session 的
    `HumanMessage`/internal carrier，推入当前 turn 的 state；
  - 方案 C：让 `sessions_yield` 返回完成消息本身，而不是只返回一句“delivered”。
- [ ] 添加回归测试：主 agent 在 `sessions_yield` 后必须能在 transcript 中看到
  子代理结果；没有 WebSocket 的 eval 路径也必须可见。
- [ ] 验证生产路径没有被测试 harness 的改动绕过：third path 的 WS 检查、auto-turn
  deferral、steering queue 仍按原语义工作。

### P0 — 修复 `sessions_messages` 的兜底读取

- [ ] 复现 `sessions_messages` 在子代理结束后返回空的问题；比较
  `spawn` 返回的 `session_key`、`session_thread_id()` 映射、实际 checkpoint
  `thread_id`。
- [ ] 若 checkpoint 被 `_release_child_resources` 清理，保留 latest checkpoint
  的 messages 读取路径；若读的是不同 saver，统一到 `agent.state_port` 的同一个
  checkpointer。
- [ ] 测试：正常完成、超时、400 error、pending 后 killed 四种 run 都能读到最后一条
  assistant/tool 结果，或明确返回“已被清理”，而不是误导性的空。

### P1 — 压力门与交接语义

- [ ] 明确门的阻断集合：`sessions_yield` / `sessions_messages` / `subagents_list`
  不能被阻断；是否允许 `read_file` 读取子代理结果文件需要设计。
- [ ] 在门注入的 directive 中说明“结果会作为 completion 消息返回”，并给出
  `sessions_messages` 兜底；当前 directive 只说去 spawn。
- [ ] 增加“完成已交付/结果已消费”的可观测状态；主 agent 不应在结果已返回后继续
  spawn。
- [ ] 保留“spawn 不解除本回合、写不清零”的约束，不要回到首版软门的 patch 循环。

### P1 — DeepSeek thinking 重放

- [ ] 子代理显式继承/关闭 `LLM_THINKING_ENABLED`（至少 eval 子代理默认 false），
  或在 replay 时携带 thinking 的 `reasoning_content`。
- [ ] 对 400 的 run 标记为 provider invalid，不要让主 agent 误以为子代理完成了任务；
  当前两次 400 都消耗了完整 runtime。
- [ ] 加 DeepSeek thinking + tool-call replay 的回归用例（见记忆中的
  `deepseek-thinking-replay`）。

### P1 — 预算与编排

- [ ] 统计“子代理固定开销”（spawn 到完成约 3–5 分钟）并决定 eval 的 spawn 上限/
  并行策略；对于验证类工作，允许主 agent 在可用时直接执行，或把多个验证命令合并为
  一个子代理任务。
- [ ] 区分“任务预算”和“spawn 轮次预算”：在 1200 秒内不应该允许 4–6 次串行
  spawn。
- [ ] 硬超时保留；超时后至少从 worker 日志/checkpoint 收集 `tool_calls`、tokens
  和最后工具序列，避免 `tool_calls=0` 的采集缺口。

### P2 — 产物与任务实现

- [ ] 收尾清理 scratch 文件；在 eval registry/report 中记录 out-of-scope 文件。
- [ ] 修 `httpx` 的 UTF-32/UTF-32-LE + BOM 位置检测，覆盖 4 个失败 F2P。
- [ ] 修 `cattrs` 的顶层导出、`refine` 合并/必填字段语义，覆盖 7 个失败 F2P。
- [ ] 修完后用同一命令复跑，确认是 resolved 而不是“又接近了”。

## 3. 验收标准

1. 默认两条 DeepSWE 任务在无 WebSocket 的本地 eval harness 中，主 agent 的 transcript
   能看到每个子代理的 completion 文本；
2. `sessions_messages` 对已完成子代理返回非空 transcript，或明确说明 checkpoint 已清理；
3. 子代理不再因 DeepSeek `reasoning_content` 400 空转；
4. 同一 slice 在 1200 秒内不再出现“反复 spawn/yield 直到硬超时”；
5. 最后以 `resolve_rate` 和 F2P/P2P 数字验收，而不是只看补丁是否存在。
