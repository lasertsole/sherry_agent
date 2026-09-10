- SKILL.md存放位置从只能存放在skills 细化到 只能存放在skills/builtin, skills/auto, skills/plugins 目录下
- 1.创建pub文件夹 2.pub_func文件夹 改名为 func文件夹，然后放进 pub文件夹下。然后解决一下关联import变动 3.type文件改名为types,放进pub文件夹下 然后解决关联影响，4.扫描整个项目，将项目内公用类 放 types文件夹下
- 补充 bwrap/Seatbelt 真实 Linux/macOS 集成测试（审计 #1 D项）。当前 sandbox 构造逻辑仅单测验证，需在真实环境中验证：①Linux bubblewrap 的 `--ro-bind`/`--unshare-all`/`--clearenv` 实际隔离生效；②macOS Seatbelt 的 `(deny file-write*)` + allowlist 实际拦截写操作；③探针 `probe()` 在无 bwrap/seatbelt 工具时正确返回 False。目标：从"单测验证构造逻辑"升级为"真实环境验证沙箱实际生效"。涉及文件：`agent/tools/pub_base/sandbox_bwrap.py`、`agent/tools/pub_base/sandbox_seatbelt.py`、`tests/integration/test_sandbox_matrix.py`。 执行完后删除TODO/AUDIT_REPORT.md的 ## 1. `terminal.py` 黑名单可被轻易绕过；`shell=True` 项
- TODO/AUDIT_REPORT.md的## 2. `clawhub` 运行任意远程 npm 代码用HITL的方式防护（要求显式用户确认），其他的不改，然后删除 ## 2. `clawhub` 运行任意远程 npm 代码项
- 解决TODO/AUDIT_REPORT.md 的 12,14,15,20,23,25,26,27,32,34,35,39,44,46,44,46， 修补过程中不要留修补注释，修补完后 冒烟+回归测试，测完后删除AUDIT_REPORT.md中已完成项，并给AUDIT_REPORT.md重写编排，清除历史处理痕迹
- 审计 #20 方式A修复：在 `agent/tools/subagent/registry/lifecycle.py` 中新增 `sweep_stale_lifecycle_state()` 函数，遍历 `_terminal_locks`/`_cleanup_generations`/`_deferred_cleanup_timers` 三个模块级 dict，对 `get_run(run_id) is None` 的条目执行 pop + cancel timer。在已有周期任务（`recover_orphaned_runs` 或 `finalize_suspended_deliveries` 的调度循环）中调用。同时补全 `complete_subagent_run` 早返回路径（line 62 "already being completed"、line 67 "run is None"、line 78 "generation stale"）中对 `_terminal_locks` 的 pop。约 +15-20 行，纯增量清理，零破坏性。
- 审计 #25 方式A修复：从 `server/trigger/http/subagent.py` 的 `post_subagent_run_handler` 中移除 HTTP body 对 `max_spawn_depth` / `max_children_per_agent` 的接收，改为始终从 `get_config()` 读取配置值（对齐 OpenClaw 的设计：config 是限制参数的唯一来源，请求体不可覆盖）。需先确认前端是否在发送这两个参数——若前端未发送则直接移除；若前端在发送则通知前端移除后再删除后端接收逻辑。涉及文件：`server/trigger/http/subagent.py:189-194`、`agent/tools/subagent/delegate.py:269-276`。
- 审计 #26 修复：`channels/manager.py:196-202` 创建了 4 个 untracked task（`_inbound_consume_loop`、`_outbound_consume_loop`、两个 `_start_channel`），仅 `_dispatch_task` 被存储。`stop_service()` (line 204-229) 只 cancel 了 `_dispatch_task`，其余 task 在 event loop `.stop()` 后被静默丢弃。修复：①在 `__init__` 或 `start_service` 中新增 `self._consumer_tasks: list[asyncio.Task] = []`；②将 line 196-202 的 `create_task` 结果存入该列表；③在 `stop_service` 中遍历 cancel 这些 task 并 await gather。约 +8 行，仅影响 shutdown 路径，零破坏性。涉及文件：`channels/manager.py`。
- 审计 #27 修复：`agent/tools/subagent/registry/work_admission.py:25` 的 drain-retry 路径 `asyncio.create_task(_schedule_drain_retry(...))` 未存入 `_root_work_tasks`，导致：①task 可被 GC 回收（CPython 不保证强引用）；②`pending_root_work_count()` 不计入 drain-retry task；③shutdown 时无法 cancel。修复：将 line 25 改为与 line 28-30 相同的模式——存入 `_root_work_tasks` + `add_done_callback(_root_work_tasks.discard)`。约 +2 行改动，纯增量。涉及文件：`agent/tools/subagent/registry/work_admission.py:25`。
- 审计 #34 方式A修复（对齐 OpenClaw）：删除 `agent/tools/subagent/swarm/collector.py:13` 的模块级 `_launch_fingerprints` dict，将 `launch_fingerprint` 作为字段加到 `SubagentRunRecord`（`agent/tools/subagent/types/registry.py`，在 `swarm_group_id` 附近新增 `launch_fingerprint: str | None = None`）。`reserve_swarm_run` 的幂等检查从 dict 查找改为扫描 `all_runs()`：匹配 `r.swarm_group_id == group_id and r.launch_fingerprint == launch_fingerprint`，命中且 `get_run(run_id) is not None` 时返回已有 run。run record 从 registry 删除时 fingerprint 随之消失，无需单独清理。涉及文件：`agent/tools/subagent/types/registry.py`（加字段）、`agent/tools/subagent/swarm/collector.py`（删 dict、改幂等逻辑）、`agent/tools/subagent/spawn/core.py:381`（传参不变，`reserve_swarm_run` 内部将 fingerprint 写入 run record 而非 dict）。
- 审计 #44 修复：`skills/builtin/core/cron/scripts/base.py:627,671` 使用 `asyncio.get_event_loop()`（Python 3.10+ 标记弃用，3.12+ 无运行 loop 时不再自动创建而抛错）。改为 `asyncio.get_running_loop()`，现有 `except RuntimeError` 分支已处理"没有 loop"的情况，行为不变，仅消除 deprecation。~2 行改动。涉及文件：`skills/builtin/core/cron/scripts/base.py`。
- 审计 #46 修复：`agent/tools/message_search.py:227-231` 的 `build_main_llm()` 在 retry 循环 `for attempt in range(max_retries)` 内部，每次重试都重建 LLM 客户端。将 `build_main_llm()` 提到循环外调用一次，循环内复用。注意：审计报告原文标注行号 181-189 已漂移，实际代码在 227-231。~3 行改动。涉及文件：`agent/tools/message_search.py`。
- 清理 DESIGN_PATTERN_REFACTORING_BACKEND.md, DESIGN_PATTERN_REFACTORING_FRONTEND.md 中大量残留的历史信息，解决目录和实际待办项的不对应问题。只保留现有待办项的信息。具体删除项如下：
  - [FRONTEND 文件] 删除整个第 7 节"已完成记录"(lines 108-149)：两个大表格共约 42 行已完成记录（前端 20 项 + 后端 8 项），纯历史归档
  - [FRONTEND 文件] 删除 Phase 1 历史注释 (line 67)："1.1...1.6 已完成，见第 7 节"
  - [FRONTEND 文件] 删除 Phase 2 历史注释 (line 76)："2.1-2.5, 2.8 已完成，见第 7 节"
  - [FRONTEND 文件] 删除 Phase 3 历史注释 (line 88)："3.2, 3.3 已完成，见第 7 节"
  - [FRONTEND 文件] 删除 Phase 4 历史注释 (line 100)："4.4 已完成，见第 7 节"
  - [FRONTEND 文件] 删除路线图结语 (line 104)："前端侧基础重构已就位"
  - [FRONTEND 文件] 简化第 26 行：删除"原 4.1...4.5 各子项已全部完成并从本文档移除（见第 7 节）"，仅保留"剩余项如下。"
  - [FRONTEND 文件] 第 7 行元数据：删除"（已完成项已移除，详见文末「已完成记录」）"
  - [FRONTEND 文件] 目录第 18 行：删除第 7 节目录条目
  - [FRONTEND 文件] 第 5 节 Cross-cutting Summary 表：删除"Adapter / Mapper: 3.1.4"行（3.1.4 已从 BACKEND 移除）
  - [BACKEND 文件] 简化第 9 行历史更新说明（4 行长段→1 句当前范围说明）
  - [BACKEND 文件] Summary Table 删除 18 行已完成项：#1([sid].vue God Component)、#2(bridge.ts God Module)、#3(isTauri 14 处)、#5(appendStreamChunk)、#7(useSubagentTasks 799 行)、#8(resolveWsBaseUrl/closeSocket)、#9(媒体文件选择三重复制)、#10(VITE_API_BACK_URL 9+处)、#17(WS onmessage 6 处)、#19([sid].vue 协议泄漏)、#20(ChatBox 后端 URL)、#21((controller as any))、#22(base64 剥离 3 处)、#39(handleSend 140 行)、#40(loadSessionHistory 91 行)、#41(sendChatMessageWs 282 行)、#43(IndexedDB 序列化泄漏)、#44(useSubagentTasks 三方耦合)。Summary Table 从 44 行缩减为 26 行
  - [BACKEND 文件] 删除正文 §1.1.2 MultimodalProcessor 媒体类型分发 (lines 101-106)：已由路线图 2.2 完成(agent/middlewares/media_handlers.py — _MEDIA_HANDLERS 注册表)
  - [BACKEND 文件] 删除正文 §3.1.4 LangChain 消息类型泄漏到 store (lines 297-300)：已由路线图 2.4 完成(context_engine/store/core.py — MessageRowBuilder 注册表)
  - [BACKEND 文件] 确认 #12/§1.4.1 RepetitionGuardWrapper 导入私有常量是否已由路线图 2.1 解决(OutputRepetitionGuard 拆分为 repetition_detectors.py + repetition_state.py)，若已解决则一并删除
  - [两文件] 删除完成后，BE §1.1 小节 1.1.1/1.1.3/1.1.4 保持原编号不重编号；§3.1 小节 3.1.5 保持原编号不重编号；Summary Table 保持原编号不重编号，仅删行
- 将 DESIGN_PATTERN_REFACTORING_BACKEND.md 和 DESIGN_PATTERN_REFACTORING_FRONTEND.md 合并为 DESIGN_PATTERN.md,并删除 DESIGN_PATTERN_REFACTORING_BACKEND.md 和 DESIGN_PATTERN_REFACTORING_FRONTEND.md
- 执行 TODO/COMMENT_ROT_REPORT.md 内 注释腐败 的清理，然后删除 TODO/COMMENT_ROT_REPORT.md，并解决删除导致的文档漂移
- 提交暂存区所有代码并推送，然后绿灯通过github CI流水线

---

## T4/T5 合并 + 不重置 Bug 修复

**文件**: `agent/middlewares/summarization.py`

**问题**: 两个独立但语义相同的溢出重试计数器（`_OVERFLOW_RETRIES_T4_KEY` / `_OVERFLOW_RETRIES_T5_KEY`），且只递增不归零。`_reset_turn_state`（`:1746`）重置了 9 个 key，唯独漏了这两个。导致 session 内累计 3 次溢出恢复后，后续恢复永久失效。

**T4/T5 现状对比**:

|               | T4              | T5               |
| ------------- | --------------- | ---------------- |
| 错误来源      | HTTP 413 状态码 | 错误消息文本匹配 |
| 典型 provider | OpenAI          | Anthropic / 其他 |
| 本质          | 输入太大        | 输入太大         |

**合并理由**:

1. 语义完全相同——都是输入溢出
2. 同一 provider 不会同时报两种错误，独立计数器的"6 次总重试"场景实际不存在
3. 合并后代码更简单：一个 key、一个映射、一处重置
4. 如果未来出现新的溢出错误分类，合并后的单计数器更易扩展

**合并 + 修复**: 用单一 `_OVERFLOW_RETRIES_KEY` 替换两个 T4/T5 key，并在 `_reset_turn_state` 中重置。

### 1. 改 Key 定义（`:106-121`）

```python
# 删除:
_OVERFLOW_RETRIES_T4_KEY = "summarization_overflow_retries_t4"
_OVERFLOW_RETRIES_T5_KEY = "summarization_overflow_retries_t5"

# 替换为:
_OVERFLOW_RETRIES_KEY = "summarization_overflow_retries"

# _TRIGGER_BY_ERROR_CLASS 保留（日志区分仍有价值）:
_TRIGGER_BY_ERROR_CLASS: dict[str, str] = {
    PAYLOAD_TOO_LARGE: "T4",
    CONTEXT_OVERFLOW: "T5",
}
# _RETRY_KEY_BY_ERROR_CLASS 统一指向同一个 key:
_RETRY_KEY_BY_ERROR_CLASS: dict[str, str] = {
    PAYLOAD_TOO_LARGE: _OVERFLOW_RETRIES_KEY,
    CONTEXT_OVERFLOW: _OVERFLOW_RETRIES_KEY,
}
```

### 2. 改 `_reset_turn_state`（`:1746-1757`）

末尾追加一行（替代原来的两行 T4/T5）:

```python
state_register_mem.set_state(session_id, _OVERFLOW_RETRIES_KEY, 0)
```

**验证**: 确认同轮内 `while True` 循环仍受 `MAX_OVERFLOW_RETRIES=3` 约束；跨轮计数器归零。

### 3. 改测试

**文件**: `tests/agent/middlewares/test_compression_e2e_static.py`

- `:74` import 改为 `_OVERFLOW_RETRIES_KEY`
- `:615` 断言改为 `get_state(sid, _OVERFLOW_RETRIES_KEY, 0) == 1`
- `:666` 断言改为 `get_state(sid, _OVERFLOW_RETRIES_KEY, 0) == 3`

**文件**: `tests/agent/middlewares/test_compression_comprehensive.py`

- `:795-796` 删除 `t4_key` / `t5_key`，改为单个 `overflow_key = mget("_OVERFLOW_RETRIES_KEY")`
- `:834-835` 合并为 `assert get_state(sid, overflow_key) == 1`
- `:862` 改为 `assert get_state(sid, overflow_key) == 3`
- `:891-892` 合并为 `overflow_key`
- `:925/980/1216` 同理替换
- `:956-957` 合并为 `assert get_state(sid, overflow_key) == 1`
- `:1326-1327` 合并为 `"_OVERFLOW_RETRIES_KEY"`
- `:1405/1413/1421` state 字典中的 key 统一为 `"_OVERFLOW_RETRIES_KEY"`

### 4. 改文档

**文件**: `docs/summarization/README.zh.md` `:250` / `README.md` `:255` / `README.ko.md` `:253` / `README.ja.md` `:254`

- 合并为一行：`summarization_overflow_retries`，`MAX_OVERFLOW_RETRIES = 3`

### 5. 补充测试

**文件**: `tests/agent/middlewares/test_compression_e2e_static.py`

- 新增测试：跨轮恢复后计数器归零验证
- 新增测试：计数器耗尽 → 下一轮恢复可用（当前行为是永久锁死）
- 新增测试：T4 和 T5 共享同一计数器（先触发 payload_too_large 计数+1，再触发 context_overflow 计数+2，第 3 次任一错误即耗尽）
