# 路径解析重构：改名 + 外部文件 HITL 审批

> 日期: 2026-09-09
> 状态: 未实施
> 前置: AUDIT_REPORT.md #1 已修复（`resolve_path` 已加 `is_relative_to(ROOT_DIR)` 钳制）
> 关联: AUDIT_REPORT.md #1/#5（resolve_path 越界防护）

---

## 1. 背景

### 1.1 当前状态

`agent/tools/pub_base/path_utils.py` 的 `resolve_path` 函数已加 ROOT_DIR 越界防护（`is_relative_to` 检查 + `PathOutOfBoundsError`），审计报告 #1 漏洞已闭合。但存在两个遗留问题：

### 1.2 问题一：函数名误导

`resolve_path` 只表达了"解析路径"，没体现"强制钳制在 ROOT_DIR 内"的语义。

### 1.3 问题二：无外部文件合法访问机制

所有 LLM 文件工具（`read_file`、`patch_file`、`search_files`、`write_file`）都约束在 ROOT_DIR 内。但存在合理场景需要读写外部文件（读取 `~/.config/` 配置、导出到指定路径等）。当前要么做不到，要么绕过安全检查写裸 `Path().resolve()`。

---

## 2. 方案概览

| 改动                                             | 目的                           |
| ------------------------------------------------ | ------------------------------ |
| **改名 `resolve_path` → `resolve_project_path`** | 准确表达"项目内路径"语义       |
| **新增 `resolve_external_path` + HITL 审批门**   | 合法外部文件访问经人工审批放行 |

**适用范围**：`agent/tools/file_tools/` 下全部 4 个工具（`read_file`、`patch_file`、`search_files`、`write_file`）。

### 用户决策

| 决策           | 选择                                                         |
| -------------- | ------------------------------------------------------------ |
| YOLO 实施      | 直接 Phase 2 — 客户端+后端一次改完，审批弹窗三按钮           |
| write_file     | 一起改 — override `_core`，禁用 LangChain 内置 root_dir 检查 |
| allowlist 粒度 | 精确文件路径 — 不做目录级继承                                |

---

## 3. 改动一：改名 `resolve_path` → `resolve_project_path`

### 3.1 改动范围

| 文件                                        | 改动                        |
| ------------------------------------------- | --------------------------- |
| `agent/tools/pub_base/path_utils.py`        | 函数重命名 + docstring 更新 |
| `agent/tools/pub_base/__init__.py`          | 导出名 + `__all__` 更新     |
| `agent/tools/file_tools/read_file.py:7`     | import + 调用处             |
| `agent/tools/file_tools/patch_file.py:18`   | import + 调用处             |
| `agent/tools/file_tools/search_files.py:21` | import + 调用处             |

### 3.2 改名后代码

```python
def resolve_project_path(file_path: str) -> Path:
    """Resolve file_path against ROOT_DIR; reject paths escaping the project.

    Relative paths are joined onto ROOT_DIR; ~ is expanded. The result is
    guaranteed to be ROOT_DIR or a descendant thereof — absolute paths that
    resolve outside are rejected via :class:`PathOutOfBoundsError`.

    For paths that legitimately need to reach outside ROOT_DIR, use
    :func:`resolve_external_path` instead.
    """
    p = Path(os.path.expanduser(file_path))
    if not p.is_absolute():
        p = ROOT_DIR / p
    resolved = p.resolve()
    if resolved != ROOT_DIR and not resolved.is_relative_to(ROOT_DIR):
        raise PathOutOfBoundsError(
            f"Path resolves outside project root and is not allowed: {resolved} (root={ROOT_DIR})"
        )
    return resolved
```

### 3.3 向后兼容

保留旧名作为 deprecated alias，一个版本周期后移除：

```python
def resolve_path(file_path: str) -> Path:
    """Deprecated alias for resolve_project_path."""
    import warnings
    warnings.warn(
        "resolve_path is deprecated; use resolve_project_path",
        DeprecationWarning,
        stacklevel=2,
    )
    return resolve_project_path(file_path)
```

---

## 4. 改动二：`resolve_external_path` + HITL 审批门

### 4.1 授权模型

| 授权级别         | 存储                                 | 生命周期                | 效果                                       |
| ---------------- | ------------------------------------ | ----------------------- | ------------------------------------------ |
| **Session 授权** | `state_register_mem`（内存）         | 进程重启 / 新会话即失效 | 精确路径授权，主 agent + 所有子 agent 沿用 |
| **YOLO**         | `state_register_db`（持久化 SQLite） | 永久生效                | 所有外部路径一律放行                       |

### 4.2 核心规则

1. **只授权一次**：路径首次被请求时弹 HITL interrupt，用户 approve 后写入 session 级 allowlist（**精确文件路径**，不做目录级继承）。此后同一精确路径被主 agent 或任何子 agent 再次访问时，查 allowlist 命中即放行，**不再弹审批**。
2. **子 agent 沿用**：子 agent 通过 `requester_session_key`（父会话 session_id）查 allowlist，与主 agent 共享同一份授权列表。
3. **刷新即失效**：Session 级 allowlist 存在 `state_register_mem`（进程内存），服务重启 / 新会话即清空。
4. **YOLO 一次授权处处生效**：用户在审批弹窗中选择 YOLO → 写入 `state_register_db`（持久化），此后所有外部路径一律放行，跨重启永久生效。
5. **子 agent 首次访问未授权路径**：子 agent 无法自行弹 interrupt（经 `ainvoke` 执行），返回 `PathOutOfBoundsError` → 工具返回 JSON error → 主 agent 看到提示 → 主 agent 侧补走 HITL 审批。

### 4.3 授权流程图

```
file_tool (read_file / patch_file / search_files / write_file)
  │
  ├─ resolve_project_path(file_path)
  │   ├─ 在 ROOT_DIR 内 → 返回 Path，正常执行
  │   └─ 越界 → PathOutOfBoundsError → 进入 external 路径 ↓
  │
  ├─ resolve_external_path(file_path, session_id, action_desc)
  │   │
  │   ├─ 1. 路径在 ROOT_DIR 内 → 直接返回（安全快速路径）
  │   │
  │   ├─ 2. 查 YOLO 标志 (state_register_db["__global__"]["external_path_yolo"])
  │   │   └─ True → 直接返回 resolved Path
  │   │
  │   ├─ 3. 查 session allowlist (state_register_mem, 查自身+父会话+全局)
  │   │   ├─ 精确路径命中 → 直接返回
  │   │   └─ 未命中 ↓
  │   │
  │   ├─ 4. 判断当前是否为子 agent
  │   │   ├─ 是子 agent → 抛 PathOutOfBoundsError("外部路径未授权，需主会话先审批")
  │   │   └─ 是主 agent → 触发 HITL interrupt ↓
  │   │
  │   ├─ 5. HITL interrupt（三选一）
  │   │   ├─ "允许（本次会话）"  → {"type": "approve"}
  │   │   │   → 写入 state_register_mem[session_id]["external_path_allowlist"] = [精确路径]
  │   │   │   → 返回 resolved Path
  │   │   ├─ "永久允许（YOLO）" → {"type": "yolo"}
  │   │   │   → 写入 state_register_db["__global__"]["external_path_yolo"] = True
  │   │   │   → 返回 resolved Path
  │   │   └─ "拒绝"              → {"type": "reject"}
  │   │       → 抛 PathOutOfBoundsError → 工具返回 JSON error
```

### 4.4 新增函数

在 `agent/tools/pub_base/path_utils.py` 新增：

```python
# ── State keys ──────────────────────────────────────────────────────

_GLOBAL_SESSION = "__global__"
_YOLO_KEY = "external_path_yolo"
_ALLOWLIST_KEY = "external_path_allowlist"


def _extract_session_id(run_manager) -> str:
    """Extract session_id from CallbackManagerForToolRun config.

    Returns empty string when unavailable — callers must treat empty
    as fail-closed (no HITL approval possible).
    """
    config = getattr(run_manager, "config", None) or {}
    configurable = config.get("configurable", {})
    return configurable.get("session_id", "")


def _is_yolo() -> bool:
    """Check persistent YOLO flag from state_register_db."""
    from runtime import state_register_db
    return bool(state_register_db.get_state(_GLOBAL_SESSION, _YOLO_KEY, False))


def _check_allowlist(resolved: Path, session_id: str) -> bool:
    """Check if resolved path is in the session-level allowlist (exact match).

    Checks session_id (self), requester_session_key (parent), and global.
    Exact match only — no directory-level inheritance.
    """
    from runtime import state_register_mem

    for sid in _candidate_session_ids(session_id):
        entries = state_register_mem.get_state(sid, _ALLOWLIST_KEY, [])
        if not entries:
            continue
        for entry in entries:
            if str(resolved) == entry:
                return True
    return False


def _candidate_session_ids(session_id: str) -> list[str]:
    """Return [session_id, requester_session_key, _GLOBAL_SESSION].

    The requester_session_key is looked up from state_register_mem under
    the child's own session_id. Subagents inherit the parent's allowlist.
    """
    from runtime import state_register_mem

    ids = [session_id, _GLOBAL_SESSION]
    requester = state_register_mem.get_state(session_id, "requester_session_key", "")
    if requester and requester not in ids:
        ids.insert(1, requester)
    return ids


def _add_to_allowlist(resolved: Path, session_id: str) -> None:
    """Add exact resolved path to the session allowlist (no parent dir)."""
    from runtime import state_register_mem

    entries = state_register_mem.get_state(session_id, _ALLOWLIST_KEY, [])
    path_str = str(resolved)
    if path_str not in entries:
        entries.append(path_str)
        state_register_mem.set_state(session_id, _ALLOWLIST_KEY, entries)


def _is_subagent(session_id: str) -> bool:
    """Check if the current session is a subagent by looking up caller_scope."""
    from runtime import state_register_mem

    if not session_id:
        return False
    scope = state_register_mem.get_state(session_id, "caller_scope", "main")
    return scope == "subagent"


def resolve_external_path(
    file_path: str,
    *,
    session_id: str,
    action_desc: str = "",
) -> Path:
    """Resolve a path that may be outside ROOT_DIR, gated by HITL approval.

    Checks (in order):
    1. Inside ROOT_DIR → return directly (safe path)
    2. YOLO flag (state_register_db) → return
    3. Session allowlist (state_register_mem, exact match) → return
    4. Subagent without prior auth → deny (cannot self-approve)
    5. Main agent → HITL interrupt (approve / yolo / reject)

    Args:
        file_path: The path to resolve (relative, absolute, or ~).
        session_id: Current session ID (main or child).
        action_desc: Optional description for the approval prompt.

    Raises:
        PathOutOfBoundsError: If denied by user or subagent without prior auth.
    """
    p = Path(os.path.expanduser(file_path))
    if not p.is_absolute():
        p = ROOT_DIR / p
    resolved = p.resolve()

    # 1. Inside ROOT_DIR — safe path, no approval
    if resolved == ROOT_DIR or resolved.is_relative_to(ROOT_DIR):
        return resolved

    # 2. YOLO — persistent global allow-all
    if _is_yolo():
        return resolved

    # 3. Session allowlist — exact match, inherited by subagents
    if _check_allowlist(resolved, session_id):
        return resolved

    # 4. Subagent without prior authorization — cannot self-approve
    if _is_subagent(session_id):
        raise PathOutOfBoundsError(
            f"External path not authorized for subagent: {resolved}. "
            f"Approve this path from the main session first."
        )

    # 5. Main session — trigger HITL interrupt
    from langchain.agents.middleware.human_in_the_loop import (
        ActionRequest, HITLRequest, ReviewConfig,
    )
    from langgraph.types import interrupt

    action_request = ActionRequest(
        name="external_file_access",
        args={"path": str(resolved)},
        description=(
            f"外部文件访问审批\n"
            f"  路径: {resolved}\n"
            f"  项目根: {ROOT_DIR}\n"
            f"  意图: {action_desc or '未指定'}\n\n"
            f"选项:\n"
            f"  approve — 允许（本次会话有效，子 agent 沿用）\n"
            f"  yolo    — 永久允许所有外部路径（不再弹窗）\n"
            f"  reject  — 拒绝"
        ),
    )
    review_config = ReviewConfig(
        action_name="external_file_access",
        allowed_decisions=["approve", "yolo", "reject"],
    )

    hitl_response = interrupt(HITLRequest(
        action_requests=[action_request],
        review_configs=[review_config],
    ))

    decisions = hitl_response.get("decisions", [])
    if not decisions:
        raise PathOutOfBoundsError(f"External access denied (no decision): {resolved}")

    decision_type = decisions[0].get("type", "")

    if decision_type == "approve":
        # Session-scoped: add exact path to allowlist
        _add_to_allowlist(resolved, session_id)
        return resolved

    if decision_type == "yolo":
        # Persistent global allow-all
        from runtime import state_register_db
        state_register_db.set_state(_GLOBAL_SESSION, _YOLO_KEY, True)
        return resolved

    # Reject
    msg = decisions[0].get("message", "Rejected by user")
    raise PathOutOfBoundsError(f"External file access denied: {resolved} ({msg})")
```

### 4.5 子代理授权继承机制

```
主 agent (session_id = "s1")
  │
  ├─ read_file("~/configs/app.conf")
  │   └─ resolve_external_path(..., session_id="s1")
  │       └─ HITL interrupt → 用户 approve
  │           → state_register_mem["s1"]["external_path_allowlist"]
  │             = ["/home/user/configs/app.conf"]   ← 精确路径
  │
  ├─ spawn subagent (child_session_key = "agent:coder:subagent:uuid-1")
  │   └─ spawn 时写入:
  │      state_register_mem[child]["requester_session_key"] = "s1"
  │      state_register_mem[child]["caller_scope"] = "subagent"
  │
  └─ 子 agent read_file("~/configs/app.conf")
      └─ resolve_external_path(..., session_id="agent:coder:subagent:uuid-1")
          ├─ _is_subagent() → True
          ├─ _check_allowlist():
          │   _candidate_session_ids = [
          │     "agent:coder:subagent:uuid-1",  # 自身
          │     "s1",                            # requester_session_key（父会话）
          │     "__global__"                     # 全局
          │   ]
          │   → 查 state_register_mem["s1"]["external_path_allowlist"]
          │   → 精确匹配 "/home/user/configs/app.conf" → 放行 ✓
          └─ 返回 resolved Path，不弹审批

  若子 agent 访问 "~/configs/other.conf"（同目录不同文件）:
      └─ 精确匹配未命中（只授权了 app.conf）→ 抛 PathOutOfBoundsError
         → 主 agent 看到提示 → 主 agent 补走审批 → 新路径写入 allowlist
```

### 4.6 spawn 侧改动

在 `agent/tools/subagent/spawn/core.py` 的 `child_agent.ainvoke` 调用前，新增：

```python
state_register_mem.set_state(
    run.child_session_key, "requester_session_key", requester_session_key
)
state_register_mem.set_state(
    run.child_session_key, "caller_scope", "subagent"
)
```

---

## 5. 改动三：write_file.py 改造

`FormattedWriteFileTool` 继承 LangChain `WriteFileTool`，有自己的 `root_dir` 约束（只管相对路径，绝对路径可越界——审计报告 #5 提到过）。

### 5.1 改造方案

1. 构造时设置 `root_dir="/"`（禁用 LangChain 内置检查）
2. `_core` 前置 `resolve_project_path` / `resolve_external_path` 校验
3. `_run`/`_arun` 提取 `session_id` 并传入 `_core`

```python
class FormattedWriteFileTool(WriteFileTool):
    def _core(
        self,
        file_path: str,
        text: str,
        append: bool = False,
        session_id: str = "",
    ) -> str:
        # Path validation (replaces LangChain's weak root_dir check)
        try:
            resolved = resolve_project_path(file_path)
        except PathOutOfBoundsError:
            try:
                resolved = resolve_external_path(
                    file_path, session_id=session_id, action_desc="write file"
                )
            except PathOutOfBoundsError as e:
                return json.dumps({"error": str(e)}, ensure_ascii=False)

        # Proceed with LangChain's write logic using the validated path
        is_py = resolved.suffix == ".py"
        # ... formatting logic ...
        return super()._run(file_path=str(resolved), text=text, append=append)

    @override
    def _run(self, file_path, text, append=False, run_manager=None):
        session_id = _extract_session_id(run_manager)
        return self._core(file_path, text, append, session_id)

    @override
    async def _arun(self, file_path, text, append=False, run_manager=None):
        session_id = _extract_session_id(run_manager)
        return await asyncio.to_thread(self._core, file_path, text, append, session_id)


def build_write_file_tool() -> WriteFileTool:
    tool = FormattedWriteFileTool(root_dir="/")  # disable LangChain check
    tool.handle_tool_error = True
    tool.name = "write_file"
    tool.metadata = {"idempotent": False}
    return tool
```

---

## 6. file_tools 接入模式

三个走 `pub_base` 的工具（`read_file`、`patch_file`、`search_files`）按同一模式接入。以 `read_file` 为例：

```python
from agent.tools.pub_base import (
    resolve_project_path, resolve_external_path, PathOutOfBoundsError,
    _extract_session_id,
)

def _core(self, file_path: str, offset: int = 1, limit: int = 500,
          session_id: str = "") -> str:
    try:
        resolved = resolve_project_path(file_path)
    except PathOutOfBoundsError:
        try:
            resolved = resolve_external_path(
                file_path, session_id=session_id, action_desc="read file"
            )
        except PathOutOfBoundsError as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    # ... rest of _core (exists / is_dir / read) ...

@override
def _run(self, file_path: str, offset: int = 1, limit: int = 500,
         run_manager: CallbackManagerForToolRun | None = None) -> str:
    session_id = _extract_session_id(run_manager)
    return self._core(file_path, offset, limit, session_id)

@override
async def _arun(self, file_path: str, offset: int = 1, limit: int = 500,
                run_manager: CallbackManagerForToolRun | None = None) -> str:
    session_id = _extract_session_id(run_manager)
    return self._core(file_path, offset, limit, session_id)
```

`patch_file`（action_desc="patch file"）和 `search_files`（action_desc="search directory"）同理。

---

## 7. 前端改动

HITL 审批弹窗增加 YOLO 按钮：

| 现有按钮       | 新增按钮        |
| -------------- | --------------- |
| 允许 (approve) | 永久允许 (yolo) |
| 拒绝 (reject)  | —               |

客户端识别 `ReviewConfig.allowed_decisions` 中的 `"yolo"`，渲染第三个按钮。点击后以 `{"type": "yolo"}` 回复 `Command(resume={"decisions": [{"type": "yolo"}]})`。

---

## 8. 安全边界

| 规则                 | 说明                                                    |
| -------------------- | ------------------------------------------------------- |
| 项目内路径不触发审批 | `resolve_external_path` 先检 `is_relative_to(ROOT_DIR)` |
| fail-closed          | interrupt 不可用时抛异常而非放行                        |
| reject 后不重试      | 返回 `PathOutOfBoundsError` → JSON error                |
| 子代理不能自弹审批   | 经 `ainvoke` 执行，未授权路径直接拒绝                   |
| allowlist 精确匹配   | 只授权具体文件路径，同目录其他文件仍需审批              |
| YOLO 持久化          | `state_register_db`，跨重启永久生效                     |
| Session 级隔离       | allowlist 按 session_id 隔离                            |

---

## 9. 改动文件汇总

| 文件                                     | 改动类型    | 说明                                                                             |
| ---------------------------------------- | ----------- | -------------------------------------------------------------------------------- |
| `agent/tools/pub_base/path_utils.py`     | 改名 + 新增 | `resolve_path` → `resolve_project_path`；新增 `resolve_external_path` + 辅助函数 |
| `agent/tools/pub_base/__init__.py`       | 更新导出    | 更新 import 和 `__all__`                                                         |
| `agent/tools/file_tools/read_file.py`    | 修改        | `_core` 加 `session_id` + external fallback                                      |
| `agent/tools/file_tools/patch_file.py`   | 修改        | 同上                                                                             |
| `agent/tools/file_tools/search_files.py` | 修改        | 同上                                                                             |
| `agent/tools/file_tools/write_file.py`   | 修改        | `root_dir="/"` + `_core` 加路径校验 + `session_id`                               |
| `agent/tools/subagent/spawn/core.py`     | 修改        | spawn 时写入 `requester_session_key` + `caller_scope`                            |
| `client/app/...`（前端 HITL 组件）       | 修改        | 审批弹窗增加 YOLO 按钮                                                           |

---

## 10. 测试集

### 10.1 测试文件结构

```
tests/agent/tools/pub_base/
  test_path_utils.py              ← 新建：resolve_project_path + resolve_external_path 单元测试
tests/agent/tools/file_tools/
  __init__.py                      ← 新建
  test_external_access.py          ← 新建：file_tools + external path 集成测试
  test_subagent_inheritance.py     ← 新建：子 agent 授权继承测试
```

### 10.2 测试 Harness

#### 单元测试 harness（`test_path_utils.py`）

```python
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from agent.tools.pub_base.path_utils import (
    resolve_project_path, resolve_external_path,
    PathOutOfBoundsError, _is_yolo, _check_allowlist,
    _is_subagent, _candidate_session_ids, _add_to_allowlist,
    _extract_session_id,
)
from config import ROOT_DIR

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


@pytest.fixture
def clean_state(monkeypatch):
    """Reset state_register_mem and state_register_db to clean state."""
    from runtime import state_register_mem, state_register_db
    # Clear in-memory state
    monkeypatch.setattr(state_register_mem, "_states", {})
    # state_register_db: use temp file or mock
    monkeypatch.setattr(state_register_db, "get_state",
                        lambda sid, key, default=None: default)
    monkeypatch.setattr(state_register_db, "set_state",
                        lambda sid, key, val: True)
    yield
    monkeypatch.undo()


@pytest.fixture
def mock_interrupt(monkeypatch):
    """Patch langgraph.types.interrupt to return canned response."""
    def _make(response: dict):
        def _interrupt(value):
            return response
        monkeypatch.setattr("langgraph.types.interrupt", _interrupt)
    return _make
```

#### 集成测试 harness（`test_external_access.py`）

复用 `test_hitl_sandbox_bypass.py` 的 `_ScriptedModel` + `create_agent` + `MemorySaver` 模式：

```python
from __future__ import annotations
import json, asyncio
from typing import Any, ClassVar
import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import AgentState
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from config import ROOT_DIR

pytestmark = [pytest.mark.module, pytest.mark.timeout(60)]


class _ScriptedModel(BaseChatModel):
    """Emits one scripted tool call, then idles."""
    calls: ClassVar[int] = 0
    scripted_calls: ClassVar[list[dict[str, Any]]] = []

    @property
    def _llm_type(self) -> str:
        return "stub-external-access"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        type(self).calls += 1
        if type(self).calls == 1 and type(self).scripted_calls:
            msg = AIMessage(content="", tool_calls=list(type(self).scripted_calls))
        else:
            msg = AIMessage(content="Done.")
        return ChatResult(generations=[ChatGeneration(message=msg)])


class _HarnessState(AgentState):
    session_id: str


def _build_graph(scripted_calls, tools):
    _ScriptedModel.calls = 0
    _ScriptedModel.scripted_calls = list(scripted_calls)
    graph = create_agent(
        model=_ScriptedModel(),
        state_schema=_HarnessState,
        checkpointer=MemorySaver(),
        tools=list(tools),
    )
    return graph


def _invoke_and_resume(graph, thread_id, session_id, resume_decision):
    config = {"configurable": {"thread_id": thread_id}}
    graph.invoke(
        {"messages": [HumanMessage(content="read it")], "session_id": session_id},
        config,
    )
    return graph.invoke(Command(resume={"decisions": [resume_decision]}), config)
```

### 10.3 测试矩阵（37 个用例）

#### A. `resolve_project_path` 基本功能（5 个）

| #   | 测试名                 | 输入                          | 期望                                |
| --- | ---------------------- | ----------------------------- | ----------------------------------- |
| A1  | 相对路径               | `"src/main.py"`               | `ROOT_DIR / "src/main.py"` resolved |
| A2  | 绝对路径在 ROOT_DIR 内 | `str(ROOT_DIR / "config.py")` | 同上                                |
| A3  | `~` 展开在 ROOT_DIR 内 | mock expanduser               | resolved Path                       |
| A4  | 绝对路径越界           | `"/etc/passwd"`               | `PathOutOfBoundsError`              |
| A5  | deprecated alias       | 调用 `resolve_path`           | DeprecationWarning + 正确结果       |

#### B. 安全快速路径（3 个）

| #   | 测试名                    | 输入                       | 期望                 |
| --- | ------------------------- | -------------------------- | -------------------- |
| B1  | ROOT_DIR 内路径不触发检查 | `"src/main.py"`            | 直接返回，不查 state |
| B2  | ROOT_DIR 边界路径         | `str(ROOT_DIR)`            | 直接返回             |
| B3  | ROOT_DIR 子目录           | `str(ROOT_DIR / "subdir")` | 直接返回             |

#### C. YOLO 机制（5 个）

| #   | 测试名                | 前置                             | 输入          | 期望                                    |
| --- | --------------------- | -------------------------------- | ------------- | --------------------------------------- |
| C1  | YOLO 已开启           | `state_register_db` YOLO=True    | 外部路径      | 直接返回                                |
| C2  | YOLO 未开启           | `state_register_db` YOLO=False   | 外部路径      | 不返回，进入 allowlist 检查             |
| C3  | YOLO 选择             | interrupt 回复 `{"type":"yolo"}` | 外部路径      | 写 `state_register_db` YOLO=True + 返回 |
| C4  | YOLO 跨重启           | `state_register_db` 持久化       | 新进程读 YOLO | True                                    |
| C5  | YOLO 写入后后续不弹窗 | C3 后再访问其他外部路径          | 另一外部路径  | 直接返回                                |

#### D. Session allowlist 精确匹配（7 个）

| #   | 测试名                 | 前置                                   | 输入                              | 期望                     |
| --- | ---------------------- | -------------------------------------- | --------------------------------- | ------------------------ |
| D1  | 空白名单               | allowlist=[]                           | 外部路径                          | 进入 interrupt           |
| D2  | 精确路径命中           | allowlist=["/home/u/configs/app.conf"] | 同路径                            | 直接返回                 |
| D3  | 同目录不同文件不命中   | allowlist=["/home/u/configs/app.conf"] | `/home/u/configs/other.conf`      | 进入 interrupt           |
| D4  | 不相关路径未命中       | allowlist=["/home/u/configs/app.conf"] | `/home/u/other/file`              | 进入 interrupt           |
| D5  | approve 后写入精确路径 | interrupt approve                      | `/home/u/docs/report.md`          | allowlist 含精确路径     |
| D6  | 同文件二次访问不弹窗   | D5 后再次访问同路径                    | 同路径                            | 直接返回                 |
| D7  | search_files 外部目录  | approve `/home/u/docs`                 | search_files(path="/home/u/docs") | 直接返回（精确匹配目录） |

#### E. 子 agent 授权继承（5 个）

| #   | 测试名                        | 前置                                                        | 输入     | 期望                                           |
| --- | ----------------------------- | ----------------------------------------------------------- | -------- | ---------------------------------------------- |
| E1  | 子 agent + 父 allowlist 命中  | 父 session 有 allowlist，子 `requester_session_key` 指向父  | 同路径   | 直接返回                                       |
| E2  | 子 agent + 无授权             | 父 allowlist 空                                             | 外部路径 | `PathOutOfBoundsError`                         |
| E3  | 子 agent + YOLO               | `state_register_db` YOLO=True                               | 外部路径 | 直接返回                                       |
| E4  | `caller_scope` 识别           | `state_register_mem[session_id]["caller_scope"]="subagent"` | —        | `_is_subagent()` 返回 True                     |
| E5  | `_candidate_session_ids` 回溯 | 子 session 有 `requester_session_key="parent_s1"`           | —        | 返回 `["child_s1", "parent_s1", "__global__"]` |

#### F. HITL interrupt 行为（5 个）

| #   | 测试名               | 前置                           | 输入     | 期望                                      |
| --- | -------------------- | ------------------------------ | -------- | ----------------------------------------- |
| F1  | 主 agent approve     | interrupt `{"type":"approve"}` | 外部路径 | 返回 Path + allowlist 更新                |
| F2  | 主 agent YOLO        | interrupt `{"type":"yolo"}`    | 外部路径 | 返回 Path + `state_register_db` YOLO=True |
| F3  | 主 agent reject      | interrupt `{"type":"reject"}`  | 外部路径 | `PathOutOfBoundsError`                    |
| F4  | 主 agent no decision | interrupt `{}`                 | 外部路径 | `PathOutOfBoundsError`                    |
| F5  | interrupt 不可用     | 无 langgraph                   | 外部路径 | fail-closed 抛异常                        |

#### G. file_tools 集成（4 个）

| #   | 测试名                     | 场景                                               | 期望                   |
| --- | -------------------------- | -------------------------------------------------- | ---------------------- |
| G1  | read_file external approve | `read_file("~/configs/app.conf")` → approve        | 返回文件内容 JSON      |
| G2  | patch_file external        | `patch_file("~/output/result.txt", ...)` → approve | 返回 patch 结果        |
| G3  | write_file external        | `write_file("~/output/new.txt", ...)` → approve    | 返回写入成功           |
| G4  | write_file root_dir 禁用   | `write_file("/etc/passwd", ...)` 无 approve        | `PathOutOfBoundsError` |

#### H. 子 agent 集成（3 个）

| #   | 测试名                           | 场景                                         | 期望                                            |
| --- | -------------------------------- | -------------------------------------------- | ----------------------------------------------- |
| H1  | 子 agent 继承授权                | 父会话已 approve → 子 agent read_file 同路径 | 直接返回，不弹 interrupt                        |
| H2  | 子 agent 未授权拒绝              | 父会话未授权 → 子 agent read_file 外部路径   | JSON error "需主会话先授权"                     |
| H3  | spawn 写入 requester_session_key | spawn 后检查 state_register_mem              | `requester_session_key` + `caller_scope` 已写入 |

---

## 11. 灵活调整机制

| 调整点                      | 方式                                                                      | 影响                        |
| --------------------------- | ------------------------------------------------------------------------- | --------------------------- |
| allowlist 粒度              | 改 `_add_to_allowlist` 存父目录 + `_check_allowlist` 用 `is_relative_to`  | 切换为目录级授权            |
| YOLO 存储                   | 改 `_is_yolo` 读 env var 或 config                                        | 切换 YOLO 触发方式          |
| 子 agent 自弹审批           | 改 `_is_subagent` 返回 False                                              | 子 agent 也能触发 interrupt |
| session_id 获取             | 改 `_extract_session_id` 从 thread-local 或其他来源                       | 不依赖 run_manager          |
| interrupt mock 与真实不一致 | 集成测试切换为真实 `create_agent` + `MemorySaver` + `Command(resume=...)` | 更真实的 E2E                |
| state_register_db 测试污染  | 用临时 SQLite 文件 `tmp_path` 替换 `db_path`                              | 不影响生产 DB               |

每一步都是独立函数，可单独 mock/替换/调整，不影响主流程。

---

## 12. 已知局限

| 局限                             | 影响                                   | 缓解                                |
| -------------------------------- | -------------------------------------- | ----------------------------------- |
| 精确路径授权较繁琐               | 同目录每个文件都需独立审批             | 设计意图——用户选择的安全性优先      |
| `~` 展开后可能指向任意位置       | 用户 home 下有敏感文件                 | 审批 prompt 展示完整 resolved path  |
| 无文件类型白名单                 | 可审批读取任何文件                     | 设计意图——由人判断                  |
| 子 agent 首次访问需主 agent 介入 | 子 agent 不能自弹审批                  | 主 agent error 消息明确提示         |
| 流式路径下 interrupt 时机        | `astream` 中 interrupt 暂停整个 stream | 与现有 sandbox 审批一致             |
| YOLO 无细粒度控制                | 全局开关                               | 设计意图——YOLO 是"信任所有外部访问" |
