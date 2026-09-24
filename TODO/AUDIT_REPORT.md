# 审计报告 — Sherry Agent

**审计范围**：`D:\selfProj\sherry_agent` 全项目（排除 `.venv`、`node_modules`、`client/node_modules`、vendored 代码、`logs/`、模型权重数据）。
**审计方法**：6 个并行审计子代理覆盖全栈（后端安全、Agent 核心、数据层/运行时/模型、前端 Nuxt+Tauri、基础设施/技能/CI、代码质量/架构/类型），对关键发现进行源码直接复核。
**审计日期**：2026-08-24（第一轮）/ 2026-09-04（第二轮）/ 2026-09-14（第三轮全栈重审）
**更新日期**：2026-09-14 — 全栈重审后合并所有未修复条目，新增前端审计和代码质量审计，编号连续重排。
**核实记录**：2026-09-15 — 逐条对照源码复核当前全部条目（以“修复后应存在的守卫/改写/调用”为模式做全仓 grep，关键文件精读）：**零删除**，56 条经核实全部仍未修复。
**本报告只保留未修复项；编号已于 2026-09-15、2026-09-24 两轮重排（旧编号作废）**。此前已修复移除 19 项（旧编号 19、20、27、38、44、45、48、50、51、53、57、58、59、61、63、68、70、73、75），不再占用编号；旧→新对照见文末「编号对照（旧 → 新）」。
**2026-09-24**：P0 五项（#1、#3、#16、#17、#18）修复完成并从本报告移除，剩余 51 条重排为连续编号 1-51（对照规则见文末「编号对照」）；全仓代码注释与测试中的 `audit #N` 引用同步改写（移除项改为直接命名威胁，保留项更新为新编号）。

---

# 🔴 严重（Critical）— 安全 / RCE / 可用性

## 1. `delegate.py` — `time.sleep()` 在事件循环线程上阻塞

**文件**：`agent/tools/subagent/delegate.py:128-141`

```python
asyncio.get_running_loop()  # 检测到运行中的事件循环
in_loop = True
if in_loop:
    while self.is_running():
        time.sleep(poll_interval)  # ← 阻塞事件循环！
```

- `result()` 是同步方法，检测到运行中的事件循环后用 `time.sleep()` 轮询。注释声称"不阻塞外层循环"但 `time.sleep()` **确实阻塞**。
- **影响**：整个服务器在轮询期间冻结——所有 WS 连接、所有会话的流、所有定时器停顿。
- **修复**：提供 `async def result_async()` 方法，使用 `await asyncio.sleep()`。
- **状态**：未修复。

# messages.py: 获取 session_id 后直接传入 clear_session
# DAO/messages.py:10
_session_folder = lambda sid: (Path(SESSIONS_DIR) / sid)  # 无校验
# DAO/messages.py:45-47
path = Path(_session_folder(session_id))
shutil.rmtree(path)  # 删除任意目录
```

- `session_id` 来自请求 body，无清洗/校验，直接拼路径后 `shutil.rmtree`。
- **利用**：`DELETE /sessions` body `{"session_id":"../../workspace"}` → 删除 workspace。
- **修复**：`session_id` 正则校验 + `resolve().is_relative_to(SESSIONS_DIR.resolve())` 检查。
- **状态**：已修复（2026-09-24）——HTTP 边界（`messages.py`）与 DAO（`clear_session`）双层以 `is_safe_session_id` 拒绝非单一安全路径段的 id（纵深防御）；测试 `tests/server/trigger/http/test_messages_http.py` + `tests/server/DAO/test_clear_session.py`。

---

# 🟠 高（High）— 安全 / 数据完整性 / 资源泄漏 / 并发

## 2. 异步路径上的阻塞同步 SQLite

**文件**：`context_engine/store/core.py:11,286,360,429`；`context_engine/store/db.py`

- `async` 函数直接在事件循环线程上执行阻塞式 `executemany`/`commit`；模块级共享连接被 WS 循环、cron 线程、子代理线程共用（非线程安全）。
- **修复**：用 `aiosqlite` 或每线程连接 + 锁。
- **状态**：未修复。

## 3. 缺认证 + 通配 CORS — 所有端点未鉴权

**文件**：`server/trigger/core.py:16` — `ALLOW_CORS(app, origins=["*"])`

- `server/__main__.py` 与 `trigger/core.py` 无任何认证中间件。所有 HTTP/WS 端点未鉴权、跨域可达。
- **利用**：任意网站可向 agent API 发请求。衍生以下未鉴权高风险端点（#4-#7）。
- **状态**：未修复。

## 4. 未鉴权 `.env` 读写 — 密钥暴露

**文件**：`server/trigger/http/env.py:6-13`

- `GET /env` 返回含 API Key 的完整配置；`PUT /env` 可改写运行时凭据。
- **利用**：`GET /env` 窃取 `MAIN_LLM_API_KEY`、`AUXILIARY_LLM_API_KEY`、`TAVILY_API_KEY` 等。
- **状态**：未修复。

## 5. 未鉴权 WebSocket agent 控制

**文件**：`server/trigger/ws/messages.py:116-183` — `app.websocket("/sessions/agent/ws")`

- 客户端可发 `multi_modal_message` 触发 `async_generate`、`hitl_response` 批准/拒绝工具调用、`stop` 取消。
- **利用**：攻击者驱动 agent 并批准危险工具调用。
- **状态**：未修复。

## 6. 未鉴权日志流 — 密钥泄漏

**文件**：`server/trigger/ws/logs.py:119-148` — `app.websocket("/logs/ws")`

- 把每条 loguru 记录（可能含 API Key、请求头、提示词）流式推给任意未鉴权 WS 客户端。
- **利用**：连接 `/logs/ws` 从日志帧读取密钥。
- **状态**：未修复。已添加 bounded deque (maxlen=2000)，但未鉴权问题仍在。

## 7. 技能系统沙箱/供应链

- **① 技能描述（不可信 SKILL.md frontmatter）注入 LLM 上下文**：`skills/loader.py:191` `get_skills_text` 把每个技能的 `description`（解析自 SKILL.md）直接拼进 `<available_skills>` XML，无 XML 转义。**存在恶意/被植入技能时触发。**
- **② 技能扫描器故障放行**：`server/service/skill_scan_policy.py:56-58` `build_reject_message` 在扫描器 `UNAVAILABLE` 时返回 `None`（放行）；`skills_snapshot.py:42-44`、`clawhub_runner.py:152-157` 均为 fail-open。**扫描器缺失/报错时触发。**
- **③ `tool_guardrails/core.py` 仅做循环检测，非权限闸门**：检测失败重复、同工具失败累积、无进展（第 119-172 行），**不**审批/确认首次工具调用。当前 `sandbox=False` 未沙箱执行已有 HITL 人工审批门：主会话经 HITL interrupt 人工审批，子代理/后台 scope 一律 `ToolException` 硬拒（接线在 `agent/middlewares/humanInTheLoop/`），危险命令正则也先于任何子进程生成执行；但面向其余工具的首次调用确认闸门仍不存在。
- **状态**：未修复。

## 8. SSRF — 未校验媒体 URL 下载

**文件**：`agent/middlewares/media_pipeline/media_handlers.py:90-92`（音频/视频 URL 下载）；`agent/middlewares/media_pipeline/media_handlers.py:186-188`（图片 URL 仅经 `is_url` 校验后透传）

```python
req = urllib.request.Request(url, headers={...})
with urllib.request.urlopen(req, timeout=30) as resp:
    data = resp.read()
```

- `is_url`（`pub/func/validator/is_url.py`）仅校验 scheme 白名单，**不阻止** `127.0.0.1`、`169.254.169.254`（云元数据）、内网 RFC1918 地址。
- **利用**：`image_url: {"url": "http://169.254.169.254/latest/meta-data/iam/security-credentials/"}` → 响应存盘并经 LLM 摘要回显。
- **修复**：增加内网 IP 黑名单（拒绝 RFC1918/loopback/link-local 地址）。
- **状态**：未修复。

## 9. `media_pipeline` — 异步钩子中的阻塞网络 I/O + 文件 I/O + CPU 密集操作

**文件**：`agent/middlewares/media_pipeline/core.py:208,223,179,185,190`；`agent/middlewares/media_pipeline/media_handlers.py:91,103,204`

- `abefore_agent`/`aafter_agent`（async）直接调用同步的 `_before_agent_impl`/`_after_agent_impl`：`urllib.request.urlopen()`（网络）、`Image.open()`（CPU 解码）、`write_bytes()`（文件 I/O）、`iterdir()`/`unlink()`（文件系统操作）——全部在事件循环线程上同步执行。
- **影响**：一个大文件下载或图像解码阻塞所有其他会话的流。
- **修复**：将实现体包装在 `await asyncio.to_thread(...)` 中。
- **状态**：未修复。

## 10. `StateRegisterDB` 同步 SQLite 在异步中间件路径中被调用

**文件**：`runtime/session/state_register.py:152,166,182,193,208,222,233,247`

- 每个方法都打开**新的** `sqlite3.connect()` + 执行 + 关闭。被 `awrap_model_call`/`awrap_tool_call`/`aafter_agent`（全 async）每次 agent 回合调用**多次**。
- **修复**：改 `aiosqlite` 或在异步路径中用 `asyncio.to_thread` 包装。
- **状态**：未修复。

## 11. `summarization/core.py` — `awrap_model_call` 异步路径中的同步 SQLite + 文件 I/O

**文件**：`agent/middlewares/summarization/core.py:1517,1526,1976-1979`

- `_build_summary_prompt`（async 路径调用）→ `taskflow_store.get_active_flows_sync()`（同步 SQLite）+ `get_tiered_store().read_facts()`（同步文件 I/O）。
- `_aapply_compression_under_lock`（async）→ `memory_store.load_from_disk()`（文件 I/O）+ `build_system_prompt()`（含 `state_register_db` 同步 SQLite）+ `state_register_db.set_state()`（同步 SQLite）。
- **修复**：同 #10。
- **状态**：未修复。

## 12. `WsTurnExecutor.execute` — 被取消时不取消 child 任务

**文件**：`server/service/turn_runner.py:298-314`

- `execute` 被取消时 `await child` 收到 `CancelledError`，re-raise 但 **child 从未被取消**。`finally` 从 `_active_tasks` 弹出槽位但不 cancel child，且调用 `on_turn_finished` 可能触发更多 queued turn——此时 child 仍在后台运行。
- **修复**：在 re-raise 路径前加 `child.cancel()`。
- **状态**：未修复。

## 13. `compaction_lock.py` — 异步 `acquire()` 中的同步 SQLite

**文件**：`agent/middlewares/summarization/compaction_lock.py:87-106`

- `acquire()` async 上下文管理器调用 `_try_acquire()`，后者打开新的 `sqlite3.connect()` 并执行 SQL 同步。`_release()` 和 `_current_holder()` 同样使用同步 sqlite3。
- **修复**：迁移到 `aiosqlite` 或用 `asyncio.to_thread` 包装。
- **状态**：未修复。

## 14. `context_engine/embeddings/search.py` — 异步路径中的同步 SQLite + 阻塞 I/O

**文件**：`context_engine/embeddings/search.py:23-28`

- `semantic_search` 是 `async` 但调用同步的 `index_pending_messages()` 和 `load_all_embeddings()`（阻塞 SQLite I/O）。
- `models/embed_model/core.py:128` 使用同步 `requests.post()` 从异步路径调用。
- **修复**：用 `asyncio.to_thread` 包装或迁移到异步 HTTP 客户端。
- **状态**：新发现。

## 15. `context_engine/session_continuity.py` — 异步路径中的同步 SQLite

**文件**：`context_engine/session_continuity.py:130,153`

- `auto_save_on_session_end` 是 `async` 但调用同步 `get_messages_by_lastest_n_turns()` 和 `_get_active_taskflow_ids_sync()`。注释承认："Synchronous store read: the SQLite store is stdlib sqlite3 and the latest-turns helper is not awaitable."
- **修复**：`await asyncio.to_thread(...)` 包装。
- **状态**：新发现。

## 16. `context_engine/curator/orchestrator.py` — 异步路径中调用同步 `llm.invoke()`

**文件**：`context_engine/curator/orchestrator.py:96`

- `_run_llm_review` 调用 `llm.invoke()`（同步）而非 `await llm.ainvoke()`，阻塞事件循环。`_schedule_system_prompt_refresh` 用 `asyncio.to_thread` 规避，但 curator 本身未修。
- **修复**：改用 `await llm.ainvoke()` 或包装 `asyncio.to_thread`。
- **状态**：新发现。

## 17. `agent/tools/message_search.py` — `asyncio.run()` 在工具中

**文件**：`agent/tools/message_search.py:487`

- `_run_semantic_search` 调用 `asyncio.run(semantic_search(...))`。如果工具从 async 上下文调用（LangChain tool node 运行 sync 工具在 thread pool 中），该线程无运行中的事件循环，`asyncio.run()` 可工作；但这是脆弱的隐式假设。
- **修复**：提供 async 版本或用 `run_async` 模式。
- **状态**：新发现。

## 18. `models/reranker_model/core.py` — `verify=False` 全局禁用 TLS

**文件**：`models/reranker_model/core.py:573,623,679`；`models/embed_model/core.py:51,128`

- `CloudReranker` 和 `CustomEmbedding` 的 `requests.post()` 使用 `verify=False`，且 `urllib3.disable_warnings()` 在模块导入时全局禁用 SSL 警告。
- 无方式启用 TLS 验证。Response 对象未显式关闭。
- **修复**：移除 `verify=False`，改为配置项；移除全局 `disable_warnings`。
- **状态**：新发现。

## 19. 子代理 child checkpointer 连接泄漏

**文件**：`agent/tools/subagent/spawn/core.py:773-774`

- 每个 child agent 创建 `await build_async_sqlite_checkpointer()` + `await child_checkpointer.setup()`，但从未显式关闭。aiosqlite 连接泄漏。swarm 场景下大量并发子代理会产生大量连接。
- **修复**：在 child agent 执行完成后 `await child_checkpointer.aclose()`。
- **状态**：新发现。

## 20. 技能上传/切换端点零测试覆盖

**文件**：`server/trigger/http/skills/lifecycle.py`（242 行代码）

- `upload_skill_handler` 和 `toggle_skill_handler` 是接受第三方代码的关键安全端点，涉及路径遍历防护、安全扫描门控、状态文件写入——**完全无测试**。
- **修复**：编写覆盖路径遍历、安全扫描门控、状态文件原子性的集成测试。
- **状态**：新发现。

## 21. CI 无 SAST/依赖漏洞扫描

**文件**：`.github/workflows/ci.yml`

- CI 仅运行 ruff + pytest + import-linter，无 Bandit/Semgrep 静态安全扫描、无 pip-audit/safety 依赖漏洞检查、无 CodeQL 集成。
- **修复**：在 CI pipeline 中添加安全扫描步骤。
- **状态**：新发现。

---

# 🟡 中（Medium）— 代码质量 / 架构 / 正确性

## 22. `bus/core.py` 单一全局队列，无按渠道路由

- `MessageBus` 一个入站 + 一个出站 `asyncio.Queue`，所有渠道共享。高并发多渠道时是瓶颈且无法按渠道背压。
- `channels/manager.py:111` `_consume_loop` 对每条出站消息遍历所有已配置渠道，而非仅发送到目标渠道。
- **状态**：未修复。有界队列已添加，但路由问题仍在。

## 23. 未鉴权日志文件读取

**文件**：`server/trigger/http/logs.py:170-198` — `GET /logs?path=...`

- 路径经 `LOG_DIR` 校验（受控，已修复路径穿越），但端点未鉴权，日志可能含密钥。
- **状态**：路径穿越已修复，未鉴权仍在。

## 24. 知识图谱遍历参数无上限

**文件**：`server/trigger/http/knowledge_graph.py:146,150`

```python
max_depth = max(0, int(query.get("max_depth", 3)))  # 仅最小值，无上限
max_nodes = max(1, int(query.get("max_nodes", 1000)))  # 仅最小值
```

- **状态**：未修复。

## 25. `interrupt_marker` — 异步函数中调用同步 SQLite

**文件**：`server/service/interrupt_marker.py:242`

- **修复**：`await asyncio.to_thread(store_core.get_messages_by_lastest_n_turns, ...)`。
- **状态**：未修复。

## 26. `steering_queue` — 异步方法中使用 `threading.Lock`

**文件**：`agent/tools/subagent/announce/steering_queue.py:119,136,338`

- `enqueue_steering`/`drain`（async）用 `with state.lock:`（`threading.Lock`），另一线程持锁时事件循环阻塞。设计文档明确"NEVER held across an await"，但技术上仍有阻塞风险。
- **修复**：改 `asyncio.Lock`。
- **状态**：未修复。

## 27. `run_async` — 从运行中的事件循环调用时 `future.result()` 阻塞

**文件**：`pub/func/run_async.py:77-114`

- **修复**：确保所有 `run_async` 调用方不在事件循环线程上，或提供 `await` 版本。
- **状态**：未修复。有改进（timeout 后取消 pending tasks + shutdown pool），但阻塞本身仍在。

## 28. `list_descendant_runs` — O(N*D) BFS 重复全量扫描

**文件**：`agent/tools/subagent/registry/queries.py:12-26`

- BFS 每展开一个节点遍历内存中**全部** run 记录。`count_active_descendant_runs` 和 `count_pending_descendant_runs` 各调用一次 `list_descendant_runs`，再迭代。
- **修复**：先调用 `build_read_index()`（已存在）构建 requester→runs 索引。
- **状态**：未修复。

## 29. swarm 计数器 — `all_runs()` 全量扫描在 pump_lane 循环中

**文件**：`agent/tools/subagent/swarm/collector.py:329-356,212-228`

- `_count_active_swarm_runs` = `sum(1 for r in all_runs() if ...)`，`_pump_lane` while 循环每次迭代调用。`reserve_swarm_run` 也全量扫描做指纹去重。
- **修复**：维护 per-group 增量计数器。
- **状态**：未修复。

## 30. `StateRegisterDB` — 每次操作新建 SQLite 连接

**文件**：`runtime/session/state_register.py:150-247`

- `set_state`/`get_state`/`has_session` 每个方法都 `sqlite3.connect()` + `close()`。`update_states` 在单连接内循环插入，未用 `executemany`。
- `ContextEpoch.prepare()` (line 300) 创建连接后未关闭（其他方法用了 `with` 但此方法没有）。
- **修复**：复用模块级连接（加锁）或使用 `aiosqlite`。
- **状态**：未修复。

## 31. `ContextEpoch` 连接泄漏

**文件**：`runtime/session/state_register.py:300`

- `prepare()` 方法内联 `sqlite3.connect(self._db.db_path)` 但未关闭——其他方法（280, 334, 348）用了 `with` 上下文管理器，此方法没有。
- **状态**：新发现。

## 32. `agent/tools/subagent/registry/memory.py` — `_runs` 字典无界增长

**文件**：`agent/tools/subagent/registry/memory.py:6-7`

- `_runs: dict[str, SubagentRunRecord] = {}` 终端 run 记录从不自动驱逐。sweeper 持久化到磁盘但不修剪内存字典。`clear()` 存在但仅在显式调用时执行。
- **状态**：新发现。

## 33. `agent/middlewares/summarization/core.py` — `_RESTORED_COOLDOWN_SESSIONS` 无界增长

**文件**：`agent/middlewares/summarization/core.py:128`

- `_RESTORED_COOLDOWN_SESSIONS: set[str] = set()` — sessions 被添加但从不移除。
- **状态**：新发现。

## 34. `agent/tools/subagent/orphan/recovery.py` — `recovery_attempts_persisted` 无界增长

**文件**：`agent/tools/subagent/orphan/recovery.py:25,29`

- `_recovery_tasks` 和 `recovery_attempts_persisted` 字典无界增长，`cancel_recovery` 可能不被所有 run 调用。
- **状态**：新发现。

## 35. `agent/tools/subagent/registry/settle_wake.py` — 异步路径中的同步 SQLite

**文件**：`agent/tools/subagent/registry/settle_wake.py:103`

- `retire_after_settle`（async）调用 `_persist_state()` → `save_settle_wake_state()`（同步 sqlite3）。
- **状态**：新发现。

## 36. `config/schema.py` 从 `models/` 导入 — 违反架构规则

**文件**：`config/schema.py:185,260`

```python
from models.providers.registry import PROVIDERS  # 函数级延迟导入
from models.providers.registry import find_by_name
```

- 违反 AGENTS.md："config/features/** MUST NOT import from agent/, server/, or models/ — config is dependency-free"
- **修复**：将 provider 匹配逻辑移到 models 层或新建 resolver 层。
- **状态**：新发现。

## 37. `context_engine/events/store.py` — `__import__()` 反模式 + `db: Any` 类型

**文件**：`context_engine/events/store.py:17,19-23,39`

- 使用 `__import__()` 做延迟导入而非正常 import 语句。`db: Any = None` 参数未类型化。
- **状态**：新发现。

## 38. 前端 DOMPurify 允许 `style` 属性

**文件**：`client/app/constants/security.ts:68`

- DOMPurify 配置允许 `style` 属性，可被用于 CSS 注入攻击（如 `background: url(...)` 发起外部请求）。
- **修复**：如不需要 GFM 表格对齐，移除 `style`；或添加 `ALLOWED_URI_REGEXP` 限制 URL 模式。
- **状态**：新发现。

## 39. `runtime/process/crash_loop_breaker.py` — 配置在导入时读取

**文件**：`runtime/process/crash_loop_breaker.py:35-39`

- `WINDOW_S`、`TRIP_THRESHOLD`、`RETENTION_S` 在模块级读取配置，运行时配置变更不会生效。
- **状态**：新发现。

## 40. `models/LLMs/main_llm.py` — 环境变量在导入时读取

**文件**：`models/LLMs/main_llm.py:17-22,64-88`

- 环境变量在模块导入时读取。`model_config` 是模块级可变 dict，被 `apply_thinking_budget()` 修改——非线程安全。`int(os.getenv(...))` 无 try/except。
- **状态**：新发现。

## 41. `context_engine/embeddings/indexer.py` — 硬编码模型常量

**文件**：`context_engine/embeddings/indexer.py:10-12`

- `_EMBED_MODEL_NAME = "bge-m3"`、`_EMBED_DIM = 1024`、`_BATCH_SIZE = 32` — 不可配置。
- **状态**：新发现。

## 42. Windows 上 sandbox 降级为无沙箱

**文件**：`agent/tools/pub_base/sandbox.py`

- `SANDBOX_POLICY=auto`（默认）在 Windows 上降级为无沙箱。bwrap 仅 Linux 可用，seatbelt 仅 macOS 可用。
- Windows 部署完全依赖正则黑名单和 builtins 限制（可被反射绕过：`().__class__.__bases__[0].__subclasses__()`）。
- **状态**：新发现。

## 43. 全局异常处理器暴露 `str(error)`

**文件**：`server/trigger/core.py:36`；`server/trigger/http/knowledge_graph.py:205`；`server/trigger/http/stats.py:126`；`server/trigger/http/curator.py:41,136`

- 全局异常处理器返回 `"error": str(error)`，可能暴露内部文件路径或状态。
- **状态**：新发现。

---

# 🟢 低（Low）— 小问题 / 清理

## 44. `sender_task.cancel()` 未 `await`（2 处）

**文件**：`server/trigger/ws/subagent_ws.py:175`、`server/trigger/ws/logs.py:144`

- **修复**：`sender_task.cancel(); await asyncio.wait_for(sender_task, timeout=1.0)`。
- **状态**：未修复。

## 45. Channel 线程事件循环未关闭

**文件**：`server/trigger/channels/core.py:330-342`

- `event_loop.run_forever()` 退出后无 `event_loop.close()`。对比 cron 服务线程有 `loop.close()`。
- **修复**：在 `_run()` 的 `finally` 块中加 `event_loop.close()`。
- **状态**：未修复。

## 46. `threading.Lock` 在异步调用链中使用（3 处）

**文件**：

- `agent/tools/subagent/registry/session_state.py:93,143,151,160` — `_HITL_LOCK`/`_INFLIGHT_LOCK`
- `agent/tools/subagent/registry/memory.py:6` — `_lock = threading.Lock()`
- `server/trigger/ws/subagent_ws.py:66,74,104,128` — `_subscribers_lock`/`_hooks_lock`
- 锁持有时间极短，实际阻塞风险低但技术上是 `threading.Lock` 在异步调用链中。
- **状态**：未修复。

## 47. `delegate_task` — `asyncio.run()` 未检查运行中的事件循环

**文件**：`agent/tools/subagent/delegate.py:439`

- 从异步上下文调用会抛 `RuntimeError`。作为 sync 工具入口点，在 thread pool 中执行时安全，但是脆弱的隐式假设。
- **状态**：未修复。

## 48. 前端 `JSON.parse(JSON.stringify())` 深拷贝

**文件**：`client/app/composables/db.ts:501`

- 对于大量消息的 turn，每次状态变更都全量序列化/反序列化。
- **修复**：使用 `structuredClone`（更快）。
- **状态**：新发现。

## 49. 前端 dev server 绑定 `0.0.0.0`

**文件**：`client/nuxt.config.ts:62-64`

- `server.host: '0.0.0.0'` — 局域网内其他设备可访问 dev server。
- **状态**：新发现。

## 50. 缺失 `__init__.py`

**文件**：

- `skills/builtin/code_wiki/scripts/` — 有 .py 文件但无 `__init__.py`
- `plugins/channels/qq/` — 有 .py 文件但无 `__init__.py`
- **状态**：新发现。

## 51. `evals/nudge_extraction/suite.py` 加载 `.env` 到 eval 环境

**文件**：`evals/nudge_extraction/suite.py:528-529`

- `load_dotenv(REPO_ROOT / ".env", override=False)` — eval 运行可访问生产 API 密钥。
- **状态**：新发现。


---

# 超大模块（>500 行，建议拆分）

| 文件                                            | 行数 |
| ----------------------------------------------- | ---- |
| `agent/middlewares/summarization/core.py`            | 1996 |
| `agent/tools/skill_tools/skill_manage.py`       | 882  |
| `models/STT_model/core.py`                      | 785  |
| `agent/tools/subagent/spawn/core.py`            | 762  |
| `server/queue/user_input_queue.py`              | 717  |
| `agent/tools/memory.py`                         | 782  |
| `models/reranker_model/core.py`                 | 701  |
| `server/service/stream_dispatch.py`             | 656  |
| `agent/middlewares/context_engine/nudge.py`     | 649  |
| `context_engine/store/core.py`                  | 648  |
| `context_engine/curator/orchestrator.py`        | 614  |
| `server/service/messages.py`                    | 625  |
| `agent/tools/pub_base/skill_usage.py`           | 623  |
| `agent/tools/taskflow/registry/store_sqlite.py` | 601  |
| `agent/tools/message_search.py`                 | 555  |
| `server/service/turn_runner.py`                 | 552  |
| `agent/wrapper/repetition_guard.py`             | 550  |
| `agent/middlewares/tool_guardrails/core.py`          | 546  |
| `agent/middlewares/output_repetition_guard/core.py`  | 536  |
| `agent/tools/subagent/registry/lifecycle.py`    | 536  |

**共 56 个核心源码文件超过 300 行**（不含 vendored / tests / docs / client）。

---

# 代码质量统计

| 维度                                                    | 数量                                   |
| ------------------------------------------------------- | -------------------------------------- |
| 缺少返回类型注解的函数                                  | 292                                    |
| `Any` 类型使用（非测试代码）                            | 490                                    |
| `except Exception:` / `except:` / `pass` 模式（agent/） | 64                                     |
| 模块级可变全局变量（agent/）                            | 52                                     |
| 模块级可变全局变量（server/）                           | 14                                     |
| 重复函数名跨文件                                        | 308                                    |
| 硬编码截断长度（应移入配置）                            | 8+                                     |
| `config/schema.py` 导入 `models/`                       | 2 处                                   |
| 核心 TODO/FIXME 注释                                    | 0（仅在 vendored 和 skill scripts 中） |

### `Any` 类型分布

| 目录            | `Any` 次数 |
| --------------- | ---------- |
| skills/         | 213        |
| agent/          | 149        |
| server/         | 63         |
| models/         | 32         |
| context_engine/ | 11         |
| runtime/        | 8          |
| config/         | 6          |
| plugins/        | 3          |
| channels/       | 2          |
| pub/            | 2          |

---

# 前端审计摘要

## 已实施的良好实践

- **零 `v-html` 使用**：ESLint 强制 `vue/no-v-html: error`
- **`v-safe-html` 指令**：markdown-it → DOMPurify → innerHTML
- **反向 tabnabbing 防护**：`rel="noopener noreferrer"` 自动添加
- **Tauri CSP**：`object-src 'none'`、`base-uri 'self'`、`script-src 'self'`
- **KeepAlive LRU 限制**：`max: 20` 防止内存无限增长
- **Dexie 增量查询**：仅请求缓存中缺失的新 turn
- **WS 重连防风暴**：superseded-socket guard + 指数退避
- **组件级错误捕获**：`useErrorCaptured` 组合式函数

## 前端待修复项（已在上方编号）

- #38 DOMPurify 允许 `style` 属性
- #48 `JSON.parse(JSON.stringify())` 深拷贝
- #49 dev server 绑定 `0.0.0.0`
- Tauri Rust 源文件均为 0 字节（仅脚手架，无实际实现）
- `@nuxtjs/i18n` 精确锁定版本（非范围）
- `nuxt.config.ts` 无 HTTP 安全头

---

# 已复核为非漏洞（受控）

- `server/service/workplace.py:23-25` `write_system_prompt_file` 校验 `ALL_SYSTEM_FILE_NAMES` 允许列表。受控。
- `server/service/env.py:140-143` `write_env_file` 拒绝未知键。受控。
- `agent/tools/pub_base/skill_utils.py:38-41` `yaml.load` 用 `CSafeLoader`/`SafeLoader`。非反序列化漏洞。
- `context_engine/store/core.py`、`runtime/session/state_register.py` 全部 SQL 均参数化。无 SQLi。
- `agent/tools/skill_tools/skill_manage.py` 路径校验、名称正则、文件大小上限。已加固。
- `server/trigger/http/logs.py` 路径穿越已修复（`_resolve_log_path` 含 `resolve()` + `is_relative_to(LOG_DIR)`）。
- `server/trigger/http/skills/lifecycle.py` 上传端点路径穿越已修复（`_validate_skill_name` 正则 + `resolved.relative_to` 检查）。
- `server/trigger/http/media.py` 已校验 session_id 和 filename。
- `server/queue/user_input_queue.py` f-string SQL 插值仅用于常量，非用户输入。安全。
- `server/trigger/http/stats.py` 无用户输入 SQL 插值。安全。
- 脚本安全：`scripts/hooks/*.sh` 使用 `set -euo pipefail`，变量正确引用。
- Dockerfile：多阶段构建、非 root 用户、HEALTHCHECK、`.dockerignore` 排除 `.env`。
- 工作区模板：无 API 密钥或凭证。
- `.env` 正确 gitignored。
- import-linter server 层级契约：无违规。所有 import 方向向下。
- `agent/tools/pub_base/sandbox.py` ↔ `sandbox_bwrap`/`sandbox_seatbelt` 循环依赖通过延迟导入设计解决。可接受。
- `agent/tools/terminal.py` 和 `python_repl.py` 使用 `env=scrub_env()`。正确。

---

# 修复优先级建议

## P0 — 立即修复（2026-09-24 全部完成，条目已移出本报告）

原五项：`catalog.py` 路径穿越读、`DELETE /sessions` session_id 删目录、clawhub 供应链（钉版本 + 环境清洗）、前端 Token 迁出 localStorage、Tauri Shell 权限整体移除。修法与验证见 2026-09-24 的修复提交。

## P1 — 尽快修复

- **加认证**：`server/trigger/core.py` 加 token/API-key 中间件并作用于所有路由与 WebSocket，去掉通配 CORS。覆盖 #3-#6、#23、#43。
- **#1** `delegate.py` `time.sleep` 阻塞事件循环（提供 `result_async`）
- **#8** 内网 IP 黑名单（SSRF 防护）
- **#12** `execute` 取消 child 任务
- **#20** 为技能上传/切换端点编写测试
- **#21** CI 添加 SAST/依赖漏洞扫描
- **#36** `config/schema.py` 从 models 导入违规

## P2 — 计划修复

- **异步/阻塞收口**：#2 store 迁移 `aiosqlite`、#9-#11 media_pipeline/summarization/StateRegisterDB 阻塞 I/O 移 `to_thread`、#13 compaction_lock、#14-#16 embeddings/session_continuity/curator 异步路径阻塞
- **资源泄漏**：#19 child checkpointer、#32-#34 无界增长的全局变量
- **性能**：#28-#29 冗余扫描与计数、#30 连接风暴收口
- **fail-open 与确认闸门**：#7-② 重审扫描器故障放行策略、#7-③ 为高风险工具增加首次调用确认闸门、#7-① 技能描述 XML 转义
- **前端**：#38 DOMPurify
- **#18** 移除 `verify=False` 和全局 `disable_warnings`

## P3 — 低优先级清理

- **大文件拆分**：summarization/core.py (1996行) 优先拆分
- **类型注解**：292 个函数缺少返回类型，149 处 `Any`（agent/）
- **错误处理统一**：64 处 `except Exception:` 至少添加日志
- **全局状态治理**：52 处模块级可变变量评估封装
- **#22** bus 单队列路由
- **#24** 知识图谱遍历参数钳制
- **#42** Windows sandbox 策略
- **#44-#47、#48、#49-#50、#51** 逐项修复低优先级项
- 修复后重新审计，确认以上各域闭合

---

## 编号对照（旧 → 新）

> 2026-09-15 重排：左列为重排前的旧编号（中间空缺为更早轮次已修复移除的编号），右列为重排后的连续编号。本轮核实结果为**零删除**，全部 56 条均为保留项。
>
> 2026-09-24 重排：#1、#3、#16、#17、#18 修复完成后移除，剩余 51 条重排为连续编号 1-51（#2→#1；#4-#15 各减 2；#19-#56 各减 5）。上表「新编号」列为 2026-09-15 编号，映射到现编号按上述规则；5 个移除编号不再占用。本轮同步清理了全仓 `audit #N` 注释与测试引用（移除项改写为直接命名威胁，保留项更新为新编号）。

| 旧编号 | 新编号 | 条目 | 本轮核实 |
| ------ | ------ | ---- | -------- |
| 1 | 1 | HTTP 路径穿越读取 — `GET /skills/*skill_path` 绕过路径检查 | 未修复（OPEN） |
| 2 | 2 | `delegate.py` — `time.sleep()` 在事件循环线程上阻塞 | 未修复（OPEN） |
| 3 | 3 | `DELETE /sessions` — session_id 路径穿越删除任意目录 | 未修复（OPEN） |
| 4 | 4 | 异步路径上的阻塞同步 SQLite | 未修复（OPEN） |
| 5 | 5 | 缺认证 + 通配 CORS — 所有端点未鉴权 | 未修复（OPEN） |
| 6 | 6 | 未鉴权 `.env` 读写 — 密钥暴露 | 未修复（OPEN） |
| 7 | 7 | 未鉴权 WebSocket agent 控制 | 未修复（OPEN） |
| 8 | 8 | 未鉴权日志流 — 密钥泄漏 | 未修复（OPEN） |
| 9 | 9 | 技能系统沙箱/供应链 | 未修复（OPEN） |
| 10 | 10 | SSRF — 未校验媒体 URL 下载 | 未修复（OPEN） |
| 11 | 11 | `media_pipeline` — 异步钩子中的阻塞网络 I/O + 文件 I/O + CPU 密集操作 | 未修复（OPEN） |
| 12 | 12 | `StateRegisterDB` 同步 SQLite 在异步中间件路径中被调用 | 未修复（OPEN） |
| 13 | 13 | `summarization/core.py` — `awrap_model_call` 异步路径中的同步 SQLite + 文件 I/O | 未修复（OPEN） |
| 14 | 14 | `WsTurnExecutor.execute` — 被取消时不取消 child 任务 | 未修复（OPEN） |
| 15 | 15 | `compaction_lock.py` — 异步 `acquire()` 中的同步 SQLite | 未修复（OPEN） |
| 16 | 16 | clawhub 供应链风险 — `npx --yes clawhub@latest` + 环境泄漏 | 未修复（OPEN） |
| 17 | 17 | 前端 Token 存储在 localStorage — XSS 可窃取 | 未修复（OPEN） |
| 18 | 18 | Tauri Shell 权限过宽 — 可执行任意系统命令 | 未修复（OPEN） |
| 21 | 19 | `context_engine/embeddings/search.py` — 异步路径中的同步 SQLite + 阻塞 I/O | 未修复（OPEN） |
| 22 | 20 | `context_engine/session_continuity.py` — 异步路径中的同步 SQLite | 未修复（OPEN） |
| 23 | 21 | `context_engine/curator/orchestrator.py` — 异步路径中调用同步 `llm.invoke()` | 未修复（OPEN） |
| 24 | 22 | `agent/tools/message_search.py` — `asyncio.run()` 在工具中 | 未修复（OPEN） |
| 25 | 23 | `models/reranker_model/core.py` — `verify=False` 全局禁用 TLS | 未修复（OPEN） |
| 26 | 24 | 子代理 child checkpointer 连接泄漏 | 未修复（OPEN） |
| 28 | 25 | 技能上传/切换端点零测试覆盖 | 未修复（OPEN） |
| 29 | 26 | CI 无 SAST/依赖漏洞扫描 | 未修复（OPEN） |
| 30 | 27 | `bus/core.py` 单一全局队列，无按渠道路由 | 未修复（OPEN） |
| 31 | 28 | 未鉴权日志文件读取 | 未修复（OPEN） |
| 32 | 29 | 知识图谱遍历参数无上限 | 未修复（OPEN） |
| 33 | 30 | `interrupt_marker` — 异步函数中调用同步 SQLite | 未修复（OPEN） |
| 34 | 31 | `steering_queue` — 异步方法中使用 `threading.Lock` | 未修复（OPEN） |
| 35 | 32 | `run_async` — 从运行中的事件循环调用时 `future.result()` 阻塞 | 未修复（OPEN） |
| 36 | 33 | `list_descendant_runs` — O(N*D) BFS 重复全量扫描 | 未修复（OPEN） |
| 37 | 34 | swarm 计数器 — `all_runs()` 全量扫描在 pump_lane 循环中 | 未修复（OPEN） |
| 39 | 35 | `StateRegisterDB` — 每次操作新建 SQLite 连接 | 未修复（OPEN） |
| 40 | 36 | `ContextEpoch` 连接泄漏 | 未修复（OPEN） |
| 41 | 37 | `agent/tools/subagent/registry/memory.py` — `_runs` 字典无界增长 | 未修复（OPEN） |
| 42 | 38 | `agent/middlewares/summarization/core.py` — `_RESTORED_COOLDOWN_SESSIONS` 无界增长 | 未修复（OPEN） |
| 43 | 39 | `agent/tools/subagent/orphan/recovery.py` — `recovery_attempts_persisted` 无界增长 | 未修复（OPEN） |
| 46 | 40 | `agent/tools/subagent/registry/settle_wake.py` — 异步路径中的同步 SQLite | 未修复（OPEN） |
| 47 | 41 | `config/schema.py` 从 `models/` 导入 — 违反架构规则 | 未修复（OPEN） |
| 49 | 42 | `context_engine/events/store.py` — `__import__()` 反模式 + `db: Any` 类型 | 未修复（OPEN） |
| 52 | 43 | 前端 DOMPurify 允许 `style` 属性 | 未修复（OPEN） |
| 54 | 44 | `runtime/process/crash_loop_breaker.py` — 配置在导入时读取 | 未修复（OPEN） |
| 55 | 45 | `models/LLMs/main_llm.py` — 环境变量在导入时读取 | 未修复（OPEN） |
| 56 | 46 | `context_engine/embeddings/indexer.py` — 硬编码模型常量 | 未修复（OPEN） |
| 60 | 47 | Windows 上 sandbox 降级为无沙箱 | 未修复（OPEN） |
| 62 | 48 | 全局异常处理器暴露 `str(error)` | 未修复（OPEN） |
| 64 | 49 | `sender_task.cancel()` 未 `await`（2 处） | 未修复（OPEN） |
| 65 | 50 | Channel 线程事件循环未关闭 | 未修复（OPEN） |
| 66 | 51 | `threading.Lock` 在异步调用链中使用（3 处） | 未修复（OPEN） |
| 67 | 52 | `delegate_task` — `asyncio.run()` 未检查运行中的事件循环 | 未修复（OPEN） |
| 69 | 53 | 前端 `JSON.parse(JSON.stringify())` 深拷贝 | 未修复（OPEN） |
| 71 | 54 | 前端 dev server 绑定 `0.0.0.0` | 未修复（OPEN） |
| 72 | 55 | 缺失 `__init__.py` | 未修复（OPEN） |
| 74 | 56 | `evals/nudge_extraction/suite.py` 加载 `.env` 到 eval 环境 | 未修复（OPEN） |

### 此前已修复移除（不占用编号）

| 旧编号 | 新编号 | 条目 | 本轮核实 |
| ------ | ------ | ---- | -------- |
| 19 | — | （已修复移除） | 已修复移除 |
| 20 | — | （已修复移除） | 已修复移除 |
| 27 | — | （已修复移除） | 已修复移除 |
| 38 | — | （已修复移除） | 已修复移除 |
| 44 | — | （已修复移除） | 已修复移除 |
| 45 | — | （已修复移除） | 已修复移除 |
| 48 | — | （已修复移除） | 已修复移除 |
| 50 | — | （已修复移除） | 已修复移除 |
| 51 | — | （已修复移除） | 已修复移除 |
| 53 | — | （已修复移除） | 已修复移除 |
| 57 | — | （已修复移除） | 已修复移除 |
| 58 | — | （已修复移除） | 已修复移除 |
| 59 | — | （已修复移除） | 已修复移除 |
| 61 | — | （已修复移除） | 已修复移除 |
| 63 | — | （已修复移除） | 已修复移除 |
| 68 | — | （已修复移除） | 已修复移除 |
| 70 | — | （已修复移除） | 已修复移除 |
| 73 | — | （已修复移除） | 已修复移除 |
| 75 | — | （已修复移除） | 已修复移除 |
