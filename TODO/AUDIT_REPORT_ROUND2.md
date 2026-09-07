# 第二轮审计 — 2026-09-04 补充

**审计方法**：6 个并行探索子代理覆盖（资源泄漏、错误处理/静默吞没、异步/同步边界违规、SQL 安全/输入校验、安全/路径穿越/SSRF、性能/可扩展性），对关键发现进行源码直接复核。所有条目均为**新增**，不与 `AUDIT_REPORT.md` #1-#22 重复。编号 #1 起。

---

# 🔴 严重（Critical）— 新增

## 1. HTTP 路径穿越读取 — `GET /skills/*skill_path` 绕过 `resolve_path`

**文件**：`server/trigger/http/skills.py:122-128`

```python
skill_path = path_params["skill_path"]
full_path = ROOT_DIR / skill_path          # 无 is_relative_to 检查
content = full_path.read_text(...)         # 读取任意文件
```

- `resolve_path`（AUDIT_REPORT #1）已修复（含 `is_relative_to(ROOT_DIR)` 检查），但此端点**完全绕过** `resolve_path`，直接 `ROOT_DIR / skill_path`。
- **利用**：`GET /skills/../../.env` → 窃取 `MAIN_LLM_API_KEY`。
- **修复**：加 `full_path.resolve().is_relative_to(ROOT_DIR.resolve())` 检查。

## 2. `delegate.py` — `time.sleep()` 在事件循环线程上阻塞

**文件**：`agent/tools/subagent/delegate.py:128-141`

```python
asyncio.get_running_loop()   # 检测到运行中的事件循环
in_loop = True
if in_loop:
    while self.is_running():
        time.sleep(poll_interval)   # ← 阻塞事件循环！
```

- `result()` 是同步方法，检测到运行中的事件循环后用 `time.sleep()` 轮询。注释声称"不阻塞外层循环"但 `time.sleep()` **确实阻塞**。
- **影响**：整个服务器在轮询期间冻结——所有 WS 连接、所有会话的流、所有定时器停顿。
- **修复**：提供 `async def result_async()` 方法，使用 `await asyncio.sleep()`。

## 3. 裸 `except:` 吞没所有异常（含 `SystemExit`/`KeyboardInterrupt`）

**文件**：`models/STT_model/utils/infer_utils.py:11-21`

```python
try:
    from onnxruntime import (...)
except:                          # 裸 except，吞没一切
    print("please pip3 install onnxruntime")
import jieba                     # 导入失败后仍继续
```

- 裸 `except:` 还吞没 `SystemExit`/`KeyboardInterrupt`。仅 `print` 到 stdout（守护进程中不可见）。
- **修复**：改 `except ImportError as e:`，加 `logger.error` 后 `raise`。

## 4. 渠道事件循环崩溃被静默吞没

**文件**：`server/trigger/channels/core.py:339-342`

```python
try:
    event_loop.run_forever()
except Exception:
    pass                         # 事件循环崩溃，静默无日志
```

- 渠道管理器事件循环崩溃后所有渠道（QQ 等）停止工作但**无任何日志告警**。进程继续运行但渠道全部失联。
- **修复**：`logger.exception("Channel event loop crashed")`。

## 5. Curator 主循环吞没所有异常

**文件**：`context_engine/curator/__init__.py:111-117`

```python
while True:
    try:
        maybe_run_curator(idle_for_seconds=...)
    except Exception:
        pass                     # curator 每次失败都静默吞没，无日志
```

- curator 核心逻辑（技能归档、清理、状态迁移）异常被静默吞没，curator 永远循环但永远失败。DB 损坏、LLM 调用失败、状态迁移错误全部隐藏。
- **修复**：`except Exception as e: logger.error("Curator cycle failed: {}", e)`。

---

# 🟠 高（High）— 新增

## 6. `session_id` 路径穿越写 — multimodal_processor

**文件**：`agent/middlewares/multimodal_processor.py:92,97,208,221,244,253,276,285,375`

```python
temp_dir = SRC_DIR / session_id / "mutil_temp"   # session_id 未清洗
temp_dir.mkdir(parents=True, exist_ok=True)
temp_path.write_bytes(data)                      # 写入任意路径
```

- **输入源**：`server/trigger/ws/messages.py:168` → `session_id = obj.get("session_id")` 从 WS JSON body 取值，**无** `_validate_session_id` 调用（对比 `media.py:29-42` 有防护）。
- **利用**：WS 帧 `{"session_id":"../../workspace","multi_modal_message":{...}}` → 写入项目外。
- **修复**：在 `_before_agent_impl` 入口调用 `_validate_session_id`。

## 7. SSRF — 未校验媒体 URL 下载

**文件**：`agent/middlewares/multimodal_processor.py:71-83`

```python
req = urllib.request.Request(url, headers={...})
with urllib.request.urlopen(req, timeout=30) as resp:   # 无内网 IP 黑名单
    data = resp.read()
```

- `is_url`（`pub_func/validator/is_url.py`）仅校验 scheme 白名单，**不阻止** `127.0.0.1`、`169.254.169.254`（云元数据）、内网 RFC1918 地址。
- **利用**：`image_url: {"url": "http://169.254.169.254/latest/meta-data/iam/security-credentials/"}` → 响应存盘并经 LLM 摘要回显。
- **修复**：增加内网 IP 黑名单（拒绝 RFC1918/loopback/link-local 地址）。

## 8. multipart 文件名路径穿越写 — 知识图谱上传

**文件**：`server/trigger/http/knowledge_graph.py:67-93`

```python
name = str(filename)                          # 攻击者可控的 multipart 文件名
ext = Path(name).suffix.lower()
if ext not in _ALLOWED_EXT: ...                # 仅检查后缀，不阻挡 "../"
staged = stage_dir / name                      # name 含 "../" → 穿越
staged.write_bytes(data)
```

- **利用**：multipart filename `../../../config/evil.txt`（后缀 `.txt` 允许）→ 写入 staging 目录外。对比 `skills.py:upload_skill_handler` **有** `relative_to` 防护。
- **修复**：用 `Path(name).name` 取纯文件名。

## 9. WS 断连时不取消运行中任务（任务泄漏）

**文件**：`server/trigger/ws/messages.py:295-300`

```python
for sid, task in list(_active_tasks.items()):
    if task.done():          # 仅移除已完成的
        _active_tasks.pop(sid, None)
```

- 仍在运行的任务留在 `_active_tasks` 中继续执行——持续调用 LLM API、运行工具——但 `_send_ws()` 因 socket 已关闭而静默失败。浪费 API 配额、阻塞同 session 的新请求。
- **修复**：`if not task.done(): task.cancel()`。

## 10. `multimodal_processor` — 异步钩子中的阻塞网络 I/O + 文件 I/O + CPU 密集操作

**文件**：`agent/middlewares/multimodal_processor.py:411-413,426-428`

- `abefore_agent`/`aafter_agent`（async）直接调用同步的 `_before_agent_impl`/`_after_agent_impl`：`urllib.request.urlopen()`（网络）、`Image.open()`（CPU 解码）、`write_bytes()`（文件 I/O）——全部在事件循环线程上同步执行。
- **影响**：一个大文件下载或图像解码阻塞所有其他会话的流。
- **修复**：将实现体包装在 `await asyncio.to_thread(...)` 中。

## 11. `StateRegisterDB` 同步 SQLite 在异步中间件路径中被调用

**文件**：`runtime/state_register.py:116-128` + 调用方

- 每个方法都打开**新的** `sqlite3.connect()` + 执行 + 关闭。被 `awrap_model_call`/`awrap_tool_call`/`aafter_agent`（全 async）每次 agent 回合调用**多次**。
- 未被 AUDIT_REPORT #4/#14/#15 覆盖（不同文件）。
- **修复**：改 `aiosqlite` 或在异步路径中用 `asyncio.to_thread` 包装。

## 12. `summarization.py` — `awrap_model_call` 异步路径中的同步 SQLite + 文件 I/O

**文件**：`agent/middlewares/summarization.py:412-415`

- `_apply_compression`（sync）→ `memory_store.load_from_disk()`（文件 I/O）+ `build_system_prompt()`（含 `state_register_db` 同步 SQLite）+ `state_register_db.set_state()`（同步 SQLite），被 `awrap_model_call`（async）调用。
- **修复**：同 #11。

## 13. `_drain_loop` — `claim_next` 无异常保护，DB 错误静默杀死 drain loop

**文件**：`server/service/turn_runner.py:211-222`

```python
while True:
    row = await queue.claim_next(session_id)    # DB 错误直接抛出
    if row is None: break
    await _execute_claimed_row(session_id, row)
```

- `claim_next` 抛异常 → task 异常被 asyncio 静默吞没 → drain loop 死亡 → **后续排队的消息永远不被处理**（用户输入静默丢失）。
- **修复**：在 while 循环内加 `try/except` 包裹 `claim_next`，记录错误后重试。

## 14. 后台 `asyncio.create_task()` 无异常处理（4 处）

**文件**：

- `agent/tools/subagent/registry/settle_wake.py:85-90` — `_retry` task 无 `add_done_callback`，`complete_batch` 异常导致父代理永远不被唤醒（死锁）
- `agent/tools/subagent/registry/lifecycle.py:330-343` — deferred cleanup task 异常导致清理永不执行
- `agent/tools/subagent/announce/core.py:160-173` — `_check` task 中 `count_active_descendant_runs` 在 try/except 之外
- `context_engine/curator/orchestrator.py:648-655` — `ensure_future(asyncio.to_thread(...))` 无异常处理

- **修复**：每个 task 加内部 `try/except` 或 `add_done_callback` 记录异常。

## 15. 子代理生命周期 hook 调用：静默吞没 + 不一致

**文件**：`agent/tools/subagent/spawn/core.py`

- 行 458-461：`fire_spawned_hook` → `logger.debug(...)` ✓
- 行 625-628：`fire_progress_hook` → `except Exception: pass` ✗
- 行 652-655, 664-667：`fire_ended_hook` → `except Exception: pass` ✗
- 行 827-830：`_remove_run` → `except Exception: pass` ✗
- 行 834-844：`fire_stop_hooks` → `except Exception: pass` ✗
- **修复**：统一为 `except Exception as e: logger.debug(...)` 模式。

## 16. `kill`/`resume` — `wake_yield` 失败静默吞没 → 父代理死锁

**文件**：

- `agent/tools/subagent/control/kill.py:128-131,182-184`
- `agent/tools/subagent/registry/lifecycle.py:386-389`

```python
try:
    await wake_yield_if_all_children_settled(...)
except Exception:
    pass                         # 唤醒失败 → 父代理永远阻塞在 yield 上
```

- **修复**：至少 `logger.warning("wake_yield failed for ...: {}", e)`。

## 17. WS `_cancel_session` — 宽泛 `except (..., Exception)` 吞没真实错误

**文件**：`server/trigger/ws/messages.py:150-153`

```python
await asyncio.wait_for(task, timeout=5.0)
except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
    pass                         # Exception 在元组中 → 捕获所有真实错误
```

- **修复**：分离捕获 `TimeoutError`/`CancelledError`（预期）与 `Exception`（记录日志）。

## 18. `WsTurnExecutor.execute` — 被取消时不取消 child 任务

**文件**：`server/service/turn_runner.py:298-314`

- `execute` 被取消时 `await child` 收到 `CancelledError`，re-raise 但 **child 从未被取消**。`finally` 从 `_active_tasks` 弹出槽位但不 cancel child，且调用 `on_turn_finished` 可能触发更多 queued turn——此时 child 仍在后台运行。
- **修复**：在 re-raise 路径前加 `child.cancel()`。

## 19. N+1 查询：`get_session_ids()` 会话列表

**文件**：`context_engine/store/core.py:469-513`

- 1 次 `GROUP BY` 拿所有 session_id + **N 次** `SELECT` 拿每个 session 的标题。100 个会话 = 101 次查询。
- **修复**：用窗口函数或 correlated subquery 合并为单条 SQL。

## 20. N+1 查询：`search_messages()` 上下文补充（最多 50 次）

**文件**：`context_engine/core.py:311-368`

- 对每个搜索匹配结果执行独立的 UNION ALL 查询拿前后文 = 最多 50 次独立查询，每次获取/释放 `_lock`。
- **修复**：将所有 match id 构建一条 `IN(...)` 查询。

## 21. N+1 + 连接风暴：`_refresh_all_cached_system_prompts()`

**文件**：`context_engine/curator/orchestrator.py:497-505` + `runtime/state_register.py:116-128`

- N 个会话 = 2N 次 `sqlite3.connect()` + `execute` + `close`（`StateRegisterDB.set_state` 每次新建连接）。
- **修复**：`StateRegisterDB` 复用连接或用已有 `update_states()` 批量写入。

## 22. 内存泄漏：`_terminal_locks`/`_cleanup_generations`/`_deferred_cleanup_timers` 永不清理

**文件**：`agent/tools/subagent/registry/lifecycle.py:40-42`

```python
_terminal_locks: dict[str, asyncio.Lock] = {}       # 只增不减
_cleanup_generations: dict[str, int] = {}
_deferred_cleanup_timers: dict[str, asyncio.Task] = {}
```

- 每个 subagent run 完成后，这三组 dict 中的条目**永远不删除**。长时间运行的服务器积累数千条目，每条还持有闭包引用阻止 GC。
- **修复**：在 `_finalize_cleanup()` 完成后 `.pop(run_id, None)`。

---

# 🟡 中（Medium）— 新增

## 23. 上传端点无大小限制（audio/video/image）

**文件**：`server/trigger/http/audio.py:60-64`、`video.py:60-61`、`image.py:59-60`

```python
file_path.write_bytes(data)   # 无 len(data) 检查
```

- **修复**：加 `len(data)` 上限检查（对比 `skills.py:upload` 有 `_MAX_SKILL_CONTENT_CHARS`）。

## 24. 知识图谱遍历参数无上限

**文件**：`server/trigger/http/knowledge_graph.py:146,150`

```python
max_depth = max(0, int(query.get("max_depth", 3)))    # 仅最小值，无上限
max_nodes = max(1, int(query.get("max_nodes", 1000))) # 仅最小值
```

## 25. 子代理生成安全限制可被 HTTP body 覆盖

**文件**：`server/trigger/http/subagent.py:232-237` + `agent/tools/subagent/delegate.py:271-274`

- `max_spawn_depth`/`max_children_per_agent` 从 HTTP body 取值，`int()` 后无上下界 → 绕过 `depth.py:validate_spawn_depth` 的全局配置上限。
- **修复**：钳制到配置范围 `[1, config.max_spawn_depth]`。

## 26. Channel Manager 消费者循环任务未跟踪，无法取消

**文件**：`channels/manager.py:148-149`

```python
self._event_loop.create_task(self._inbound_consume_loop())   # 引用未存储
self._event_loop.create_task(self._outbound_consume_loop())
```

- `stop_service()` 仅取消 `_dispatch_task`，两个消费者循环无法被显式取消。
- **修复**：存储 task 引用，在 `stop_service` 中 cancel。

## 27. STT 守护进程：分离式子进程无跟踪/清理

**文件**：`skills/builtin/core/speech_to_text/scripts/core.py:67-73`

- `Popen` 句柄仅用于日志 PID 后即丢弃。无 PID 文件、无停止机制。liveness 探测瞬时失败可能触发多次 spawn，各自占用模型内存。
- **修复**：将 PID 写入文件，spawn 前检查存活。

## 28. Work admission：drain-retry 任务 fire-and-forget

**文件**：`agent/tools/subagent/registry/work_admission.py:25`

```python
asyncio.create_task(_schedule_drain_retry(coro, label, delay=5.0))  # 未加入 _root_work_tasks
```

- 对比第 28-30 行的 `_run_and_cleanup` 路径正确跟踪。此 task 无 `add_done_callback`，异常静默吞没。

## 29. `interrupt_marker` — 异步函数中调用同步 SQLite

**文件**：`server/service/interrupt_marker.py:242`

```python
rows = store_core.get_messages_by_lastest_n_turns(session_id, last_n=2)  # SYNC SQLite in async
```

- **修复**：`await asyncio.to_thread(store_core.get_messages_by_lastest_n_turns, ...)`。

## 30. `steering_queue` — 异步方法中使用 `threading.Lock`

**文件**：`agent/tools/subagent/announce/steering_queue.py:119,173,220`

- `enqueue_steering`/`drain`（async）用 `with state.lock:`（`threading.Lock`），另一线程持锁时事件循环阻塞。
- **修复**：改 `asyncio.Lock`。

## 31. `run_async` — 从运行中的事件循环调用时 `future.result()` 阻塞

**文件**：`pub_func/run_async.py:77-114`

```python
if loop and loop.is_running():
    future = pool.submit(_run_in_worker)
    return future.result(timeout=timeout)   # 阻塞事件循环线程
```

- **修复**：确保所有 `run_async` 调用方不在事件循环线程上，或提供 `await` 版本。

## 32. 广泛的 `except Exception: pass` 模式（25+ 处，15+ 文件）

以下文件均含至少一处无日志的 `except Exception: pass`，隐藏潜在 bug：

| 文件                                                    | 处数 | 典型行号        |
| ------------------------------------------------------- | ---- | --------------- |
| `agent/stream_repetition_guard_wrapper.py`              | 4    | 139,151,389,394 |
| `agent/tools/skill_tools/skill_manage.py`               | 3    | 773,959,986     |
| `agent/tools/memory.py`                                 | 2    | 187,467         |
| `agent/tools/pub_base/skill_usage.py`                   | 2    | 89,352          |
| `context_engine/curator/orchestrator.py`                | 2    | 205,291         |
| `context_engine/curator/usage.py`                       | 2    | 177,259         |
| `models/reranker_model/core.py`                         | 3    | 30,531,540      |
| `server/trigger/http/channels.py`                       | 2    | 71,87           |
| `agent/tools/subagent/control/send.py`                  | 2    | 75,113          |
| `skills/builtin/core/clawhub/scripts/clawhub_runner.py` | 4    | 170,180,203,254 |

- **修复**：至少改为 `except Exception as e: logger.debug("...: {}", e)`，确保最小可见性。

## 33. `list_descendant_runs` — O(N*D) BFS 重复全量扫描

**文件**：`agent/tools/subagent/registry/queries.py:12-26`

- BFS 每展开一个节点遍历内存中**全部** run 记录。对 D 层 N 条记录 = O(N*D)。
- **修复**：先调用 `build_read_index()`（已存在）构建 requester→runs 索引。

## 34. `save_runs_to_sqlite` — 全删全插每 30 秒

**文件**：`agent/tools/subagent/registry/store_sqlite.py:189-199`

```python
await db.execute("DELETE FROM subagent_runs")  # 删除全部行
for run_id, run in runs.items():               # 逐条重新插入
    await db.execute("INSERT INTO subagent_runs ...")
```

- 1000 个 run = 每 30 秒 1 次 DELETE + 1000 次 INSERT + 1000 次 `model_dump_json()`。
- **修复**：改增量 upsert（已有 `upsert_run_to_sqlite`）。

## 35. `agent_created_report` — 单次 curator 运行中调用 3-4 次

**文件**：`context_engine/curator/orchestrator.py:130,164,181,249,266`

- 每次调用遍历 `skills/auto/` 目录树，对每个技能读 JSON + 读 SKILL.md + 检查 .pinned。
- **修复**：在 `run_curator_review()` 开始时调用一次并缓存。

## 36. `heartbeat` — 每次心跳重建整个 agent + LLM

**文件**：`server/service/heartbeat.py:71-76`

```python
main_llm = build_main_llm()      # 每次新建 LLM 客户端
agent = create_agent(model=main_llm, tools=tools)  # 每次重建状态图
```

- **修复**：缓存 agent 实例，仅在配置变化时重建。

## 37. swarm 计数器 — `all_runs()` 全量扫描在 pump_lane 循环中

**文件**：`agent/tools/subagent/swarm/collector.py:329-356,212-228`

- `_count_active_swarm_runs` = `sum(1 for r in all_runs() if ...)`，`_pump_lane` while 循环每次迭代调用。
- **修复**：维护 per-group 增量计数器。

## 38. 缺失索引：`messages` 表 `role` 列

**文件**：`context_engine/store/db.py:55-77`

- 所有按 role 过滤的查询（FTS5 搜索、LIKE 搜索、标题提取）在拿到 session_id 的行后逐行扫描 role。
- **修复**：`CREATE INDEX idx_messages_session_role ON messages(session_id, role)`。

## 39. `_launch_fingerprints` 永不清理（swarm collector）

**文件**：`agent/tools/subagent/swarm/collector.py:13`

- `reserve_swarm_run` 添加映射，run 完成后无任何地方删除。
- **修复**：在 `complete_swarm_run` 或 sweeper 中删除对应条目。

## 40. `_pending_args`/`_pending_raw` 在会话异常断开时泄漏

**文件**：`server/service/messages.py:26-29`

- WS 连接在 turn 中途断开时 `_clear_pending_args` 不被调用，session 的 pending args 和 raw buffer 永久驻留。无 TTL、无全局清理。
- **修复**：在 WS disconnect handler 中调用 `_clear_pending_args(session_id)`。

## 41. `add_messages` — 5N 次 `json.dumps` 在事件循环线程上

**文件**：`context_engine/store/core.py:33-251`

- `async def` 函数中，每条消息最多 5 次 `json.dumps`（content/tool_calls/images/audios/videos）。长对话 batch 阻塞事件循环。
- **修复**：移到 `asyncio.to_thread()` 或用 `orjson`。

## 42. `get_history_by_turn_page` — 5N 次 `json.loads` 在事件循环线程上

**文件**：`context_engine/store/core.py:296-309,364-378`

- 每行最多 5 次 `json.loads`，从 async 路径调用。
- **修复**：同 #41。

## 43. `StateRegisterDB` — 每次操作新建 SQLite 连接

**文件**：`runtime/state_register.py:116-224`

- `set_state`/`get_state`/`has_session` 每个方法都 `sqlite3.connect()` + `close()`。热路径上频繁新建连接。
- **修复**：复用模块级连接（加锁）或使用 `aiosqlite`。

---

# 🟢 低（Low）— 新增

## 44. 不安全的 `yaml.Loader` — STT 模型配置

**文件**：`models/STT_model/utils/infer_utils.py:360` — `yaml.load(f, Loader=yaml.Loader)`（非 `SafeLoader`）。

- **修复**：改为 `yaml.SafeLoader`。

## 45. cron `every_ms` 无下限

**文件**：`server/trigger/http/cron.py:102-103` — `every_ms=int(every_ms)` 无最小值，`1` → 紧密循环 DoS。

## 46. 分页 `turn_page_size` 无上限

**文件**：`server/trigger/http/messages.py:48` + `context_engine/store/core.py:318` — `Field(ge=1)` 仅有最小值。

## 47. Checkpointer 主连接无关闭方法

**文件**：`agent/checkpointer/async_sqlite_checkpointer.py:16-23` — `ThreadSafeAsyncSqliteSaver` 无 `close()`/`aclose()` 方法，连接永不关闭。

- 注：原 AUDIT_REPORT #10（条目已移除）已修复 `delete_thread_history` 的连接泄漏，但主 checkpointer 连接是独立问题。

## 48. `sender_task.cancel()` 未 `await`（2 处）

**文件**：`server/trigger/ws/subagent_ws.py:175`、`server/trigger/ws/logs.py:144`

- **修复**：`sender_task.cancel(); await asyncio.wait_for(sender_task, timeout=1.0)`。

## 49. Channel 线程事件循环未关闭

**文件**：`server/trigger/channels/core.py:330-342` — `event_loop.run_forever()` 退出后无 `event_loop.close()`。对比 cron 服务线程有 `loop.close()`。

- **修复**：在 `_run()` 的 `finally` 块中加 `event_loop.close()`。

## 50. `threading.Lock` 在异步调用链中使用（3 处）

**文件**：

- `agent/tools/subagent/registry/session_state.py:93,143,151,160` — `_HITL_LOCK`/`_INFLIGHT_LOCK`
- `agent/tools/subagent/registry/memory.py:6` — `_lock = threading.Lock()`
- `server/trigger/ws/subagent_ws.py:66,74,104,128` — `_subscribers_lock`/`_hooks_lock`
- 锁持有时间极短，实际阻塞风险低但技术上是 `threading.Lock` 在异步调用链中。

## 51. cron `base.py` — `asyncio.get_event_loop()` 应为 `get_running_loop()`

**文件**：`skills/builtin/core/cron/scripts/base.py:389,433` — 已弃用 API。

## 52. `delegate_task` — `asyncio.run()` 未检查运行中的事件循环

**文件**：`agent/tools/subagent/delegate.py:428` — 从异步上下文调用会抛 `RuntimeError`。

## 53. `_summarize` — 每次重试都重建 LLM 客户端

**文件**：`agent/tools/message_search.py:181-189` — `build_main_llm()` 在 retry 循环内。

- **修复**：在循环外调用一次，循环内复用。

## 54. 缺失索引：`pending_injections` 表 `status` 列

**文件**：`agent/tools/subagent/registry/pending_injections.py:48-53` — `list_pending()` 按 `status='pending'` 过滤但无索引。

- **修复**：`CREATE INDEX idx_pending_injections_status ON pending_injections(status) WHERE status='pending'`。

## 55. `get_session_ids` — GROUP BY 包含子代理会话（Python 层过滤）

**文件**：`context_engine/store/core.py:469-477,480-484` — SQL 聚合所有 session（含子代理），Python 层 `if not _is_top_level_session(session_id): continue` 过滤。

- **修复**：SQL 中预过滤 `WHERE session_id NOT LIKE '%:subagent:%'`。

---

# 更新后的超大模块表（>500 行）

| 文件                                        | 行数 | 备注 |
| ------------------------------------------- | ---- | ---- |
| `agent/tools/skill_tools/skill_manage.py`   | 845  |      |
| `server/service/messages.py`                | 611  |      |
| `agent/tools/subagent/spawn/core.py`        | 585  |      |
| `agent/tools/pub_base/skill_usage.py`       | 507  |      |
| `context_engine/curator/orchestrator.py`    | 504  |      |
| `agent/tools/memory.py`                     | 503  |      |
| `agent/middlewares/multimodal_processor.py` | ~480 | 新增 |

---

# 第二轮修复优先级建议

10. **CRITICAL 立即修**：#1 skills.py 路径穿越（加 `is_relative_to` 检查）；#2 delegate.py `time.sleep` → `async def result_async`；#3-#5 三处裸 `except`/静默吞没改为带日志记录。
11. **安全加固**：#6 session_id 校验、#7 SSRF 内网 IP 黑名单、#8 multipart 文件名清洗、#23 上传大小限制、#24-#25 参数钳制。覆盖 #6-#8、#23-#25。
12. **资源泄漏**：#9 WS 断连取消任务、#18 execute 取消 child、#22 `_terminal_locks` 清理、#26-#28 三个未跟踪 task。覆盖 #9、#18、#22、#26-#28。
13. **异步阻塞**：#10-#12 multimodal_processor/summarization 的阻塞 I/O 移到 `to_thread`；#11-#12 StateRegisterDB 迁移；#13 drain_loop 异常保护；#14-#17 后台 task 异常处理与 hook 一致性。覆盖 #10-#17。
14. **性能**：#19-#21 三个 N+1 查询、#22 内存泄漏、#33-#37 curator/swarm/persistence 冗余计算、#38 缺失索引。覆盖 #19-#21、#33-#38。
15. **错误处理**：#32 批量将 `except Exception: pass` 至少加 `logger.debug`。覆盖 #32 全部 25+ 处。
16. **低优先级清理**：#44-#55 逐项修复。
