# QC 全量代码审查报告

- 审查日期：2026-10-08
- 范围：`agent/` `server/` `runtime/` `pub/` `config/` `context_engine/` `workspace/` `skills/` `models/` `tests/`
- 方法：三路并行源码扫描，逐 file:line 核验
- 结论：**无导入违规、无 SQL 注入、无 TODO/FIXME 残留、无裸 except**。问题集中在连接泄漏、竞态、assert 用作运行时控制、死配置键。

---

## BUG — 必须修复（8 项）

### B1. Checkpointer 连接泄漏——每次 force_rebuild 丢一个 aiosqlite 连接 + 工作线程 + 文件句柄

- **文件**：`agent/core.py:323-329`
- **问题**：`built_agent(force_rebuild=True)` 在每个 WS turn 和 resume 时被调用。rebuild 时直接 `_agent = await _build_graph(...)`，旧 graph 的 `ThreadSafeAsyncSqliteSaver` 连接从未 `aclose()`。`aclose()` 存在（`thread_safe_checkpointer.py:113-125`）但无人调用。同文件 `delete_thread_history` 的注释已经识别过同类问题（"without it, every clear_session call leaked one connection"），但 rebuild 路径没修。
- **修复**：rebuild 前若 `_agent is not None`，先 `await _agent.checkpointer.aclose()`。

### B2. `data:image/` 无逗号时 IndexError 穿透

- **文件**：`agent/middlewares/media_pipeline/media_handlers.py:282-284`
- **问题**：`url.split(",")[1]` 在 `try` 块外面。`data:image/png`（截断、无逗号）匹配 `startswith("data:image/")` 但没有第二个元素 → `IndexError` 穿透媒体管线。下一行的 `base64.b64decode` 反而有 try 保护。
- **修复**：把 split 移进 try，或用 `url.split(",", 1)` + 长度检查。

### B3. WS receive 循环在断连时 CPU 空转

- **文件**：`server/trigger/core.py:255-257`
- **问题**：`while True: ... except Exception:` 会捕获 `WebSocketDisconnect`（它是 `Exception` 子类），日志后立即在死 socket 上再调 `receive_text()` → 无限 CPU 空转。兄弟处理器 `ws/messages.py:363` 已正确 re-raise `WebSocketDisconnect`，此处漏了。
- **修复**：在 `except Exception` 前加 `except (WebSocketDisconnect, ConnectionResetError): raise`。

### B4. `RelationManager.clear_session` 传错参数

- **文件**：`runtime/session/relation_register.py:175`
- **问题**：第二行 `self.unregister_websocket_by_websocket_id(session_id)` 把 `session_id` 当 `websocket_id` 传——永远 pop 到 None，是死代码。第一行已正确清理。
- **修复**：删除第二行，或传解析出的 websocket_id。

### B5. `ContextEpoch` 写方法绕过共享连接 + 锁

- **文件**：`runtime/session/state_register.py:349, 405, 419`
- **问题**：`initialize`/`replace`/`advance` 各自 `sqlite3.connect(self._db.db_path)` 开新连接，而 `StateRegisterDB` 所有操作走 `self._conn_lock` 的单连接。类 docstring（:136-143）承诺"单锁单连接避免 WAL/handle churn"。`prepare`（:369-377）已修好走共享连接，三个写方法没修。后果：WAL/handle churn + epoch 写不序列化 → 并发下 `database is locked`。
- **修复**：三个写方法走 `self._db.ensure_initialized()` + `self._db._conn_lock`，与 `prepare` 一致。

### B6. `Lane.set_max` 在热更新时困住等待者

- **文件**：`runtime/lane/core.py:141-151`
- **问题**：`_rebind` 用新 Semaphore 替换 `self._sem`，但已 `await sem.acquire()` 阻塞的 task 持有旧 semaphore。`release()` 释放新 semaphore → 旧等待者永远不被唤醒，`_queued` 计数永不递减。`LaneManager.set_concurrency` 在正常热更新路径暴露此问题。
- **修复**：rebind 前把待唤醒的等待者迁移到新 semaphore，或 `set_max` 时若 `_queued > 0` 拒绝。

### B7. `_drain_loop` 在异常时遗弃 CLAIMED 行

- **文件**：`server/service/turn_runner.py:346-354`
- **问题**：`claim_next` 成功（行→CLAIMED）后 `_execute_single` 若抛异常（如 `mark_terminal` 自身失败），异常传到 `_drain_loop` 的 `except Exception`，日志 + sleep + 重试。重试时 `claim_next` 取下一行 QUEUED，原行卡在 CLAIMED 直到 24h `recover()` 扫描。卡住的行阻塞 `on_turn_finished` 的 deferral 并占用 dedup key。
- **修复**：`_execute_single` 的失败路径用 `finally` 确保 CLAIMED 行被 finalize（VOIDED/FAILED）。

### B8. 单例测试写真实生产 MesMemory DB

- **文件**：`tests/context_engine/store/test_store_db_singleton.py:32-47`
- **问题**：`fresh_singleton` fixture 重置 `_db`/`_db_lock` 但不 patch `_db_path`。`test_concurrent_first_calls_share_one_connection` 和 `test_busy_timeout_at_least_five_seconds` 调 `db_mod.get_db()` → 对真实路径 `SRC_DIR/store/mes_memory/mes_memory.db` 执行 schema self-heal + migrate。同目录所有其他测试都正确 patch（`test_store_decode.py:67` 等）。
- **修复**：`fresh_singleton` 加 `monkeypatch.setattr(db_mod, "_db_path", tmp_path / "mes_memory.db")`。

---

## RISK — 应修复（15 项）

### R1. `assert` 用作运行时控制流（`-O` 下消失）

- **文件**：`agent/tools/file_tools/write_file.py:245`、`agent/tools/taskflow/registry/store_sqlite.py:307,410`、`server/queue/user_input_queue.py:471,513,532`
- **问题**：`assert license is not None`、`assert flow is not None`、`assert count_row is not None` 在 `python -O` 下被剥离。若条件不满足，`-O` 下变成 `AttributeError`/`TypeError` 而非有意义的错误消息。
- **修复**：替换为 `if x is None: return json.dumps({"error": ...})` 或 `raise RuntimeError(...)`。

### R2. 模块级 `_agent`/`_agent_loop` 无锁竞态

- **文件**：`agent/core.py:124-125, 323-329`
- **问题**：两个并发 caller（不同线程/loop）可同时通过 None/loop-mismatch 检查，同时 rebuild，互相覆盖——孤儿化一个 graph + 其 checkpointer 连接（与 B1 同类泄漏）。
- **修复**：check-and-build 加锁。

### R3. `except BaseException` 捕获 `CancelledError`

- **文件**：`agent/middlewares/summarization/overflow.py:703,743`
- **问题**：`_execute_with_recovery`/`_aexecute_with_recovery` 用 `except BaseException` 捕获后做 `classify_provider_error` 再 re-raise。`asyncio.CancelledError` 是 `BaseException` 子类——捕获它并做分类工作违反 asyncio 取消语义。
- **修复**：改 `except Exception`。

### R4. 沙箱绕过中断吞异常无日志

- **文件**：`agent/middlewares/humanInTheLoop/core.py:344-350`
- **问题**：`_sandbox_bypass_interrupt` catch `Exception` 后返回 deny `ToolMessage`，无任何 `logger` 调用。同文件 `clarify`（:186-188）正确 `logger.exception`。调试不可见。
- **修复**：加 `logger.exception("sandbox-bypass interrupt failed")`。

### R5. `list()` 同步桥泄漏 async generator

- **文件**：`agent/checkpointer/thread_safe_checkpointer.py:162-171`
- **问题**：中途异常时 `aiter_` 未 `aclose()` 即 `loop.close()`，async generator 资源绑在已关闭的 loop 上。
- **修复**：`finally` 中 `loop.run_until_complete(aiter_.aclose())` 再 `loop.close()`。

### R6. 并行工具执行下 guardrail 状态竞态

- **文件**：`agent/middlewares/tool_guardrails/core.py:111-115, 344-382`
- **问题**：`_get_state` 返回 `state_register_mem` 的对象引用，`_wrap_tool_call_impl` 直接 mutate（`records.append`、count 更新、`blocked_tools.add`）再 `_save_state`。若 ToolNode 并行执行多个 tool call，两个 `awrap_tool_call` 无锁 mutate 同一 `gs` 对象——计数可丢/双计，误触 BLOCK/HALT。
- **修复**：copy-on-read 或序列化 mutation 段。

### R7. `web_search.py` monkeypatch 私有方法

- **文件**：`agent/tools/web_search.py:43,78`
- **问题**：`base._arun = _arun_with_retry` 覆盖 `TavilySearch._arun`。若 `langchain_tavily` 改名 `_arun` 或改分派入口，retry/timeout 静默失效——无错误，只是不再重试。
- **修复**：在公共 `arun` 边界 wrap，或子类化而非 patch 私有名。

### R8. 会话连续性保存失败零日志

- **文件**：`server/DAO/messages.py:50`
- **问题**：`except Exception: pass  # noqa: S110`——完全静默，连 debug 都没有。
- **修复**：加 `logger.debug(..., exc_info=True)`。

### R9. `get_pending_interrupt` 真实失败只记 DEBUG 返回 None

- **文件**：server/service/messages.py:594-596`
- **问题**：graph-state 读失败日志在 DEBUG 并返回 None → 客户端永远拿不到 HITL 审批对话框，turn 静默结束。
- **修复**：非 `KeyError` 场景提到 `warning`。

### R10. `CallbackExecutor.create_task` 静默丢异常

- **文件**：`runtime/session/_callback_executor.py:92-98`
- **问题**：`_wrapped` 只 catch `CancelledError`；其他异常设在 un-awaited task 上，只触发 asyncio "Task exception was never retrieved" 警告，无 `logger.exception`。同文件 `run_coroutine`（:60-66）正确记日志——不一致。
- **修复**：加 `except Exception: logger.exception(...)`。

### R11. `WSPushChannel._sender` 吞全部发送错误

- **文件**：`server/trigger/ws/push_channel.py:74-75`
- **问题**：`except Exception: return` 无日志。`serve()` 的 `except (WebSocketDisconnect, ConnectionResetError, Exception)` 前两个被子类 `Exception` dead-code 化。
- **修复**：sender 退出记 debug；外层 except 删冗余异常名。

### R12. 内部异常文本回显给 WS 客户端

- **文件**：`server/service/stream_driver.py:176`、`server/trigger/ws/messages.py:380`
- **问题**：`str(e)` 作为 error frame content 发给客户端。`server/trigger/core.py:100` 已有更好的策略（非 ValueError 只给类名）。
- **修复**：发通用消息 + 服务端记全量。

### R13. `build_thinking_floor_kwargs` 前缀检查范围过大

- **文件**：`models/LLMs/reasoning_payload.py:312`
- **问题**：文档说只对"无法禁用 thinking 的 always-think 模型"（`_ALWAYS_THINK_PREFIXES = ("glm-5",)`）返回 floor payload，但实际检查 `_ZHIPU_REASONING_PREFIXES = ("glm-4.5", "glm-4.6", "glm-5")`。glm-4.5/4.6 可以禁用 thinking 但会被错误返回 floor。当前路径安全（`thinking_control_mode` 只对 glm-5 返回 `"levels"`），但函数内部不自洽。
- **修复**：:312 改测 `_ALWAYS_THINK_PREFIXES`。

### R14. `embed_model` 缺配置时 `sys.exit(1)` 杀进程

- **文件**：`models/embed_model/core.py:14-16,37-41`
- **问题**：remote embedding 模式下缺 `EMBEDDING_API_BASE`/`API_KEY` 等 → `sys.exit(1)`。从库模块直接杀进程，首次 embed 调用时触发——一个配置笔误就杀整个服务器。
- **修复**：改 `raise ValueError/RuntimeError`，让调用方处理。

### R15. 本地模型路径依赖未验证的 HuggingFace repo

- **文件**：`models/LLMs/auxiliary_llm/core.py:102-104`、`models/ITTT_model/core.py:90-92`、`models/VTTT_model/core.py:84-86`
- **问题**：三处本地 fallback 都下载 `lmstudio-community/Qwen3.5-9B-GGUF`。Qwen 官方无"3.5-9B"命名（Qwen2.5 止于 72B，Qwen3 有 8B 无 9B）。若 repo 不存在，所有本地辅助/ITTT/VTTT 路径首次使用时 404。**未能从本环境验证 repo 是否存在**（huggingface.co 不可达）。
- **修复**：确认 repo 和文件名正确；若意图是其他 Qwen 发布版，三处一起改。

---

## SMELL / DEAD — 建议清理（10 项）

### S1. "loop-keyed cache" 注释与单条目实现不符

- **文件**：`agent/core.py:107-123`（注释）vs `124-125`（代码）
- **问题**：注释描述"per-loop dict"，实现是单个 `_agent`/`_agent_loop` 对——来回切 loop 每次 rebuild。
- **修复**：对齐注释，或实现 per-loop dict。

### S2. `write_file` 异常路径格式不一致 + 跳过 evidence 标记

- **文件**：`agent/tools/file_tools/write_file.py:282-283`
- **问题**：`except Exception: return "Error: " + safe_error_detail(e)` 返回纯字符串，其余路径都返回 `json.dumps({"error":...})`。且在 `mark_evidence_stale`（:285）之前 → 异常时跳过 evidence 失效标记。`patch_file` 无此 catch-all——两个文件工具不一致。
- **修复**：统一 JSON 格式；确保 `mark_evidence_stale` 在所有路径执行。

### S3. `Literal["absent"]` 在 `str | None` 联合中被擦除

- **文件**：`agent/tools/file_tools/write_file.py:126`
- **问题**：`-> str | None | Literal["absent"]`——`Literal["absent"] <: str`，联合塌缩为 `str | None`，静态分析无法 narrow。
- **修复**：删 `Literal["absent"]`，或改 enum。

### S4. `web_search.py` 导入时副作用

- **文件**：`agent/tools/web_search.py:10,14`
- **问题**：`load_dotenv(override=True)` + `os.getenv("TAVILY_API_KEY")` 在 import 时执行。`agent/core.py:62-67` 已把副作用移出 import 时间——此模块违反约定，且 key 只读一次。
- **修复**：移进 `build_web_search_tool`。

### S5. 吞异常无日志

- **文件**：`agent/middlewares/task_intent/core.py:271`（`except Exception: return False` 无日志）、`agent/tools/terminal.py:308`（`except Exception as e: return f"Error: {e}"` 无日志）
- **修复**：加 `logger.debug`/`logger.exception`。

### S6. 死配置键：`curator.prune_builtins`

- **文件**：`config/sherry_settings.py:57,77`
- **问题**：定义了、UI 暴露了、但无任何代码调 `get_sherry_setting("curator.prune_builtins")`。curator 的 `_archive_pruned_skills` 无条件 prune。
- **修复**：接进 `_archive_pruned_skills`（False 时跳过 builtin），或删键。

### S7. 死配置键：`SUBAGENT_TODO_DONE_FUNC`

- **文件**：`config/sherry_settings.py:42`、`config/features/infra_side/server_http.py:113`
- **问题**：定义了、env 配置对话框暴露了、但无代码消费。"done func = archive" 行为未实现。
- **修复**：实现行为或删键 + `env_split_out_keys` 条目。

### S8. 无效三元表达式

- **文件**：`config/path.py:92`
- **问题**：`candidates = [requested] if requested != DEFAULT else [requested]`——两分支相同，条件死。`requested == default` 时列表有重复元素。
- **修复**：改为真正的 dedup 逻辑。

### S9. `int(os.getenv())` 在 import 时崩

- **文件**：`models/LLMs/main_llm.py:28-29`、`models/LLMs/reasoner_llm.py:16-18`
- **问题**：非数字值（如 `"128K"`）在 `import models.LLMs.main_llm` 时 `ValueError`，先于 `assert_max_token_valid` 的清晰错误。`auxiliary_llm/core.py:71-72` 在工厂函数内解析——只在首次使用时触发。
- **修复**：try/except 重抛 `TokenGuardError`，或像 auxiliary_llm 一样延迟到工厂函数。

### S10. DAO 层向上依赖 agent + runtime

- **文件**：`server/DAO/messages.py:9-10`
- **问题**：`from runtime import clear_all_register_sessions` + `from agent.checkpointer... import delete_thread_history`——DAO（应是最底层）向上依赖。`clear_session` 在做编排（调 context_engine、agent.checkpointer、runtime）而非纯数据访问。不违反显式禁令（禁的是 agent→server），但层级反转。
- **修复**：把 `clear_session` 编排逻辑移到 service 层，DAO 只做纯数据操作。

---

## 已确认无问题（不需修改）

| 维度 | 结论 |
|---|---|
| 导入禁令 | agent↛server、config↛models、context_engine↛agent、workspace↛agent/context_engine、skills↛server——全部零违反 |
| SQL 注入 | 所有 `execute` 用 `?` 占位符，f-string 只插模块级常量 |
| 裸 except | 无；所有 `except Exception` 大多有 `logger.exception`/`logger.warning` |
| SSRF | `safe_fetch.py` DNS 钉定 + 逐跳复查 |
| 路径穿越 | `is_safe_session_id` + `resolve_within` + `O_NOFOLLOW` |
| 认证 | `secrets.compare_digest` + JWT 黑名单 + WS ticket |
| 资源管理 | 文件 `with`、atomic_write 清 temp、WS `finally` cancel+unregister |
| TODO/FIXME | 项目自有代码零残留（vendored LightRAG 的 6 个是上游的） |
| 死代码/未用导入 | 无显著项 |
| checkpointer 逻辑 | DCL 单例 + 序列化连接 + self-heal schema 正确 |
| token guard | `assert_max_token_valid` 已接通 core/server/spawn 四处 |
