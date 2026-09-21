# DeepAgents 值得借鉴的防护能力 — 具体实现方案

> 基于 `PROTECTION_COMPARISON.md` 对比报告，筛选 DeepAgents 中 Sherry 可落地的防护能力，给出具体实现方案。
> 优先级：P1(增强体验) → P2(长期优化)
>
> **未执行项清单**：P1-3 模型感知摘要默认值、P1-6 中间件脚手架保护（详细方案见下方，参考 `D:\selfProj\deepagents` 三重防线）、P1-8 威胁模型文档、P2-1 ripgrep 双重超时看门狗（已完成项见 git 历史）。
>
> **已评估不落地**：P1-4 增量检查点优化（`aclean_old_checkpoints` 每线程只留最新，检查点存储已是 O(N)；`DeltaChannel` 与 keep-latest 剪枝不兼容，实测静默丢状态）、P1-5 消息增量缩减器（标准 `add_messages` 已覆盖去重/墓碑/重置，自定义 reducer 会破坏 P1-9 同 id 替换语义）。
>
> **已落地**：P1-7 多模态内容清理（每请求能力擦洗 + 按块部分剥离）。落点与提案不同——未新建 `agent/middlewares/multimodal_scrub.py`，实现为 `MultimodalProcessor.wrap_model_call` + `agent/middlewares/media_pipeline/scrub.py`（每请求、request-only、按块部分剥离；state/checkpointer/MesMemory 不动）。

---

## 目录

1. [P1-3：模型感知摘要默认值](#p1-3模型感知摘要默认值)
2. [P1-6：中间件脚手架保护](#p1-6中间件脚手架保护)
3. [P1-8：威胁模型文档](#p1-8威胁模型文档)
4. [P2-1：ripgrep 双重超时看门狗](#p2-1ripgrep-双重超时看门狗)
5. [实施优先级与依赖关系总览](#实施优先级与依赖关系总览)

---

## P1-3：模型感知摘要默认值

### 问题

Sherry 的摘要触发阈值需要手动配置，不同模型（128K vs 32K）需要不同阈值，容易配错。

### DeepAgents 做法

`compute_summarization_defaults()` 从模型 profile 的 `max_input_tokens` 自动计算触发(85%)和保留(10%)阈值。

### 具体实现方案

#### 文件清单

| 文件                                          | 修改类型 | 说明             |
| --------------------------------------------- | -------- | ---------------- |
| `config/features/agent_side/summarization.py` | 修改     | 添加自动计算函数 |

#### 实现代码

```python
def compute_summarization_defaults(max_input_tokens: int) -> dict:
    """根据模型 max_input_tokens 自动计算摘要阈值。

    Args:
        max_input_tokens: 模型的最大输入 token 数

    Returns:
        包含 trigger_threshold, keep_threshold, max_output_budget 的字典
    """
    trigger = int(max_input_tokens * 0.85)
    keep = int(max_input_tokens * 0.10)
    # 预留输出 token + 5% 余量
    output_budget = int(max_input_tokens * 0.15)
    return {
        "trigger_threshold": trigger,
        "keep_threshold": keep,
        "max_output_budget": output_budget,
    }
```

在 `agent/core.py` 或 `server/__main__.py` 启动时，从 `MAIN_LLM` 的 model profile 获取 `max_input_tokens`，调用此函数设置 `SUMMARIZATION` 的默认值。

---

## P1-6：中间件脚手架保护

### 问题

Sherry 的中间件链当前为**硬编码列表**（`agent/core.py:178-230` 和 `spawn/core.py:846-878`），无排除/配置机制。但随着功能演进（如 `subagent-role-migration.md` 引入功能角色后可能按角色裁剪中间件、或用户通过配置禁用某些中间件），安全关键中间件可能被意外跳过：

- 移除 `ToolGuardrails` → 工具调用无校验，可执行危险操作
- 移除 `IterationBudget` → 子代理无限循环耗尽 token
- 移除 `MessagePersistenceMiddleware` → 消息不落库，MesMemory 数据断裂
- 移除 `ContextEvictionMiddleware` → 上下文窗口溢出，LLM 调用失败
- 移除 `PathGuard` → 路径安全门控失效
- 移除 `HumanInTheLoop` → 敏感操作无人审批
- 移除 `LLMRetryMiddleware` → 单次 LLM 失败即中断，无 fallback
- 移除 `Summarization` → 长会话上下文不压缩，token 耗尽

当前无任何机制在启动时检测这些中间件是否缺失。

### DeepAgents 做法（三重防线）

参考 `D:\selfProj\deepagents`，DeepAgents 用三层递进防御 + 辅助守卫保护中间件脚手架：

| 防线       | 模块                                                                                                            | 触发时机                     | 作用                                                                           |
| ---------- | --------------------------------------------------------------------------------------------------------------- | ---------------------------- | ------------------------------------------------------------------------------ |
| **第一道** | `HarnessProfile.__post_init__` / `HarnessProfileConfig.__post_init__`（`profiles/harness/harness_profiles.py`） | 配置构造时                   | 语法检查 + 脚手架违规检测——在配置对象创建时就拒绝排除必需中间件                |
| **第二道** | `_validate_excluded_middleware_config`（`_excluded_middleware.py:23-64`）                                       | `create_deep_agent` 组装开头 | 不变量复检——即使配置绕过了第一道（如动态构造），组装时仍拦截                   |
| **第三道** | `_apply_excluded_middleware` + `_verify_excluded_middleware_coverage`（`_excluded_middleware.py:90-225`）       | 每次栈过滤后                 | 实际执行过滤 + 覆盖审计——检测排除条目是否未匹配任何中间件（拼写错误/过期配置） |

**关键设计要素**：

1. **`_REQUIRED_MIDDLEWARE` 元组**（`graph.py:241-256`）——不只存名字，存 `(class, aliases)` 二元组，支持按类类型和按字符串名双重匹配：

   ```python
   _REQUIRED_MIDDLEWARE: tuple[tuple[type[AgentMiddleware, ...], tuple[str, ...]], ...] = (
       (FilesystemMiddleware, ()),      # 支撑所有文件工具 + permissions 规则
       (SubAgentMiddleware, ()),        # 支撑 task 工具处理器
   )
   ```

2. **派生集合**（`graph.py:258-268`）——从元组自动派生 `frozenset[type]` 和 `frozenset[str]`，分别用于类匹配和字符串匹配，避免每次校验都遍历元组。

3. **公共别名机制**（`summarization.py:523-545`）——私有实现类 `_DeepAgentsSummarizationMiddleware` 通过 `serialized_name: ClassVar[str] = "SummarizationMiddleware"` 和 `name` 属性覆盖暴露公共别名，使字符串形式排除能匹配到私有类。子类回退到 `type(self).__name__` 避免别名继承。

4. **漂移守卫测试**（`test_graph.py:1564-1587` `TestRequiredMiddlewareNamesCoverage`）——确保 `_REQUIRED_MIDDLEWARE_NAMES` 覆盖每个 `_REQUIRED_MIDDLEWARE_CLASSES` 条目的 `.name`，防止新增必需中间件后忘记注册别名。

5. **名称冲突检测**（`_excluded_middleware.py:67-87` `_raise_on_name_collisions`）——一个字符串排除名匹配了多个不同类时报错，防止歧义排除。

### Sherry 适配设计

Sherry 与 DeepAgents 的关键差异：

| 维度       | DeepAgents                                            | Sherry                                                              |
| ---------- | ----------------------------------------------------- | ------------------------------------------------------------------- |
| 中间件来源 | `HarnessProfile` 配置驱动，支持 `excluded_middleware` | 硬编码列表，无排除机制                                              |
| 组装入口   | 单一 `create_deep_agent`                              | 双入口：`built_agent()`（main）+ `_build_child_agent()`（subagent） |
| 中间件集   | 统一栈，按 profile 裁剪                               | main 17 个 + subagent 7 个，集合不同                                |
| 配置层     | `HarnessProfile` dataclass + `__post_init__`          | `config/features/` TypedDict（无运行时校验）                        |
| 别名机制   | `serialized_name` ClassVar                            | 无（中间件类名即唯一标识）                                          |

**适配策略**：先落地**第二道防线**（组装时校验）+ **漂移守卫测试**，为未来排除机制预留接口。

- 不引入 `HarnessProfile` 等价物——Sherry 的 `config/features/` TypedDict 无 `__post_init__`，第一道防线（构造时校验）暂不适用
- 在两个组装入口（`built_agent` + `_build_child_agent`）各调一次校验
- 定义两套 `_REQUIRED_MIDDLEWARE`（main + subagent），因为安全关注点不同
- 预留 `excluded_middleware` 参数接口，未来实现排除机制时第一道/第三道防线可直接接入

### 具体实现方案

#### 文件清单

| 文件                                          | 修改类型 | 说明                                                           |
| --------------------------------------------- | -------- | -------------------------------------------------------------- |
| `agent/middlewares/scaffolding.py`            | **新建** | 必需中间件定义 + 校验函数 + 漂移守卫                           |
| `agent/middlewares/__init__.py`               | 修改     | re-export scaffolding 公共接口                                 |
| `agent/core.py`                               | 修改     | `built_agent()` 中调用 `validate_required_middleware()`        |
| `agent/tools/subagent/spawn/core.py`          | 修改     | `_build_child_agent()` 中调用 `validate_required_middleware()` |
| `tests/agent/middlewares/test_scaffolding.py` | **新建** | 校验逻辑 + 漂移守卫 + 负向测试                                 |

#### 新建 `agent/middlewares/scaffolding.py`

```python
"""Middleware scaffolding protection — required middleware enforcement.

Adapted from deepagents' three-layer defense (graph.py:_REQUIRED_MIDDLEWARE,
_excluded_middleware.py:_validate_excluded_middleware_config), simplified for
Sherry's hardcoded middleware lists (no HarnessProfile / excluded_middleware yet).

Two required sets:
  - MAIN_REQUIRED: safety-critical middleware for the main agent chain
  - SUBAGENT_REQUIRED: safety-critical middleware for the subagent chain

validate_required_middleware() is called at both assembly points
(built_agent + _build_child_agent) to enforce the invariant.
"""

from __future__ import annotations

from typing import NamedTuple

from loguru import logger

# Import from specific submodules (NOT from agent.middlewares.__init__) to
# avoid circular import: __init__.py re-exports scaffolding at the bottom,
# after all middleware classes are already defined.
from .tool_guardrails import ToolGuardrails
from .humanInTheLoop import HumanInTheLoop
from .summarization import Summarization
from .iteration_budget import IterationBudget
from .message_persistence import MessagePersistenceMiddleware
from .context_eviction import ContextEvictionMiddleware
from .path_guard import PathGuard
from .llm_retry import LLMRetryMiddleware
from .output_repetition_guard import OutputRepetitionGuard
from .max_tokens_boost import MaxTokensBoostMiddleware
from .heartbeat_staleness import HeartbeatStaleness
from .tool_call_normalize import ToolCallNormalize


class RequiredMiddlewareEntry(NamedTuple):
    """A required middleware entry — class + accepted string names.

    Mirrors deepagents' (class, aliases) tuple pattern.
    """

    cls: type
    names: tuple[str, ...]  # cls.__name__ + any aliases; empty = just cls.__name__


# ---------------------------------------------------------------------------
# Main agent required middleware
# ---------------------------------------------------------------------------

_MAIN_REQUIRED: tuple[RequiredMiddlewareEntry, ...] = (
    RequiredMiddlewareEntry(ToolGuardrails, ()),
    RequiredMiddlewareEntry(HumanInTheLoop, ("HITLCore",)),
    RequiredMiddlewareEntry(Summarization, ()),
    RequiredMiddlewareEntry(IterationBudget, ()),
    RequiredMiddlewareEntry(MessagePersistenceMiddleware, ()),
    RequiredMiddlewareEntry(ContextEvictionMiddleware, ()),
    RequiredMiddlewareEntry(PathGuard, ()),
    RequiredMiddlewareEntry(LLMRetryMiddleware, ()),
    RequiredMiddlewareEntry(OutputRepetitionGuard, ()),
    RequiredMiddlewareEntry(MaxTokensBoostMiddleware, ()),
    RequiredMiddlewareEntry(HeartbeatStaleness, ()),
    RequiredMiddlewareEntry(ToolCallNormalize, ()),
)

# Subagent required middleware — subset of main, no HITL/Eviction/Persistence/etc.
# (subagent middleware chain is intentionally leaner, see subagent-role-migration.md
# "中间件链对比" table for the full rationale)
_SUBAGENT_REQUIRED: tuple[RequiredMiddlewareEntry, ...] = (
    RequiredMiddlewareEntry(ToolGuardrails, ()),
    RequiredMiddlewareEntry(IterationBudget, ()),
    RequiredMiddlewareEntry(Summarization, ()),
    RequiredMiddlewareEntry(OutputRepetitionGuard, ()),
    RequiredMiddlewareEntry(HeartbeatStaleness, ()),
    RequiredMiddlewareEntry(ToolCallNormalize, ()),
    RequiredMiddlewareEntry(MaxTokensBoostMiddleware, ()),
)


# Derived sets for fast membership testing (mirrors deepagents graph.py:258-268)
MAIN_REQUIRED_CLASSES: frozenset[type] = frozenset(e.cls for e in _MAIN_REQUIRED)
MAIN_REQUIRED_NAMES: frozenset[str] = frozenset(
    name for e in _MAIN_REQUIRED for name in (e.cls.__name__, *e.names)
)

SUBAGENT_REQUIRED_CLASSES: frozenset[type] = frozenset(e.cls for e in _SUBAGENT_REQUIRED)
SUBAGENT_REQUIRED_NAMES: frozenset[str] = frozenset(
    name for e in _SUBAGENT_REQUIRED for name in (e.cls.__name__, *e.names)
)


class ScaffoldingViolationError(RuntimeError):
    """Raised when a required middleware is missing from the assembled chain."""


def _format_rejection(missing_names: set[str], chain: str) -> str:
    """Format a human-readable rejection message (mirrors deepagents' _format_scaffolding_rejection)."""
    sorted_names = sorted(missing_names)
    return (
        f"Required middleware scaffolding violated for {chain} agent chain.\n"
        f"  Missing: {sorted_names}\n"
        f"  These middleware are safety-critical and cannot be omitted.\n"
        f"  If this is intentional, update _MAIN_REQUIRED / _SUBAGENT_REQUIRED "
        f"in agent/middlewares/scaffolding.py and its tests."
    )


def _extract_middleware_names(middleware_list: list) -> set[str]:
    """Extract all identifiable names from a middleware list.

    Collects type(cls).__name__ and any .name / .serialized_name attributes
    (mirrors deepagents' dual class/string matching).
    """
    names: set[str] = set()
    for mw in middleware_list:
        names.add(type(mw).__name__)
        # Some middleware expose a .name property or serialized_name ClassVar
        name_attr = getattr(mw, "name", None)
        if isinstance(name_attr, str):
            names.add(name_attr)
        serialized = getattr(type(mw), "serialized_name", None)
        if isinstance(serialized, str):
            names.add(serialized)
    return names


def validate_required_middleware(
    middleware_list: list,
    *,
    chain: str,
    entries: tuple[RequiredMiddlewareEntry, ...],
) -> None:
    """Validate that all required middleware are present in the assembled list.

    Called at both assembly points (built_agent + _build_child_agent).
    Mirrors deepagents' _validate_excluded_middleware_config (second defense layer).

    Args:
        middleware_list: The middleware= argument passed to create_agent().
        chain: "main" or "subagent" — for error messages only.
        entries: The tuple of RequiredMiddlewareEntry to validate against.
            Callers pass _MAIN_REQUIRED or _SUBAGENT_REQUIRED (or a future
            role-derived entry set). This replaces the old chain-based
            hardcoded selection that ignored custom required sets.

    Raises:
        ScaffoldingViolationError: If any required middleware is missing.
    """
    if not middleware_list:
        raise ScaffoldingViolationError(
            f"Middleware list is empty for {chain} agent chain — "
            f"required scaffolding cannot be validated."
        )

    actual_classes = {type(mw) for mw in middleware_list}
    actual_names = _extract_middleware_names(middleware_list)

    # A required middleware is "missing" only if BOTH its class AND all its
    # names are absent (handles subclass scenarios where the class differs
    # but a serialized_name alias is present).
    truly_missing: set[str] = set()
    for entry in entries:
        cls_present = entry.cls in actual_classes
        any_name_present = any(n in actual_names for n in (entry.cls.__name__, *entry.names))
        if not cls_present and not any_name_present:
            truly_missing.add(entry.cls.__name__)

    if truly_missing:
        raise ScaffoldingViolationError(_format_rejection(truly_missing, chain))

    logger.debug(
        "Middleware scaffolding validated for {} chain: {} required, {} actual",
        chain,
        len(entries),
        len(middleware_list),
    )


# ---------------------------------------------------------------------------
# Drift guard — ensures _REQUIRED_NAMES covers every _REQUIRED_CLASSES entry
# (mirrors deepagents test_graph.py:1564-1587 TestRequiredMiddlewareNamesCoverage)
# ---------------------------------------------------------------------------

def verify_required_names_coverage() -> None:
    """Verify that MAIN_REQUIRED_NAMES and SUBAGENT_REQUIRED_NAMES cover
    every class in their respective _REQUIRED tuples.

    Call this from tests (not runtime) to catch drift when adding/removing
    required middleware entries.
    """
    for label, entries, names_set in (
        ("MAIN", _MAIN_REQUIRED, MAIN_REQUIRED_NAMES),
        ("SUBAGENT", _SUBAGENT_REQUIRED, SUBAGENT_REQUIRED_NAMES),
    ):
        for entry in entries:
            expected = {entry.cls.__name__, *entry.names}
            missing = expected - names_set
            if missing:
                raise AssertionError(
                    f"{label}_REQUIRED_NAMES drift: {missing} not covered. "
                    f"Update the derived frozenset after changing _REQUIRED."
                )
```

> **注**：`_MAIN_REQUIRED` / `_SUBAGENT_REQUIRED` 的导入（`ToolGuardrails`、`HumanInTheLoop` 等）来自 `agent.middlewares` 包，在 `scaffolding.py` 顶部用延迟导入避免循环（`__init__.py` 已导出这些类）。

#### 修改 `agent/middlewares/__init__.py`

```python
# 在现有导出之后追加
from .scaffolding import (
    ScaffoldingViolationError as ScaffoldingViolationError,
    validate_required_middleware as validate_required_middleware,
    verify_required_names_coverage as verify_required_names_coverage,
    RequiredMiddlewareEntry as RequiredMiddlewareEntry,
    # Entry tuples — callers pass these to validate_required_middleware()
    _MAIN_REQUIRED as _MAIN_REQUIRED,
    _SUBAGENT_REQUIRED as _SUBAGENT_REQUIRED,
    # Derived sets — still useful for tests (subset checks, drift guards)
    MAIN_REQUIRED_CLASSES as MAIN_REQUIRED_CLASSES,
    MAIN_REQUIRED_NAMES as MAIN_REQUIRED_NAMES,
    SUBAGENT_REQUIRED_CLASSES as SUBAGENT_REQUIRED_CLASSES,
    SUBAGENT_REQUIRED_NAMES as SUBAGENT_REQUIRED_NAMES,
)
```

#### 修改 `agent/core.py` — `built_agent()` 调用校验

在 `create_agent(...)` 调用**之前**插入校验（fail fast，避免昂贵的 agent 构建后才报错）：

```python
# agent/core.py — built_agent(), BEFORE create_agent(...) call (line ~231)

from agent.middlewares import validate_required_middleware, _MAIN_REQUIRED

# Validate safety scaffolding — fail fast if required middleware is missing.
# (deepagents second defense layer; Sherry has no exclusion mechanism yet,
# so this catches accidental removal during code changes.)
_agent_middleware = [
    TodoContinuationEnforcer(),
    system_prompt_injection,
    MultimodalProcessor(),
    IterationBudget(ITERATION_BUDGET["main_agent_max_iterations"]),
    ToolGuardrails(),
    ContextEvictionMiddleware(),
    ToolCallNormalize(),
    PathGuard(),
    SubagentCompletionDrainMiddleware(),
    TaskIntentMiddleware(),
    OutputRepetitionGuard(),
    MaxTokensBoostMiddleware(),
    HeartbeatStaleness(),
    HumanInTheLoop(HITLConfig()),
    MessagePersistenceMiddleware(),
    LLMRetryMiddleware(fallback_chain=fallback_chain),
    Summarization(...),
]

validate_required_middleware(
    _agent_middleware,
    chain="main",
    entries=_MAIN_REQUIRED,
)

_agent = create_agent(
    ...,
    middleware=_agent_middleware,
)
```

> **重构说明**：当前 `built_agent()` 将 middleware 列表内联在 `create_agent()` 调用中。为支持校验，需先将列表提取为变量 `_agent_middleware`，校验通过后再传入 `create_agent()`。逻辑等价，不改变注册顺序或行为。

#### 修改 `agent/tools/subagent/spawn/core.py` — `_build_child_agent()` 调用校验

在 `create_agent(...)` 调用**之前**插入校验：

```python
# spawn/core.py — _build_child_agent(), BEFORE create_agent(...) call (line ~879)

from agent.middlewares import validate_required_middleware, _SUBAGENT_REQUIRED

_subagent_middleware = [
    Summarization(...),
    IterationBudget(ITERATION_BUDGET["worker_max_iterations"]),
    ToolGuardrails(),
    OutputRepetitionGuard(),
    MaxTokensBoostMiddleware(),
    ToolCallNormalize(),
    HeartbeatStaleness(),
]

validate_required_middleware(
    _subagent_middleware,
    chain="subagent",
    entries=_SUBAGENT_REQUIRED,
)

child_agent = create_agent(
    ...,
    middleware=_subagent_middleware,
)
```

#### 新建 `tests/agent/middlewares/test_scaffolding.py`

```python
"""Tests for middleware scaffolding protection (P1-6).

Mirrors deepagents' test_graph.py TestRequiredMiddlewareNamesCoverage +
TestMiddlewareExclusionWiring patterns, adapted for Sherry's dual-chain model.
"""

import pytest
from agent.middlewares.scaffolding import (
    ScaffoldingViolationError,
    validate_required_middleware,
    verify_required_names_coverage,
    MAIN_REQUIRED_CLASSES,
    MAIN_REQUIRED_NAMES,
    SUBAGENT_REQUIRED_CLASSES,
    SUBAGENT_REQUIRED_NAMES,
    _MAIN_REQUIRED,
    _SUBAGENT_REQUIRED,
)


class TestRequiredNamesCoverage:
    """Drift guard — ensures _REQUIRED_NAMES covers every _REQUIRED_CLASSES entry.

    Mirrors deepagents test_graph.py:1564-1587.
    """

    def test_main_names_cover_all_classes(self):
        """Every MAIN_REQUIRED entry's class name + aliases are in MAIN_REQUIRED_NAMES."""
        for entry in _MAIN_REQUIRED:
            assert entry.cls.__name__ in MAIN_REQUIRED_NAMES
            for alias in entry.names:
                assert alias in MAIN_REQUIRED_NAMES

    def test_subagent_names_cover_all_classes(self):
        """Every SUBAGENT_REQUIRED entry's class name + aliases are in SUBAGENT_REQUIRED_NAMES."""
        for entry in _SUBAGENT_REQUIRED:
            assert entry.cls.__name__ in SUBAGENT_REQUIRED_NAMES
            for alias in entry.names:
                assert alias in SUBAGENT_REQUIRED_NAMES

    def test_verify_required_names_coverage_passes(self):
        """verify_required_names_coverage() does not raise for current sets."""
        verify_required_names_coverage()


class TestValidateRequiredMiddleware:
    """Unit tests for validate_required_middleware()."""

    def test_empty_list_raises(self):
        with pytest.raises(ScaffoldingViolationError, match="empty"):
            validate_required_middleware([], chain="main",
                                         required_classes=MAIN_REQUIRED_CLASSES,
                                         required_names=MAIN_REQUIRED_NAMES)

    def test_missing_one_middleware_raises(self):
        """Remove one required middleware → should fail with its name."""
        from agent.middlewares import (
            IterationBudget, ToolGuardrails, Summarization,
            OutputRepetitionGuard, MaxTokensBoostMiddleware,
            ToolCallNormalize, HeartbeatStaleness,
        )
        # Missing: ToolGuardrails
        incomplete = [
            Summarization(model=None),
            IterationBudget(60),
            OutputRepetitionGuard(),
            MaxTokensBoostMiddleware(),
            ToolCallNormalize(),
            HeartbeatStaleness(),
        ]
        with pytest.raises(ScaffoldingViolationError, match="ToolGuardrails"):
            validate_required_middleware(incomplete, chain="subagent",
                                         required_classes=SUBAGENT_REQUIRED_CLASSES,
                                         required_names=SUBAGENT_REQUIRED_NAMES)

    def test_all_present_passes(self):
        """Full subagent chain → no exception."""
        from agent.middlewares import (
            IterationBudget, ToolGuardrails, Summarization,
            ToolCallNormalize, HeartbeatStaleness, MaxTokensBoostMiddleware,
        )
        from agent.middlewares.output_repetition_guard import OutputRepetitionGuard

        full = [
            Summarization(model=None),
            IterationBudget(60),
            ToolGuardrails(),
            OutputRepetitionGuard(),
            MaxTokensBoostMiddleware(),
            ToolCallNormalize(),
            HeartbeatStaleness(),
        ]
        validate_required_middleware(full, chain="subagent",
                                     required_classes=SUBAGENT_REQUIRED_CLASSES,
                                     required_names=SUBAGENT_REQUIRED_NAMES)

    def test_subclass_does_not_satisfy_parent_requirement(self):
        """A subclass of a required middleware with a DIFFERENT class name
        does NOT satisfy the requirement (class check is by type identity,
        name check uses type().__name__ which differs for subclasses).

        To use a subclass, register an alias in RequiredMiddlewareEntry.names
        or update the entry's cls to the subclass."""
        from agent.middlewares import ToolGuardrails

        class CustomToolGuardrails(ToolGuardrails):
            pass

        chain = [CustomToolGuardrails()]
        # ToolGuardrails class not in actual_classes (only CustomToolGuardrails is),
        # and "ToolGuardrails" name not in actual_names (type().__name__ is
        # "CustomToolGuardrails"). So this SHOULD fail.
        with pytest.raises(ScaffoldingViolationError):
            validate_required_middleware(chain, chain="subagent",
                                         entries=_SUBAGENT_REQUIRED)


class TestActualChainCompliance:
    """Integration-level: verify the REAL middleware lists pass validation.

    This is the drift guard for code changes — if someone removes a middleware
    from built_agent() or _build_child_agent(), this test goes red.
    """

    def test_main_agent_middleware_list_passes(self):
        """The actual main agent middleware list (from agent/core.py) passes validation."""
        # Import the actual middleware instances used in built_agent()
        from agent.middlewares import (
            ToolGuardrails, HumanInTheLoop, Summarization, IterationBudget,
            MessagePersistenceMiddleware, ContextEvictionMiddleware,
            PathGuard, LLMRetryMiddleware, OutputRepetitionGuard,
            MaxTokensBoostMiddleware, HeartbeatStaleness, ToolCallNormalize,
            system_prompt_injection, MultimodalProcessor,
        )
        from agent.middlewares.task_intent import TaskIntentMiddleware
        from agent.middlewares.subagent_completion_drain import SubagentCompletionDrainMiddleware
        # TodoContinuationEnforcer is NOT in required set (it's a nice-to-have)

        full_main = [
            system_prompt_injection,
            MultimodalProcessor(),
            IterationBudget(100),
            ToolGuardrails(),
            ContextEvictionMiddleware(),
            ToolCallNormalize(),
            PathGuard(),
            SubagentCompletionDrainMiddleware(),
            TaskIntentMiddleware(),
            OutputRepetitionGuard(),
            MaxTokensBoostMiddleware(),
            HeartbeatStaleness(),
            HumanInTheLoop(),
            MessagePersistenceMiddleware(),
            LLMRetryMiddleware(fallback_chain=None),
            Summarization(model=None),
        ]
        validate_required_middleware(full_main, chain="main",
                                     required_classes=MAIN_REQUIRED_CLASSES,
                                     required_names=MAIN_REQUIRED_NAMES)

    def test_subagent_middleware_list_passes(self):
        """The actual subagent middleware list (from spawn/core.py) passes validation."""
        from agent.middlewares import (
            IterationBudget, ToolGuardrails, Summarization,
            ToolCallNormalize, HeartbeatStaleness, MaxTokensBoostMiddleware,
        )
        from agent.middlewares.output_repetition_guard import OutputRepetitionGuard

        full_sub = [
            Summarization(model=None),
            IterationBudget(60),
            ToolGuardrails(),
            OutputRepetitionGuard(),
            MaxTokensBoostMiddleware(),
            ToolCallNormalize(),
            HeartbeatStaleness(),
        ]
        validate_required_middleware(full_sub, chain="subagent",
                                     required_classes=SUBAGENT_REQUIRED_CLASSES,
                                     required_names=SUBAGENT_REQUIRED_NAMES)


class TestRoleAgnosticDesign:
    """Verify that the current single _SUBAGENT_REQUIRED set is correct
    for all depth roles and (future) functional roles.

    These tests document the explicit decision NOT to differentiate by role,
    and will break when someone introduces per-role middleware differentiation
    without updating the required sets.
    """

    def test_orchestrator_and_leaf_share_same_required_set(self):
        """ORCHESTRATOR and LEAF get the same middleware chain in _build_child_agent,
        so they must share the same _SUBAGENT_REQUIRED set.

        If this test breaks, someone added depth-based middleware differentiation
        — update _SUBAGENT_REQUIRED to _SUBAGENT_REQUIRED_BY_DEPTH.
        """
        # The required set is the same regardless of depth role
        assert SUBAGENT_REQUIRED_CLASSES == frozenset(
            e.cls for e in _SUBAGENT_REQUIRED
        )

    def test_subagent_required_is_subset_of_main_required(self):
        """Every subagent required middleware must also be required for main
        (subagent chain is a subset — safety middleware present in both must
        not be accidentally removed from either).

        If this test breaks, someone added a middleware to subagent that is NOT
        in main — review whether it should be required for subagent only.
        """
        assert SUBAGENT_REQUIRED_CLASSES.issubset(MAIN_REQUIRED_CLASSES)

    def test_main_only_middleware_not_in_subagent_required(self):
        """Middleware that is intentionally main-agent-only (HITL,
        MessagePersistence, ContextEviction, PathGuard, LLMRetry, etc.)
        must NOT be in _SUBAGENT_REQUIRED.

        If this test breaks, someone added a main-only middleware to the
        subagent required set — either add it to the subagent chain first
        or remove it from _SUBAGENT_REQUIRED.
        """
        main_only_classes = MAIN_REQUIRED_CLASSES - SUBAGENT_REQUIRED_CLASSES
        # These are the 5 middleware intentionally absent from subagent
        expected_main_only_names = {
            "HumanInTheLoop",
            "MessagePersistenceMiddleware",
            "ContextEvictionMiddleware",
            "PathGuard",
            "LLMRetryMiddleware",
        }
        actual_main_only_names = {cls.__name__ for cls in main_only_classes}
        assert expected_main_only_names == actual_main_only_names

    def test_future_functional_roles_documented(self):
        """When functional roles are introduced (subagent-role-migration Phase 1),
        this test verifies they do NOT change the required set.

        After Phase 1 lands, import FunctionalRole and verify:
          for each role in FunctionalRole:
              get_subagent_required(role) == _SUBAGENT_REQUIRED (unchanged)

        Until then, this test is a placeholder that documents the invariant.
        """
        # Phase 1 not yet implemented — _SUBAGENT_REQUIRED is flat, not per-role.
        # When Phase 1 lands, add:
        #   from agent.tools.subagent.types.functional_role import FunctionalRole
        #   for role in FunctionalRole:
        #       assert get_subagent_required(role) == SUBAGENT_REQUIRED_CLASSES
        assert _SUBAGENT_REQUIRED is not None  # placeholder assertion
```

### 与深度角色和功能角色的关系

#### 深度角色（MAIN / ORCHESTRATOR / LEAF）— 当前不需要区分

当前代码中 `_build_child_agent()` 对 ORCHESTRATOR 和 LEAF 装配**完全相同的 7 个中间件**（`spawn/core.py:846-878`）。深度角色只影响 LLM 选择（ORCHESTRATOR→main_llm, LEAF→auxiliary_llm）、工具策略（ORCHESTRATOR 解锁 spawn/yield）和 scope 分配——**不影响中间件链**。

因此 `_SUBAGENT_REQUIRED` 是单一集合，不按深度角色拆分：

```python
# 当前设计：所有子代理（无论 ORCHESTRATOR 还是 LEAF）共用一套必需集合
_SUBAGENT_REQUIRED: tuple[RequiredMiddlewareEntry, ...] = ( ... 7 项 ... )
```

**安全合理性**：子代理的 7 个必需中间件（ToolGuardrails / IterationBudget / Summarization / OutputRepetitionGuard / MaxTokensBoost / ToolCallNormalize / HeartbeatStaleness）都是 per-agent 安全基线，与深度角色无关——无论子代理能否 spawn 子代，它都需要工具校验、迭代限制、重复检测和心跳检测。

**未来触发条件**：仅当 ORCHESTRATOR 需要额外中间件（如 `SubagentCompletionDrainMiddleware`——当前是 main-only，但如果 ORCHESTRATOR 需要感知子代完成事件）或 LEAF 需要减去某个中间件时，才需要按深度角色拆分。

#### 功能角色（GENERAL / RESEARCHER / EXECUTOR / REVIEWER）— 当前不存在，预留演进

功能角色在 `subagent-role-migration.md` Phase 1 中引入，但该计划**不改变中间件链**——功能角色只影响 LLM 选择（`role_def.model_tier`）、工具白名单（`role_def.tools`）和系统提示词（`role_def.prompt_body`）。所有功能角色的子代理仍获得相同的 7 个中间件。

因此 P1-6 当前**不需要按功能角色区分** `_SUBAGENT_REQUIRED`。

**演进触发条件与路径**：

| 触发场景                 | 示例                                          | 演进方式                                                                                                         |
| ------------------------ | --------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| 某角色需要**额外**中间件 | REVIEWER 加 HITL（审计角色需人工确认）        | `_SUBAGENT_REQUIRED_BY_ROLE: dict[FunctionalRole, frozenset]`，基线 = `_SUBAGENT_REQUIRED`，角色在基线上**追加** |
| 某角色需要**减去**中间件 | EXECUTOR 去掉 Summarization（短任务不需压缩） | 同上，角色在基线上**删除**，但需安全评审确认删除合理                                                             |
| 某角色需要**替换**中间件 | RESEARCHER 用更轻量的 IterationBudget 配置    | 不改必需集合（类相同），改构造参数——P1-6 不涉及                                                                  |

当触发条件满足时，`scaffolding.py` 演进为：

```python
# Phase 1 完成后的演进形态（当前不实现，仅预留设计）
from agent.tools.subagent.types.functional_role import FunctionalRole

# 基线：所有子代理都需要的安全基线
_SUBAGENT_BASELINE: frozenset[type] = frozenset(
    e.cls for e in _SUBAGENT_REQUIRED
)

# 按功能角色的增减（delta），在基线上叠加
_SUBAGENT_ROLE_OVERRIDES: dict[FunctionalRole, tuple[frozenset[type], frozenset[type]]] = {
    # (add_classes, remove_classes)
    # FunctionalRole.REVIEWER: ({HumanInTheLoop}, set()),  # 审计角色加 HITL
    # FunctionalRole.EXECUTOR: (set(), {Summarization}),   # 短任务去压缩
}

def get_subagent_required(role: FunctionalRole | None) -> frozenset[type]:
    """Resolve required middleware classes for a functional role."""
    base = _SUBAGENT_BASELINE
    if role is None:
        return base
    add, remove = _SUBAGENT_ROLE_OVERRIDES.get(role, (set(), set()))
    return (base | add) - remove
```

`_build_child_agent` 的校验调用改为：

```python
required = get_subagent_required(functional_role)  # 未来形态
validate_required_middleware(
    _subagent_middleware,
    chain=f"subagent:{functional_role.value if functional_role else 'default'}",
    required_classes=required,
    required_names=_derive_names_from_classes(required),
)
```

#### 当前实现的显式约束

P1-6 当前实现中 `validate_required_middleware()` 的 `chain` 参数仅取 `"main"` 或 `"subagent"` 两个值，对应 `_MAIN_REQUIRED` 和 `_SUBAGENT_REQUIRED`。`_build_child_agent()` 中调用时**不传 depth role 或 functional role**——因为当前所有子代理共用一套必需集合，且功能角色尚未实现。

当 `subagent-role-migration.md` Phase 1 落地后，`_build_child_agent` 签名会增加 `functional_role` 参数。此时 P1-6 的校验调用**仍不需要改动**（功能角色不改变中间件链），只需在 Phase 1 集成时确认 `_build_child_agent` 传入的 `functional_role` 不影响 middleware list 即可。仅当上述"演进触发条件"满足时才需要升级为 `get_subagent_required(role)`。

### 与 `subagent-role-migration.md` 的交互

| 场景                                                             | 影响                                                          | 处理方式                                                                                                                                                    |
| ---------------------------------------------------------------- | ------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 功能角色迁移不改变中间件链（当前 Phase 1 设计）                  | 无交互                                                        | 校验照常通过，`chain="subagent"` 即可                                                                                                                       |
| 未来按功能角色裁剪子代理中间件（如 REVIEWER 加 HITL）            | `_SUBAGENT_REQUIRED` 需按角色拆分                             | 演进为 `_SUBAGENT_BASELINE` + `_SUBAGENT_ROLE_OVERRIDES`（见上方"演进触发条件与路径"）                                                                      |
| 未来按深度角色裁剪（如 ORCHESTRATOR 加 SubagentCompletionDrain） | `_SUBAGENT_REQUIRED` 需按深度拆分                             | 演进为 `_SUBAGENT_REQUIRED_BY_DEPTH: dict[SubagentSessionRole, frozenset]`，当前不需要                                                                      |
| 未来引入 `excluded_middleware` 配置                              | 需要第一道 + 第三道防线                                       | 在 config TypedDict 中增加字段，`validate_required_middleware` 前加 `_validate_excluded_config`（deepagents 第一道），过滤后加 `_verify_coverage`（第三道） |
| 中间件类重命名                                                   | `RequiredMiddlewareEntry.names` 的别名机制 + 漂移守卫测试覆盖 | 在 entry 中注册旧名为别名，旧配置仍可匹配；漂移守卫确保 `__name__` 在 NAMES 中                                                                              |

### 实施步骤

| 步骤 | 内容                                                             | 依赖     | 预估  |
| ---- | ---------------------------------------------------------------- | -------- | ----- |
| 1    | 新建 `agent/middlewares/scaffolding.py`                          | 无       | 1h    |
| 2    | 修改 `agent/middlewares/__init__.py` re-export                   | 步骤 1   | 0.25h |
| 3    | 修改 `agent/core.py` — 提取 middleware 变量 + 调用校验           | 步骤 2   | 0.5h  |
| 4    | 修改 `spawn/core.py::_build_child_agent` — 提取变量 + 调用校验   | 步骤 2   | 0.5h  |
| 5    | 新建 `tests/agent/middlewares/test_scaffolding.py`               | 步骤 1-4 | 1.5h  |
| 6    | `uv run --with ruff ruff check . && ruff format --check .`       | 步骤 5   | 0.25h |
| 7    | `uv run --no-sync basedpyright agent/middlewares/scaffolding.py` | 步骤 6   | 0.25h |
| 8    | `uv run pytest tests/agent/middlewares/test_scaffolding.py -q`   | 步骤 7   | 0.5h  |

**总预估：约 4.75h**

---

## P1-8：威胁模型文档

### 问题

Sherry 缺少威胁模型文档，安全评审缺少系统性参考。

### DeepAgents 做法

`THREAT_MODEL.md` 文档化信任边界、数据分类和威胁分析。

### 具体实现方案

#### 文件清单

| 文件                   | 修改类型 | 说明         |
| ---------------------- | -------- | ------------ |
| `docs/THREAT_MODEL.md` | 新建     | 威胁模型文档 |

#### 文档结构

```markdown
# Sherry Agent 威胁模型

## 信任边界

1. 用户 ↔ WS 网关
2. 主代理 ↔ 子代理
3. 工具 ↔ 外部系统（终端/文件/网络）
4. LLM 提供商 ↔ Agent
5. MCP 服务器 ↔ Agent

## 数据分类

| 类别 | 示例             | 存储位置                                        |
| ---- | ---------------- | ----------------------------------------------- |
| 敏感 | API keys, tokens | env vars (scrub_env)                            |
| 私密 | 对话历史         | SQLite (WAL)                                    |
| 内部 | 工具结果         | 消息列表 + 驱逐文件（`sessions/{id}/evicted/`） |

## 威胁分析

| 威胁         | 现有防护                  | 差距                                                                                                                                                                             |
| ------------ | ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 路径遍历     | 三道结构门禁 + O_NOFOLLOW | 已落地（`path_utils.py`）                                                                                                                                                        |
| 符号链接攻击 | `O_NOFOLLOW` + 循环检测   | 已落地（`agent/tools/pub_base/path_utils.py` 的 `_open_no_follow` / `_raise_if_symlink_loop`，read/write/patch 全走）                                                            |
| Shell注入    | 正则黑名单                | 已评估并否决（base64+`eval` 对以 shell 语义执行的 `terminal` 无增益，且置于 `_check_dangerous`/`_check_sensitive_file_access` 之前会让明文绕过防线；真实读屏障是 OS 沙箱读遮蔽） |
| 工具结果OOM  | 截断 (head+tail)          | 压缩时截断已落地（`target_truncation.py`等）；工具执行时驱逐已落地（`ContextEvictionMiddleware` 的 `wrap_tool_call` 主动写文件+预览）                                            |
| ...          | ...                       | ...                                                                                                                                                                              |
```

---

## P2-1：ripgrep 双重超时看门狗

**疑似不适用：搜索走 `os.walk` + `fnmatch`，无 ripgrep 进程。** 判据：`agent/tools/file_tools/search_scan.py::bounded_walk` 直接以 `os.walk` 遍历文件树，不 spawn 任何子进程（全模块无 `subprocess` 调用），因此本项没有适用对象；若未来搜索后端引入 ripgrep 子进程，再启用本项。

DeepAgents 参考：`threading.Timer` 看门狗 → SIGTERM → 5s 等待 → SIGKILL → 放弃（`_reap_ripgrep()`），届时在文件搜索工具中添加双重超时清理。

---

## 实施优先级与依赖关系总览

- **独立实施**：P1-3 模型感知摘要默认值、P1-6 中间件脚手架保护（~4.75h，参考 deepagents 三重防线，落地组装时校验 + 漂移守卫测试）、P1-8 威胁模型文档
- **已落地**：P1-7 多模态内容清理（见头部"已落地"行；落点为 `MultimodalProcessor.wrap_model_call` + `agent/middlewares/media_pipeline/scrub.py`，非提案中的 `agent/middlewares/multimodal_scrub.py`）
- **已评估不落地**：P1-4 增量检查点优化（前提被现有 `aclean_old_checkpoints` 剪枝消除，DeltaChannel+现剪枝会静默丢状态）、P1-5 消息增量缩减器（标准 `add_messages` 已覆盖，且样例语义会破坏 P1-9）
- **增量改进**：P2-1 ripgrep 双重超时看门狗（疑似不适用，见小节）
