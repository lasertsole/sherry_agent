# 审计报告 — Sherry Agent

**审计范围**：`D:\selfProj\sherry_agent` 全项目（排除 `.venv`、`node_modules`、`client/node_modules`、vendored 代码、`logs/`、模型权重数据）。
**审计方法**：6 个并行审计子代理覆盖全栈（后端安全、Agent 核心、数据层/运行时/模型、前端 Nuxt+Tauri、基础设施/技能/CI、代码质量/架构/类型），对关键发现进行源码直接复核。
**审计日期**：2026-08-24（第一轮）/ 2026-09-04（第二轮）/ 2026-09-14（第三轮全栈重审）
**更新日期**：2026-09-14 — 全栈重审后合并所有未修复条目，新增前端审计和代码质量审计，编号连续重排。
**核实记录**：2026-09-15 — 逐条对照源码复核当前全部条目（以“修复后应存在的守卫/改写/调用”为模式做全仓 grep，关键文件精读）：**零删除**，56 条经核实全部仍未修复。
**本报告只保留未修复项；编号已于 2026-09-15、2026-09-24、2026-09-25（两轮）、2026-09-26（三轮）共七轮重排（旧编号作废）**。此前已修复移除 19 项（旧编号 19、20、27、38、44、45、48、50、51、53、57、58、59、61、63、68、70、73、75），不再占用编号；旧→新对照见文末「编号对照（旧 → 新）」。
**2026-09-24**：P0 五项（#1、#3、#16、#17、#18）修复完成并从本报告移除，剩余 51 条重排为连续编号 1-51（对照规则见文末「编号对照」）；全仓代码注释与测试中的 `audit #N` 引用同步改写（移除项改为直接命名威胁，保留项更新为新编号）。
**2026-09-25**：三项（#38 DOMPurify style 注入、#48 JSON 深拷贝、#49 dev server 绑定）修复完成并从本报告移除，剩余 48 条重排为连续编号 1-48（对照规则见文末「编号对照」）；本轮落码未引入编号引用，无外部漂移。
**2026-09-25（第二轮）**：一项（#11 `summarization` — `awrap_model_call` 异步路径同步 SQLite + 文件 I/O）修复完成并从本报告移除，剩余 47 条重排为连续编号 1-47（#12-#48 各减 1，对照规则见文末「编号对照」）；本轮落码未引入编号引用，无外部漂移。
**2026-09-26**：P2/P3 批次修复 17 项并从本报告移除，剩余 30 条重排为连续编号（对照规则见文末「编号对照」）——#9 media_pipeline 异步钩子 offload（测试 tests/agent/middlewares/test_media_pipeline_offload.py）；#10 StateRegisterDB 与 #15 curator 复核为**早已收口**（分别是「锁保护的单连接 + 单行语句」与「专用线程事件循环 + HTTP 入口 `to_thread`」），#29 StateRegisterDB 连接复核为已收口，本轮另补了 todolist 那一处 DB 写入的 offload；#12 compaction_lock async 收口（test_compaction_lock.py）；#13 embeddings/search、#14 session_continuity offload；#17 移除 `verify=False`（改为 `SHERRY_HTTP_VERIFY_TLS`，默认校验）并删除全局 `disable_warnings`（test_http_client.py）；#18 child checkpointer 执行结束即关闭；#23 知识图谱遍历参数上限（test_knowledge_graph_api.py）；#30 `ContextEpoch.prepare()` 复用寄存器共享连接；#31 registry `_runs` 改 OrderedDict + `registry_max_retained_runs` 封顶（优先驱逐终端 run）；#32 冷却会话集合改有界（上限 512）；#42 push_channel 取消后 `await`；#43 channel 线程事件环 `finally` 关闭；#46 补 `skills/builtin/code_wiki/scripts/__init__.py` 与 `plugins/channels/qq/__init__.py`；#7 ①②③（① `<description>` XML 转义 skills/loader.py `_xml_text`；② 扫描器启用但不可用时上传 fail-closed——skill_scan_policy + clawhub_runner 回滚，仅 `SKILL_SCANNER_ENABLED=0` 显式关闭才放行；③ HITL 首次调用确认闸门 `first_call_confirmation_tools`，交互式 turn only、YOLO 绕过、按会话记忆——测试 test_skill_scanner.py、test_clawhub_scan_rollback.py、test_hitl_first_call_gate.py）。本轮落码未引入编号引用，无外部漂移。
**2026-09-26（第二轮）**：P3 清理批 5 项处理并从本报告移除，剩余 25 条重排为连续编号 1-25（对照规则见文末「编号对照」）——**#21** bus 单队列复核为**有意设计**：出站消息早已由 `_dispatch_outbound()` 按 `msg.channel` 只投递目标渠道（报告所述"遍历所有渠道"的 `_consume_loop` 不存在），唯一消费者不变量固定在 manager docstring 与 channels 四语文档中；单队列 + 有界背压是文档化的解耦设计（bus/core.py），按渠道背压属多渠道规模化时的后续演进，非缺陷。**#40** Windows 无原生沙箱复核为**文档化降级**：优先级矩阵（sandbox.py docstring）明确 auto→无后端时降级，terminal/python_repl 每次实际调用都发 loguru 告警（terminal.py:206-211、python_repl.py:122-126），`SANDBOX_POLICY=required` 提供 fail-closed 逃生门；Windows 原生沙箱（job object 等）属新功能而非审计修复。**#44** 三处 `threading.Lock` 复核为**正确原语**：三处均为事件循环线程与其他线程（sweeper/持久化/WS handler）共享的状态，临界区全部同步且无 await；换成 asyncio.Lock 反而丧失线程安全并破坏同步调用方（memory.py 已补注释固定该约束；push_channel/subagent_ws docstring 本就有跨线程说明）。**#45** 修复：`delegate_task` 入口新增运行中事件循环防护（报错点名 `spawn_subagent_direct`/`to_thread` 备选，tests/agent/tools/subagent/test_delegate.py::TestLoopGuard），并顺带修复**活体缺陷**——`POST /subagents/runs` 的 async handler 此前在循环线程上直接同步调用 `delegate_task`（`asyncio.run`/新循环 `run_until_complete` 在 Python 3.13 下必然 RuntimeError→500），现改经 `asyncio.to_thread` 派发（tests/server/trigger/http/test_subagent_runs_api.py，运行日志证实该 POST 从未被成功调用过）。**#47** 修复：eval 套件改读 `evals/nudge_extraction/.env`（套件本地、gitignored），不再加载生产 `.env`；进程环境保持权威（override=False），缺钥时套件照旧自跳过（tests/evals/nudge_extraction/test_suite_env.py）。本轮落码未引入编号引用，无外部漂移。
**2026-09-26（第三轮）**：P2 批次 5 项修复/收口并从本报告移除，剩余 20 条重排为连续编号 1-20（对照规则见文末「编号对照」）——**#2 + #13** store 异步调用点逐一 offload（本批共 5 个裸调用站点：message_persistence 的 watermark SELECT/UPDATE、context_eviction `awrap_tool_call` 的整段重写（含逐出文件 I/O）、DAO `clear_session` 的批量 DELETE、HTTP 历史分页读取、#13 interrupt_marker 的去重扫描；aiosqlite 迁移评估为**不必要**：连接已是 `check_same_thread=False` + 锁保护单例 + autocommit，且 store 的异步入口（`add_messages`/`search_messages_async`）本就在内部 offload，剩余工作只是消费方收口）。**#16** `list_descendant_runs` 改为单次 requester 索引 BFS（原实现每个展开节点全扫一次注册表，O(N*D)→O(N)，结果顺序不变）。**#17** `_pump_lane` 改「每 group pump 锁 + 单次权威扫描换预算」：消除每次激活的全量扫描（N=2000 时 790→223µs/次，另把枚举查找提出循环），并发 pump 不再各自消费过期预算（admission 是进入 ACTIVE 的唯一漏斗）；`admission_checked` 跳过的失败路径由 pump 循环接管。**#18** `_recovery_loop` 改 try/finally 单一清理点（提前返回与异常不再泄漏已完成任务条目），run 消失/终结后丢弃尝试计数器（结构性有界）。测试：tests/agent/tools/subagent/{test_queries,test_swarm,test_orphan_v3}.py、tests/agent/middlewares/{message_persistence,context_eviction}/、tests/server/{service/test_interrupt_marker,trigger/http/test_messages_http,DAO/test_clear_session}.py。本轮落码未引入编号引用，无外部漂移。

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

# 🟠 高（High）— 安全 / 数据完整性 / 资源泄漏 / 并发

## 2. 缺认证 + 通配 CORS — 所有端点未鉴权

**文件**：`server/trigger/core.py:16` — `ALLOW_CORS(app, origins=["*"])`

- `server/__main__.py` 与 `trigger/core.py` 无任何认证中间件。所有 HTTP/WS 端点未鉴权、跨域可达。
- **利用**：任意网站可向 agent API 发请求。衍生以下未鉴权高风险端点（#3-#5）。
- **状态**：未修复。

## 3. 未鉴权 `.env` 读写 — 密钥暴露

**文件**：`server/trigger/http/env.py:6-13`

- `GET /env` 返回含 API Key 的完整配置；`PUT /env` 可改写运行时凭据。
- **利用**：`GET /env` 窃取 `MAIN_LLM_API_KEY`、`AUXILIARY_LLM_API_KEY`、`TAVILY_API_KEY` 等。
- **状态**：未修复。

## 4. 未鉴权 WebSocket agent 控制

**文件**：`server/trigger/ws/messages.py:116-183` — `app.websocket("/sessions/agent/ws")`

- 客户端可发 `multi_modal_message` 触发 `async_generate`、`hitl_response` 批准/拒绝工具调用、`stop` 取消。
- **利用**：攻击者驱动 agent 并批准危险工具调用。
- **状态**：未修复。

## 5. 未鉴权日志流 — 密钥泄漏

**文件**：`server/trigger/ws/logs.py:119-148` — `app.websocket("/logs/ws")`

- 把每条 loguru 记录（可能含 API Key、请求头、提示词）流式推给任意未鉴权 WS 客户端。
- **利用**：连接 `/logs/ws` 从日志帧读取密钥。
- **状态**：未修复。已添加 bounded deque (maxlen=2000)，但未鉴权问题仍在。

## 6. SSRF — 未校验媒体 URL 下载

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

## 7. `WsTurnExecutor.execute` — 被取消时不取消 child 任务

**文件**：`server/service/turn_runner.py:298-314`

- `execute` 被取消时 `await child` 收到 `CancelledError`，re-raise 但 **child 从未被取消**。`finally` 从 `_active_tasks` 弹出槽位但不 cancel child，且调用 `on_turn_finished` 可能触发更多 queued turn——此时 child 仍在后台运行。
- **修复**：在 re-raise 路径前加 `child.cancel()`。
- **状态**：未修复。

## 8. `agent/tools/message_search.py` — `asyncio.run()` 在工具中

**文件**：`agent/tools/message_search.py:487`

- `_run_semantic_search` 调用 `asyncio.run(semantic_search(...))`。如果工具从 async 上下文调用（LangChain tool node 运行 sync 工具在 thread pool 中），该线程无运行中的事件循环，`asyncio.run()` 可工作；但这是脆弱的隐式假设。
- **修复**：提供 async 版本或用 `run_async` 模式。
- **状态**：新发现。

## 9. 技能上传/切换端点零测试覆盖

**文件**：`server/trigger/http/skills/lifecycle.py`（242 行代码）

- `upload_skill_handler` 和 `toggle_skill_handler` 是接受第三方代码的关键安全端点，涉及路径遍历防护、安全扫描门控、状态文件写入——**完全无测试**。
- **修复**：编写覆盖路径遍历、安全扫描门控、状态文件原子性的集成测试。
- **状态**：新发现。

## 10. CI 无 SAST/依赖漏洞扫描

**文件**：`.github/workflows/ci.yml`

- CI 仅运行 ruff + pytest + import-linter，无 Bandit/Semgrep 静态安全扫描、无 pip-audit/safety 依赖漏洞检查、无 CodeQL 集成。
- **修复**：在 CI pipeline 中添加安全扫描步骤。
- **状态**：新发现。

---

# 🟡 中（Medium）— 代码质量 / 架构 / 正确性

## 11. 未鉴权日志文件读取

**文件**：`server/trigger/http/logs.py:170-198` — `GET /logs?path=...`

- 路径经 `LOG_DIR` 校验（受控，已修复路径穿越），但端点未鉴权，日志可能含密钥。
- **状态**：路径穿越已修复，未鉴权仍在。

## 12. `steering_queue` — 异步方法中使用 `threading.Lock`

**文件**：`agent/tools/subagent/announce/steering_queue.py:119,136,338`

- `enqueue_steering`/`drain`（async）用 `with state.lock:`（`threading.Lock`），另一线程持锁时事件循环阻塞。设计文档明确"NEVER held across an await"，但技术上仍有阻塞风险。
- **修复**：改 `asyncio.Lock`。
- **状态**：未修复。

## 13. `run_async` — 从运行中的事件循环调用时 `future.result()` 阻塞

**文件**：`pub/func/run_async.py:77-114`

- **修复**：确保所有 `run_async` 调用方不在事件循环线程上，或提供 `await` 版本。
- **状态**：未修复。有改进（timeout 后取消 pending tasks + shutdown pool），但阻塞本身仍在。

## 14. `agent/tools/subagent/registry/settle_wake.py` — 异步路径中的同步 SQLite

**文件**：`agent/tools/subagent/registry/settle_wake.py:103`

- `retire_after_settle`（async）调用 `_persist_state()` → `save_settle_wake_state()`（同步 sqlite3）。
- **状态**：新发现。

## 15. `config/schema.py` 从 `models/` 导入 — 违反架构规则

**文件**：`config/schema.py:185,260`

```python
from models.providers.registry import PROVIDERS  # 函数级延迟导入
from models.providers.registry import find_by_name
```

- 违反 AGENTS.md："config/features/** MUST NOT import from agent/, server/, or models/ — config is dependency-free"
- **修复**：将 provider 匹配逻辑移到 models 层或新建 resolver 层。
- **状态**：新发现。

## 16. `context_engine/events/store.py` — `__import__()` 反模式 + `db: Any` 类型

**文件**：`context_engine/events/store.py:17,19-23,39`

- 使用 `__import__()` 做延迟导入而非正常 import 语句。`db: Any = None` 参数未类型化。
- **状态**：新发现。

## 17. `runtime/process/crash_loop_breaker.py` — 配置在导入时读取

**文件**：`runtime/process/crash_loop_breaker.py:35-39`

- `WINDOW_S`、`TRIP_THRESHOLD`、`RETENTION_S` 在模块级读取配置，运行时配置变更不会生效。
- **状态**：新发现。

## 18. `models/LLMs/main_llm.py` — 环境变量在导入时读取

**文件**：`models/LLMs/main_llm.py:17-22,64-88`

- 环境变量在模块导入时读取。`model_config` 是模块级可变 dict，被 `apply_thinking_budget()` 修改——非线程安全。`int(os.getenv(...))` 无 try/except。
- **状态**：新发现。

## 19. `context_engine/embeddings/indexer.py` — 硬编码模型常量

**文件**：`context_engine/embeddings/indexer.py:10-12`

- `_EMBED_MODEL_NAME = "bge-m3"`、`_EMBED_DIM = 1024`、`_BATCH_SIZE = 32` — 不可配置。
- **状态**：新发现。

## 20. 全局异常处理器暴露 `str(error)`

**文件**：`server/trigger/core.py:36`；`server/trigger/http/knowledge_graph.py:205`；`server/trigger/http/stats.py:126`；`server/trigger/http/curator.py:41,136`

- 全局异常处理器返回 `"error": str(error)`，可能暴露内部文件路径或状态。
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

## 前端待修复项

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

- **加认证**：`server/trigger/core.py` 加 token/API-key 中间件并作用于所有路由与 WebSocket，去掉通配 CORS。覆盖 #2-#5、#11、#20。
- **#1** `delegate.py` `time.sleep` 阻塞事件循环（提供 `result_async`）
- **#6** 内网 IP 黑名单（SSRF 防护）
- **#7** `execute` 取消 child 任务
- **#9** 为技能上传/切换端点编写测试
- **#10** CI 添加 SAST/依赖漏洞扫描
- **#15** `config/schema.py` 从 models 导入违规

## P2 — 计划修复（2026-09-26 批次，编号项全部闭合）

**已完成**

- **异步/阻塞收口（第一批）**：#9 media_pipeline 钩子、#12 compaction_lock、#13 embeddings/search、#14 session_continuity 全部改走 `asyncio.to_thread`；#10（StateRegisterDB）与 #15（curator）复核为**早已收口**（分别是「锁保护的单连接 + 单行语句」与「专用线程事件循环 + HTTP 入口 `to_thread`」），本轮只补了 todolist 那一处 DB 写入的 offload。
- **资源泄漏（第一批）**：#18 child checkpointer 执行结束即关闭；#31 registry `_runs` 加封顶（`registry_max_retained_runs`，优先驱逐终端 run）；#32 冷却会话集合改有界；#30 `ContextEpoch.prepare()` 不再泄漏 sqlite 句柄。
- **性能（第一批）**：#29 复核为**已收口**（StateRegisterDB 已是锁保护共享连接，非每操作新建）。
- **fail-open 与确认闸门（第一批）**：#7-① XML 转义、#7-② 扫描器 fail-closed、#7-③ HITL 首次调用确认闸门 全部落地。
- **TLS（第一批）**：#17 移除 `verify=False`（改为 `SHERRY_HTTP_VERIFY_TLS`，默认校验）并删除全局 `disable_warnings`。
- **异步/阻塞收口（第二批，2026-09-26 第三轮）**：#2 + #13 五个裸调用站点全部 offload（message_persistence watermark SELECT/UPDATE、context_eviction `awrap_tool_call` 整段重写、DAO `clear_session` 批量 DELETE、HTTP 历史分页读取、interrupt_marker 去重扫描）；aiosqlite 迁移评估为不必要（连接已 `check_same_thread=False` + 锁保护单例 + autocommit，store 异步入口本就内部 offload）。
- **性能（第二批）**：#16 `list_descendant_runs` 单次索引 BFS（O(N*D)→O(N)）；#17 `_pump_lane` 每 group 锁 + 单次权威扫描换预算，消除逐次激活全量扫描（790→223µs/次 @N=2000）。
- **资源泄漏（第二批）**：#18 `_recovery_loop` try/finally 单一清理点 + 终端/消失 run 丢弃尝试计数器（结构性有界）。

**P2 全部闭合**——后续遗留（大文件拆分、类型注解、错误处理统一、全局状态治理）见 P3 的「未完成（下一批）」。

## P3 — 低优先级清理（2026-09-26 两批，编号项全部闭合）

**已完成**

- 2026-09-26 第一批：#23 知识图谱遍历参数上限、#42 取消后 await、#43 channel 事件环关闭、#46 缺失的 `__init__.py`。
- 2026-09-26 第二批：#21 bus 单队列（复核为有意设计——出站早已按 `msg.channel` 定向投递，唯一消费者不变量固定在 docstring 与 channels 四语文档；按渠道背压留作多渠道规模化演进）；#40 Windows 无沙箱（复核为文档化降级——每次沙箱调用告警 + `SANDBOX_POLICY=required` fail-closed，原生 Windows 沙箱属新功能）；#44 三处 `threading.Lock`（复核为正确原语——跨线程共享状态、临界区无 await，memory.py 已补注释固定约束）；#45 delegate_task 循环防护 + `POST /subagents/runs` 改经 `asyncio.to_thread`（顺带修复 async handler 上同步派发必然 RuntimeError→500 的活体缺陷）；#47 eval 改读套件本地 `.env`。

**未完成（下一批，均为跨批次扫除项）**

- **大文件拆分**：`summarization/core.py`（约 2000 行）尚未拆分。注意该模块已完成「组合根 + mixin」拆分（compression / overflow / summary_generation / thrash），继续拆分的下一步是把组合根本体按生命周期钩子再切分。
- **类型注解**：`agent/` 下 292 个函数缺返回类型。建议按子目录分批（middlewares → tools → wrapper），每批跑 `basedpyright agent/`。
- **错误处理统一**：`agent/` 下 64 处 `except Exception:`。建议只给「静默吞掉异常」的站点补日志，不机械加行。
- **全局状态治理**：`agent/` 52 处模块级可变全局。需逐项判断：进程级单例（注册表、缓存）应保留并注明，会话态才考虑封装。
- 修复后重新审计，确认以上各域闭合。

## 编号对照（旧 → 新）

> 2026-09-15 重排：左列为重排前的旧编号（中间空缺为更早轮次已修复移除的编号），右列为重排后的连续编号。本轮核实结果为**零删除**，全部 56 条均为保留项。
>
> 2026-09-24 重排：#1、#3、#16、#17、#18 修复完成后移除，剩余 51 条重排为连续编号 1-51（#2→#1；#4-#15 各减 2；#19-#56 各减 5）。上表「新编号」列为 2026-09-15 编号，映射到现编号按上述规则；5 个移除编号不再占用。本轮同步清理了全仓 `audit #N` 注释与测试引用（移除项改写为直接命名威胁，保留项更新为新编号）。
>
> 2026-09-25 重排：#38、#48、#49 修复完成后移除，剩余 48 条重排为连续编号 1-48（#39-#47 各减 1；#50-#51 各减 3）。上表「新编号」列仍为 2026-09-15 编号，映射到现编号按 2026-09-24 注与本次规则复合计算。
>
> 2026-09-25 第二轮重排：#11（summarization — awrap_model_call 异步路径同步 SQLite + 文件 I/O）修复完成后移除，剩余 47 条重排为连续编号 1-47（#12-#48 各减 1）。上表「新编号」列仍为 2026-09-15 编号，映射到现编号按前述各注与本次规则复合计算。
>
> 2026-09-26（两批）重排：P2/P3 批次移除 17 项、P3 清理批移除 5 项，剩余 25 条重排为连续编号 1-25。上表「新编号」列仍为 2026-09-15 编号，映射到现编号按前述各注扣除在其之前移除的条目数复合计算；保留项与本轮移除项的对应关系已在各批次说明中直接命名，不再逐行更新本表。
>
> 2026-09-26（第三轮）重排：P2 批次移除 5 项（#2、#13、#16、#17、#18），剩余 20 条重排为连续编号 1-20（#3-#12 各减 1；#14-#15 各减 2；#19-#25 各减 5）。上表「新编号」列仍为 2026-09-15 编号，映射规则同上复合计算。

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
