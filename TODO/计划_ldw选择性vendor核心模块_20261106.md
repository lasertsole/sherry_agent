# 计划：选择性 vendor ldw 核心 5 模块（剥离 deepagents）

> **目标**：不装 deepagents、不全 fork ldw，只拷 ldw 的 5 个核心模块（`_context`/`_journal`/`_dag`/`_roster`/`_engine`）到 sherry，从中删 deepagents import，让 sherry 能 `import` 后用 `run_workflow` + `Roster`（注册 sherry 自己的 agent 作 leaf）。
>
> **比全 fork 轻**：不拷 `_leaves.py`（deepagents 便利构造器）、`middleware.py`（AgentMiddleware）、`tool.py`（AST gate + run_script）、`__init__.py`（会 import 上述）。只拿核心，自己写 `__init__.py` 导出需要的符号。
>
> **比 stub 稳**：不依赖「ldw 核心路径不调 deepagents」的假设——直接删掉那行 import，显式无 deepagents。
>
> **日期**：2026-11-06

---

## 一、源与目标

**源**（ldw 仓库）：`D:\selfProj\langchain-dynamic-workflow\src\langchain_dynamic_workflow\`

**目标**（sherry）：`vendor/ldw/`（新建包）

```
vendor/ldw/
├── __init__.py          # 新写，只导出核心符号
├── _context.py          # 拷贝（原语：agent/parallel/dag/race/loop_until/batch_map/checkpoint）
├── _journal.py          # 拷贝（SHA-256 缓存 + JournalStore Protocol + InMemoryJournalStore）
├── _dag.py              # 拷贝（DagNode + 拓扑序 Kahn）
├── _roster.py           # 拷贝（Roster.register + runnable_for）
└── _engine.py           # 拷贝 + 改（删 deepagents import）
```

**不拷**：`_leaves.py`（`create_deep_agent` deepagents 构造器）、`middleware.py`（`AgentMiddleware` + deepagents host）、`tool.py`（`create_workflow_tool` + `run_script` AST gate）、原 `__init__.py`（import 上述会触发 deepagents）、`_ast_gate.py`、`_codegen.py`（sherry 用可信编译，不走 run_script）。

## 二、依赖核实（5 核心模块的 import）

| 模块 | 依赖 | sherry 满足？ |
|---|---|---|
| `_context.py` | langgraph（ctx 原语）、langchain（Runnable/HumanMessage/AIMessage） | ✅ |
| `_journal.py` | hashlib、pydantic | ✅ |
| `_dag.py` | 纯 Python（dataclass） | ✅ |
| `_roster.py` | langchain Runnable | ✅ |
| `_engine.py` | langgraph（@entrypoint/@task）、**deepagents（删）** | ✅（删后） |

sherry: langgraph 1.2.20、langchain 1.3.9、pydantic 2.x、python 3.13——满足 ldw 核心版本下限（`>=1.0`/`>=3.12`）。

## 三、改 `_engine.py`（删 deepagents）

探查确认 `_engine.py:15` 是 `from deepagents.backends.protocol import SandboxBackendProtocol`，据称仅 isinstance 用。

**拷贝后核实 + 改**：
1. grep `_engine.py` 里所有 `SandboxBackendProtocol` / `deepagents` 引用
2. 若仅 isinstance 检查（如 `isinstance(x, SandboxBackendProtocol)`）→ 删该行 import + 删/改 isinstance 检查（改为 `False` 或删整个条件分支——sherry 的 agent 不是 SandboxBackendProtocol，检查本就返回 False）
3. 若有实际调用（`SandboxBackendProtocol(...)`）→ 需评估是否核心路径；探查说是「仅 isinstance」，但**拷贝时必须核实**，不能盲信探查

```python
# _engine.py 改前（:15）
from deepagents.backends.protocol import SandboxBackendProtocol

# _engine.py 改后
# （删掉这行。若 isinstance(x, SandboxBackendProtocol) 存在，改为 False 或删该分支）
```

## 四、新写 `__init__.py`（只导出核心）

```python
# vendor/ldw/__init__.py
"""选择性 vendor 的 ldw 核心（无 deepagents）。只暴露 sherry 需要的符号。"""
from ._engine import run_workflow
from ._context import Ctx
from ._dag import DagNode
from ._roster import Roster
from ._journal import JournalStore, InMemoryJournalStore

__all__ = ["run_workflow", "Ctx", "DagNode", "Roster", "JournalStore", "InMemoryJournalStore"]
```

- 不导出 `_leaves`/`middleware`/`tool`/`create_workflow_tool`/`run_script`（sherry 不用）。
- **SqliteWorkflowStore**：原 ldw 在 `__init__.py:110` 懒加载（`[sqlite]` extra）。需查它的实际实现模块（可能在单独的 `_sqlite_store.py` 或 `__init__.py` 内）。若在单独模块 → 一并拷（+ 它的依赖）；若在 `__init__.py` 内 → 提取到 `__init__.py` 或单独模块。**MVP 可先不 vendor SQLite store**，用 `InMemoryJournalStore`（同进程 resume 够用）；跨进程 resume 后续补。

## 五、内部 import 修复

5 个模块间可能有相对 import（`from ._journal import ...`）。拷贝成 `vendor/ldw/` 包后，相对 import 仍工作（同包内）。需核实：

1. grep 5 个模块的 `from .` / `from langchain_dynamic_workflow` import
2. `from .xxx import`（同包相对）→ 保留，工作
3. `from langchain_dynamic_workflow.xxx import`（绝对）→ 改为 `from .xxx import`（相对）或 `from vendor.ldw.xxx import`
4. 若 5 核心模块 import 了非核心模块（`_leaves`/`_ast_gate`/`_codegen`）→ 需评估：若是核心路径 → 要么拷那个模块、要么 stub、要么删该路径。探查说核心 5 模块只依赖 langgraph/langchain，但**拷贝时核实**。

## 六、包路径注册

sherry 的 `pyproject.toml` / import 路径需能找到 `vendor.ldw`：
- 若 `vendor/` 在 sherry 项目根（`D:\selfProj\sherry_agent\vendor\ldw\`）→ 确保 `vendor` 是 Python 包（有 `__init__.py`）且在 `sys.path`（项目根通常已在 path）
- 或放 `agent/tools/subagent/ldw/`（已在 sherry 包树内，import 路径 `agent.tools.subagent.ldw`）

**推荐** `vendor/ldw/`（顶层，ldw 是独立 vendor 不属 agent 子模块）+ `vendor/__init__.py`（空，标记包）。

## 七、验证

```python
# 测试脚本
from vendor.ldw import run_workflow, Ctx, DagNode, Roster, InMemoryJournalStore
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableLambda

# 假 leaf（sherry agent 的替身）
def fake_leaf(input):
    msgs = input["messages"]
    return {"output": f"done: {msgs[-1].content}"}
roster = Roster()
roster.register(name="fake", runnable=RunnableLambda(func=fake_leaf))

async def orchestrate(ctx):
    r1 = await ctx.agent(prompt="step1", agent_type="fake")
    r2 = await ctx.agent(prompt=f"step2 after {r1}", agent_type="fake")
    return {"r1": r1, "r2": r2}

import asyncio
result = asyncio.run(run_workflow(orchestrate, roster=roster))
print(result)  # {"r1": "done: step1", "r2": "done: step2 after done: step1"}
```

验证点：
1. `from vendor.ldw import run_workflow, Ctx, DagNode, Roster, InMemoryJournalStore` 无 ImportError（无 deepagents）
2. 假 leaf（RunnableLambda）注册 + `ctx.agent` 调用跑通
3. `ctx.dag([DagNode(...)])` 拓扑序正确
4. journal：同 prompt+agent_type 二次调 `ctx.agent` 命中缓存（零调用）
5. `grep -r deepagents vendor/ldw/` 零命中

## 八、sherry agent 作 leaf（Roster 注册）

vendor 验证通后，把 sherry 子 agent（LangGraph 图 = Runnable）注册成 leaf：

```python
from vendor.ldw import Roster
from agent.core import built_agent

def make_leaf_agent(functional_role, tools, model_profile):
    """sherry 子 agent（LangGraph 图）= langchain Runnable，直接作 ldw leaf。"""
    return built_agent(tools=tools, model=build_model(model_profile), ...)

# 注册（session 注入见下）
roster = Roster()
roster.register(name="executor", runnable=make_leaf_agent("executor", tools, mp))
```

**session_id 注入**（ldw修订计划阻断 3）：sherry 工具进程级单例 + session 从 contextvar 读。leaf 图 session-agnostic，调用前设 contextvar：

```python
from langchain_core.runnables import RunnableLambda

def wrap_leaf_with_session(agent_graph, session_id):
    async def wrapped(input):
        with set_session_contextvar(session_id):  # sherry 的 session contextvar
            return await agent_graph.ainvoke(input)
    return RunnableLambda(func=wrapped)

roster.register(name="executor", runnable=wrap_leaf_with_session(graph, session_id))
```

**前提核实**：sherry 工具的 session-scoped state 确实通过 contextvar 传递（不是构造时捕获）。若工具在构造时捕获 session → 需改成运行时从 contextvar 读（可能要改工具层）。

## 九、涉及文件

| # | 文件 | 改动 |
|---|---|---|
| 1 | `vendor/ldw/__init__.py`（新） | 导出 run_workflow/Ctx/DagNode/Roster/JournalStore/InMemoryJournalStore |
| 2 | `vendor/ldw/_context.py`（拷） | 从 ldw 源拷贝，核实内部 import |
| 3 | `vendor/ldw/_journal.py`（拷） | 同上 |
| 4 | `vendor/ldw/_dag.py`（拷） | 同上 |
| 5 | `vendor/ldw/_roster.py`（拷） | 同上 |
| 6 | `vendor/ldw/_engine.py`（拷+改） | 拷贝 + 删 deepagents import + 删/改 isinstance |
| 7 | `vendor/__init__.py`（新，空） | 标记 vendor 为包 |
| 8 | `pyproject.toml`（可能） | 若 vendor 不在默认 path，加 package-data / include |

不改动：sherry 现有代码（vendor 是新增）、ldw 源仓库（只读拷贝）。

## 十、测试

| 测试 | 覆盖点 |
|---|---|
| `test_vendor_ldw_imports.py`（新） | 5 核心符号可导入；`grep deepagents vendor/ldw/` 零；无 ImportError |
| `test_vendor_ldw_dag.py`（新） | 假 leaf + ctx.dag 拓扑序正确；依赖传递；失败节点 skip 下游 |
| `test_vendor_ldw_journal.py`（新） | 同 prompt+agent_type 二次命中缓存（零调用）；不同 prompt 不命中 |
| `test_vendor_ldw_sherry_leaf.py`（新） | sherry built_agent 作 leaf：ainvoke({"messages":[...]}) 跑通；session contextvar 正确注入 |
| `test_vendor_ldw_internal_imports.py`（新） | 5 模块间相对 import 全通；无对非核心模块的依赖 |

## 十一、实施顺序

1. **拷贝 5 核心模块** 到 `vendor/ldw/`（从 ldw 源）
2. **grep 核实 deepagents 引用** in `_engine.py`（isinstance？实际调用？）
3. **改 `_engine.py`**：删 deepagents import + 删/改 isinstance
4. **grep 核实内部 import**（5 模块间的 `from .` / `from langchain_dynamic_workflow`）→ 修绝对 import 为相对
5. **grep 核实非核心依赖**（5 模块是否 import `_leaves`/`_ast_gate`/`_codegen`）→ 若有，评估处理
6. **写 `__init__.py`** 导出核心符号
7. **写 `vendor/__init__.py`**（空）
8. **验证脚本**（七节）：import + 假 leaf + dag + journal
9. **sherry leaf 适配**：`make_leaf_agent` + `wrap_leaf_with_session` + 核实工具 session contextvar 机制
10. 测试

## 十二、风险

| 风险 | 等级 | 缓解 |
|---|---|---|
| `_engine.py` 的 deepagents 不止 isinstance（有实际调用） | 中 | 拷贝时 grep 核实；若实际调用在核心路径，需 deeper 改（或回退全 fork C） |
| 5 核心模块 import 非核心模块（`_leaves`/`_ast_gate`） | 中 | grep 核实；若是核心路径要么拷那个模块、要么 stub、要么删路径 |
| SqliteWorkflowStore 实现在 `__init__.py` 内（不拷就没 SQLite） | 低 | MVP 用 InMemoryJournalStore；跨进程 resume 后续补拷 SQLite 模块 |
| ldw 上游更新核心模块（新功能/bugfix）需 re-pull | 低 | 核心 5 模块稳定（journal/dag/roster/context/engine 不大变）；偶尔 re-pull |
| sherry 工具构造时捕获 session（非 contextvar） | 中 | 核实；若是，需改工具层从 contextvar 读（可能触多文件）——标为实现待办 |
| vendor 包路径不在 sys.path | 低 | `vendor/__init__.py` + 项目根在 path；或放 agent 包树内 |

## 十三、与 ldw 重放计划的关系

本计划是 `计划_ldw重放已验证plan_20261106.md` 的 **Phase 0（fork ldw）替代方案**——用选择性 vendor 5 核心模块替代全 fork。落地后，ldw 重放计划的 Phase 1+（sherry leaf + 编译器 + replay）可直接基于 `vendor.ldw` 的 `run_workflow`/`Roster`/`DagNode` 开始。

**比原 Phase 0 全 fork 轻**：不维护完整 fork（不拷 `_leaves`/`middleware`/`tool`/`_ast_gate`/`_codegen`），不 rebase 那些模块的上游变动。只跟踪 5 核心模块。
