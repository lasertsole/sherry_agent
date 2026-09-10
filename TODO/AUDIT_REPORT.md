# 审计报告 — Sherry Agent

**审计范围**：`C:\app\code\project\sherry_agent` 全项目（排除 `.venv`、`node_modules`、`client/`、`future/`、`temp/`、`logs/`、vendored LightRAG/模型权重数据）。
**审计方法**：4 个并行审计子代理（安全、代码质量/架构、正确性/数据、技能系统与沙箱）+ 对关键发现进行源码直接复核；第二轮补充审计（2026-09-04）由 6 个并行探索子代理覆盖（资源泄漏、错误处理/静默吞没、异步/同步边界违规、SQL 安全/输入校验、安全/路径穿越/SSRF、性能/可扩展性），同样经源码直接复核。
**审计日期**：2026-08-24（第一轮）/ 2026-09-04（第二轮）
**更新日期**：2026-09-10 — 本报告仅保留尚未修复的审计条目，编号按文档顺序重排为连续编号。

---

# 🔴 严重（Critical）— 安全 / RCE / 可用性

## 1. HTTP 路径穿越读取 — `GET /skills/*skill_path` 绕过 `resolve_path`
**文件**：`server/trigger/http/skills.py:122-128`
```python
skill_path = path_params["skill_path"]
full_path = ROOT_DIR / skill_path  # 无 is_relative_to 检查
content = full_path.read_text(...)  # 读取任意文件
```
- `resolve_path` 已修复（含 `is_relative_to(ROOT_DIR)` 检查），但此端点**完全绕过** `resolve_path`，直接 `ROOT_DIR / skill_path`。
- **利用**：`GET /skills/../../.env` → 窃取 `MAIN_LLM_API_KEY`。
- **修复**：加 `full_path.resolve().is_relative_to(ROOT_DIR.resolve())` 检查。

## 2. `delegate.py` — `time.sleep()` 在事件循环线程上阻塞
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

---

# 🟠 高（High）— 安全 / 数据完整性 / 资源泄漏 / 并发

## 3. 异步路径上的阻塞同步 SQLite
**文件**：`context_engine/store/core.py:11,143,186`；`context_engine/store/db.py`
- `async` 函数直接在事件循环线程上执行阻塞式 `executemany`/`commit`；模块级共享连接被 WS 循环、cron 线程、子代理线程共用（非线程安全）。
- **修复**：用 `aiosqlite` 或每线程连接 + 锁。

## 4. 缺认证 + 通配 CORS — 所有端点未鉴权
**文件**：`server/trigger/core.py:14` — `ALLOW_CORS(app, origins=["*"])`
- `server/__main__.py` 与 `trigger/core.py` 无任何认证中间件。所有 HTTP/WS 端点未鉴权、跨域可达。
- **利用**：任意网站可向 agent API 发请求。衍生以下未鉴权高风险端点（#5-#8）。

## 5. 未鉴权 `.env` 读写 — 密钥暴露
**文件**：`server/trigger/http/env.py:6-32`
- `GET /env` 返回含 API Key 的完整配置；`PUT /env` 可改写运行时凭据。
- **利用**：`GET /env` 窃取 `MAIN_LLM_API_KEY`。

## 6. 未鉴权 session 清除 + 路径穿越删除
**文件**：`server/trigger/http/messages.py:16-23` + `server/DAO/messages.py:8-9,32-34`
- `DELETE /sessions` 携带 `session_id`（如 `../../src`）经 `_session_folder`/`shutil.rmtree` 穿越删除任意目录。无鉴权、无清洗。
- **利用**：`DELETE /sessions` body `{"session_id":"../../workspace"}`。

## 7. 未鉴权 WebSocket agent 控制
**文件**：`server/trigger/ws/messages.py:116-183` — `app.websocket("/sessions/agent/ws")`
- 客户端可发 `multi_modal_message` 触发 `async_generate`、`hitl_response` 批准/拒绝工具调用、`stop` 取消。
- **利用**：攻击者驱动 agent 并批准危险工具调用。

## 8. 未鉴权日志流 — 密钥泄漏
**文件**：`server/trigger/ws/logs.py:119-148` — `app.websocket("/logs/ws")`
- 把每条 loguru 记录（可能含 API Key、请求头、提示词）流式推给任意未鉴权 WS 客户端。
- **利用**：连接 `/logs/ws` 从日志帧读取密钥。

## 9. 技能系统沙箱/供应链
- **① 技能描述（不可信 SKILL.md frontmatter）注入 LLM 上下文**：`skills/loader.py:92-124` `get_skills_text` 把每个技能的 `description`（解析自 SKILL.md，第 65-67 行）直接拼进 `<available_skills>` XML；`skill_view.py:202-210,391` 只记录注入模式，仍把完整不可信内容返回 LLM。**存在恶意/被植入技能时触发。**
- **② 技能扫描器故障放行**：`server/service/skill_scanner.py:486-514` `build_reject_message` 在扫描器 `UNAVAILABLE` 时返回 `None`（放行）；`skills_snapshot.py:11-54`、`clawhub_runner.py:150-156` 均为 fail-open。**扫描器缺失/报错时触发。**
- **③ `tool_guardrails.py` 仅做循环检测，非权限闸门**：检测失败重复、同工具失败累积、无进展（第 119-172 行），**不**审批/确认首次工具调用。因此**所有工具（含上述危险项）只要 LLM 发出调用即自动执行**——这是架构性关键缺口：防护针对死循环，而非恶意/错误工具使用。当前 `sandbox=False` 未沙箱执行已有 HITL 人工审批门：主会话经 HITL interrupt 人工审批，子代理/后台 scope 一律 `ToolException` 硬拒（接线在 `agent/middlewares/humanInTheLoop/`），危险命令正则也先于任何子进程生成执行；但面向其余工具的首次调用确认闸门仍不存在。

## 10. SSRF — 未校验媒体 URL 下载
**文件**：`agent/middlewares/multimodal_processor.py:71-83`
```python
req = urllib.request.Request(url, headers={...})
with urllib.request.urlopen(req, timeout=30) as resp:  # 无内网 IP 黑名单
    data = resp.read()
```
- `is_url`（`pub/func/validator/is_url.py`）仅校验 scheme 白名单，**不阻止** `127.0.0.1`、`169.254.169.254`（云元数据）、内网 RFC1918 地址。
- **利用**：`image_url: {"url": "http://169.254.169.254/latest/meta-data/iam/security-credentials/"}` → 响应存盘并经 LLM 摘要回显。
- **修复**：增加内网 IP 黑名单（拒绝 RFC1918/loopback/link-local 地址）。

## 11. `multimodal_processor` — 异步钩子中的阻塞网络 I/O + 文件 I/O + CPU 密集操作
**文件**：`agent/middlewares/multimodal_processor.py:411-413,426-428`
- `abefore_agent`/`aafter_agent`（async）直接调用同步的 `_before_agent_impl`/`_after_agent_impl`：`urllib.request.urlopen()`（网络）、`Image.open()`（CPU 解码）、`write_bytes()`（文件 I/O）——全部在事件循环线程上同步执行。
- **影响**：一个大文件下载或图像解码阻塞所有其他会话的流。
- **修复**：将实现体包装在 `await asyncio.to_thread(...)` 中。

## 12. `StateRegisterDB` 同步 SQLite 在异步中间件路径中被调用
**文件**：`runtime/state_register.py:116-128` + 调用方
- 每个方法都打开**新的** `sqlite3.connect()` + 执行 + 关闭。被 `awrap_model_call`/`awrap_tool_call`/`aafter_agent`（全 async）每次 agent 回合调用**多次**。
- 未被条目 #3（异步路径上的阻塞同步 SQLite）覆盖（不同文件）。
- **修复**：改 `aiosqlite` 或在异步路径中用 `asyncio.to_thread` 包装。

## 13. `summarization.py` — `awrap_model_call` 异步路径中的同步 SQLite + 文件 I/O
**文件**：`agent/middlewares/summarization.py:412-415`
- `_apply_compression`（sync）→ `memory_store.load_from_disk()`（文件 I/O）+ `build_system_prompt()`（含 `state_register_db` 同步 SQLite）+ `state_register_db.set_state()`（同步 SQLite），被 `awrap_model_call`（async）调用。
- **修复**：同 #12。

## 14. `WsTurnExecutor.execute` — 被取消时不取消 child 任务
**文件**：`server/service/turn_runner.py:298-314`
- `execute` 被取消时 `await child` 收到 `CancelledError`，re-raise 但 **child 从未被取消**。`finally` 从 `_active_tasks` 弹出槽位但不 cancel child，且调用 `on_turn_finished` 可能触发更多 queued turn——此时 child 仍在后台运行。
- **修复**：在 re-raise 路径前加 `child.cancel()`。

---

# 🟡 中（Medium）— 代码质量 / 架构 / 正确性

## 15. `bus/core.py:16-18` 单一全局队列，无按渠道路由
- `MessageBus` 一个入站 + 一个出站 `asyncio.Queue`，所有渠道共享，路由在下游按 `msg.channel` 判断。高并发多渠道时是瓶颈且无法按渠道背压（当前规模可接受，中级优先）。

## 16. 未鉴权日志文件读取
**文件**：`server/trigger/http/logs.py:170-198` — `GET /logs?path=...`。路径经 `LOG_DIR` 校验（受控），但端点未鉴权，日志可能含密钥。**利用：** `GET /logs?path=<info 日志>`。

## 17. 知识图谱遍历参数无上限
**文件**：`server/trigger/http/knowledge_graph.py:146,150`
```python
max_depth = max(0, int(query.get("max_depth", 3)))  # 仅最小值，无上限
max_nodes = max(1, int(query.get("max_nodes", 1000)))  # 仅最小值
```

## 18. `interrupt_marker` — 异步函数中调用同步 SQLite
**文件**：`server/service/interrupt_marker.py:242`
```python
rows = store_core.get_messages_by_lastest_n_turns(session_id, last_n=2)  # SYNC SQLite in async
```
- **修复**：`await asyncio.to_thread(store_core.get_messages_by_lastest_n_turns, ...)`。

## 19. `steering_queue` — 异步方法中使用 `threading.Lock`
**文件**：`agent/tools/subagent/announce/steering_queue.py:119,173,220`
- `enqueue_steering`/`drain`（async）用 `with state.lock:`（`threading.Lock`），另一线程持锁时事件循环阻塞。
- **修复**：改 `asyncio.Lock`。

## 20. `run_async` — 从运行中的事件循环调用时 `future.result()` 阻塞
**文件**：`pub/func/run_async.py:77-114`
```python
if loop and loop.is_running():
    future = pool.submit(_run_in_worker)
    return future.result(timeout=timeout)  # 阻塞事件循环线程
```
- **修复**：确保所有 `run_async` 调用方不在事件循环线程上，或提供 `await` 版本。

## 21. `list_descendant_runs` — O(N*D) BFS 重复全量扫描
**文件**：`agent/tools/subagent/registry/queries.py:12-26`
- BFS 每展开一个节点遍历内存中**全部** run 记录。对 D 层 N 条记录 = O(N*D)。
- **修复**：先调用 `build_read_index()`（已存在）构建 requester→runs 索引。

## 22. swarm 计数器 — `all_runs()` 全量扫描在 pump_lane 循环中
**文件**：`agent/tools/subagent/swarm/collector.py:329-356,212-228`
- `_count_active_swarm_runs` = `sum(1 for r in all_runs() if ...)`，`_pump_lane` while 循环每次迭代调用。
- **修复**：维护 per-group 增量计数器。

## 23. `add_messages` — 5N 次 `json.dumps` 在事件循环线程上
**文件**：`context_engine/store/core.py:33-251`
- `async def` 函数中，每条消息最多 5 次 `json.dumps`（content/tool_calls/images/audios/videos）。长对话 batch 阻塞事件循环。
- **修复**：移到 `asyncio.to_thread()` 或用 `orjson`。

## 24. `get_history_by_turn_page` — 5N 次 `json.loads` 在事件循环线程上
**文件**：`context_engine/store/core.py:296-309,364-378`
- 每行最多 5 次 `json.loads`，从 async 路径调用。
- **修复**：同 #23。

## 25. `StateRegisterDB` — 每次操作新建 SQLite 连接
**文件**：`runtime/state_register.py:116-224`
- `set_state`/`get_state`/`has_session` 每个方法都 `sqlite3.connect()` + `close()`。热路径上频繁新建连接。
- **修复**：复用模块级连接（加锁）或使用 `aiosqlite`。

---

# 🟢 低（Low）— 小问题 / 清理

## 26. Checkpointer 主连接无关闭方法
**文件**：`agent/checkpointer/async_sqlite_checkpointer.py:16-23` — `ThreadSafeAsyncSqliteSaver` 无 `close()`/`aclose()` 方法，连接永不关闭。
- 注：本条指主 checkpointer 连接，与 `delete_thread_history` 的连接问题相互独立。

## 27. `sender_task.cancel()` 未 `await`（2 处）
**文件**：`server/trigger/ws/subagent_ws.py:175`、`server/trigger/ws/logs.py:144`
- **修复**：`sender_task.cancel(); await asyncio.wait_for(sender_task, timeout=1.0)`。

## 28. Channel 线程事件循环未关闭
**文件**：`server/trigger/channels/core.py:330-342` — `event_loop.run_forever()` 退出后无 `event_loop.close()`。对比 cron 服务线程有 `loop.close()`。
- **修复**：在 `_run()` 的 `finally` 块中加 `event_loop.close()`。

## 29. `threading.Lock` 在异步调用链中使用（3 处）
**文件**：
- `agent/tools/subagent/registry/session_state.py:93,143,151,160` — `_HITL_LOCK`/`_INFLIGHT_LOCK`
- `agent/tools/subagent/registry/memory.py:6` — `_lock = threading.Lock()`
- `server/trigger/ws/subagent_ws.py:66,74,104,128` — `_subscribers_lock`/`_hooks_lock`
- 锁持有时间极短，实际阻塞风险低但技术上是 `threading.Lock` 在异步调用链中。

## 30. `delegate_task` — `asyncio.run()` 未检查运行中的事件循环
**文件**：`agent/tools/subagent/delegate.py:428` — 从异步上下文调用会抛 `RuntimeError`。

---

# 超大模块（>500 行，范围内，建议拆分）

| 文件 | 行数 |
|---|---|
| `agent/tools/skill_tools/skill_manage.py` | 845 |
| `server/service/messages.py` | 611 |
| `agent/tools/subagent/spawn/core.py` | 585 |
| `agent/tools/pub_base/skill_usage.py` | 507 |
| `context_engine/curator/orchestrator.py` | 504 |
| `agent/tools/memory.py` | 503 |

注：`agent/middlewares/multimodal_processor.py` 现约 211 行，降至阈值以下，不再列入本表。

# 类型安全备注
- `pub/types/message.py`、`pub/types/client.py`、`pub/types/bus.py` 类型良好（Pydantic/dataclass），无问题。
- `context_engine/core.py` 的 DB 行用 `dict[str, Any]` 返回——可接受，但可用 dataclass 收紧类型。

---

# 已复核为非漏洞（受控）
- `server/service/workplace.py:23-25` `write_system_prompt_file` 校验 `ALL_SYSTEM_FILE_NAMES` 允许列表。受控。
- `server/service/env.py:140-143` `write_env_file` 拒绝未知键。受控。
- `agent/tools/pub_base/skill_utils.py:38-41` `yaml.load` 用 `CSafeLoader`/`SafeLoader`（安全，非不安全加载器）。非反序列化漏洞。
- `context_engine/store/core.py`、`runtime/state_register.py` 全部 SQL 均参数化。无 SQLi。
- `agent/tools/skill_tools/skill_manage.py` 路径校验、名称正则、文件大小上限。已加固。

---

# 修复优先级建议
1. **先修 CRITICAL**：#1 `skills.py` HTTP 路径穿越（加 `is_relative_to` 检查）、#2 `delegate.py` `time.sleep` 阻塞事件循环（提供 `result_async`）。
2. **加认证**：`server/trigger/core.py` 加 token/API-key 中间件并作用于所有路由与 WebSocket，去掉通配 CORS。覆盖 #4-#8、#16。
3. **路径穿越 / SSRF / 输入校验收口**：#10 内网 IP 黑名单、#6 `shutil.rmtree` 前清洗 `session_id`，以及 #17 知识图谱遍历参数钳制。
4. **资源泄漏与后台任务治理**：#14 `execute` 取消 child 任务。
5. **异步/阻塞收口**：#3 store 迁移 `aiosqlite`、#11-#13 multimodal_processor/summarization/StateRegisterDB 阻塞 I/O 移 `to_thread` 或迁 `aiosqlite`、#18-#20 异步路径中的同步调用收口。
6. **性能**：#21-#22 冗余扫描与计数、#23-#25 JSON 序列化与连接风暴收口。
7. **fail-open 与确认闸门**：#9-② 重审扫描器故障放行策略、#9-③ 为高风险工具增加首次调用确认闸门。
8. **低优先级清理**：#15 bus 单队列路由（当前规模可接受）与 #26-#30 逐项修复。
9. 修复后重新审计，确认以上各域闭合。
