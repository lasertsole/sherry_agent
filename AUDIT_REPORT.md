# 审计报告 — Sherry Agent

**审计范围**：`C:\app\code\project\sherry_agent` 全项目（排除 `.venv`、`node_modules`、`client/`、`future/`、`temp/`、`logs/`、vendored LightRAG/模型权重数据）。
**审计方法**：4 个并行审计子代理（安全、代码质量/架构、正确性/数据、技能系统与沙箱）+ 对关键发现进行源码直接复核。
**审计日期**：2026-08-24
**更新日期**：2026-09-04 — 已修复/已失效的条目（原 #1、#5、#7、#8、#9、#10、#11、#13、#19、#26、#28、#32）已从本报告移除，其余条目重编码为连续编号 #1-#22，正文交叉引用与“修复优先级建议”已同步更新；2026-09-03/04 沙箱加固层落地（#2 部分缓解、#11-③ 新增针对性审批门），详见“沙箱加固层”一节。

---

# 🔴 严重（Critical）— 安全 / RCE

## 1. `resolve_path` 无 ROOT_DIR 越界防护 — 任意文件读/写
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

## 2. `terminal.py` 黑名单可被轻易绕过；`shell=True`
> **状态更新（2026-09-04）：部分修复**：旧元素级 `BLACKLIST`（`"rm -rf /"`、`"mkfs"`、`"shutdown"`、`"reboot"` 裸子串）已被 `DANGEROUS_COMMAND_REGEX`（`agent/tools/terminal.py:65-77`）取代——6 个可选模式、`re.IGNORECASE`、对 `" && "` 连接后的整条命令串匹配，封死审计时点名的 `["echo ok", "rm -rf /"]` 元素级绕过；命中即抛 `ToolException("Blocked: unsafe command.")`（174-178 行，`handle_tool_error=True` 走错误 ToolMessage 通道）。新增无条件防线：`env=scrub_env()` 每个生成点执行（300/332 行，密钥类变量名不再进入子进程环境）、`cwd=str(ROOT_DIR)` 钳制；Linux bubblewrap / macOS Seatbelt OS 沙箱（`backend.wrap` 列表 exec，无 shell kwarg，208/341-347 行）；`sandbox=False` 主会话 HITL 人工审批门 + 子代理/后台 scope 硬拒（151-172 行）。

**文件**：`agent/tools/terminal.py:12`（已验证）
- `BLACKLIST = {"rm -rf /", "mkfs", "shutdown", "reboot"}` 是裸子串检查。绕过方式：`rm -rf /tmp/../`、`reboot -f`、`shutdown -h now`、`curl | sh`，以及任何未列入表的破坏性命令。
- 第 98 行 `shell=True`、第 59 行 `create_subprocess_shell`。
- **修复**：shlex 解析、去掉 `shell=True`，改用允许列表或真实沙箱。
- **剩余风险**：Windows/无后端回退路径按设计保留 `shell=True`（228-235/278/349 行，与加固前字节一致，仅加 `env=`）；正则门仍是黑名单而非允许列表；`SANDBOX_POLICY=auto`（默认）+ 无后端时降级为无沙箱执行（仅一条 loguru 警告）。原建议中的 shlex 解析/去 `shell=True`/允许列表未采纳——采纳的是“真实沙箱”路线，且 OS 沙箱构造逻辑仅单测验证、未在真实 Linux/macOS 上实测。

## 3. `clawhub` 运行任意远程 npm 代码
**文件**：`skills/builtin/core/clawhub/scripts/clawhub_runner.py:227` — `npx --yes clawhub@latest`
- `--yes` 自动安装并执行 npm 供应的任何内容 → 供应链 RCE。装后扫描器（`_scan_plugin_skills`）是**故障放行（fail-open）**缓解，非硬性闸门。
- **修复**：固定版本；要求显式用户确认；移除 `--yes`。

---

# 🟠 高（High）— 数据完整性 / 资源泄漏 / 并发

## 4. 异步路径上的阻塞同步 SQLite
**文件**：`context_engine/store/core.py:11,143,186`；`context_engine/store/db.py`
- `async` 函数直接在事件循环线程上执行阻塞式 `executemany`/`commit`；模块级共享连接被 WS 循环、cron 线程、子代理线程共用（非线程安全）。
- **修复**：用 `aiosqlite` 或每线程连接 + 锁。

## 5. `resolve_path` 下游：任意文件读/写（见严重 #1 的利用链）
- `read_file.py:60`、`patch_file.py:101,133` 经 `resolve_path()` 放行绝对路径；`write_file.py:70` 受 `root_dir` 约束但 LangChain 仅约束相对路径，绝对路径仍可越界。**LLM 触发**。

## 6. 缺认证 + 通配 CORS — 所有端点未鉴权
**文件**：`server/trigger/core.py:14` — `ALLOW_CORS(app, origins=["*"])`
- `server/__main__.py` 与 `trigger/core.py` 无任何认证中间件。所有 HTTP/WS 端点未鉴权、跨域可达。
- **利用**：任意网站可向 agent API 发请求。衍生以下未鉴权高风险端点（#7-#10）。

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

---

# 🟡 中（Medium）— 代码质量 / 架构 / 正确性

## 12. `skills/skills_snapshot.py:73` 双重 `os.path.join` 于已绝对路径
```python
file_path: str = os.path.join(SKILLS_DIR, 'skills_snapshot.json')   # 第 70 行
if os.path.exists(file_path):                                        # 第 72 行
    with open(os.path.join(SKILLS_DIR, file_path), ...)              # 第 73 行
```
- `file_path` 已是 `SKILLS_DIR / 'skills_snapshot.json'`（绝对）。`os.path.join(SKILLS_DIR, file_path)` 第二个参数为绝对路径时丢弃 `SKILLS_DIR`，二者路径下恰巧能工作，但代码冗余而脆弱——若 `SKILLS_DIR` 变为相对路径即破坏。**修复：** `with open(file_path, ...)`。

> **状态更新（2026-09-05）：已完成**（随 #18 顺带解决）：`read_skills_snapshot` 已按 #18 的依赖单向化方案迁至 `skills/loader.py`，迁移时函数体改为 `file_path = SKILLS_DIR / "skills_snapshot.json"` + `file_path.exists()`，双重 join 消除，行为等价。`tests/unit/skills/test_loader.py::TestReadSkillsSnapshot` 覆盖。

## 13. 共享可变状态 / 线程安全（`runtime/` 注册表簇）
- **`runtime/core.py:5-24`** — 类级 `_instances = {}` 在 `__new__` 中无锁修改；`clear_all_register_sessions` 无锁遍历 `cls.__subclasses__()`；`__new__` 单例非线程安全。
- **`runtime/state_register.py:14,17-91`** — `_states` 无锁修改；`get_all_states`（第 40 行）返回**活对象**，调用方可直接改动共享态。
- **`runtime/relation_register.py`** — 三个 websocket 字典无锁且非原子更新，可能部分覆写。
- **`runtime/count_call_register.py`**、**`runtime/timer_call_register.py`** — 共享字典无锁。
- **`runtime/_callback_executor.py:22,39-40`** — `self._loop` 跨线程访问未同步；`_ensure_running` 并发调用可能多线程重复 spawn。

> **状态更新（2026-09-05）：已完成**。逐项落地（`tests/unit/runtime/test_registry_thread_safety.py` 11 个测试以插桩确定性复现竞态——`RaceWriteDict` 在首次 `__setitem__` 内睡眠制造检查-写入窗口、`SleepyId` 假 websocket 卡在三次字典写之间——修复前 6 个判别测试稳定 RED）：
> - **core.py**：`Register` 新增类级 `_registry_lock = threading.RLock()`，`__new__` 单例创建与 `clear_all_register_sessions` 子类遍历均在锁内（RLock 允许 clear_all 经 `subclass()` 重入 `__new__`）；并发首建测试在旧代码下稳定产生 2 次实例创建（1 次泄漏）、修复后恰好 1 次。顺带修正 `from venv import logger` 笔误为 `loguru`。
> - **state_register.py**：`StateRegisterMeM` 全部方法持 `_lock`；`get_all_states` 改返回**快照**（`dict(...)` 副本）——已核实仅有的两个调用方（`runtime/core.py` 遍历键、测试断言相等）均兼容副本语义，`get_state` 层的活对象读取（`tool_guardrails` 等读-改-写模式依赖）不受影响；首触竞态测试下旧代码 12 线程只剩 1 个键、修复后 12 键全在。
> - **relation_register.py**：新增 `_rm_lock = threading.RLock()`，register/unregister 三个 websocket 字典与两个 channel 字典的多字典写全部原子化（`SleepyId` 插桩下旧代码稳定产出"撕裂状态"：ws 映射在、反向映射缺失），读取（跨字典的 `get_websocket_by_session_id` 等）同样持锁。
> - **count_call_register.py**：既有 `_lock`（#9 时落地，仅覆盖 increase/reset_count）扩展到 `register`/`unregister`/`clear_session`——旧代码下 unregister 可在 locked increase 的 counter 检查与 trigger 取回之间插入 `del`，令 increase 抛 `KeyError`（插桩测试复现）。
> - **timer_call_register.py**：新增 `_timers_lock`，`register`/`unregister`/`reset_timer`/`clear_session` 与 `_run_timer` 清理尾全部持锁；executor 的 cancel_task/create_task 调用保持在锁外（先 pop/改映射，后动 executor）。
> - **_callback_executor.py**：`_ensure_running` 加 `_ensure_lock` 双检锁——旧代码在插桩窗口下 2 个并发首次调用 spawn 出 4 个后台 loop 线程，修复后恰好 1 个；`loop` 读取经 `_start_event` 保持 happens-before 可见性。
> - **诚实边界**：`get_all_states` 为浅拷贝（顶层字典副本；嵌套可变 value 仍与共享态共享引用，与 `get_state` 既有语义一致）。

## 14. `context_engine/core.py:11-12` 异步上下文中的阻塞 `threading.Lock` + 共享连接
- `search_messages` 内 `threading.Lock` 会阻塞事件循环线程；`sqlite3` 为阻塞 I/O。**修复：** 用 `asyncio.Lock` 或 executor 执行 DB 工作。

> **状态更新（2026-09-05）：已修复**。模块级 `_db = get_db()`（导入时副作用）移除，改为 `_shared_db()` 惰性建连（`context_engine/core.py`，首次 DB 访问才创建，进程内测试断言 `import context_engine.core` 后 `_db is None`）；4 处 `_db.execute` 全部改走 `_shared_db()`，既有测试的 `_db`/`_lock` patch 方式不受影响。新增 `search_messages_async()`（同文件末尾，`context_engine/__init__.py` 已导出）：经 `asyncio.to_thread` 把整段阻塞搜索（threading.Lock + sqlite3 I/O）卸载到 worker 线程，事件循环在搜索期间保持响应（0.3s 慢搜索期间 ticker 协程持续调度）。**诚实边界**：同步 `search_messages` 本身仍为阻塞实现——同步工具由 LangChain 在 executor 线程中调用属预期路径，协程内直接调用方应改用 `search_messages_async`。测试：`tests/module/test_search_messages_async.py`（5 个：导入无副作用、惰性建连、异步/同步结果一致、事件循环不阻塞、空查询）。

## 15. `context_engine/store/db.py:20-40` 非线程安全 `get_db()` 单例 + 短超时
- `if _db:` 双重检查无锁 → 两线程可能各建一个连接，第二个覆盖第一个（泄漏一个）。`timeout=1.0`（第 30 行）过短，并发时抛 `sqlite3.OperationalError: database is locked` 且无重试。**修复：** 加锁、加超时或加重试循环。

> **状态更新（2026-09-05）：已修复**。`get_db()` 改为带 `_db_lock` 的双重检查锁定（`context_engine/store/db.py`），并发压测（8 线程 + 屏障 + 人为放慢 `sqlite3.connect` 0.1s）下旧代码产生 7 个互异连接、修复后恰好 1 次 `connect` 调用且所有线程共享同一对象。`timeout=1.0` → `SQLITE_BUSY_TIMEOUT_S = 10.0`；新增 `_connect_with_retry()`：连接 + 迁移阶段的瞬时 `database is locked`/`busy` 错误按线性退避重试（`_CONNECT_ATTEMPTS=5`、`_RETRY_DELAY_S=0.2` 基数），失败尝试关闭半开连接防 fd 泄漏，非锁定类 `OperationalError`（如磁盘 I/O）立即上抛不重试。查询期的锁竞争由 10s busy timeout + WAL 覆盖。测试：`tests/module/test_store_db_singleton.py`（4 个：并发单连接、locked 重试、非 locked 不重试、busy timeout ≥5s）。

## 16. `channels/manager.py:154` `run_forever()` 阻塞调用线程
- `start_service()` 调用 `self._event_loop.run_forever()` 永久阻塞；模块级 `channel_manager = ChannelManager()`（第 236 行）带导入时副作用。

> **状态更新（2026-09-05）：已修复**。`start_service()` 重写为"调度后即返回"（`channels/manager.py`）：调度 dispatcher + inbound/outbound 消费循环 + 各 channel start 任务共 N+3 个任务到 `self._event_loop` 后立即 return，`run_forever()` 归调用方所有（`server/trigger/channels/core.py:_run()` 原有的守护线程 + `run_forever()` 收尾无需改动即自然成为循环属主）；以 `_started` 标志保证幂等，重复调用不再重复调度，无 channels / 无事件循环时为告警 no-op；`stop_service()` 复位 `_started` 并对 `None` 事件循环加护。模块级单例 `channel_manager = ChannelManager()`（导入时读配置、扫描插件、建事件循环的副作用）移除，改为 `get_channel_manager()` 惰性单例 + PEP 562 模块/包级 `__getattr__`（`channels/manager.py` 与 `channels/__init__.py`）——`import channels` 不再加载 `channels.manager`，`from channels import channel_manager` 与 `from channels.manager import channel_manager` 两种导入得到同一实例。附带修正：`_channels`/`_config`/`_bus`/`_event_loop` 等由共享类属性改为实例属性（在无配置早退 return 之前初始化）。测试：`tests/unit/channels/test_manager_lifecycle.py`（6 个：两条子进程导入惰性断言——带进程组 SIGKILL 超时护栏防 CI 挂死、start_service 不碰 run_forever、幂等、无 channels no-op、调度任务在调用方驱动循环后真实执行）。

## 17. `bus/core.py:16-18` 单一全局队列，无按渠道路由
- `MessageBus` 一个入站 + 一个出站 `asyncio.Queue`，所有渠道共享，路由在下游按 `msg.channel` 判断。高并发多渠道时是瓶颈且无法按渠道背压（当前规模可接受，中级优先）。

## 18. `skills/loader.py` 循环导入变通 + 字符串路径检查
- `scan_skills` 在函数内部做 `from .skills_snapshot import read_skills_snapshot`（循环导入变通）；`_is_third_party` 用字符串 `"./skills/plugins/"` 判断而非 `Path` 解析。

> **状态更新（2026-09-05）：已完成**。循环的真实成因：loader 与 skills_snapshot 互相需要对方的函数（scan_skills 要读快照缓存、build_skills_snapshot 要调 scan_skills），双方都在函数内惰性导入绕行。修复采用**依赖单向化**——把叶子函数 `read_skills_snapshot`（仅依赖 json + SKILLS_DIR）下沉到 `loader.py`（其唯一消费者），依赖变为单向 snapshot → loader，两处函数内导入全部移除：
> - `loader.py`：新增模块级 `read_skills_snapshot`（顺带修复 #12 的双重 `os.path.join`——移动时改为 `SKILLS_DIR / "skills_snapshot.json"`，行为等价）；`scan_skills` 函数体内不再有任何导入语句。
> - `skills_snapshot.py`：顶层 `from .loader import ...`（单向，无环），`read_skills_snapshot` 以 `as` 形式再导出保持旧导入面兼容；`build_skills_snapshot` 的惰性导入一并移除。
> - `_is_third_party` 改为 Path 段语义：`Path(location).parts[:2] == ("skills", "plugins")`（注意 pathlib 会规范化掉开头的 `.`，parts 不含 "."；修复了子串误判：`./skills/builtin/skills/plugins/foo` 旧代码误判为第三方，现正确为 False）。
> 测试：`tests/unit/skills/test_loader.py`（15 个：模块级属性存在性、scan_skills 无函数内导入的结构 pin、双导入顺序子进程验证、Path 语义含 lookalike 反例、scan_skills 原行为全套——第三方默认停用/状态文件激活/排序/缓存命中与 use_cache=False 跳过缓存读取、快照文件缺失返回 None）。回归 37 通过（含既有 `test_skill_scope.py`）。

## 19. 未鉴权日志文件读取
**文件**：`server/trigger/http/logs.py:170-198` — `GET /logs?path=...`。路径经 `LOG_DIR` 校验（受控），但端点未鉴权，日志可能含密钥。**利用：** `GET /logs?path=<info 日志>`。

## 20. FTS5 MATCH 注入（受限，非经典 SQLi）
**文件**：`context_engine/core.py:269,280-296`
- 查询作为绑定参数传入（对经典 SQLi 安全），`_sanitize_fts5_query`（第 62-112 行）为尽力过滤。恶意查询可触发昂贵/滥用型 FTS5 操作 → DoS。（非经典 SQL 注入。）

> **状态更新（2026-09-05）：已完成**。先做依赖库配置排查（用户要求验证"是否有现成配置可直接解决"，SQLite 3.53.1 实测）：`SQLITE_LIMIT_EXPR_DEPTH` 与 `SQLITE_LIMIT_LIKE_PATTERN_LENGTH` **均不进入 FTS5 解析器**（设为极小值后 MATCH 耗时不变）；`SQLITE_LIMIT_LENGTH` 能在 bind 层拦下超大查询（`DataError`）但为**连接级全局限制**，共享连接上会打断大内容消息写入——不可用。结论：**无现成配置可解，须在输入侧加确定性上限**。
> 实测攻击面（50k 行 FTS5 表）：2 万词 OR 链 ≈ **1.0s/查询**、3 万词 ≈ 2.1s、10 万词 ≈ **49s** 纯 CPU 烧灼（解析成本线性于 token 数，FTS5 无内部 token 数保护）；`t*` 型通配符 ≈ 17ms/词（词表扫描）。修复（`context_engine/core.py` `_sanitize_fts5_query` 输入侧上限）：**≤64 token**（1-3ms，DoS 归零）、**单 token ≤64 字符**、**通配符词 ≤4 个**（超出者剥离 `*` 降级为精确匹配）。上限在管线最前端生效，截断尾部的未配对引号由既有剥离步骤优雅降级；截断后悬空布尔算子由既有 dangling-op 逻辑清除。
> 测试：`tests/module/test_fts5_query_caps.py`（6 个：OR 链 64-token 截断、长 token 截断、通配符上限、常规查询不变、引号短语存活、**50k 词查询在真实 messages+FTS5 结构上 <1.5s 返回且结果非空**——旧代码同场景 >4s）。既有 `test_fts5_recall.py` 全量（常规/CJK/短语/布尔查询）回归通过。

## 21. 时间戳碰撞
**文件**：`context_engine/store/core.py:43` — `datetime.now().strftime("%Y%m%d%H%M%S")` 1 秒分辨率。同一秒内多回合共享时间戳，破坏 `get_session_ids`（第 390 行 `MAX(timestamp)`）排序。

> **状态更新（2026-09-05）：已完成**。**影响面先行**：客户端以 STRICT 14 位格式解析 `createTime`（`sessionFilter.parseSessionCreateTime`，17 位纯数字被判 null → 日期筛选静默丢会话）且 `formatCompactTimeString` 拒绝任何 `length !== 14`（消息时间显示变空串），`stats.py:88` 也按 14 位 strptime——**API 表面必须保持 14 位不变**。因此不改动可见的 `timestamp` 列格式，走 store 既有的版本化迁移模式：
> - **迁移**（`context_engine/store/db.py`）：新增步骤 `add_turn_ts_ms_column` —— `ALTER TABLE messages ADD COLUMN ts_ms INTEGER` + 全量回填（按 `timestamp` 字符串转 epoch-ms，空/坏值落 0 不崩溃，幂等：仅回填 `ts_ms IS NULL` 行，列已存在时仍执行回填）。
> - **生成**（`store/core.py`）：新增 `_next_turn_stamp()` —— 返回 (epoch-ms, 14 位显示戳)，epoch-ms 进程内**严格递增**（同毫秒调用 +1ms 错开，锁保护），同毫秒两回合不再打平。
> - **写入**：`add_messages` 每行写 `ts_ms`（批内共享，同旧行为）。
> - **排序**：`get_session_ids` 改按 `MAX(ts_ms) DESC` 排序（`last_time` 仍为 `MAX(timestamp)` 14 位，客户端契约不变）；同秒启动的两个会话按真实活动顺序稳定排列。
> - **API 兼容**：历史行 decode 时 `pop("ts_ms")`，行形状字节级不变；`timestamp` 列格式永久 14 位，无新旧混排的字典序问题；`stats.py` 严格解析不受影响。
> 测试：`tests/module/test_store_timestamp_ordering.py`（8 个：迁移回填/空坏值容错、同毫秒严格递增、14 位契约、add_messages 双回合递增、同秒会话按活动排序、last_time 14 位、历史行不泄漏 ts_ms）。回归 123 通过。诚实边界：跨进程严格递增不成立（robyn 已固定单进程，见 #16 附带发现）；ts_ms 为 0 的历史脏行排序时沉底。

> **状态更新（2026-09-07）：迁移与回填代码已移除**。所有已跟踪数据库均已应用 v8/v9 迁移且无 `ts_ms IS NULL` 行，`add_turn_ts_ms_column` / `backfill_missing_ts_ms` / `_legacy_ts_to_ms` 从 `db.py` 删除；`ts_ms INTEGER NOT NULL` 直接进入基础 schema（`build_messages_tb`）——新库开箱即含完整结构，已存库（v9）经版本门控不受影响。测试同步删除迁移/回填用例（迁移加列、空坏值回填、step-8 后重跑、`_legacy_ts_to_ms` 边界），保留生成/排序/API 形状用例。

## 22. cron"every"间隔漂移
**文件**：`skills/builtin/core/cron/scripts/base.py:57-81` — interval 任务从 `now` 而非上次运行时间推算下次运行。作业耗时过长时定时漂移。

> **状态更新（2026-09-05）：已完成**。`_compute_next_run` 增加 `anchor_ms` 参数（`skills/builtin/core/cron/scripts/base.py`）：`every` 任务下次运行锚定在**刚消费的调度槽位**上（`next = consumed_slot + k·everyMs`，k 取使结果严格晚于 now 的最小值，O(1) 跳过因超时/停机错过的槽位，不补发不雪崩）。调用侧两处落地：
> - `_execute_job`：回合完成后以 `job.state.next_run_at_ms`（刚触发的槽位）为锚重排——60s 间隔 + 30s 耗时的任务恒定在 T60/T120/T180 触发（旧代码 T90/T150/T210，每周期漂移 +30s 且无限累积）；手动 `run_job` 语义为"预触发当前 pending 槽位"（消费它，网格不动，无双重触发）。
> - `_recompute_next_runs`（服务启动）：仍指向未来的持久化 `nextRunAtMs` 原样保留（网格跨重启存活），缺失/已过期的槽位才从 now 重锚。
> 降级退避（breaker）跳过路径无需改动：`_execute_job` 照常推进槽位，锚定后自动保持网格。`add_job`/`enable_job`/`reset_failures` 的首次调度语义不变（网格从 now 起）。README "Scheduling Semantics" 表已同步。测试：`tests/unit/cron/test_cron_interval_drift.py`（9 个：锚点防漂移、错过槽位跳过不补发、无锚点首排、at 忽略锚点、三周期固定网格、手动运行消费槽位、重启保留未来槽位/过期重锚/缺失重锚；可控时钟注入 `_now_ms` 模拟任务耗时）。

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

# 沙箱加固层（2026-09-03/04）

2026-09-03/04 落地的沙箱加固层，对应 #2（部分缓解）、#11-③（针对性审批门）。实现位于 `agent/tools/pub_base/{env_scrub,sandbox,sandbox_bwrap,sandbox_seatbelt}.py` + 两个工具（`agent/tools/terminal.py`、`agent/tools/python_repl.py`）+ HITL 中间件（`agent/middlewares/humanInTheLoop/`）。

- **L1 环境变量清洗**（`scrub_env`）：11 个密钥子串拦截、~29 个精确保留名、`LC_`/`XDG_`/`CONDA` 前缀保留、11 个项目密钥强制拒绝；优先级为 保留 > 强制拒绝 > 子串拦截；在两个工具的每个生成点无条件执行（含已批准的 `sandbox=False` 调用）。
- **L2 OS 沙箱**：Linux bubblewrap——`--ro-bind / /` 只读根 + 仅项目根/临时目录可写 + `--unshare-all` + `--clearenv` 先于 `--setenv`；macOS Seatbelt——`(deny file-write*)` + 子路径 allowlist + `json.dumps` 转义路径。两后端均为列表 exec，无 shell kwarg。
- **策略三态**：`SANDBOX_POLICY=required|auto|off`（默认 `auto`；非法值抛 `ValueError`；每次调用重读）。
- **审批门**：`sandbox=False` 仅主会话经 HITL interrupt 人工审批，子代理/后台 scope 一律 `ToolException` 硬拒；危险命令正则与 sandbox 标志无关、先于任何子进程生成执行。
- **测试**：6 格优先级矩阵由 `tests/integration/test_sandbox_matrix.py` 14 个测试逐格覆盖；配套测试共 ~80+（env_scrub 29、matrix 14、bwrap/seatbelt 构造、HITL characterization 19 + bypass 17）。
- **诚实局限**：bwrap/Seatbelt 构造逻辑仅单测验证、未在真实 Linux/macOS 上实测；Windows 无 OS 沙箱；清洗按变量名（不扫值）；`auto` + 无后端时降级为无沙箱执行（有意放行，仅一条 loguru 警告）。
- **详细文档**：`docs/harness/sandbox/README.md`（EN/zh/ko/ja 四语）。

---

# 并发修复层（2026-09-05）

2026-09-05 落地的并发/生命周期修复批次，对应 **#13（已完成）、#14（已修复）、#15（已修复）、#16（已修复）**。TDD 流程：先写失败测试以插桩确定性复现竞态缺陷，再实现修复，最后以真实 Robyn 服务器端到端验证。

- **修复范围**：`context_engine/store/db.py`（单例加锁 + busy timeout 10s + locked 重试）、`context_engine/core.py`（惰性连接 + `search_messages_async` executor 卸载）、`channels/manager.py` + `channels/__init__.py`（非阻塞 `start_service` + 惰性单例）、`runtime/` 注册表簇（`core.py` 单例 RLock、`state_register.py` 全方法持锁 + 快照、`relation_register.py` 多字典原子更新、`count/timer_call_register.py` 写路径补锁、`_callback_executor.py` 双检锁）、**`server/__main__.py`（robyn 固定单进程，见下）**。
- **附带发现与修复：robyn `--fast` 进程池导致 WS 帧静默丢失**。e2e 验证期间发现 chunk/done 帧间歇性不到达客户端（A/B 实测先于本批次改动存在）。插桩定位（debugging skill 流程）：`--fast` 使 robyn 设 `processes=(cpu*2)+1`、`workers=2`，`init_processpool` fork 出 N 个**独立 Python 进程**；receiver socket 在 worker A 的 `relation_register` 注册，回合若在 worker B 执行则查到空注册表 → `websocket=None` → `_send_ws` 逐帧静默跳过（回合照常完成并持久化——SQLite 为共享磁盘状态）。跨进程 `id()` 相同的假象曾误导"同实例"判断，以 `os` 层证据（源码 `argument_parser.py:107` + `processpool.py` fork 循环 + 服务端 `send skipped, websocket=None` 日志）闭合。**修复**：`server/__main__.py` 在 `app.start()` 前固定 `app.config.processes = 1`、`app.config.workers = 1`——应用的全部会话状态均为进程内单例，多进程模式从未真正兼容。验证：修复前 ~50% 连接对丢帧，修复后连续 3 个连接对帧 100% 到达 + 全部落库。
- **测试**：新增 26 个——`tests/module/test_store_db_singleton.py`（4）、`tests/module/test_search_messages_async.py`（5）、`tests/unit/channels/test_manager_lifecycle.py`（6，含子进程导入断言与进程组超时护栏）、`tests/unit/runtime/test_registry_thread_safety.py`（11，插桩确定性竞态：RaceWriteDict/SleepyId/屏障，修复前 6 个判别测试稳定 RED）；回归 241+ 全部通过；ruff（E4/E7/E9+F）清洁。
- **端到端验证（Robyn）**：`python -m server --fast --disable-openapi` 启动 ~15s；`GET /sessions`、`GET /channels` 200；经 `/sessions/agent/ws` 发送对话、`/sessions/ws?session_id=…` 收流（接收端 socket 注册在后者，属既有架构），agent 经 GLM 真实回复并确认 `mes_memory.db` 中 human/ai 同 turn 持久化（`add_messages` 全链路走修复后的 `get_db`），累计 6 个 e2e 会话全部 2 行落库；SIGINT 干净退出。
- **诚实局限**：同步 `search_messages` 仍为阻塞实现（协程内调用方须改用 `search_messages_async`，同步工具走 LangChain executor 属预期路径）；查询期锁竞争依赖 busy timeout 而非逐语句重试；`get_all_states` 为浅拷贝（嵌套可变 value 仍共享引用，与 `get_state` 既有语义一致）。

---

# 修复优先级建议
1. **先修 CRITICAL**：#1/#5 `resolve_path`（加 ROOT_DIR 越界钳制）、#2 `terminal.py`（部分缓解——沙箱加固层已落地：正则门 + env 清洗 + OS 沙箱 + 审批门，Windows 回退仍 `shell=True`，见该条目 2026-09-04 状态更新）、#3 `clawhub`（固定版本 + 去 `--yes`）。
2. **加认证**：`server/trigger/core.py` 加 token/API-key 中间件并作用于所有路由与 WebSocket，去掉通配 CORS。覆盖 #6-#10、#19。
3. **加固路径处理**：`resolve_path` 拒绝/钳制 ROOT_DIR 外路径；`server/DAO/messages.py` 在 `shutil.rmtree` 前清洗 `session_id`。
4. **注册表并发**：**#13/#14/#15 已于 2026-09-05 全部修复**（见各条目状态更新与"并发修复层"章节）。
5. **重审 fail-open 扫描策略**（#11-②），并考虑为高风险工具增加确认闸门（#11-③）。（sandbox=False 的审批门已于 2026-09-04 落地）
6. 修复后重新审计，确认以上各域闭合。
