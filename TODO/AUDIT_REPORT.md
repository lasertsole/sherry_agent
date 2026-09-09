# 审计报告 — Sherry Agent

**审计范围**：`C:\app\code\project\sherry_agent` 全项目（排除 `.venv`、`node_modules`、`client/`、`future/`、`temp/`、`logs/`、vendored LightRAG/模型权重数据）。
**审计方法**：4 个并行审计子代理（安全、代码质量/架构、正确性/数据、技能系统与沙箱）+ 对关键发现进行源码直接复核；第二轮补充审计（2026-09-04）由 6 个并行探索子代理覆盖（资源泄漏、错误处理/静默吞没、异步/同步边界违规、SQL 安全/输入校验、安全/路径穿越/SSRF、性能/可扩展性），同样经源码直接复核。
**审计日期**：2026-08-24（第一轮）/ 2026-09-04（第二轮）
**更新日期**：2026-09-10 — 第二轮审计条目并入本报告，两轮中已修复的条目全部移除，剩余条目重编码为连续编号 #1-#46，正文交叉引用与"修复优先级建议"同步更新。

---

# 🔴 严重（Critical）— 安全 / RCE / 可用性

## 1. `terminal.py` 黑名单可被轻易绕过；`shell=True`
> **状态更新（2026-09-04）：部分修复**：旧元素级 `BLACKLIST`（`"rm -rf /"`、`"mkfs"`、`"shutdown"`、`"reboot"` 裸子串）已被 `DANGEROUS_COMMAND_REGEX`（`agent/tools/terminal.py:65-77`）取代——6 个可选模式、`re.IGNORECASE`、对 `" && "` 连接后的整条命令串匹配，封死审计时点名的 `["echo ok", "rm -rf /"]` 元素级绕过；命中即抛 `ToolException("Blocked: unsafe command.")`（174-178 行，`handle_tool_error=True` 走错误 ToolMessage 通道）。新增无条件防线：`env=scrub_env()` 每个生成点执行（300/332 行，密钥类变量名不再进入子进程环境）、`cwd=str(ROOT_DIR)` 钳制；Linux bubblewrap / macOS Seatbelt OS 沙箱（`backend.wrap` 列表 exec，无 shell kwarg，208/341-347 行）；`sandbox=False` 主会话 HITL 人工审批门 + 子代理/后台 scope 硬拒（151-172 行）。

**文件**：`agent/tools/terminal.py:12`（已验证）
- `BLACKLIST = {"rm -rf /", "mkfs", "shutdown", "reboot"}` 是裸子串检查。绕过方式：`rm -rf /tmp/../`、`reboot -f`、`shutdown -h now`、`curl | sh`，以及任何未列入表的破坏性命令。
- 第 98 行 `shell=True`、第 59 行 `create_subprocess_shell`。
- **修复**：shlex 解析、去掉 `shell=True`，改用允许列表或真实沙箱。
- **剩余风险**：Windows/无后端回退路径按设计保留 `shell=True`（228-235/278/349 行，与加固前字节一致，仅加 `env=`）；正则门仍是黑名单而非允许列表；`SANDBOX_POLICY=auto`（默认）+ 无后端时降级为无沙箱执行（仅一条 loguru 警告）。原建议中的 shlex 解析/去 `shell=True`/允许列表未采纳——采纳的是"真实沙箱"路线，且 OS 沙箱构造逻辑仅单测验证、未在真实 Linux/macOS 上实测。

## 2. `clawhub` 运行任意远程 npm 代码
**文件**：`skills/builtin/core/clawhub/scripts/clawhub_runner.py:227` — `npx --yes clawhub@latest`
- `--yes` 自动安装并执行 npm 供应的任何内容 → 供应链 RCE。装后扫描器（`_scan_plugin_skills`）是**故障放行（fail-open）**缓解，非硬性闸门。
- **修复**：固定版本；要求显式用户确认；移除 `--yes`。

## 3. HTTP 路径穿越读取 — `GET /skills/*skill_path` 绕过 `resolve_path`
**文件**：`server/trigger/http/skills.py:122-128`
```python
skill_path = path_params["skill_path"]
full_path = ROOT_DIR / skill_path  # 无 is_relative_to 检查
content = full_path.read_text(...)  # 读取任意文件
```
- `resolve_path` 已修复（含 `is_relative_to(ROOT_DIR)` 检查），但此端点**完全绕过** `resolve_path`，直接 `ROOT_DIR / skill_path`。
- **利用**：`GET /skills/../../.env` → 窃取 `MAIN_LLM_API_KEY`。
- **修复**：加 `full_path.resolve().is_relative_to(ROOT_DIR.resolve())` 检查。

## 4. `delegate.py` — `time.sleep()` 在事件循环线程上阻塞
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

## 5. 异步路径上的阻塞同步 SQLite
**文件**：`context_engine/store/core.py:11,143,186`；`context_engine/store/db.py`
- `async` 函数直接在事件循环线程上执行阻塞式 `executemany`/`commit`；模块级共享连接被 WS 循环、cron 线程、子代理线程共用（非线程安全）。
- **修复**：用 `aiosqlite` 或每线程连接 + 锁。

## 6. 缺认证 + 通配 CORS — 所有端点未鉴权
**文件**：`server/trigger/core.py:14` — `ALLOW_CORS(app, origins=["*"])`
- `server/__main__.py` 与 `trigger/core.py` 无任何认证中间件。所有 HTTP/WS 端点未鉴权、跨域可达。
- **利用**：任意网站可向 agent API 发请求。衍生以下未鉴权高风险端点（#5-#8）。

## 7. 未鉴权 `.env` 读写 — 密钥暴露
**文件**：`server/trigger/http/env.py:6-32`
- `GET /env` 返回含 API Key 的完整配置；`PUT /env` 可改写运行时凭据。
- **利用**：`GET /env` 窃取 `MAIN_LLM_API_KEY`。

## 8. 未鉴权 session 清除 + 路径穿越删除
**文件**：`server/trigger/http/messages.py:16-23` + `server/DAO/messages.py:8-9,32-34`
- `DELETE /sessions` 携带 `session_id`（如 `../../src`）经 `_session_folder`/`shutil.rmtree` 穿越删除任意目录。无鉴权、无清洗。
- **利用**：`DELETE /sessions` body `{"session_id":"../../workspace"}`。

## 9. 未鉴权 WebSocket agent 控制
**文件**：`server/trigger/ws/messages.py:116-183` — `app.websocket("/sessions/agent/ws")`
- 客户端可发 `multi_modal_message` 触发 `async_generate`、`hitl_response` 批准/拒绝工具调用、`stop` 取消。
- **利用**：攻击者驱动 agent 并批准危险工具调用。

## 10. 未鉴权日志流 — 密钥泄漏
**文件**：`server/trigger/ws/logs.py:119-148` — `app.websocket("/logs/ws")`
- 把每条 loguru 记录（可能含 API Key、请求头、提示词）流式推给任意未鉴权 WS 客户端。
- **利用**：连接 `/logs/ws` 从日志帧读取密钥。

## 11. 技能系统沙箱/供应链
- **① 技能描述（不可信 SKILL.md frontmatter）注入 LLM 上下文**：`skills/loader.py:92-124` `get_skills_text` 把每个技能的 `description`（解析自 SKILL.md，第 65-67 行）直接拼进 `<available_skills>` XML；`skill_view.py:202-210,391` 只记录注入模式，仍把完整不可信内容返回 LLM。**存在恶意/被植入技能时触发。**
- **② 技能扫描器故障放行**：`server/service/skill_scanner.py:486-514` `build_reject_message` 在扫描器 `UNAVAILABLE` 时返回 `None`（放行）；`skills_snapshot.py:11-54`、`clawhub_runner.py:150-156` 均为 fail-open。**扫描器缺失/报错时触发。**
- **③ `tool_guardrails.py` 仅做循环检测，非权限闸门**：检测失败重复、同工具失败累积、无进展（第 119-172 行），**不**审批/确认首次工具调用。因此**所有工具（含上述危险项）只要 LLM 发出调用即自动执行**——这是架构性关键缺口：防护针对死循环，而非恶意/错误工具使用。
  - **状态更新（2026-09-04）：部分缓解**——沙箱加固为最高风险动作（`sandbox=False` 未沙箱执行）新增了人工审批门：主会话经 HITL interrupt 人工审批，子代理/后台 scope 一律 `ToolException` 硬拒（接线在 `agent/middlewares/humanInTheLoop/`）；且危险命令正则先于任何子进程生成执行、与 sandbox 标志无关。但"所有工具 LLM 发出调用即自动执行"的一般性缺口在其余工具上依然成立——首次工具调用确认闸门仍不存在。

## 12. `session_id` 路径穿越写 — multimodal_processor
**文件**：`agent/middlewares/multimodal_processor.py:92,97,208,221,244,253,276,285,375`
```python
temp_dir = SRC_DIR / session_id / "mutil_temp"  # session_id 未清洗
temp_dir.mkdir(parents=True, exist_ok=True)
temp_path.write_bytes(data)  # 写入任意路径
```
- **输入源**：`server/trigger/ws/messages.py:168` → `session_id = obj.get("session_id")` 从 WS JSON body 取值，**无** `_validate_session_id` 调用（对比 `media.py:29-42` 有防护）。
- **利用**：WS 帧 `{"session_id":"../../workspace","multi_modal_message":{...}}` → 写入项目外。
- **修复**：在 `_before_agent_impl` 入口调用 `_validate_session_id`。

## 13. SSRF — 未校验媒体 URL 下载
**文件**：`agent/middlewares/multimodal_processor.py:71-83`
```python
req = urllib.request.Request(url, headers={...})
with urllib.request.urlopen(req, timeout=30) as resp:  # 无内网 IP 黑名单
    data = resp.read()
```
- `is_url`（`pub_func/validator/is_url.py`）仅校验 scheme 白名单，**不阻止** `127.0.0.1`、`169.254.169.254`（云元数据）、内网 RFC1918 地址。
- **利用**：`image_url: {"url": "http://169.254.169.254/latest/meta-data/iam/security-credentials/"}` → 响应存盘并经 LLM 摘要回显。
- **修复**：增加内网 IP 黑名单（拒绝 RFC1918/loopback/link-local 地址）。

## 14. multipart 文件名路径穿越写 — 知识图谱上传
**文件**：`server/trigger/http/knowledge_graph.py:67-93`
```python
name = str(filename)  # 攻击者可控的 multipart 文件名
ext = Path(name).suffix.lower()
if ext not in _ALLOWED_EXT:
    ...  # 仅检查后缀，不阻挡 "../"
staged = stage_dir / name  # name 含 "../" → 穿越
staged.write_bytes(data)
```
- **利用**：multipart filename `../../../config/evil.txt`（后缀 `.txt` 允许）→ 写入 staging 目录外。对比 `skills.py:upload_skill_handler` **有** `relative_to` 防护。
- **修复**：用 `Path(name).name` 取纯文件名。

## 15. WS 断连时不取消运行中任务（任务泄漏）
**文件**：`server/trigger/ws/messages.py:295-300`
```python
for sid, task in list(_active_tasks.items()):
    if task.done():  # 仅移除已完成的
        _active_tasks.pop(sid, None)
```
- 仍在运行的任务留在 `_active_tasks` 中继续执行——持续调用 LLM API、运行工具——但 `_send_ws()` 因 socket 已关闭而静默失败。浪费 API 配额、阻塞同 session 的新请求。
- **修复**：`if not task.done(): task.cancel()`。

## 16. `multimodal_processor` — 异步钩子中的阻塞网络 I/O + 文件 I/O + CPU 密集操作
**文件**：`agent/middlewares/multimodal_processor.py:411-413,426-428`
- `abefore_agent`/`aafter_agent`（async）直接调用同步的 `_before_agent_impl`/`_after_agent_impl`：`urllib.request.urlopen()`（网络）、`Image.open()`（CPU 解码）、`write_bytes()`（文件 I/O）——全部在事件循环线程上同步执行。
- **影响**：一个大文件下载或图像解码阻塞所有其他会话的流。
- **修复**：将实现体包装在 `await asyncio.to_thread(...)` 中。

## 17. `StateRegisterDB` 同步 SQLite 在异步中间件路径中被调用
**文件**：`runtime/state_register.py:116-128` + 调用方
- 每个方法都打开**新的** `sqlite3.connect()` + 执行 + 关闭。被 `awrap_model_call`/`awrap_tool_call`/`aafter_agent`（全 async）每次 agent 回合调用**多次**。
- 未被条目 #5（异步路径上的阻塞同步 SQLite）覆盖（不同文件）。
- **修复**：改 `aiosqlite` 或在异步路径中用 `asyncio.to_thread` 包装。

## 18. `summarization.py` — `awrap_model_call` 异步路径中的同步 SQLite + 文件 I/O
**文件**：`agent/middlewares/summarization.py:412-415`
- `_apply_compression`（sync）→ `memory_store.load_from_disk()`（文件 I/O）+ `build_system_prompt()`（含 `state_register_db` 同步 SQLite）+ `state_register_db.set_state()`（同步 SQLite），被 `awrap_model_call`（async）调用。
- **修复**：同 #17。

## 19. `WsTurnExecutor.execute` — 被取消时不取消 child 任务
**文件**：`server/service/turn_runner.py:298-314`
- `execute` 被取消时 `await child` 收到 `CancelledError`，re-raise 但 **child 从未被取消**。`finally` 从 `_active_tasks` 弹出槽位但不 cancel child，且调用 `on_turn_finished` 可能触发更多 queued turn——此时 child 仍在后台运行。
- **修复**：在 re-raise 路径前加 `child.cancel()`。

## 20. 内存泄漏：`_terminal_locks`/`_cleanup_generations`/`_deferred_cleanup_timers` 永不清理
**文件**：`agent/tools/subagent/registry/lifecycle.py:40-42`
```python
_terminal_locks: dict[str, asyncio.Lock] = {}  # 只增不减
_cleanup_generations: dict[str, int] = {}
_deferred_cleanup_timers: dict[str, asyncio.Task] = {}
```
- 每个 subagent run 完成后，这三组 dict 中的条目**永远不删除**。长时间运行的服务器积累数千条目，每条还持有闭包引用阻止 GC。
- **修复**：在 `_finalize_cleanup()` 完成后 `.pop(run_id, None)`。

---

# 🟡 中（Medium）— 代码质量 / 架构 / 正确性

## 21. `bus/core.py:16-18` 单一全局队列，无按渠道路由
- `MessageBus` 一个入站 + 一个出站 `asyncio.Queue`，所有渠道共享，路由在下游按 `msg.channel` 判断。高并发多渠道时是瓶颈且无法按渠道背压（当前规模可接受，中级优先）。

## 22. 未鉴权日志文件读取
**文件**：`server/trigger/http/logs.py:170-198` — `GET /logs?path=...`。路径经 `LOG_DIR` 校验（受控），但端点未鉴权，日志可能含密钥。**利用：** `GET /logs?path=<info 日志>`。

## 23. 上传端点无大小限制（audio/video/image）
**文件**：`server/trigger/http/audio.py:60-64`、`video.py:60-61`、`image.py:59-60`
```python
file_path.write_bytes(data)  # 无 len(data) 检查
```
- **修复**：加 `len(data)` 上限检查（对比 `skills.py:upload` 有 `_MAX_SKILL_CONTENT_CHARS`）。

## 24. 知识图谱遍历参数无上限
**文件**：`server/trigger/http/knowledge_graph.py:146,150`
```python
max_depth = max(0, int(query.get("max_depth", 3)))  # 仅最小值，无上限
max_nodes = max(1, int(query.get("max_nodes", 1000)))  # 仅最小值
```

## 25. 子代理生成安全限制可被 HTTP body 覆盖
**文件**：`server/trigger/http/subagent.py:232-237` + `agent/tools/subagent/delegate.py:271-274`
- `max_spawn_depth`/`max_children_per_agent` 从 HTTP body 取值，`int()` 后无上下界 → 绕过 `depth.py:validate_spawn_depth` 的全局配置上限。
- **修复**：钳制到配置范围 `[1, config.max_spawn_depth]`。

## 26. Channel Manager 消费者循环任务未跟踪，无法取消
**文件**：`channels/manager.py:148-149`
```python
self._event_loop.create_task(self._inbound_consume_loop())  # 引用未存储
self._event_loop.create_task(self._outbound_consume_loop())
```
- `stop_service()` 仅取消 `_dispatch_task`，两个消费者循环无法被显式取消。
- **修复**：存储 task 引用，在 `stop_service` 中 cancel。

## 27. Work admission：drain-retry 任务 fire-and-forget
**文件**：`agent/tools/subagent/registry/work_admission.py:25`
```python
asyncio.create_task(_schedule_drain_retry(coro, label, delay=5.0))  # 未加入 _root_work_tasks
```
- 对比第 28-30 行的 `_run_and_cleanup` 路径正确跟踪。此 task 无 `add_done_callback`，异常静默吞没。

## 28. `interrupt_marker` — 异步函数中调用同步 SQLite
**文件**：`server/service/interrupt_marker.py:242`
```python
rows = store_core.get_messages_by_lastest_n_turns(session_id, last_n=2)  # SYNC SQLite in async
```
- **修复**：`await asyncio.to_thread(store_core.get_messages_by_lastest_n_turns, ...)`。

## 29. `steering_queue` — 异步方法中使用 `threading.Lock`
**文件**：`agent/tools/subagent/announce/steering_queue.py:119,173,220`
- `enqueue_steering`/`drain`（async）用 `with state.lock:`（`threading.Lock`），另一线程持锁时事件循环阻塞。
- **修复**：改 `asyncio.Lock`。

## 30. `run_async` — 从运行中的事件循环调用时 `future.result()` 阻塞
**文件**：`pub_func/run_async.py:77-114`
```python
if loop and loop.is_running():
    future = pool.submit(_run_in_worker)
    return future.result(timeout=timeout)  # 阻塞事件循环线程
```
- **修复**：确保所有 `run_async` 调用方不在事件循环线程上，或提供 `await` 版本。

## 31. `list_descendant_runs` — O(N*D) BFS 重复全量扫描
**文件**：`agent/tools/subagent/registry/queries.py:12-26`
- BFS 每展开一个节点遍历内存中**全部** run 记录。对 D 层 N 条记录 = O(N*D)。
- **修复**：先调用 `build_read_index()`（已存在）构建 requester→runs 索引。

## 32. `agent_created_report` — 单次 curator 运行中调用 3-4 次
**文件**：`context_engine/curator/orchestrator.py:130,164,181,249,266`
- 每次调用遍历 `skills/auto/` 目录树，对每个技能读 JSON + 读 SKILL.md + 检查 .pinned。
- **修复**：在 `run_curator_review()` 开始时调用一次并缓存。

## 33. swarm 计数器 — `all_runs()` 全量扫描在 pump_lane 循环中
**文件**：`agent/tools/subagent/swarm/collector.py:329-356,212-228`
- `_count_active_swarm_runs` = `sum(1 for r in all_runs() if ...)`，`_pump_lane` while 循环每次迭代调用。
- **修复**：维护 per-group 增量计数器。

## 34. `_launch_fingerprints` 永不清理（swarm collector）
**文件**：`agent/tools/subagent/swarm/collector.py:13`
- `reserve_swarm_run` 添加映射，run 完成后无任何地方删除。
- **修复**：在 `complete_swarm_run` 或 sweeper 中删除对应条目。

## 35. `_pending_args`/`_pending_raw` 在会话异常断开时泄漏
**文件**：`server/service/messages.py:26-29`
- WS 连接在 turn 中途断开时 `_clear_pending_args` 不被调用，session 的 pending args 和 raw buffer 永久驻留。无 TTL、无全局清理。
- **修复**：在 WS disconnect handler 中调用 `_clear_pending_args(session_id)`。

## 36. `add_messages` — 5N 次 `json.dumps` 在事件循环线程上
**文件**：`context_engine/store/core.py:33-251`
- `async def` 函数中，每条消息最多 5 次 `json.dumps`（content/tool_calls/images/audios/videos）。长对话 batch 阻塞事件循环。
- **修复**：移到 `asyncio.to_thread()` 或用 `orjson`。

## 37. `get_history_by_turn_page` — 5N 次 `json.loads` 在事件循环线程上
**文件**：`context_engine/store/core.py:296-309,364-378`
- 每行最多 5 次 `json.loads`，从 async 路径调用。
- **修复**：同 #36。

## 38. `StateRegisterDB` — 每次操作新建 SQLite 连接
**文件**：`runtime/state_register.py:116-224`
- `set_state`/`get_state`/`has_session` 每个方法都 `sqlite3.connect()` + `close()`。热路径上频繁新建连接。
- **修复**：复用模块级连接（加锁）或使用 `aiosqlite`。

---

# 🟢 低（Low）— 小问题 / 清理

## 39. 不安全的 `yaml.Loader` — STT 模型配置
**文件**：`models/STT_model/utils/infer_utils.py:360` — `yaml.load(f, Loader=yaml.Loader)`（非 `SafeLoader`）。
- **修复**：改为 `yaml.SafeLoader`。

## 40. Checkpointer 主连接无关闭方法
**文件**：`agent/checkpointer/async_sqlite_checkpointer.py:16-23` — `ThreadSafeAsyncSqliteSaver` 无 `close()`/`aclose()` 方法，连接永不关闭。
- 注：`delete_thread_history` 的连接泄漏已在此前修复，但主 checkpointer 连接是独立问题。

## 41. `sender_task.cancel()` 未 `await`（2 处）
**文件**：`server/trigger/ws/subagent_ws.py:175`、`server/trigger/ws/logs.py:144`
- **修复**：`sender_task.cancel(); await asyncio.wait_for(sender_task, timeout=1.0)`。

## 42. Channel 线程事件循环未关闭
**文件**：`server/trigger/channels/core.py:330-342` — `event_loop.run_forever()` 退出后无 `event_loop.close()`。对比 cron 服务线程有 `loop.close()`。
- **修复**：在 `_run()` 的 `finally` 块中加 `event_loop.close()`。

## 43. `threading.Lock` 在异步调用链中使用（3 处）
**文件**：
- `agent/tools/subagent/registry/session_state.py:93,143,151,160` — `_HITL_LOCK`/`_INFLIGHT_LOCK`
- `agent/tools/subagent/registry/memory.py:6` — `_lock = threading.Lock()`
- `server/trigger/ws/subagent_ws.py:66,74,104,128` — `_subscribers_lock`/`_hooks_lock`
- 锁持有时间极短，实际阻塞风险低但技术上是 `threading.Lock` 在异步调用链中。

## 44. cron `base.py` — `asyncio.get_event_loop()` 应为 `get_running_loop()`
**文件**：`skills/builtin/core/cron/scripts/base.py:389,433` — 已弃用 API。

## 45. `delegate_task` — `asyncio.run()` 未检查运行中的事件循环
**文件**：`agent/tools/subagent/delegate.py:428` — 从异步上下文调用会抛 `RuntimeError`。

## 46. `_summarize` — 每次重试都重建 LLM 客户端
**文件**：`agent/tools/message_search.py:181-189` — `build_main_llm()` 在 retry 循环内。
- **修复**：在循环外调用一次，循环内复用。

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
- `type/message.py`、`type/client.py`、`type/bus.py` 类型良好（Pydantic/dataclass），无问题。
- `context_engine/core.py` 的 DB 行用 `dict[str, Any]` 返回——可接受，但可用 dataclass 收紧类型。

---

# 已复核为非漏洞（受控）
- `server/service/workplace.py:23-25` `write_system_prompt_file` 校验 `ALL_SYSTEM_FILE_NAMES` 允许列表。受控。
- `server/service/env.py:140-143` `write_env_file` 拒绝未知键。受控。
- `agent/tools/pub_base/skill_utils.py:38-41` `yaml.load` 用 `CSafeLoader`/`SafeLoader`（安全，非不安全加载器）。非反序列化漏洞。
- `context_engine/store/core.py`、`runtime/state_register.py` 全部 SQL 均参数化。无 SQLi。
- `agent/tools/skill_tools/skill_manage.py` 路径校验、名称正则、文件大小上限。已加固。

---

# 沙箱加固层（2026-09-03/04）

2026-09-03/04 落地的沙箱加固层，对应 #1（部分缓解）、#9-③（针对性审批门）。实现位于 `agent/tools/pub_base/{env_scrub,sandbox,sandbox_bwrap,sandbox_seatbelt}.py` + 两个工具（`agent/tools/terminal.py`、`agent/tools/python_repl.py`）+ HITL 中间件（`agent/middlewares/humanInTheLoop/`）。

- **L1 环境变量清洗**（`scrub_env`）：11 个密钥子串拦截、~29 个精确保留名、`LC_`/`XDG_`/`CONDA` 前缀保留、11 个项目密钥强制拒绝；优先级为 保留 > 强制拒绝 > 子串拦截；在两个工具的每个生成点无条件执行（含已批准的 `sandbox=False` 调用）。
- **L2 OS 沙箱**：Linux bubblewrap——`--ro-bind / /` 只读根 + 仅项目根/临时目录可写 + `--unshare-all` + `--clearenv` 先于 `--setenv`；macOS Seatbelt——`(deny file-write*)` + 子路径 allowlist + `json.dumps` 转义路径。两后端均为列表 exec，无 shell kwarg。
- **策略三态**：`SANDBOX_POLICY=required|auto|off`（默认 `auto`；非法值抛 `ValueError`；每次调用重读）。
- **审批门**：`sandbox=False` 仅主会话经 HITL interrupt 人工审批，子代理/后台 scope 一律 `ToolException` 硬拒；危险命令正则与 sandbox 标志无关、先于任何子进程生成执行。
- **测试**：6 格优先级矩阵由 `tests/integration/test_sandbox_matrix.py` 14 个测试逐格覆盖；配套测试共 ~80+（env_scrub 29、matrix 14、bwrap/seatbelt 构造、HITL characterization 19 + bypass 17）。
- **诚实局限**：bwrap/Seatbelt 构造逻辑仅单测验证、未在真实 Linux/macOS 上实测；Windows 无 OS 沙箱；清洗按变量名（不扫值）；`auto` + 无后端时降级为无沙箱执行（有意放行，仅一条 loguru 警告）。
- **详细文档**：`docs/harness/sandbox/README.md`（EN/zh/ko/ja 四语）。

---

# 修复优先级建议
1. **先修 CRITICAL**：#1 `terminal.py`（沙箱加固层已落地：正则门 + env 清洗 + OS 沙箱 + 审批门，Windows 回退仍 `shell=True`，见该条目状态更新）、#2 `clawhub`（固定版本 + 去 `--yes`）、#3 `skills.py` HTTP 路径穿越（加 `is_relative_to` 检查）、#4 `delegate.py` `time.sleep` 阻塞事件循环（提供 `result_async`）。
2. **加认证**：`server/trigger/core.py` 加 token/API-key 中间件并作用于所有路由与 WebSocket，去掉通配 CORS。覆盖 #6-#10、#22。
3. **路径穿越 / SSRF / 输入校验收口**：#12 `session_id` 校验、#13 内网 IP 黑名单、#14 multipart 文件名清洗、#8 `shutil.rmtree` 前清洗 `session_id`，以及 #23 上传大小限制、#24-#25 参数钳制。
4. **资源泄漏与后台任务治理**：#15 WS 断连取消任务、#19 `execute` 取消 child、#20 `_terminal_locks` 等三组字典清理、#26-#27 未跟踪 task、#34-#35 缓存与 pending 缓冲清理。
5. **异步/阻塞收口**：#5 store 迁移 `aiosqlite`、#16-#18 multimodal_processor/summarization/StateRegisterDB 阻塞 I/O 移 `to_thread` 或迁 `aiosqlite`、#28-#30 异步路径中的同步调用收口。
6. **性能**：#31-#33 冗余扫描与计数、#36-#38 JSON 序列化与连接风暴收口。
7. **fail-open 与确认闸门**：#11-② 重审扫描器故障放行策略、#11-③ 为高风险工具增加首次调用确认闸门（`sandbox=False` 的审批门已于 2026-09-04 落地）。
8. **低优先级清理**：#21 bus 单队列路由（当前规模可接受）与 #39-#46 逐项修复。
9. 修复后重新审计，确认以上各域闭合。
