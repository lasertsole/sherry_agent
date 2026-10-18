# 🧵 Tool Parallel Safety

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> LangGraph's `ToolNode` starts every tool call in one model batch at once. Sherry replaces it with a batch-aware node and two resource-lock layers, so calls that share a file are ordered while independent calls stay parallel.

## The Problem

A model can return several tool calls in one response. ToolNode gathers them with `asyncio.gather`, which is correct for independent reads and dangerous for two writes, a read racing a write, or a shell command racing another agent's shell command. An all-or-nothing rule (serialize the whole batch if any tool writes) is safe but throws away parallelism for unrelated files. The design therefore needs a resource key, not just a boolean.

## Static Classification

`agent/tools/parallel_safety.py` owns the decision:

- `READ_ONLY_TOOLS` lists tools with no side effects.
- A tool's metadata may add `concurrent_safe` (explicitly safe) or `side_effect_scope` (`none`, `workspace`, `system`, `session`, `user_interaction`, ...).
- Unknown tools and missing metadata are unsafe.
- `destructive` overrides every allow declaration.
- `idempotent` is deliberately **not** used: `memory` is idempotent but writes session state, while `sessions_spawn` is non-idempotent but can run concurrently.

## Batch Plan and Ordering

For every ToolNode batch, `plan_batch` produces groups:

- `read_file`, `write_file`, `patch_file` are file operations keyed by their canonical `file_path`.
- A path group with a write runs in the model's `tool_calls` order; a group of reads may run concurrently.
- Different paths are independent.
- Any non-file unsafe call (for example `terminal`) makes the whole batch a barrier; its target resource cannot be inferred from arguments.
- A file path that cannot be resolved is also a barrier.

The ordering rule is the model's call order: `read_file(A)` before `write_file(A)` reads the old content and licenses the write; the reverse writes first and the later read sees the new content. Two writes to the same path run in order, and the last one wins.

## File Locks

The batch planner orders conflicts **inside one batch**. The file layer protects concurrent agents and processes:

- `agent/tools/pub_base/path_lock.py` provides `path_read_lock` (shared) and `path_write_lock` (exclusive, writer preferred).
- `agent/tools/pub_base/file_lock.py` adds a shared `flock` (`LOCK_SH`) and keeps the exclusive one (`LOCK_EX`).
- `read_file` takes `file_read_lock`; `write_file` and `patch_file` keep `file_write_lock`.
- The fixed lock order is in-process gate, then cross-process `flock`.

Two readers of one path overlap; a reader and a writer of one path never do; different paths never contend. Across batches/agents the order is lock-acquisition order, not the model's call order.

## Resource Locks

`agent/middlewares/tool_resource_lock.py` covers the two resources a path cannot describe:

- `side_effect_scope: "system"` (`terminal`, `python_repl`, `execute_code`, `ptc`) takes a process-wide lock; `TOOL_PARALLEL["system_lock"]` is OFF by default because it serializes all shell work.
- `side_effect_scope: "user_interaction"` (`question`, `exit_plan_mode`) takes a per-session lock; `interaction_lock` is ON by default so one HITL channel handles one prompt at a time.
- `workspace`, `session` and `network` are not re-locked: file locks, the memory store, SQLite stores and the browser manager own those resources.

## Installation and Scope

`create_agent` has no `tool_node` parameter. `agent/core.py` patches `langchain.agents.factory.ToolNode` once at import, gated by `TOOL_PARALLEL["enabled"]`, so main, subagent and auxiliary graphs all use `SafeToolNode`. The subagent middleware chain also carries `ToolResourceLockMiddleware`. `TOOL_PARALLEL["enforce"] = False` keeps native concurrency and only logs the plan; `True` applies it. A compatibility test pins the private `ToolNode` methods the node depends on, so a LangGraph upgrade fails loudly.

## Limits

- `terminal`, `python_repl`, `execute_code` and `ptc` remain whole-batch barriers: their file targets cannot be reliably parsed from command strings.
- Cross-agent ordering is not guaranteed; only mutual exclusion is. `system_lock` is the explicit exception when enabled.
- The batch planner uses the canonical path; a path that cannot be resolved falls back to a barrier rather than guessing.
- The file locks are local-filesystem locks (`flock`); NFS semantics are out of scope.

## Code Map

- `agent/safe_tool_node.py`: `SafeToolNode`, `_afunc` / `_func`, factory installation.
- `agent/tools/parallel_safety.py`: classification, `file_resource_key`, `plan_batch`.
- `agent/tools/pub_base/path_lock.py`: in-process read/write gate.
- `agent/tools/pub_base/file_lock.py`: `file_read_lock` / `file_write_lock`, `flock_path(shared=...)`.
- `agent/middlewares/tool_resource_lock.py`: process-wide and per-session resource locks.
- `agent/tools/file_tools/read_file.py`: the read-lock call site.
- `tests/agent/test_safe_tool_node.py`, `tests/agent/tools/test_parallel_safety.py`, `tests/agent/tools/file_tools/test_file_rw_lock.py`, `tests/agent/middlewares/test_tool_resource_lock.py`: the behavior and invariants above.
