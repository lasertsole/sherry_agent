# 循环依赖消除计划

**创建日期**：2026-09-15
**状态**：已完成（2026-09-15）
**完成记录**：2026-09-15/16 — 9 组循环依赖全部消除（Phase 1–4，grep 双向零残留）；6 条 import-linter 契约已加入配置并通过反证验证（lint-imports 7 kept / 0 broken）；各 Phase 验收与全量验收（lint-imports / run_tests_split / basedpyright / ruff / 前端）全部通过。
**核实记录（启动前）**：2026-09-15 — 对 9 组循环逐组、双向 grep 复核（`agent→server`、`context_engine→agent`、`workspace→agent`、`models→agent`、`middlewares↔tools`、`config→models`、`context_engine↔workspace`、`agent↔skills`、`skills↔server`）：**零组已消除**，9 组全部仍存在，编号 1–9 连续，本轮无删改；计划尚未开始落地（`runtime/hooks.py`、`runtime/data_provider.py`、`pub/types/llm.py` 三个新建文件均不存在，Phase 1–3 均未实施）。

## 背景

项目存在 **9 组循环依赖**（8 跨包 + 1 包内），当前全部通过 lazy import 规避运行时错误，但掩盖了架构边界问题，增加了维护风险。其中 1 组违反 AGENTS.md 契约。

### 依赖全景

| #   | 循环对                         | 反向导入数                        | 类型 | 严重度                        |
| --- | ------------------------------ | --------------------------------- | ---- | ----------------------------- |
| 1   | agent ↔ server                 | 4 (agent→server, 全 lazy)         | 跨包 | 高                            |
| 2   | agent ↔ context_engine         | 8 (context_engine→agent, 全 lazy) | 跨包 | 中                            |
| 3   | agent ↔ workspace              | 6 (workspace→agent, 全 lazy)      | 跨包 | 中                            |
| 4   | agent ↔ models                 | 1 (models→agent, lazy)            | 跨包 | 低                            |
| 5   | middlewares ↔ tools            | 2 顶层 + 4 lazy (双向)            | 包内 | 中                            |
| 6   | **config ↔ models**            | 2 lazy                            | 跨包 | **高（违反 AGENTS.md 契约）** |
| 7   | **context_engine ↔ workspace** | 1+1 lazy                          | 跨包 | 中                            |
| 8   | **agent ↔ skills**             | 1 lazy (skills→agent)             | 跨包 | 中                            |
| 9   | **skills ↔ server**            | 2 lazy (skills→server)            | 跨包 | 中                            |

### 逐组明细

#### 1. agent ↔ server

```
# agent → server（4 处，全 lazy）
agent/middlewares/todo_continuation.py:177             → server.service.auto_turn.maybe_trigger_auto_turn
agent/tools/subagent/announce/delivery.py:162          → server.service.auto_turn.maybe_trigger_auto_turn
agent/tools/subagent/registry/session_state.py:104      → server.trigger.ws.messages
agent/tools/subagent/registry/session_state.py:134      → server.service.auto_turn

# server → agent（28 处，多为顶层）
server/service/messages.py:5                            → agent.built_agent (顶层)
server/service/heartbeat.py:21                          → agent.tools (顶层)
server/DAO/messages.py:6                               → agent.checkpointer (顶层)
server/service/auto_turn.py:29-31                       → agent.tools.subagent.* (顶层)
... 等
```

#### 2. agent ↔ context_engine

```
# context_engine → agent（8 处，全 lazy）
context_engine/session_continuity.py:196-197            → agent.tools.taskflow.registry, agent.tools.taskflow.tools._shared
context_engine/curator/orchestrator.py:403              → agent.tools.skill_tools.skill_manage
context_engine/curator/orchestrator.py:566,579,615      → agent.tools.skill_tools.skill_manage
context_engine/facts/queue.py:38                        → agent.tools.memory_tiered
```

#### 3. agent ↔ workspace

```
# workspace → agent（6 处，全 lazy）
workspace/prompt_builder.py:27                          → agent.tools.todolist.registry.store_sqlite
workspace/prompt_builder.py:121                          → agent.tools.taskflow.registry.store_sqlite
workspace/prompt_builder.py:122                          → agent.tools.taskflow.tools._shared
workspace/prompt_builder.py:187                          → agent.tools.todolist.knowledge.prompt_block
workspace/prompt_builder.py:266                          → agent.tools.memory
workspace/prompt_builder.py:283                          → agent.tools.memory_tiered
```

#### 4. agent ↔ models

```
# models → agent（1 处，lazy）
models/LLMs/main_llm.py:138                             → agent.middlewares.llm_retry.FallbackCandidate
```

#### 5. middlewares ↔ tools（agent 包内）

```
# middlewares → tools（16 处，其中 2 处顶层）
agent/middlewares/subagent_completion_drain.py:38        → agent.tools.subagent.announce.steering_queue (顶层)
agent/middlewares/todo_continuation.py:34-35            → agent.tools.todolist.* (顶层)
agent/middlewares/context_engine/nudge.py:430,466,634,641,684,745 → agent.tools.* (lazy)
agent/middlewares/context_engine/core.py:119            → agent.tools.todolist (lazy)
agent/middlewares/summarization.py:289-290,1529,1876,1979 → agent.tools.* (lazy)

# tools → middlewares（4 处，全 lazy）
agent/tools/question.py:112                             → agent.middlewares.humanInTheLoop.approval
agent/tools/subagent/spawn/core.py:634,750,758         → agent.middlewares.*
```

#### 6. config ↔ models — 违反 AGENTS.md 契约

```
# config → models（2 处，lazy）
config/schema.py:185                                    → models.providers.registry.PROVIDERS
config/schema.py:260                                    → models.providers.registry.find_by_name
```

AGENTS.md 明确规定 "config MUST NOT import from agent/, server/, or models/"。这两处虽在 `config/schema.py` 而非 `config/features/`，但 config 作为底层包不应反向依赖 models。

#### 7. context_engine ↔ workspace

```
# context_engine → workspace（1 处，lazy）
context_engine/curator/orchestrator.py:500              → workspace.prompt_builder.build_system_prompt

# workspace → context_engine（1 处，lazy）
workspace/prompt_builder.py:177                         → context_engine.session_continuity.build_continuity_prompt
```

#### 8. agent ↔ skills

```
# agent → skills（5 处）
agent/core.py:3                                          → from skills import build_skills_snapshot (顶层!)
agent/tools/skill_tools/skill_manage.py:956             → from skills import build_skills_snapshot (lazy)
agent/tools/skill_tools/skill_view.py:188               → from skills.loader import scan_skills (lazy)
agent/tools/skill_tools/skill_list.py:41                → from skills.loader import scan_skills (lazy)
agent/tools/subagent/delegate.py:45                     → from skills.loader import get_skills_text, scan_skills (顶层)

# skills → agent（1 处，lazy）
skills/builtin/core/cron/scripts/base.py:472            → from agent.tools import ... (lazy)
```

#### 9. skills ↔ server

```
# server → skills（10 处，多为顶层）
server/__main__.py:114                                  → skills.builtin.core.cron.scripts (顶层)
server/service/heartbeat.py:23                          → skills.builtin.core.heartbeat.scripts (顶层)
server/trigger/http/cron.py:18                          → skills.builtin.core.cron.scripts (顶层)
server/trigger/http/skills/catalog.py:5                → skills.loader (顶层)
... 等

# skills → server（2 处，lazy）
skills/skills_snapshot.py:23                            → server.service.skill_scanner.scan_skill
skills/builtin/core/clawhub/scripts/clawhub_runner.py:118 → server.service.skill_scanner
```

### 非循环（单向）的跨包依赖补充

以下为单向依赖，不构成循环，但记录备查：

| 方向                                                 | 导入数                  | 说明                                  |
| ---------------------------------------------------- | ----------------------- | ------------------------------------- |
| context_engine → runtime                             | 3 处（1 顶层 + 2 lazy） | 合理，runtime 是中间层                |
| workspace → runtime                                  | 1 lazy                  | 合理                                  |
| workspace → context_engine                           | 1 lazy                  | 见循环 #7 的另一方向                  |
| skills → {models, bus, channels, workspace, runtime} | 多处                    | skills 是用户脚本，引用系统能力，单向 |

### 核心原则

`runtime/` 已是跨包共享状态的中间层（`state_register`、`relation_register`、`count_call_register` 等）。本次改造扩展该模式：

- **类型定义** 下沉到 `pub/types/`
- **跨包回调** 通过 `runtime/hooks.py` 注册
- **只读数据访问** 通过 `runtime/data_provider.py` Protocol 模式
- **包内循环** 改为 lazy import
- **契约违规** 通过回调注入修复

改造后所有依赖箭头单向，无循环。

---

## Phase 1 — 快速修复（低风险）

### 1.1 FallbackCandidate 移到 pub/types/

**目标**：消除循环 #4（agent ↔ models）。

**改动**：

| 操作      | 文件                                                                                     |
| --------- | ---------------------------------------------------------------------------------------- |
| 新建      | `pub/types/llm.py` — 放 `FallbackCandidate` dataclass                                    |
| 改 import | `agent/middlewares/llm_retry.py:80` — 改为 `from pub.types.llm import FallbackCandidate` |
| 改 import | `models/LLMs/main_llm.py:138` — 改为 `from pub.types.llm import FallbackCandidate`       |

**风险**：极低，纯类型搬移。

### 1.2 middlewares→tools 顶层 import 改 lazy

**目标**：消除循环 #5 中的 2 处顶层导入。

**改动**：

```
agent/middlewares/subagent_completion_drain.py:38
  改前: from agent.tools.subagent.announce.steering_queue import drain, rehydrate  (顶层)
  改后: 函数内 lazy import

agent/middlewares/todo_continuation.py:34-35
  改前: from agent.tools.todolist.registry.store_sqlite import get_todos_sync  (顶层)
        from agent.tools.todolist.stagnation_tracker import ...
  改后: 函数内 lazy import
```

**风险**：极低。

### 1.3 config→models 改回调注入

**目标**：消除循环 #6（config ↔ models），修复 AGENTS.md 契约违规。

**改动**：

```
# config/schema.py — 改前
from models.providers.registry import PROVIDERS        (line 185, lazy)
from models.providers.registry import find_by_name      (line 260, lazy)

# config/schema.py — 改后
# 不直接 import models，改为通过注册的回调获取
_provider_registry_fn: Callable | None = None
_provider_find_fn: Callable | None = None

def set_provider_registry(get_providers: Callable, find_by_name: Callable) -> None:
    global _provider_registry_fn, _provider_find_fn
    _provider_registry_fn = get_providers
    _provider_find_fn = find_by_name
```

- **注册方**：`server/__main__.py` 或 `models/__init__.py` 启动时注册
- **调用方**：`config/schema.py` 改为调用 `_provider_registry_fn()` / `_provider_find_fn()`

**风险**：低。config 中 Pydantic schema 校验时需要 provider 列表，但可在运行时注入。

**验收**：`uv run --no-sync basedpyright agent/ config/` 0 errors；`uv run pytest tests/agent/middlewares tests/config -q -k "not llm_e2e"` 全过。

---

## Phase 2 — runtime/hooks.py 回调注册（消除 agent→server）

**目标**：消除循环 #1（agent ↔ server）。agent 不再知道 server 的存在。

### 问题根因

agent 有 4 处 lazy import 到 server，本质是两种调用：

| 调用                                              | 用途                                      | 调用方                                                 |
| ------------------------------------------------- | ----------------------------------------- | ------------------------------------------------------ |
| `maybe_trigger_auto_turn(session_key, injection)` | todo 完成 / subagent 交付后触发新对话轮次 | `todo_continuation.py:177`, `announce/delivery.py:162` |
| `ws_messages.set_hitl_pending(session_id)`        | subagent HITL 等待用户审批                | `subagent/registry/session_state.py:104,134`           |

### 改动

**新建 `runtime/hooks.py`**：

```python
from typing import Any, Callable

_registry: dict[str, Callable[..., Any]] = {}

def register(name: str, fn: Callable[..., Any]) -> None:
    _registry[name] = fn

def resolve(name: str) -> Callable[..., Any] | None:
    return _registry.get(name)
```

**agent 侧改 4 处**：

```
agent/middlewares/todo_continuation.py:177
  改前: from server.service.auto_turn import maybe_trigger_auto_turn
  改后: _fn = runtime.hooks.resolve("maybe_trigger_auto_turn")
        if _fn: await _fn(session_key, injection)

agent/tools/subagent/announce/delivery.py:162
  同上

agent/tools/subagent/registry/session_state.py:104
  改前: from server.trigger.ws import messages as ws_messages
  改后: _fn = runtime.hooks.resolve("set_hitl_pending")
        if _fn: _fn(session_id)

agent/tools/subagent/registry/session_state.py:134
  改前: from server.service import auto_turn as auto_turn_module
  改后: _fn = runtime.hooks.resolve("auto_turn_module")
        ... 使用 _fn 替代 auto_turn_module
```

**server 侧注册**（`server/__main__.py` 启动时）：

```python
from runtime import hooks
from server.service.auto_turn import maybe_trigger_auto_turn
from server.trigger.ws.messages import set_hitl_pending  # 需导出此函数

hooks.register("maybe_trigger_auto_turn", maybe_trigger_auto_turn)
hooks.register("set_hitl_pending", set_hitl_pending)
```

### 涉及文件

| 文件                                             | 操作                          |
| ------------------------------------------------ | ----------------------------- |
| `runtime/hooks.py`                               | 新建                          |
| `agent/middlewares/todo_continuation.py`         | 改 1 处                       |
| `agent/tools/subagent/announce/delivery.py`      | 改 1 处                       |
| `agent/tools/subagent/registry/session_state.py` | 改 2 处                       |
| `server/__main__.py`                             | 加注册代码                    |
| `server/trigger/ws/messages.py`                  | 可能需导出 `set_hitl_pending` |

### 风险

- 中等。需确认所有调用点的调用签名和返回值处理一致。
- `session_state.py:134` 的 `auto_turn_module` 用法较复杂（可能是模块级引用而非单函数），需单独分析。

### 验收

- `uv run --no-sync basedpyright agent/ runtime/` 0 errors
- `uv run pytest tests/agent/ -q -k "not llm_e2e"` 全过
- `uv run --no-sync lint-imports` 通过（agent 不再 import server）

---

## Phase 3 — runtime/data_provider.py 数据访问层

**目标**：消除循环 #2（agent ↔ context_engine）、循环 #3（agent ↔ workspace）、循环 #7（context_engine ↔ workspace）。workspace 和 context_engine 不再直接 import agent，context_engine 和 workspace 之间也不再互相 import。

### 问题根因

| 消费方                                   | 需要的数据               | 当前来源                                                    |
| ---------------------------------------- | ------------------------ | ----------------------------------------------------------- |
| `workspace/prompt_builder.py`            | todos                    | `agent.tools.todolist.registry.store_sqlite.get_todos_sync` |
| `workspace/prompt_builder.py`            | taskflow 快照            | `agent.tools.taskflow.registry.store_sqlite`                |
| `workspace/prompt_builder.py`            | memory 文件              | `agent.tools.memory.memory_store`                           |
| `workspace/prompt_builder.py`            | tiered facts             | `agent.tools.memory_tiered.get_tiered_store`                |
| `workspace/prompt_builder.py`            | todolist knowledge block | `agent.tools.todolist.knowledge.prompt_block`               |
| `workspace/prompt_builder.py`            | continuity prompt        | `context_engine.session_continuity.build_continuity_prompt` |
| `context_engine/session_continuity.py`   | taskflow registry        | `agent.tools.taskflow.registry.store_sqlite`                |
| `context_engine/facts/queue.py`          | tiered memory            | `agent.tools.memory_tiered.get_tiered_store`                |
| `context_engine/curator/orchestrator.py` | skill 创建/写入          | `agent.tools.skill_tools.skill_manage`                      |
| `context_engine/curator/orchestrator.py` | system prompt            | `workspace.prompt_builder.build_system_prompt`              |

### 改动

**新建 `runtime/data_provider.py`**：

```python
from typing import Protocol

class PromptDataProvider(Protocol):
    def get_todos(self, session_id: str) -> list[dict]: ...
    def get_taskflow_snapshot(self, session_id: str) -> dict: ...
    def get_memory_files(self) -> dict[str, str]: ...
    def get_tiered_facts(self) -> list[str]: ...
    def build_todolist_knowledge_block(self, session_id: str) -> str: ...
    def build_continuity_prompt(self, session_id: str) -> str: ...
    def build_system_prompt(self, session_id: str) -> str: ...

class SkillWriteProvider(Protocol):
    def create_skill(self, name: str, content: str) -> str: ...
    def write_file(self, path: str, content: str) -> None: ...

_provider: PromptDataProvider | None = None
_skill_writer: SkillWriteProvider | None = None

def set_prompt_data_provider(p: PromptDataProvider) -> None:
    global _provider
    _provider = p

def get_prompt_data_provider() -> PromptDataProvider | None:
    return _provider

def set_skill_write_provider(p: SkillWriteProvider) -> None:
    global _skill_writer
    _skill_writer = p

def get_skill_write_provider() -> SkillWriteProvider | None:
    return _skill_writer
```

**agent 侧注册**（`agent/core.py` 的 `init()` 中）：

```python
from runtime import data_provider
from agent.tools.todolist.registry.store_sqlite import get_todos_sync
from agent.tools.taskflow.registry import store_sqlite as taskflow_store
from agent.tools.memory import memory_store
from agent.tools.memory_tiered import get_tiered_store
from agent.tools.todolist.knowledge.prompt_block import build_knowledge_block
from workspace.prompt_builder import build_system_prompt

class _ConcretePromptDataProvider:
    def get_todos(self, session_id: str) -> list[dict]:
        return get_todos_sync(session_id)
    def get_taskflow_snapshot(self, session_id: str) -> dict:
        return taskflow_store.get_snapshot(session_id)
    def get_memory_files(self) -> dict[str, str]:
        return memory_store.read_all()
    def get_tiered_facts(self) -> list[str]:
        return get_tiered_store().list_facts()
    def build_todolist_knowledge_block(self, session_id: str) -> str:
        return build_knowledge_block(session_id)
    def build_continuity_prompt(self, session_id: str) -> str:
        from context_engine.session_continuity import build_continuity_prompt
        return build_continuity_prompt(session_id)
    def build_system_prompt(self, session_id: str) -> str:
        return build_system_prompt(session_id)

data_provider.set_prompt_data_provider(_ConcretePromptDataProvider())
```

**workspace/prompt_builder.py 改 7 处**（6 处 agent + 1 处 context_engine）：

```python
# 改前
from agent.tools.todolist.registry.store_sqlite import get_todos_sync
from context_engine.session_continuity import build_continuity_prompt

# 改后
from runtime.data_provider import get_prompt_data_provider

def _read_todos_sync(session_id: str) -> list[dict]:
    p = get_prompt_data_provider()
    return p.get_todos(session_id) if p else []

def _read_continuity_prompt(session_id: str) -> str:
    p = get_prompt_data_provider()
    return p.build_continuity_prompt(session_id) if p else ""
```

**context_engine 改 9 处**（8 处 agent + 1 处 workspace）：

```python
# context_engine/curator/orchestrator.py:500 — 改前
from workspace.prompt_builder import build_system_prompt
# 改后
from runtime.data_provider import get_prompt_data_provider
p = get_prompt_data_provider()
prompt = p.build_system_prompt(session_id) if p else ""
```

### 涉及文件

| 文件                                     | 操作                                         |
| ---------------------------------------- | -------------------------------------------- |
| `runtime/data_provider.py`               | 新建                                         |
| `agent/core.py`                          | 加注册代码                                   |
| `workspace/prompt_builder.py`            | 改 7 处 import（6 agent + 1 context_engine） |
| `context_engine/session_continuity.py`   | 改 2 处 import（agent）                      |
| `context_engine/facts/queue.py`          | 改 1 处 import（agent）                      |
| `context_engine/curator/orchestrator.py` | 改 5 处 import（4 agent + 1 workspace）      |

### 风险

- 较高，涉及文件多，签名需逐一核对。
- `skill_manage` 的写操作（`_write_file`、`_create_skill`）需要确认完整签名。
- `build_continuity_prompt` 和 `build_system_prompt` 互相引用（循环 #7），通过 data_provider 统一入口后，注册方在 agent/core.py 统一管理，消除 context_engine ↔ workspace 的直接互引。
- 建议分批：先迁移 PromptDataProvider（7 个方法），再迁移 SkillWriteProvider。

### 验收

- `uv run --no-sync basedpyright workspace/ context_engine/ runtime/` 0 errors
- `uv run pytest tests/workspace/ tests/context_engine/ -q` 全过
- `grep -r "from agent" workspace/ context_engine/` 无结果
- `grep -r "from workspace" context_engine/` 无结果
- `grep -r "from context_engine" workspace/` 无结果

---

## Phase 4 — skills 解耦（消除 skills→agent + skills→server）

**目标**：消除循环 #8（agent ↔ skills）和循环 #9（skills ↔ server）。

### 问题根因

```
# skills → server（2 处，lazy）
skills/skills_snapshot.py:23                            → server.service.skill_scanner.scan_skill
skills/builtin/core/clawhub/scripts/clawhub_runner.py:118 → server.service.skill_scanner

# skills → agent（1 处，lazy）
skills/builtin/core/cron/scripts/base.py:472            → agent.tools

# agent → skills（5 处，含 2 顶层）
agent/core.py:3                                          → from skills import build_skills_snapshot (顶层!)
agent/tools/subagent/delegate.py:45                     → from skills.loader import get_skills_text, scan_skills (顶层)
agent/tools/skill_tools/skill_manage.py:956             → from skills import build_skills_snapshot (lazy)
agent/tools/skill_tools/skill_view.py:188               → from skills.loader import scan_skills (lazy)
agent/tools/skill_tools/skill_list.py:41                → from skills.loader import scan_skills (lazy)

# server → skills（10 处，多为顶层）
server/__main__.py:114                                  → skills.builtin.core.cron.scripts (顶层)
server/service/heartbeat.py:23                          → skills.builtin.core.heartbeat.scripts (顶层)
... 等
```

### 改动

**skills→server**：将 `scan_skill` 函数通过 `runtime/hooks.py` 注册：

```python
# runtime/hooks.py 复用 Phase 2 的注册器
# server 注册
hooks.register("scan_skill", scan_skill)

# skills/skills_snapshot.py 改前
from server.service.skill_scanner import scan_skill
# 改后
_fn = hooks.resolve("scan_skill")
```

**skills→agent**：cron/base.py 的 `from agent.tools import ...` 改为通过 `runtime/hooks.py` 或 `runtime/data_provider.py` 获取。

**agent→skills**：`build_skills_snapshot` 是 agent 启动时的 import-time 调用（`agent/core.py:3`），需改为 lazy import 或通过注册模式。`skills.loader` 的 scan/get_skills_text 是纯文件扫描，可考虑下沉到 `pub/`。

### 涉及文件

| 文件                                                    | 操作                                  |
| ------------------------------------------------------- | ------------------------------------- |
| `skills/skills_snapshot.py`                             | 改 1 处 import                        |
| `skills/builtin/core/cron/scripts/base.py`              | 改 1 处 import                        |
| `skills/builtin/core/clawhub/scripts/clawhub_runner.py` | 改 1 处 import                        |
| `agent/core.py`                                         | `build_skills_snapshot` 改 lazy       |
| `agent/tools/subagent/delegate.py`                      | `skills.loader` import 改 lazy 或下沉 |
| `server/__main__.py`                                    | 注册 `scan_skill` 到 hooks            |

### 风险

- 中等。`agent/core.py:3` 的顶层 import 是 import-time 副作用，AGENTS.md Known Pitfalls 已标注（"calls build_skills_snapshot() at import time"），改为 lazy 需确认调用时序。
- `skills.loader` 的 scan_skills / get_skills_text 是纯文件系统操作，下沉到 `pub/` 风险低。
- `skills/builtin/` 下的用户脚本（cron、clawhub 等）引用系统能力是预期行为，但不应直接 import agent/server。

### 验收

- `uv run --no-sync basedpyright agent/ skills/ runtime/` 0 errors
- `uv run pytest tests/agent/ tests/skills/ -q -k "not llm_e2e"` 全过
- `grep -r "from server" skills/skills_snapshot.py skills/builtin/core/cron/ skills/builtin/core/clawhub/` 无结果
- `grep -r "from agent" skills/` 无结果（builtin 用户脚本除外，另行评估）

---

## 改造后依赖图

```
config ←── pub ←── models
   ↑           ↑
   │       runtime (hooks + data_provider)
   │           ↑
   ├── context_engine ──┘
   ├── workspace ───────┘
   │
   agent ←──── runtime
   agent ←──── models
   agent ←──── context_engine
   agent ←──── pub
   agent ←──── skills
   │
   server ←── agent (单向)
   server ←── runtime (注册回调)
   server ←── context_engine
   server ←── skills
   │
   skills ←── pub (若 loader 下沉)
   skills ←── runtime (若通过 hooks 获取 scan_skill)
```

所有箭头单向，无循环。

---

## 落地节奏

| 阶段     | 内容                                                                  | 涉及文件数 | 预计工时  | 风险 | 消除循环   |
| -------- | --------------------------------------------------------------------- | ---------- | --------- | ---- | ---------- |
| Phase 1  | 1.1 FallbackCandidate + 1.2 middlewares lazy + 1.3 config→models 回调 | ~8         | 1 day     | 低   | #4, #5, #6 |
| Phase 2  | runtime/hooks.py 回调注册                                             | ~6         | 1-2 days  | 中   | #1         |
| Phase 3a | PromptDataProvider (7 方法)                                           | ~8         | 1-2 days  | 较高 | #2, #3, #7 |
| Phase 3b | SkillWriteProvider (2 方法)                                           | ~5         | 0.5-1 day | 中   | #2 剩余    |
| Phase 4  | skills 解耦                                                           | ~6         | 1-2 days  | 中   | #8, #9     |
| 全量验收 | lint + typecheck + 全量测试                                           | -          | 0.5 day   | -    | -          |

**总计**：约 5-8 个工作日。

---

## 注意事项

1. **每个 Phase 独立可交付**，Phase 1 失败不影响后续评估
2. **Phase 3 可按方法粒度拆分**，每次迁移一个 provider 方法即可验证
3. **Phase 4 的 `agent/core.py:3` 顶层 import 是已知 import-time 副作用**（AGENTS.md Known Pitfalls 已标注），改为 lazy 需确认 `build_skills_snapshot()` 的调用时序
4. **import-linter 契约需同步更新**，在 `importlinter` 配置中添加：
   - `agent MUST NOT import server`
   - `config MUST NOT import models`
   - `context_engine MUST NOT import agent`
   - `workspace MUST NOT import agent`
   - `workspace MUST NOT import context_engine`
   - `skills MUST NOT import server`（builtin 用户脚本除外）
5. **测试镜像目录需同步调整**：`tests/context_engine/` 和 `tests/workspace/` 中如有直接 import agent 的测试，也需改为通过 provider
6. **文档同步**：AGENTS.md 的 Known Pitfalls 章节需更新，移除已解决的循环依赖说明
7. **Phase 3 的 `build_continuity_prompt` 和 `build_system_prompt`** 互相引用（循环 #7），通过 data_provider 统一入口后，注册方在 `agent/core.py` 统一管理，消除 context_engine ↔ workspace 的直接互引
8. **skills/builtin/ 用户脚本**的依赖范围（引用 models/bus/channels/workspace/runtime）是否需要约束，另行评估，不在本次计划范围内
