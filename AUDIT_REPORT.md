# 审计报告 — Sherry Agent

**审计范围**：`C:\app\code\project\sherry_agent` 全项目（排除 `.venv`、`node_modules`、`client/`、`future/`、`temp/`、`logs/`、vendored LightRAG/模型权重数据）。
**审计方法**：4 个并行审计子代理（安全、代码质量/架构、正确性/数据、技能系统与沙箱）+ 对关键发现进行源码直接复核。
**审计日期**：2026-08-24
**更新日期**：2026-09-04 — CodeAct 模块已从代码库移除，相关条目（#1、#19、超大模块表）标记为失效并更新引用；#5 回合编号竞态、#7 定时器泄漏、#8 定时器任务名世代歧义、#9 计数器竞态、#10 checkpointer 连接泄漏、#11 无界队列、#26 import 时副作用、#28 providers 导入路径、#32 解释器路径跨平台已修复并标记。

---

# 🔴 严重（Critical）— 安全 / RCE

## 1. ~~`eval_sandbox` 用完整内置命名空间执行模型代码 — 无沙箱的远程代码执行~~（已失效）
> **状态更新（2026-09-03）**：`agent/codeact/` 模块（含 `utils.py` 的 `eval_sandbox`、`core.py` 的 Sandbox 图节点及 `__init__.py` 的 `_eval_fn` 默认接线）已从代码库整体移除，本项不再适用。模型代码执行能力现仅存于 `agent/tools/python_repl.py`，相关风险见 #13。

## 2. `resolve_path` 无 ROOT_DIR 越界防护 — 任意文件读/写
**文件**：`agent/tools/pub_base/path_utils.py:8-13`（已验证）
```python
def resolve_path(file_path):
    p = Path(os.path.expanduser(file_path))
    if not p.is_absolute():
        p = ROOT_DIR / p
    return p.resolve()   # 绝对路径原样放行
```
- 下游：`read_file.py:60`（读任意文件 → 窃取含 API Key 的 `.env`）；`patch_file.py:101,133`（`write_text` → 改任意文件）；`write_file.py:70`（LangChain `WriteFileTool` 只约束相对路径）。
- **影响**：LLM 触发的任意文件读取/覆写 → 密钥泄露 + 持久化植入。
- **修复**：将绝对路径钳制到 `resolve().is_relative_to(ROOT_DIR)`，或拒绝 ROOT_DIR 外路径。

## 3. `terminal.py` 黑名单可被轻易绕过；`shell=True`
**文件**：`agent/tools/terminal.py:12`（已验证）
- `BLACKLIST = {"rm -rf /", "mkfs", "shutdown", "reboot"}` 是裸子串检查。绕过方式：`rm -rf /tmp/../`、`reboot -f`、`shutdown -h now`、`curl | sh`，以及任何未列入表的破坏性命令。
- 第 98 行 `shell=True`、第 59 行 `create_subprocess_shell`。
- **修复**：shlex 解析、去掉 `shell=True`，改用允许列表或真实沙箱。

## 4. `clawhub` 运行任意远程 npm 代码
**文件**：`skills/builtin/core/clawhub/scripts/clawhub_runner.py:227` — `npx --yes clawhub@latest`
- `--yes` 自动安装并执行 npm 供应的任何内容 → 供应链 RCE。装后扫描器（`_scan_plugin_skills`）是**故障放行（fail-open）**缓解，非硬性闸门。
- **修复**：固定版本；要求显式用户确认；移除 `--yes`。

---

# 🟠 高（High）— 数据完整性 / 资源泄漏 / 并发

## 5. ~~非原子回合编号 → 静默数据损坏~~（已修复）
**文件**：`context_engine/store/core.py:39-40`
- `add_messages` 先 `get_max_turn_num()` 再 `+1`，在事务/锁之外。同一 session 的并发写入得到相同 `turn_num`，静默合并两个对话回合，破坏历史排序与分页。
- **已修复（2026-09-03）**：改用模块级 `threading.Lock`（`_turn_assign_lock`）串行化"重读 MAX → 重标行 → INSERT"临界区（取审计建议的"加锁"路线）。未采用单条 `INSERT ... SELECT` 事务方案：连接为共享单连接 + `isolation_level=None` 自提交模式，显式 `BEGIN` 会被并发读取方 `with _db:` 出口的 `commit()` 提前提交，且同连接第二个 `BEGIN` 直接报错——锁才是该连接设计下的正确串行化原语；顺带修正了"单事务批量插入"的误导注释（自提交模式实为逐行提交）。已核实 `messages` 表生产写入方仅 `add_messages` 一处，模块级锁完整覆盖进程内写入。回归测试：新建 `tests/unit/context_engine/test_store_turn_atomicity.py` 3 个用例（并发双写不合并 turn——patch `get_max_turn_num` 加 sleep 拉宽竞态窗口作确定性换牙；顺序递增 1/2/3；同批共享同一 turn）。对照验证：旧实现并发用例红。

## 6. 异步路径上的阻塞同步 SQLite
**文件**：`context_engine/store/core.py:11,143,186`；`context_engine/store/db.py`
- `async` 函数直接在事件循环线程上执行阻塞式 `executemany`/`commit`；模块级共享连接被 WS 循环、cron 线程、子代理线程共用（非线程安全）。
- **修复**：用 `aiosqlite` 或每线程连接 + 锁。

## 7. ~~回调执行器中的超时定时器泄漏~~（已修复）
**文件**：`runtime/_callback_executor.py`
- `loop.call_later(timeout, lambda: task.cancel() ...)` 在任务提前完成时**从不取消** → 每个完成的回调都留下一个存活至完整超时（默认 3600s）的定时器，累积 pending handle + 闭包。
- **已修复（2026-09-03）**：保存 `TimerHandle`，通过 `task.add_done_callback` 在任务结束（完成/取消/异常）时立即 `.cancel()`——对已触发句柄是安全空操作。回归测试：`tests/module/test_callback_executor.py` 新增 `run_coroutine`/`create_task` 完成后无 pending 定时器（对照验证：旧实现残留 1 个 3600s 句柄）+ 两条超时取消路径共 4 个用例。

## 8. ~~`unregister`/`clear_session` 上的定时器任务泄漏~~（已修复）
**文件**：`runtime/timer_call_register.py`
- `cancel_task` 按**名字**匹配任务且只取消第一个匹配项；定时器重置时可能取消错误（新）任务，旧定时器协程泄漏并持续触发。
- **已修复（2026-09-03）**：任务名改为每代唯一（`timer_{sid}_{name}_{uuid4[:8]}`）——按名取消从"一对多歧义"变单射，任何交错时序下每代恰好匹配一个任务，旧协程必然被取消（loop 繁忙 + `all_tasks` 集合无序遍历下尤其关键）。逻辑名重复注册拒绝（返回 False）保持不变，公共 API 语义未变。未采用"直接跟踪任务对象"：任务对象要等 loop 线程调度后才存在，且主线程 `task.cancel()` 非线程安全，仍需走同一条 `call_soon_threadsafe` 路径。回归测试：新建 `tests/module/test_timer_call_register.py` 11 个用例（注册/注销/reset 旧代取消/双重 reset 仅最新代存活/clear_session/注销后重注册；轮询等待避免时序赌博）。对照验证：旧实现 3 个用例红。

## 9. ~~计数器竞态 + 阈值溢出被丢弃~~（已修复）
**文件**：`runtime/count_call_register.py`
- `now_counter` 的读-改-写非原子；重置丢弃溢出（count=5、threshold=3 → 重置为 0，丢失 2）。
- **已修复（2026-09-03）**：`increase()` 的读-改-写与 `reset_count()` 的写回纳入 `threading.Lock`；触发时改用 `now_counter % threshold` 保留溢出；回调移至锁外触发以防重入死锁。回归测试：`tests/module/test_count_call_register.py` 新增并发无丢失计数（8 线程 × 250 增量精确触发）、溢出保留、锁外回调三个用例。

## 10. ~~checkpointer 连接泄漏~~（已修复）
**文件**：`agent/checkpointer/async_sqlite_checkpointer.py`
- `delete_thread_history` 中 `aiosqlite.connect` 从不关闭 → 每次 `clear_session` 泄漏一个连接 + 文件句柄。
- **已修复（2026-09-03）**：改用 `async with aiosqlite.connect(...) as conn:`——成功与异常路径均保证关闭（未提交工作随关闭回滚）。回归测试：`tests/module/test_async_sqlite_checkpointer.py` 新增成功路径关闭、异常路径关闭、真实临时库按 thread_id 精确删除（不误伤其他会话）共 3 个用例。

## 11. ~~无界队列，无背压~~（已修复）
**文件**：`bus/core.py`
- `asyncio.Queue()` 无 `maxsize` → agent 慢时内存耗尽。
- **已修复（2026-09-03）**：双队列有界化（`config.num.BUS_QUEUE_MAXSIZE`，默认 1000，构造器可覆盖，`maxsize<=0` 拒绝）；队列满时生产者 `await put()` 阻塞等待（真背压：消息延迟不丢弃——入站承载用户消息与 cron 投递，不可静默丢失），`QueueFull` 在该路径下不会出现。回归测试：`tests/module/test_bus_core.py` 新增有界默认值、自定义上限、非法参数、入/出站满载阻塞并恢复共 5 个用例。

## 12. `resolve_path` 下游：任意文件读/写（见严重 #2 的利用链）
- `read_file.py:60`、`patch_file.py:101,133` 经 `resolve_path()` 放行绝对路径；`write_file.py:70` 受 `root_dir` 约束但 LangChain 仅约束相对路径，绝对路径仍可越界。**LLM 触发**。

## 13. `python_repl.py` 暴露完整内置命名空间
**文件**：`agent/tools/python_repl.py:45` — `exec({command_repr}, {"__builtins__": __builtins__}, {})`
- 包装注释声称"受限内置白名单"，但 `{"__builtins__": __builtins__}` 是**完整** builtins 模块（`__import__`、`open`、`eval`、`exec` 全可用）。子进程隔离（第 64-79 行）与超时 kill 正确，但能力不受限。**LLM 触发。**

## 14. 缺认证 + 通配 CORS — 所有端点未鉴权
**文件**：`server/trigger/core.py:14` — `ALLOW_CORS(app, origins=["*"])`
- `server/__main__.py` 与 `trigger/core.py` 无任何认证中间件。所有 HTTP/WS 端点未鉴权、跨域可达。
- **利用**：任意网站可向 agent API 发请求。衍生以下未鉴权高风险端点（#15-#18）。

## 15. 未鉴权 `.env` 读写 — 密钥暴露
**文件**：`server/trigger/http/env.py:6-32`
- `GET /env` 返回含 API Key 的完整配置；`PUT /env` 可改写运行时凭据。
- **利用**：`GET /env` 窃取 `MAIN_LLM_API_KEY`。

## 16. 未鉴权 session 清除 + 路径穿越删除
**文件**：`server/trigger/http/messages.py:16-23` + `server/DAO/messages.py:8-9,32-34`
- `DELETE /sessions` 携带 `session_id`（如 `../../src`）经 `_session_folder`/`shutil.rmtree` 穿越删除任意目录。无鉴权、无清洗。
- **利用**：`DELETE /sessions` body `{"session_id":"../../workspace"}`。

## 17. 未鉴权 WebSocket agent 控制
**文件**：`server/trigger/ws/messages.py:116-183` — `app.websocket("/sessions/agent/ws")`
- 客户端可发 `multi_modal_message` 触发 `async_generate`、`hitl_response` 批准/拒绝工具调用、`stop` 取消。
- **利用**：攻击者驱动 agent 并批准危险工具调用。

## 18. 未鉴权日志流 — 密钥泄漏
**文件**：`server/trigger/ws/logs.py:119-148` — `app.websocket("/logs/ws")`
- 把每条 loguru 记录（可能含 API Key、请求头、提示词）流式推给任意未鉴权 WS 客户端。
- **利用**：连接 `/logs/ws` 从日志帧读取密钥。

## 19. ~~CodeAct 无用户确认自动执行~~（已失效）
> **状态更新（2026-09-03）**：CodeAct 模块（`agent/codeact/`）已从代码库移除，本项不再适用。模型代码执行相关风险现以 #13（`agent/tools/python_repl.py`）为准。

## 20. 技能系统沙箱/供应链
- **① 技能描述（不可信 SKILL.md frontmatter）注入 LLM 上下文**：`skills/loader.py:92-124` `get_skills_text` 把每个技能的 `description`（解析自 SKILL.md，第 65-67 行）直接拼进 `<available_skills>` XML；`skill_view.py:202-210,391` 只记录注入模式，仍把完整不可信内容返回 LLM。**存在恶意/被植入技能时触发。**
- **② 技能扫描器故障放行**：`server/service/skill_scanner.py:486-514` `build_reject_message` 在扫描器 `UNAVAILABLE` 时返回 `None`（放行）；`skills_snapshot.py:11-54`、`clawhub_runner.py:150-156` 均为 fail-open。**扫描器缺失/报错时触发。**
- **③ `tool_guardrails.py` 仅做循环检测，非权限闸门**：检测失败重复、同工具失败累积、无进展（第 119-172 行），**不**审批/确认首次工具调用。因此**所有工具（含上述危险项）只要 LLM 发出调用即自动执行**——这是架构性关键缺口：防护针对死循环，而非恶意/错误工具使用。

---

# 🟡 中（Medium）— 代码质量 / 架构 / 正确性

## 22. `skills/skills_snapshot.py:73` 双重 `os.path.join` 于已绝对路径
```python
file_path: str = os.path.join(SKILLS_DIR, 'skills_snapshot.json')   # 第 70 行
if os.path.exists(file_path):                                        # 第 72 行
    with open(os.path.join(SKILLS_DIR, file_path), ...)              # 第 73 行
```
- `file_path` 已是 `SKILLS_DIR / 'skills_snapshot.json'`（绝对）。`os.path.join(SKILLS_DIR, file_path)` 第二个参数为绝对路径时丢弃 `SKILLS_DIR`，二者路径下恰巧能工作，但代码冗余而脆弱——若 `SKILLS_DIR` 变为相对路径即破坏。**修复：** `with open(file_path, ...)`。

## 23. 共享可变状态 / 线程安全（`runtime/` 注册表簇）
- **`runtime/core.py:5-24`** — 类级 `_instances = {}` 在 `__new__` 中无锁修改；`clear_all_register_sessions` 无锁遍历 `cls.__subclasses__()`；`__new__` 单例非线程安全。
- **`runtime/state_register.py:14,17-91`** — `_states` 无锁修改；`get_all_states`（第 40 行）返回**活对象**，调用方可直接改动共享态。
- **`runtime/relation_register.py`** — 三个 websocket 字典无锁且非原子更新，可能部分覆写。
- **`runtime/count_call_register.py`**、**`runtime/timer_call_register.py`** — 共享字典无锁。
- **`runtime/_callback_executor.py:22,39-40`** — `self._loop` 跨线程访问未同步；`_ensure_running` 并发调用可能多线程重复 spawn。

## 24. `context_engine/core.py:11-12` 异步上下文中的阻塞 `threading.Lock` + 共享连接
- `search_messages` 内 `threading.Lock` 会阻塞事件循环线程；`sqlite3` 为阻塞 I/O。**修复：** 用 `asyncio.Lock` 或 executor 执行 DB 工作。

## 25. `context_engine/store/db.py:20-40` 非线程安全 `get_db()` 单例 + 短超时
- `if _db:` 双重检查无锁 → 两线程可能各建一个连接，第二个覆盖第一个（泄漏一个）。`timeout=1.0`（第 30 行）过短，并发时抛 `sqlite3.OperationalError: database is locked` 且无重试。**修复：** 加锁、加超时或加重试循环。

## 26. ~~`agent/core.py:29-36` 模块级 import 时副作用~~（已修复）
- 顶层 `build_skills_snapshot()`、`memory_store.load_from_disk()`、`build_main_tools()` 在导入时即触发磁盘写/读与工具构造——对测试/任何 import 都意外且缓慢。**修复：** 移入显式 `init()`，由服务入口调用。
- **已修复（2026-09-04）**：`agent/core.py` 三个顶层副作用移入显式 `init()`（`_initialized` 标志保证幂等，重复调用为空操作）；工具列表改为经 `get_agent_tools()` 调用时求值（`built_agent` 与 `agent/middlewares/context_engine/nudge.py` 均在运行期获取），`agent/__init__.py` 以 `__getattr__` 惰性转发重属性——裸 `import agent.core` 不再触发任何磁盘 I/O 或工具构造。实际修复范围比原条目更广：curator 后台线程（`context_engine.curator`）、cron 服务后台线程（`skills/builtin/core/cron/scripts`）、路由注册（`server.trigger`）的 import 副作用一并移出，统一由 `server/__main__.py` 在 `__main__` 守卫内按序显式调用四个 `init()`（agent core → curator → cron → trigger），先于任何请求与懒加载消费者。`tests/run_tests_split.py` 的双进程隔离注释中对 import 副作用的引用仍指历史问题，保留作防御性说明。

## 27. `channels/manager.py:154` `run_forever()` 阻塞调用线程
- `start_service()` 调用 `self._event_loop.run_forever()` 永久阻塞；模块级 `channel_manager = ChannelManager()`（第 236 行）带导入时副作用。

## 28. ~~`config/schema.py:169,244` 导入不存在的 `providers` 包~~（已修复）
```python
from providers.registry import PROVIDERS        # 第 169 行 (_match_provider)
from providers.registry import find_by_name     # 第 244 行 (get_api_base)
```
- 磁盘上不存在 `providers/` 目录。函数级 import → 仅在运行时调用 `get_provider`/`get_api_key` 等时抛 `ModuleNotFoundError`。这是配置层潜在崩溃。**修复：** 恢复 `providers/` 包，或改用本文件 `ProvidersConfig`（第 65-89 行）推导元数据，而非缺失注册表。
- **已修复（2026-09-03）**：审计时缺失的 `providers/` 包现已在磁盘上（含 `registry.py` 声明式注册表，22 个 provider）；按用户决定将其整体迁移至 `models/providers/`（`git mv` 保留历史），`config/schema.py` 两处函数级 import 改为 `from models.providers.registry import ...`。回归：`tests/unit/test_config_schema.py` 28 passed、module+system 319 passed、`models.providers` 导入冒烟通过；全仓无残留 `providers.registry` 旧路径引用。

## 29. `bus/core.py:16-18` 单一全局队列，无按渠道路由
- `MessageBus` 一个入站 + 一个出站 `asyncio.Queue`，所有渠道共享，路由在下游按 `msg.channel` 判断。高并发多渠道时是瓶颈且无法按渠道背压（当前规模可接受，中级优先）。

## 31. `skills/loader.py` 循环导入变通 + 字符串路径检查
- `scan_skills` 在函数内部做 `from .skills_snapshot import read_skills_snapshot`（循环导入变通）；`_is_third_party` 用字符串 `"./skills/plugins/"` 判断而非 `Path` 解析。

## 32. ~~`config/path.py:15` 写死 Windows 解释器路径~~（已修复）
**文件**：`config/path.py`
- `INTERPRETER_PATH = ROOT_DIR / ".venv/Scripts/python"` 仅适用于 Windows。POSIX 应为 `.venv/bin/python`。**修复：** 用 `sys.executable` 或按平台条件路径。
- **已修复（2026-09-03）**：`INTERPRETER_PATH = Path(sys.executable)`——直接取当前运行解释器，任何平台（Windows `Scripts\`、POSIX `bin/`、conda、系统 Python）均无需假设 venv 布局，且与仓库既有主流模式一致（pip 安装、python_repl、MCP、测试 runner 共 5+ 处已用 `sys.executable`）。启动方式 `./start.sh` / `uv run python -m server` 下运行解释器即项目 venv 解释器，二者语义等价；子进程辅助程序（如 STT 守护进程）本就应与运行环境共享依赖，故运行解释器是所有场景的正确目标。连带修复：① STT 脚本的死 fallback 删除——旧代码 `.exists()` 探测的两个候选路径在磁盘上都不存在（Windows venv 只有 `python.exe`、POSIX 是 `bin/` 布局），恒走 else 且 else 也不存在；② `start.sh` 原本在 POSIX 上必然失败（POSIX 脚本写死 `Scripts/activate`）——改为 bin/ 与 Scripts/ 自动探测，并新增 `.gitattributes`（`*.sh text eol=lf`）防止 Windows `autocrlf` 检出成 CRLF 后 Git Bash/WSL 无法执行；③ 四份 README 启动注释同步。回归测试：`tests/unit/test_config_path.py::test_interpreter_path` 改为断言 `== Path(sys.executable)` 且 `.exists()`（平台中立；旧常量因缺 `.exe` 且不存在于磁盘，Windows 上旧代码也红）。对照验证：旧实现该用例红。

## 33. 未鉴权日志文件读取
**文件**：`server/trigger/http/logs.py:170-198` — `GET /logs?path=...`。路径经 `LOG_DIR` 校验（受控），但端点未鉴权，日志可能含密钥。**利用：** `GET /logs?path=<info 日志>`。

## 34. FTS5 MATCH 注入（受限，非经典 SQLi）
**文件**：`context_engine/core.py:269,280-296`
- 查询作为绑定参数传入（对经典 SQLi 安全），`_sanitize_fts5_query`（第 62-112 行）为尽力过滤。恶意查询可触发昂贵/滥用型 FTS5 操作 → DoS。（非经典 SQL 注入。）

## 35. 时间戳碰撞
**文件**：`context_engine/store/core.py:43` — `datetime.now().strftime("%Y%m%d%H%M%S")` 1 秒分辨率。同一秒内多回合共享时间戳，破坏 `get_session_ids`（第 390 行 `MAX(timestamp)`）排序。

## 36. cron"every"间隔漂移
**文件**：`skills/builtin/core/cron/scripts/base.py:57-81` — interval 任务从 `now` 而非上次运行时间推算下次运行。作业耗时过长时定时漂移。

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

# 修复优先级建议
1. **先修 CRITICAL**：#2/#12 `resolve_path`（加 ROOT_DIR 越界钳制）、#3 `terminal.py`（去 `shell=True` + 允许列表）、#4 `clawhub`（固定版本 + 去 `--yes`）。（原 #1 `eval_sandbox` 随 CodeAct 模块移除，已失效。）
2. **加认证**：`server/trigger/core.py` 加 token/API-key 中间件并作用于所有路由与 WebSocket，去掉通配 CORS。覆盖 #14-#18、#33。
3. **加固路径处理**：`resolve_path` 拒绝/钳制 ROOT_DIR 外路径；`server/DAO/messages.py` 在 `shutil.rmtree` 前清洗 `session_id`。
4. **数据层**：`context_engine/store/core.py` 回合编号改单事务（#5，✅ 已修复——最终采用加锁方案，见该条目说明）；`_callback_executor.py` 取消 `call_later` handle（#7，✅ 已修复）；`async_sqlite_checkpointer.py` 用 `async with`（#10，✅ 已修复）；`bus/core.py` 限队列（#11，✅ 已修复）。
5. **注册表并发**：为 `runtime/*_register.py` 与 `context_engine` 共享连接加锁并返回副本（#23/#24/#25）。
6. **修复 `providers.registry` 悬空导入**（#28）。
7. ~~**移 `agent/core.py` import 副作用**入 `init()`（#26）~~。（✅ 已修复——连同 curator/cron/trigger 一并移出 import 时，见该条目说明。）
8. **重审 fail-open 扫描策略**（#20-②），并考虑为高风险工具增加确认闸门（#20-③）。
9. 修复后重新审计，确认以上各域闭合。
