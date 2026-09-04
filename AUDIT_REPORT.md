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

## 13. 共享可变状态 / 线程安全（`runtime/` 注册表簇）
- **`runtime/core.py:5-24`** — 类级 `_instances = {}` 在 `__new__` 中无锁修改；`clear_all_register_sessions` 无锁遍历 `cls.__subclasses__()`；`__new__` 单例非线程安全。
- **`runtime/state_register.py:14,17-91`** — `_states` 无锁修改；`get_all_states`（第 40 行）返回**活对象**，调用方可直接改动共享态。
- **`runtime/relation_register.py`** — 三个 websocket 字典无锁且非原子更新，可能部分覆写。
- **`runtime/count_call_register.py`**、**`runtime/timer_call_register.py`** — 共享字典无锁。
- **`runtime/_callback_executor.py:22,39-40`** — `self._loop` 跨线程访问未同步；`_ensure_running` 并发调用可能多线程重复 spawn。

## 14. `context_engine/core.py:11-12` 异步上下文中的阻塞 `threading.Lock` + 共享连接
- `search_messages` 内 `threading.Lock` 会阻塞事件循环线程；`sqlite3` 为阻塞 I/O。**修复：** 用 `asyncio.Lock` 或 executor 执行 DB 工作。

## 15. `context_engine/store/db.py:20-40` 非线程安全 `get_db()` 单例 + 短超时
- `if _db:` 双重检查无锁 → 两线程可能各建一个连接，第二个覆盖第一个（泄漏一个）。`timeout=1.0`（第 30 行）过短，并发时抛 `sqlite3.OperationalError: database is locked` 且无重试。**修复：** 加锁、加超时或加重试循环。

## 16. `channels/manager.py:154` `run_forever()` 阻塞调用线程
- `start_service()` 调用 `self._event_loop.run_forever()` 永久阻塞；模块级 `channel_manager = ChannelManager()`（第 236 行）带导入时副作用。

## 17. `bus/core.py:16-18` 单一全局队列，无按渠道路由
- `MessageBus` 一个入站 + 一个出站 `asyncio.Queue`，所有渠道共享，路由在下游按 `msg.channel` 判断。高并发多渠道时是瓶颈且无法按渠道背压（当前规模可接受，中级优先）。

## 18. `skills/loader.py` 循环导入变通 + 字符串路径检查
- `scan_skills` 在函数内部做 `from .skills_snapshot import read_skills_snapshot`（循环导入变通）；`_is_third_party` 用字符串 `"./skills/plugins/"` 判断而非 `Path` 解析。

## 19. 未鉴权日志文件读取
**文件**：`server/trigger/http/logs.py:170-198` — `GET /logs?path=...`。路径经 `LOG_DIR` 校验（受控），但端点未鉴权，日志可能含密钥。**利用：** `GET /logs?path=<info 日志>`。

## 20. FTS5 MATCH 注入（受限，非经典 SQLi）
**文件**：`context_engine/core.py:269,280-296`
- 查询作为绑定参数传入（对经典 SQLi 安全），`_sanitize_fts5_query`（第 62-112 行）为尽力过滤。恶意查询可触发昂贵/滥用型 FTS5 操作 → DoS。（非经典 SQL 注入。）

## 21. 时间戳碰撞
**文件**：`context_engine/store/core.py:43` — `datetime.now().strftime("%Y%m%d%H%M%S")` 1 秒分辨率。同一秒内多回合共享时间戳，破坏 `get_session_ids`（第 390 行 `MAX(timestamp)`）排序。

## 22. cron"every"间隔漂移
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

# 修复优先级建议
1. **先修 CRITICAL**：#1/#5 `resolve_path`（加 ROOT_DIR 越界钳制）、#2 `terminal.py`（部分缓解——沙箱加固层已落地：正则门 + env 清洗 + OS 沙箱 + 审批门，Windows 回退仍 `shell=True`，见该条目 2026-09-04 状态更新）、#3 `clawhub`（固定版本 + 去 `--yes`）。
2. **加认证**：`server/trigger/core.py` 加 token/API-key 中间件并作用于所有路由与 WebSocket，去掉通配 CORS。覆盖 #6-#10、#19。
3. **加固路径处理**：`resolve_path` 拒绝/钳制 ROOT_DIR 外路径；`server/DAO/messages.py` 在 `shutil.rmtree` 前清洗 `session_id`。
4. **注册表并发**：为 `runtime/*_register.py` 与 `context_engine` 共享连接加锁并返回副本（#13/#14/#15）。
5. **重审 fail-open 扫描策略**（#11-②），并考虑为高风险工具增加确认闸门（#11-③）。（sandbox=False 的审批门已于 2026-09-04 落地）
6. 修复后重新审计，确认以上各域闭合。
