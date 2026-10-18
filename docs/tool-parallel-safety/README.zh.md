# 🧵 工具并行安全

[**English**](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> LangGraph 的 `ToolNode` 会把一次模型回复里的所有工具调用同时启动。Sherry 用批次感知节点加两层资源锁替换它：共享同一文件的调用按顺序执行，互不相关的调用保持并行。

## 问题

模型可以在一次回复里返回多个 tool call。ToolNode 用 `asyncio.gather` 一起执行，对独立读取是正确的，对两个写、读与写竞争、或两个 agent 的 shell 竞争则危险。全有或全无的规则——只要有一个写就整批串行——虽然安全，但会让不相关文件失去并行。因此需要的是资源键，而不是一个布尔值。

## 静态分类

`agent/tools/parallel_safety.py` 负责判定：

- `READ_ONLY_TOOLS` 列出没有副作用的工具。
- 工具 metadata 可以声明 `concurrent_safe`（显式安全）或 `side_effect_scope`（`none`、`workspace`、`system`、`session`、`user_interaction` 等）。
- 未知工具和缺失 metadata 一律视为不安全。
- `destructive` 覆盖任何允许声明。
- 刻意**不使用** `idempotent`：`memory` 幂等但会写会话状态，`sessions_spawn` 非幂等却可以并行。

## 批次计划与顺序

每个 ToolNode 批次由 `plan_batch` 生成执行组：

- `read_file`、`write_file`、`patch_file` 是按 canonical `file_path` 分组的文件操作。
- 组内有写时，按模型 `tool_calls` 顺序串行；全是读的组可以并发。
- 不同 path 互不相关。
- 任何非文件类不安全调用（例如 `terminal`）会让整批成为 barrier；它的目标资源无法从参数推断。
- 无法解析的 file path 也会退回 barrier。

顺序规则就是模型的调用顺序：`read_file(A)` 在 `write_file(A)` 前会读旧内容并给写提供 license；反过来则先写，后面的读看到新内容。同一 path 两个写按顺序执行，最后一个生效。

## 文件锁

批次计划只负责**同一批内**的顺序；文件层保护并发 agent 和进程：

- `agent/tools/pub_base/path_lock.py` 提供 `path_read_lock`（共享）和 `path_write_lock`（独占、写者优先）。
- `agent/tools/pub_base/file_lock.py` 增加共享 `flock`（`LOCK_SH`），保留独占 `LOCK_EX`。
- `read_file` 使用 `file_read_lock`；`write_file` 和 `patch_file` 保持 `file_write_lock`。
- 固定锁顺序是进程内门，然后跨进程 `flock`。

同一 path 两个读可以重叠；同 path 的读和写绝不重叠；不同 path 从不竞争。跨批次/跨 agent 的顺序是锁获取顺序，不是模型调用顺序。

## 资源锁

`agent/middlewares/tool_resource_lock.py` 覆盖 path 描述不了的两个资源：

- `side_effect_scope: "system"`（`terminal`、`python_repl`、`execute_code`、`ptc`）取进程级锁；`TOOL_PARALLEL["system_lock"]` 默认关闭，因为它会把所有 shell 工作串行化。
- `side_effect_scope: "user_interaction"`（`question`、`exit_plan_mode`）取按 session 的锁；`interaction_lock` 默认开启，保证一个 HITL 通道同一时刻只处理一个提示。
- `workspace`、`session`、`network` 不重复加锁：文件锁、memory store、SQLite store 和 browser manager 已经各自负责。

## 安装与作用域

`create_agent` 没有 `tool_node` 参数。`agent/core.py` 在导入时按 `TOOL_PARALLEL["enabled"]` 一次性 patch `langchain.agents.factory.ToolNode`，因此 main、subagent 和辅助 agent 的图都使用 `SafeToolNode`。子代理中间件链也带 `ToolResourceLockMiddleware`。`TOOL_PARALLEL["enforce"] = False` 保持原生并发，只记录计划；`True` 才真正应用。兼容性测试 pin 住该节点依赖的 `ToolNode` 私有方法，LangGraph 升级会直接报错而不是静默失效。

## 限制

- `terminal`、`python_repl`、`execute_code`、`ptc` 仍是整批 barrier：无法可靠地从命令字符串解析文件目标。
- 跨 agent 不承诺顺序，只保证互斥；启用 `system_lock` 是明确的例外。
- 批次计划使用 canonical path；解析失败就退回 barrier，不猜。
- 文件锁是本地文件系统锁（`flock`）；NFS 语义不在范围内。

## 代码位置

- `agent/safe_tool_node.py`：`SafeToolNode`、`_afunc` / `_func`、factory 安装。
- `agent/tools/parallel_safety.py`：分类、`file_resource_key`、`plan_batch`。
- `agent/tools/pub_base/path_lock.py`：进程内读写门。
- `agent/tools/pub_base/file_lock.py`：`file_read_lock` / `file_write_lock`、`flock_path(shared=...)`。
- `agent/middlewares/tool_resource_lock.py`：进程级与按 session 的资源锁。
- `agent/tools/file_tools/read_file.py`：读锁调用点。
- `tests/agent/test_safe_tool_node.py`、`tests/agent/tools/test_parallel_safety.py`、`tests/agent/tools/file_tools/test_file_rw_lock.py`、`tests/agent/middlewares/test_tool_resource_lock.py`：上述行为与不变量。
