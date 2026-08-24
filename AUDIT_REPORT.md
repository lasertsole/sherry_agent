# 审计报告 — Sherry Agent

**审计范围**：`C:\app\code\project\sherry_agent` 全项目（排除 `.venv`、`node_modules`、`client/`、`future/`、`temp/`、`logs/`、vendored LightRAG/模型权重数据）。
**审计方法**：4 个并行审计子代理（安全、代码质量/架构、正确性/数据、技能系统与沙箱）+ 对关键发现进行源码直接复核。
**审计日期**：2026-08-24

---

# 🔴 严重（Critical）— 安全 / RCE

## 1. `eval_sandbox` 用完整内置命名空间执行模型代码 — 无沙箱的远程代码执行
**文件**：`agent/codeact/utils.py:67,74`（已验证）
```python
result = eval(code.strip(), builtins.__dict__, _locals)    # 第 67 行
exec(code, builtins.__dict__, _locals)                     # 第 74 行
```
- `builtins.__dict__` 暴露了 `__import__`、`open`、`eval`、`exec`，并可传递访问 `os`、`subprocess`（`open/__import__` 逃逸链可达）。函数自身 docstring（第 40-42 行）承认这**仅用于测试**，非真实沙箱。
- **默认接线**：`agent/codeact/__init__.py:32` 设置 `_eval_fn = eval_fn or eval_sandbox`；`agent/codeact/core.py:557-586` 的 Sandbox 图节点直接对**模型生成的代码**调用 `eval_fn(script, context)`，全程**无用户确认**。
- **影响**：任何诱导 LLM 生成 Python 代码的提示 → 宿主机任意代码执行。
- **修复**：改用 `langchain-sandbox`、`RestrictedPython`，或容器化子进程 + 真正的内置白名单。**最高优先级。**

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

## 5. 非原子回合编号 → 静默数据损坏
**文件**：`context_engine/store/core.py:39-40`
- `add_messages` 先 `get_max_turn_num()` 再 `+1`，在事务/锁之外。同一 session 的并发写入得到相同 `turn_num`，静默合并两个对话回合，破坏历史排序与分页。
- **修复**：单条 `INSERT ... SELECT COALESCE(MAX(turn_num),0)+1` 在同一事务内完成，或按 session 加锁。

## 6. 异步路径上的阻塞同步 SQLite
**文件**：`context_engine/store/core.py:11,143,186`；`context_engine/store/db.py`
- `async` 函数直接在事件循环线程上执行阻塞式 `executemany`/`commit`；模块级共享连接被 WS 循环、cron 线程、子代理线程共用（非线程安全）。
- **修复**：用 `aiosqlite` 或每线程连接 + 锁。

## 7. 回调执行器中的超时定时器泄漏
**文件**：`runtime/_callback_executor.py:63,88`
- `loop.call_later(timeout, lambda: task.cancel() ...)` 在任务提前完成时**从不取消** → 每个完成的回调都留下一个存活至完整超时（默认 3600s）的定时器，累积 pending handle + 闭包。
- **修复**：保存 `TimerHandle`，在任务 `finally` 中 `.cancel()`。

## 8. `unregister`/`clear_session` 上的定时器任务泄漏
**文件**：`runtime/timer_call_register.py:99-100,150-151,174-175`
- `cancel_task` 按**名字**匹配任务且只取消第一个匹配项；定时器重置时可能取消错误（新）任务，旧定时器协程泄漏并持续触发。
- **修复**：直接跟踪任务对象。

## 9. 计数器竞态 + 阈值溢出被丢弃
**文件**：`runtime/count_call_register.py:87-105`
- `now_counter` 的读-改-写非原子；重置丢弃溢出（count=5、threshold=3 → 重置为 0，丢失 2）。
- **修复**：RMW 加锁；重置为 `now_counter % threshold`。

## 10. checkpointer 连接泄漏
**文件**：`agent/checkpointer/async_sqlite_checkpointer.py:33-36`
- `delete_thread_history` 中 `aiosqlite.connect` 从不关闭 → 每次 `clear_session` 泄漏一个连接 + 文件句柄。
- **修复**：`async with aiosqlite.connect(...) as conn:`。

## 11. 无界队列，无背压
**文件**：`bus/core.py:17-18`
- `asyncio.Queue()` 无 `maxsize` → agent 慢时内存耗尽。
- **修复**：限定队列大小，处理 `QueueFull`。

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

## 19. CodeAct 无用户确认自动执行
**文件**：`agent/codeact/core.py:557-586` + `agent/codeact/__init__.py:32`
- `sandbox` 节点直接 `eval_fn(script, context)` 执行模型生成的 `script`，`eval_sandbox` 为默认实现，**无审批闸门**。**远程（LLM 触发）。**

## 20. 技能系统沙箱/供应链
- **① 技能描述（不可信 SKILL.md frontmatter）注入 LLM 上下文**：`skills/loader.py:92-124` `get_skills_text` 把每个技能的 `description`（解析自 SKILL.md，第 65-67 行）直接拼进 `<available_skills>` XML；`skill_view.py:202-210,391` 只记录注入模式，仍把完整不可信内容返回 LLM。**存在恶意/被植入技能时触发。**
- **② 技能扫描器故障放行**：`server/service/skill_scanner.py:486-514` `build_reject_message` 在扫描器 `UNAVAILABLE` 时返回 `None`（放行）；`skills_snapshot.py:11-54`、`clawhub_runner.py:150-156` 均为 fail-open。**扫描器缺失/报错时触发。**
- **③ `tool_guardrails.py` 仅做循环检测，非权限闸门**：检测失败重复、同工具失败累积、无进展（第 119-172 行），**不**审批/确认首次工具调用。因此**所有工具（含上述危险项）只要 LLM 发出调用即自动执行**——这是架构性关键缺口：防护针对死循环，而非恶意/错误工具使用。

---

# 🟡 中（Medium）— 代码质量 / 架构 / 正确性

## 21. 缺失 `config/character.py`
- README 项目结构列出 `config/character.py`（"Character profile configuration"），但磁盘上不存在。任何 `import config.character` 都会失败——死引用或缺失文件。

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

## 26. `agent/core.py:29-36` 模块级 import 时副作用
- 顶层 `build_skills_snapshot()`、`memory_store.load_from_disk()`、`build_main_tools()` 在导入时即触发磁盘写/读与工具构造——对测试/任何 import 都意外且缓慢。**修复：** 移入显式 `init()`，由服务入口调用。

## 27. `channels/manager.py:154` `run_forever()` 阻塞调用线程
- `start_service()` 调用 `self._event_loop.run_forever()` 永久阻塞；模块级 `channel_manager = ChannelManager()`（第 236 行）带导入时副作用。

## 28. `config/schema.py:169,244` 导入不存在的 `providers` 包
```python
from providers.registry import PROVIDERS        # 第 169 行 (_match_provider)
from providers.registry import find_by_name     # 第 244 行 (get_api_base)
```
- 磁盘上不存在 `providers/` 目录。函数级 import → 仅在运行时调用 `get_provider`/`get_api_key` 等时抛 `ModuleNotFoundError`。这是配置层潜在崩溃。**修复：** 恢复 `providers/` 包，或改用本文件 `ProvidersConfig`（第 65-89 行）推导元数据，而非缺失注册表。

## 29. `bus/core.py:16-18` 单一全局队列，无按渠道路由
- `MessageBus` 一个入站 + 一个出站 `asyncio.Queue`，所有渠道共享，路由在下游按 `msg.channel` 判断。高并发多渠道时是瓶颈且无法按渠道背压（当前规模可接受，中级优先）。

## 30. 重复/死代码（`pub_func/`）
- **`pub_func/string_to_unique_int.py`** 与 **`pub_func/rand_str_to_int.py`** — 两工具都做 string→int 哈希（SHA-256 前 8 字节 vs MD5 前 N hex），应合并。
- **`pub_func/extract_text_from_content.py`** — 已定义但未在 `pub_func/__init__.py` 导出（死代码）。
- **`pub_func/build_agent_config.py:15-16`** — `except ValueError` 永远不会触发（try 内无 `ValueError` 源），错误信息 `"session_id must be an integer"` 也错（实为字符串）。死 try/except。

## 31. `skills/loader.py` 循环导入变通 + 字符串路径检查
- `scan_skills` 在函数内部做 `from .skills_snapshot import read_skills_snapshot`（循环导入变通）；`_is_third_party` 用字符串 `"./skills/plugins/"` 判断而非 `Path` 解析。

## 32. `config/path.py:15` 写死 Windows 解释器路径
- `INTERPRETER_PATH = ROOT_DIR / ".venv/Scripts/python"` 仅适用于 Windows。POSIX 应为 `.venv/bin/python`。**修复：** 用 `sys.executable` 或按平台条件路径。

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
| `agent/codeact/core.py` | 712 |
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
1. **先修 CRITICAL**：#1 `eval_sandbox`（换真沙箱）、#2/#12 `resolve_path`（加 ROOT_DIR 越界钳制）、#3 `terminal.py`（去 `shell=True` + 允许列表）、#4 `clawhub`（固定版本 + 去 `--yes`）。
2. **加认证**：`server/trigger/core.py` 加 token/API-key 中间件并作用于所有路由与 WebSocket，去掉通配 CORS。覆盖 #14-#18、#33。
3. **加固路径处理**：`resolve_path` 拒绝/钳制 ROOT_DIR 外路径；`server/DAO/messages.py` 在 `shutil.rmtree` 前清洗 `session_id`。
4. **数据层**：`context_engine/store/core.py` 回合编号改单事务（#5）；`_callback_executor.py` 取消 `call_later` handle（#7）；`async_sqlite_checkpointer.py` 用 `async with`（#10）；`bus/core.py` 限队列（#11）。
5. **注册表并发**：为 `runtime/*_register.py` 与 `context_engine` 共享连接加锁并返回副本（#23/#24/#25）。
6. **修复 `providers.registry` 悬空导入**（#28）与 `config/character.py` 缺失（#21）。
7. **移 `agent/core.py` import 副作用**入 `init()`（#26）；**合并/删除 `pub_func` 重复与死代码**（#30）。
8. **重审 fail-open 扫描策略**（#20-②），并考虑为高风险工具增加确认闸门（#20-③）。
9. 修复后重新审计，确认以上各域闭合。
