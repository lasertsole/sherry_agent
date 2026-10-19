# 迁移计划：把 EvidenceLedger 从 `todolist/` 抽出到 `pub_base/`

> **状态**：提案 · **范围**：仅 `agent/tools/todolist/evidence_*.py`
> **前置条件**：任何未来的 `todolist` 移除工作（taskflow 通过 `evidence_collector.py` 硬依赖 `EvidenceLedger`）。
> **风险等级**：低 —— 两个自包含的文件，无行为变更。

## 1. 目标

把 `evidence_ledger.py` 和 `evidence_recorder.py` 从 `agent/tools/todolist/` 迁出到 `agent/tools/pub_base/`，更新所有引用方。无行为变更、无配置变更、无存储路径变更。这两个文件今天在物理上跟 todolist 同居，但在概念上是个跨工具的审计层，被 `terminal`、`python_repl`、`write_file`、`patch_file`、`subagent/announce/workspace_merge` 和 `taskflow/evidence_collector` 使用 —— 没有一个是 todolist 概念。

这一步为后续更大的"删除 todolist"工作扫清障碍，移除 taskflow 对 todolist 包的唯一反向依赖。

## 2. 当前状态 —— 完整依赖面

通过全仓库 `grep -rn "from agent.tools.todolist.evidence"` 验证。

### 2.1 待迁移的两个文件

| 文件 | 它的 import | 备注 |
|---|---|---|
| `agent/tools/todolist/evidence_ledger.py` | `agent.tools.pub_base.file_write_lock`, `config.path.resolve_evidence_ledger_path` | 自包含。已经在用 `pub_base`。 |
| `agent/tools/todolist/evidence_recorder.py:17` | `config.features.EVIDENCE_LEDGER`, `agent.tools.todolist.evidence_ledger` | 唯一的 todolist 内部依赖是同目录的兄弟文件。 |

### 2.2 写入侧消费者（import `evidence_recorder`）

| 文件:行 | import 的符号 |
|---|---|
| `agent/tools/terminal.py:76` | `record_verification_evidence` |
| `agent/tools/python_repl.py:51` | `record_verification_evidence` |
| `agent/tools/file_tools/write_file.py:55` | `invalidates_evidence` |
| `agent/tools/file_tools/patch_file.py:51` | `invalidates_evidence` |
| `agent/tools/subagent/announce/workspace_merge.py:59` | `mark_evidence_stale_for_paths`（函数内 lazy import） |

### 2.3 读取侧消费者（import `evidence_ledger`）

| 文件:行 | import 的符号 |
|---|---|
| `agent/tools/taskflow/evidence_collector.py:12` | `EvidenceLedger` |

### 2.4 引用任一模块的测试文件

| 测试文件 | 它 import 的内容 |
|---|---|
| `tests/agent/tools/todolist/test_evidence_ledger.py:13,151` | `EvidenceLedger`；还有 `from agent.tools.todolist import evidence_ledger as module` |
| `tests/agent/tools/test_evidence_auto_record.py:12-13` | `evidence_recorder`；`EvidenceLedger` |
| `tests/agent/tools/test_evidence_stale.py:9` | `EvidenceLedger` |
| `tests/agent/tools/taskflow/test_evidence_collector.py:7` | `EvidenceLedger` |
| `tests/agent/tools/taskflow/test_evidence_concurrent.py:17` | `EvidenceLedger` |
| `tests/agent/tools/taskflow/test_finish_gate.py:23` | `EvidenceLedger` |
| `tests/agent/tools/taskflow/test_chain_smoke.py:29` | `EvidenceLedger` |
| `tests/agent/middlewares/test_completion_drain_gate.py:25` | `EvidenceLedger` |

### 2.5 不需要迁移的部分

| 内容 | 位置 | 为什么不动 |
|---|---|---|
| `EVIDENCE_LEDGER` 配置 TypedDict + 实例 | `config/features/agent_side/evidence_ledger.py` | 已经在 `config/` 下，完全跟 `todolist/` 解耦。`verify_commands` 模式表是运行时配置，不是 todolist 的事。 |
| `resolve_evidence_ledger_path()` | `config/path.py:354-361` | 解析为 `SRC_DIR/data/evidence-ledger.jsonl`。模块位置跟文件路径无关。 |
| JSONL 文件本身 | `src/data/evidence-ledger.jsonl` | append-only 审计轨迹。迁移不能动它。 |
| `import-linter` 契约 | `pyproject.toml` | 没有契约专门命名 `todolist`、`pub_base` 或 `evidence`。Agent 层内部迁移不受约束。 |

### 2.6 文档引用（在阶段 4 更新）

`AGENTS.md` 在多处提到 evidence（目录表里的 todolist 行、completion-gates 列表里的 `EvidenceLedger` 段落、`taskflow/evidence_collector.py:12` 反向依赖标注）。`grep -rn "evidence" AGENTS.md docs/` 给出完整列表 —— 在代码迁移**之后**更新，不要之前。

## 3. 目标位置：`agent/tools/pub_base/`

`pub_base/` 已经托管 26 个跨工具的 leaf 模块（`file_lock`、`path_lock`、`path_utils`、`tool_utils`、`read_state`、`git_ensure`、`atomic_write`、`sqlite_store`、`text_matcher`、sandbox 家族、rg 家族、skill_* 助手……）。它是"每个工具都能用、不跨包边界"的共享基础设施的既定归宿。

`evidence_ledger` 已经 import `pub_base.file_write_lock`；放进 `pub_base/` 让它变成兄弟引用，也匹配概念层（审计轨迹 = 共享基础设施，不是 todolist 的私有存储）。

**未选**：新建顶层 `agent/tools/evidence/` 包。理由：不必要的新目录、更长的 import 路径、`pub_base/` 已经表达了"共享 leaf"契约。

## 4. 分阶段执行

每个阶段以测试全绿结束。阶段之间仓库处于可用状态，所以一个阶段就是一个安全的 commit 边界。

### 阶段 0 —— 预检（无代码改动）

| 检查项 | 命令 | 预期 |
|---|---|---|
| Baseline 全绿 | `uv run --no-sync pytest tests/agent/tools/todolist/test_evidence_ledger.py tests/agent/tools/test_evidence_auto_record.py tests/agent/tools/test_evidence_stale.py tests/agent/tools/taskflow/test_evidence_collector.py tests/agent/tools/taskflow/test_evidence_concurrent.py tests/agent/tools/taskflow/test_finish_gate.py tests/agent/tools/taskflow/test_chain_smoke.py tests/agent/middlewares/test_completion_drain_gate.py -q` | 全部通过。 |
| import-linter baseline | `uv run --no-sync lint-imports` | 通过。 |
| 无隐藏 re-export | `grep -n "evidence" agent/tools/todolist/__init__.py agent/tools/__init__.py` | 空 —— §2 已确认。 |

任一检查失败，先修再开始。不要在红色树上启动阶段 1。

### 阶段 1 —— 迁移 `evidence_ledger.py`（被依赖方）

顺序很重要：`evidence_recorder` 依赖 `evidence_ledger`，所以先把 leaf 迁过去。

**动作：**

1. `git mv agent/tools/todolist/evidence_ledger.py agent/tools/pub_base/evidence_ledger.py`
2. 迁移后的文件内部不需要改 import —— 它已经用 `from agent.tools.pub_base import file_write_lock`，现在解析为兄弟。
3. 更新 `agent/tools/todolist/evidence_recorder.py:17`：
   ```python
   # before
   from agent.tools.todolist.evidence_ledger import EvidenceLedger
   # after（绝对路径 —— 避免触发 pub_base/__init__.py 全量加载）
   from agent.tools.pub_base.evidence_ledger import EvidenceLedger
   ```
4. 更新 `agent/tools/taskflow/evidence_collector.py:12`：
   ```python
   from agent.tools.pub_base.evidence_ledger import EvidenceLedger
   ```
5. 更新测试 import —— 机械替换模块路径：
   - `tests/agent/tools/todolist/test_evidence_ledger.py:13,151` —— 同时改 `from … import EvidenceLedger` 和 `from agent.tools.todolist import evidence_ledger as module` 两种形式，指向 `agent.tools.pub_base.evidence_ledger`。
   - `tests/agent/tools/test_evidence_auto_record.py:13`
   - `tests/agent/tools/test_evidence_stale.py:9`
   - `tests/agent/tools/taskflow/test_evidence_collector.py:7`
   - `tests/agent/tools/taskflow/test_evidence_concurrent.py:17`
   - `tests/agent/tools/taskflow/test_finish_gate.py:23`
   - `tests/agent/tools/taskflow/test_chain_smoke.py:29`
   - `tests/agent/middlewares/test_completion_drain_gate.py:25`

**验证：**
```bash
uv run --no-sync pytest tests/agent/tools/todolist/test_evidence_ledger.py \
  tests/agent/tools/test_evidence_auto_record.py tests/agent/tools/test_evidence_stale.py \
  tests/agent/tools/taskflow/test_evidence_collector.py tests/agent/tools/taskflow/test_evidence_concurrent.py \
  tests/agent/tools/taskflow/test_finish_gate.py tests/agent/tools/taskflow/test_chain_smoke.py \
  tests/agent/middlewares/test_completion_drain_gate.py -q
uv run --no-sync lint-imports
uv run --with ruff ruff check agent/tools/pub_base/evidence_ledger.py agent/tools/todolist/evidence_recorder.py agent/tools/taskflow/evidence_collector.py
```

**出口条件**：全绿；`grep -rn "from agent.tools.todolist.evidence_ledger"` 零命中。

### 阶段 2 —— 迁移 `evidence_recorder.py`（依赖方）

**动作：**

1. `git mv agent/tools/todolist/evidence_recorder.py agent/tools/pub_base/evidence_recorder.py`
2. 文件内部，把阶段 1 第 3 步改过的 import 改成相对 import（既然现在是兄弟，更干净）：
   ```python
   # before（阶段 1 设置的）
   from agent.tools.pub_base.evidence_ledger import EvidenceLedger
   # after
   from .evidence_ledger import EvidenceLedger
   ```
3. 更新写入侧消费者 —— 机械替换模块路径：
   - `agent/tools/terminal.py:76` → `from agent.tools.pub_base.evidence_recorder import record_verification_evidence`
   - `agent/tools/python_repl.py:51` → 同样模块路径
   - `agent/tools/file_tools/write_file.py:55` → `from agent.tools.pub_base.evidence_recorder import invalidates_evidence`
   - `agent/tools/file_tools/patch_file.py:51` → 同样
   - `agent/tools/subagent/announce/workspace_merge.py:59` → `_mark_merged_paths_stale` 内的 lazy import，同样改路径
4. 更新唯一一个 import recorder 模块的测试：
   - `tests/agent/tools/test_evidence_auto_record.py:12` ——
     `from agent.tools.todolist import evidence_recorder` →
     `from agent.tools.pub_base import evidence_recorder`

**验证：**
```bash
uv run --no-sync pytest tests/agent/tools/ tests/agent/middlewares/test_completion_drain_gate.py -q -k "evidence or terminal or python_repl or write_file or patch_file or workspace_merge or completion_drain"
uv run --no-sync lint-imports
uv run --with ruff ruff check agent/tools/pub_base/evidence_recorder.py agent/tools/terminal.py agent/tools/python_repl.py agent/tools/file_tools/write_file.py agent/tools/file_tools/patch_file.py agent/tools/subagent/announce/workspace_merge.py
uv run --no-sync basedpyright agent/tools/pub_base/evidence_ledger.py agent/tools/pub_base/evidence_recorder.py
```

**出口条件**：全绿；全仓库 `grep -rn "from agent.tools.todolist.evidence"` 零命中（不算这份文档本身）。

### 阶段 3 —— 测试文件归位

**动作：**

1. `git mv tests/agent/tools/todolist/test_evidence_ledger.py tests/agent/tools/pub_base/test_evidence_ledger.py`
2. 打开迁移后的文件，确认它的 import 已经指向 `agent.tools.pub_base.evidence_ledger`（阶段 1 第 5 步改过）。如果文件里有任何测试引用了 todolist 的 fixture 集（比如 session-scoped `tmp_path` provider、`conftest` autouse），验证那些还生效 —— `tests/agent/tools/pub_base/conftest.py` 应该已经覆盖共享 fixture；如果没有，把最小 fixture 复制过去，不要从 `tests/agent/tools/todolist/conftest.py` 重新 import。
3. 检查 `tests/agent/tools/todolist/conftest.py` 里有没有 `test_evidence_ledger.py` 依赖的 evidence 专属 fixture 或 stub；如果有，把那个 fixture 迁到 `tests/agent/tools/pub_base/conftest.py`（缺则创建）。

**验证：**
```bash
uv run --no-sync pytest tests/agent/tools/pub_base/test_evidence_ledger.py -q
uv run --no-sync pytest tests/agent/tools/todolist/ -q   # 剩余 todolist 测试仍通过
```

**出口条件**：`test_evidence_ledger.py` 在新家跑通；剩余 todolist 测试不受影响。

### 阶段 4 —— 文档与 lint 整理

**动作：**

1. 更新 `AGENTS.md`：
   - 目录结构表：`agent/tools/todolist/` 那行的"append-only evidence ledger/recorder"描述移到 `agent/tools/pub_base/` 行。
   - completion-gates 章节里标注 `agent/tools/taskflow/evidence_collector.py:12` 反向依赖 `todolist.evidence_ledger` 的部分，改成对 `pub_base.evidence_ledger` 的同包依赖。
   - 任何其他 `grep -n "evidence" AGENTS.md` 命中、上下文跟 todolist 绑定的句子 —— 重新锚定到 `pub_base`。
2. 如果 `grep -rn "todolist.evidence\|todolist import evidence" docs/` 在其他文档有命中，一并更新。
3. 重新跑 import-linter、ruff、basedpyright 覆盖触及的树：
   ```bash
   uv run --no-sync lint-imports
   uv run --with ruff ruff check . && uv run --with ruff ruff format --check .
   uv run --no-sync basedpyright agent/tools/pub_base/ agent/tools/taskflow/evidence_collector.py agent/tools/terminal.py agent/tools/python_repl.py agent/tools/file_tools/ agent/tools/subagent/announce/workspace_merge.py
   ```
4. 重新跑文档 gate：
   ```bash
   uv run --no-sync python scripts/check_docs_parity.py
   uv run --no-sync python scripts/check_doc_links.py
   ```

**出口条件**：lint/format/type/doc gate 全绿；`grep -rn "todolist.evidence\|todolist import evidence" AGENTS.md docs/` 零命中。

### 阶段 5 —— 全仓库验证

**命令：**
```bash
uv run --no-sync python tests/run_tests_split.py        # CI gate（3 进程）
uv run --no-sync pytest tests/perf -q -s               # 性能守卫
./scripts/verify_feature.sh                             # 一次性 feature 验证
```

**出口条件**：三项全绿。如果 `tests/run_tests_split.py` 在某个跟 evidence 无关的测试上失败，单独排查 —— 不要为无关失败回滚这次迁移，但要确认它确实无关（检查失败测试的 import）。

## 5. 关键风险与缓解

| 风险 | 可能性 | 缓解 |
|---|---|---|
| **循环 import**：`pub_base/__init__.py` 如果 re-export `evidence_recorder` | 中 | **不要**把 `evidence_ledger`/`evidence_recorder` 加进 `pub_base/__init__.py` 的 re-export 块。`evidence_recorder` 已经会触发 `pub_base/__init__`（通过 `file_write_lock`）；反向 re-export 它会成环。调用方用完整路径 `from agent.tools.pub_base.evidence_recorder import …`。 |
| **过期测试 fixture** 留在 `tests/agent/tools/todolist/conftest.py`，是 `test_evidence_ledger.py` 需要的 | 低-中 | 阶段 3 第 3 步显式审计。发现就迁 fixture，不要复制留一份。 |
| **`LEDGER_PATH` 类属性** 在测试里通过 `agent.tools.todolist.evidence_ledger.EvidenceLedger` monkeypatch | 低 | 阶段 1 第 5 步更新所有这类测试 import。`LEDGER_PATH` 值本身由 `resolve_evidence_ledger_path()` 计算，它不迁移。 |
| **迁移过程中意外编辑导致行为漂移** | 低 | 两个文件是字节级迁移，不改内容。唯一编辑是 import 路径替换，ruff/basedpyright + 测试套件立刻能抓到。 |
| **`tests/full/` e2e 文件**引用旧路径 | 低 | `tests/full/` 排除在 split runner 之外、标 `llm_e2e`；那里如果有 stale import，下次显式跑 `pytest -m llm_e2e` 会浮现。阶段 5 如果某个 e2e 变红就 grep 这个目录。 |
| **隐藏的非 `from` 形式 import**（如 `import agent.tools.todolist.evidence_ledger as ev`） | 低 | §2.1 的 grep 模式 `from agent.tools.todolist.evidence` 覆盖主流形式；阶段 5 最后用 `grep -rn "agent.tools.todolist.evidence\|todolist import evidence" agent/ server/ runtime/ workspace/ context_engine/ config/ skills/ tests/` 抓其他形式。 |
| **subagent `conftest.py` sys.modules stub 干扰** | 低 | `tests/agent/tools/subagent/conftest.py` stub 的是 subagent 模块，不是 evidence。但阶段 5 的 split runner（3 进程）是跨套件 stub 污染的护栏。 |

## 6. 明确不在范围

本计划**只**迁移 `evidence_ledger.py` 和 `evidence_recorder.py`。它不：

- 动 `agent/tools/todolist/knowledge/`（独立的工具包，恰好也住在 `todolist/` 下；需要自己的抽出计划）。
- 动 `agent/tools/todolist/evidence_ledger.py` 的存储契约 —— 同一个 JSONL 文件、同路径、同 append-only 语义。
- 动 `EVIDENCE_LEDGER` 配置 TypedDict 或 `verify_commands` 模式。
- 重写 `PlanContinuationEnforcer`（仍依赖 `get_todos_sync` 和 `stagnation_tracker`）。
- 移除任何 todolist 工具、state key、WS 事件或客户端 store。
- 改 evidence 行的 `session_id` 语义（仍按主会话 id 或 `agent:{agent}:subagent:{uuid}` 子会话 key 索引）。

这是 todolist 自身能被删除前所需的**第一步**。它是移除 taskflow 对 todolist 包反向依赖的最小原子动作。

## 7. 回滚

每个阶段是一个 commit（推荐）。回滚就是 revert commit。因为 JSONL 存储文件永不动、配置永不动，没有数据要恢复。任一阶段最坏情况就是"import 重新指向旧路径、测试通过"——干净的 revert。

## 8. 验收清单

- [ ] 阶段 0 baseline 全绿
- [ ] 阶段 1：`evidence_ledger.py` 落在 `pub_base/`；`grep -rn "from agent.tools.todolist.evidence_ledger"` 空
- [ ] 阶段 2：`evidence_recorder.py` 落在 `pub_base/`；全仓库 `grep -rn "from agent.tools.todolist.evidence"` 空
- [ ] 阶段 3：`test_evidence_ledger.py` 从 `tests/agent/tools/pub_base/` 跑通
- [ ] 阶段 4：AGENTS.md 已更新；ruff / basedpyright / lint-imports / doc gate 全绿
- [ ] 阶段 5：`tests/run_tests_split.py` + perf + `verify_feature.sh` 全绿
- [ ] `agent/`、`server/`、`runtime/`、`workspace/`、`context_engine/`、`config/`、`skills/`、`tests/`、`docs/`、`AGENTS.md` 下任何地方都不再残留 `agent.tools.todolist.evidence_*` import


---

# 计划 B：从 taskflow 移除 Wave 层

> **状态**：提案 · **范围**：删除 `agent/tools/taskflow/waves.py`，简化 `taskflow_updated` 线上 payload 形状和 `ProgressFloat` 面板。
> **前置条件**：无（跟计划 A 独立 —— 可以前后任意顺序跑）。
> **风险等级**：中 —— 涉及后端 payload 形状、前端 store 类型、Vue 组件，但调度器行为不变。

## B.1. 目标

从 taskflow 的进度上报里移除 wave 抽象。wave 层是个纯展示投影（`compute_waves` 是对持久化 step dict 的纯函数），**调度器不用它**（`waves.py:7-11` 明文记录这一点）。它只被浮动进度面板和 `PlanContinuationEnforcer` 的内部文本渲染消费 —— 而且两种情况下价值都边际，且有部分误导：wave 分组暗示"wave N 必须完成才能开始 wave N+1"，这**不是**调度的实际行为（一个 step 在它自己的 `depends_on` 全部 `done` 时变 `ready`，不管它的 wave 兄弟节点）。

移除它：

- 消除一层让展示脱离调度现实的东西；
- 简化线上 payload 形状和前端渲染；
- 让调度器/展示边界保持干净，把三层（todolist / taskflow-DAG / wave）减为两层。

## B.2. 当前状态 —— 完整依赖面

通过 `grep -rn "compute_waves\|FlowWave\|current_wave\|waves:"` 验证。

### B.2.1 待删除的文件

| 文件 | 导出 | 备注 |
|---|---|---|
| `agent/tools/taskflow/waves.py` | `STATUS_ORDER`, `compute_waves`, `steps_of` | `compute_waves` 是 wave 逻辑。`STATUS_ORDER` 是线上 payload 的状态集合。`steps_of` 是个跟 wave 无关的小 helper —— 必须迁走。 |

### B.2.2 后端消费者

| 文件:行 | 做什么 | 改动 |
|---|---|---|
| `agent/tools/taskflow/progress_push.py:27` | `from .waves import STATUS_ORDER, compute_waves, steps_of` | 去掉 `compute_waves`。把 `STATUS_ORDER` 和 `steps_of` 内联（或迁到 `_shared.py`）。 |
| `agent/tools/taskflow/progress_push.py:38-61` `flow_progress(flow)` | 构造每个 flow 的线上 payload；调 `compute_waves(steps)`，产出 `current_wave` 和 `waves` 字段 | 去掉 `waves`、`current_wave`。直接产出 `steps: [_wire_step(s) for s in steps]`。 |
| `agent/tools/taskflow/progress_push.py:74-83` `progress_payload(session_id)` | 顶层 totals 包含 `current_wave` 和 `waves` 计数 | 都去掉。 |
| `agent/middlewares/todo_continuation/core.py:58` | `from agent.tools.taskflow.waves import compute_waves, steps_of` | 去掉 `compute_waves`。保留 `steps_of`（迁过位置的）。 |
| `agent/middlewares/todo_continuation/core.py:175, 200` | `steps_of(flow)` | 不变（只是 import 路径）。 |
| `agent/middlewares/todo_continuation/core.py:208-218` | `for wave in compute_waves(steps): … f"  wave {wave['index']} ({wave['done']}/{wave['total']}){suffix}: {rendered}"` | 重写为扁平 `for step in steps[:_MAX_STEPS_PER_FLOW]: … f"  step {step_id} ({status}): {rendered}"`。去掉 cyclic 后缀（单次 unlock pass 不会产生环；环只在手动改 `state_json` 时出现 —— 展示非必要）。 |

### B.2.3 前端消费者

| 文件:行 | 做什么 | 改动 |
|---|---|---|
| `client/app/stores/taskflow.ts:29-35` | `FlowWave` 接口 | 删除。 |
| `client/app/stores/taskflow.ts:47-50` | `FlowProgress` 里的 `current_wave: number; waves: FlowWave[]` | 替换为 `steps: FlowStep[]`。去掉 `current_wave`。 |
| `client/app/stores/taskflow.ts:58-62` | `ProgressTotals` 有 `current_wave`、`waves`；`EMPTY_TOTALS` 镜像它们 | 类型和空默认值都去掉这两个字段。 |
| `client/app/pages/home/components/ProgressFloat.vue:31-34` | 顶部 chip `waveChipLabel`（`data-test="progress-float-waves"`） | 替换为简单的 "X / Y done" chip，由 `store.totals.done / store.totals.total` 驱动。 |
| `client/app/pages/home/components/ProgressFloat.vue:105-130` | `v-for="wave in flow.waves"` —— 每个 wave 的头、当前 wave 高亮、cyclic 徽标、`done/total` 和每波进度条 | 移除 wave 分组层。直接 `v-for="step in flow.steps"`，每个 step 显示自己的 status。 |
| `client/app/pages/home/components/ProgressFloat.vue:134` | `v-for="step in wave.steps"` | 变成 `v-for="step in flow.steps"`（从 wave `v-for` 里移出来）。 |
| `client/app/pages/home/components/ProgressFloat.vue:167` | `import type { FlowWave }` | 删除。 |
| `client/app/pages/home/components/ProgressFloat.vue:195-196` | `for (const wave of flow.waves) for (const step of wave.steps) keys.add(...)` | 摊平为 `for (const step of flow.steps) keys.add(...)`。 |
| `client/app/pages/home/components/ProgressFloat.vue:222-235` | `waveChipLabel` 计算属性（3 态：未开始 / "wave X of Y" / 等 gate） | 替换为 2 态计算属性：未开始 / "X of Y done"。"等 gate" 态是 wave 派生的；扁平视图里它塌缩成"已开始但没有 dispatched step"。 |

### B.2.4 测试

| 文件 | 改动 |
|---|---|
| `tests/agent/tools/taskflow/test_progress_push.py` | 更新 payload 断言：`waves` → `steps`，去掉 `current_wave`。 |
| `client/app/pages/home/components/__tests__/ProgressFloat.integration.test.ts`（如果断言 wave 结构） | 更新选择器和断言。阶段 B.0 审计。 |

### B.2.5 不变的部分

- `agent/tools/taskflow/tools/_shared.py:232-269` `unlock_dependents` —— 调度器，不动。
- `agent/tools/taskflow/tools/taskflow_dispatch.py` —— 仍派 `ready` step，无 wave 逻辑。
- `agent/tools/taskflow/tools/taskflow_resume.py:462-464` `_auto_dispatch_ready` —— 仍 auto-dispatch `newly_ready` step id；注释里的"wave"是口语化用法，不是真依赖。
- `server/trigger/core.py` 里的 `taskflow_refresh` WS handler —— 返回同一个 `progress_payload`，形状变化对它透明。

## B.3. 新 payload 形状

**之前**（每个 flow）：

```json
{
  "flow_id": "F1", "status": "active", "description": "…",
  "total": 5, "done": 2,
  "current_wave": 2,
  "by_status": {"done": 2, "dispatched": 1, "ready": 0, "blocked": 2},
  "waves": [
    {"index": 1, "total": 3, "done": 3, "by_status": {}, "steps": []},
    {"index": 2, "total": 2, "done": 0, "by_status": {}, "steps": []}
  ]
}
```

**之后**（每个 flow）：

```json
{
  "flow_id": "F1", "status": "active", "description": "…",
  "total": 5, "done": 2,
  "by_status": {"done": 2, "dispatched": 1, "ready": 0, "blocked": 2},
  "steps": [
    {"step_id": "S1", "task": "…", "status": "done"},
    {"step_id": "S2", "task": "…", "status": "dispatched"}
  ]
}
```

顶层 `totals` 去掉 `current_wave` 和 `waves`，保留 `flows`、`total`、`done`。

## B.4. 分阶段执行

阶段 B.1 和 B.2 **部署上原子**（线上 payload 形状变了；前端在它们之间会断）。把它们作为同一个 PR / release 的两个 commit 落地。阶段 B.3 只能在 B.1 之后（waves.py 必须已无用）。阶段 B.4 是文档。

### 阶段 B.0 —— 预检（无代码改动）

| 检查项 | 命令 | 预期 |
|---|---|---|
| Baseline 全绿 | `uv run --no-sync pytest tests/agent/tools/taskflow/ -q` | 全部通过。 |
| 前端 baseline | `cd client && pnpm test:unit && pnpm test:integration` | 通过。 |
| 审计前端 ProgressFloat 测试 | `grep -rn "wave\|FlowWave\|current_wave" client/app/pages/home/components/__tests__/` | 列出任何断言 wave 的测试；据此估算阶段 B.2 的工作量。 |
| 审计 taskflow_refresh handler | `grep -n "taskflow_refresh\|progress_payload" server/trigger/core.py` | 确认它返回 `progress_payload`（无 wave 专属代码）。 |

### 阶段 B.1 —— 后端 payload 形状变更 + PlanContinuationEnforcer 重写

**动作：**

1. 把 `STATUS_ORDER` 和 `steps_of` 从 `waves.py` 迁到 `agent/tools/taskflow/tools/_shared.py`（或内联到 `progress_push.py`）。
2. 重写 `agent/tools/taskflow/progress_push.py:38-61` `flow_progress(flow)`，产出 `steps` 而不是 `waves` + `current_wave`：

   ```python
   def flow_progress(flow):
       steps = steps_of(flow)
       by_status = {s: 0 for s in STATUS_ORDER}
       for step in steps:
           by_status[str(step.get("status") or "ready")] += 1
       return {
           "flow_id": str(flow.get("flow_id") or ""),
           "status": str(flow.get("status") or ""),
           "description": str((flow.get("state") or {}).get("description") or "")[:_DESCRIPTION_WIDTH],
           "total": len(steps),
           "done": by_status["done"],
           "by_status": by_status,
           "steps": [
               {"step_id": str(s.get("step_id") or ""),
                "task": str(s.get("task") or "")[:_TASK_WIDTH],
                "status": str(s.get("status") or "ready")}
               for s in steps
           ],
       }
   ```

3. 从 `progress_payload` 的顶层 `totals` 里去掉 `current_wave` 和 `waves`。
4. 重写 `agent/middlewares/todo_continuation/core.py:208-218`：

   ```python
   # before
   for wave in compute_waves(steps):
       clipped = wave["steps"][:_MAX_STEPS_PER_FLOW]
       extra = wave["total"] - len(clipped)
       suffix = " (cyclic deps)" if wave.get("cyclic") else ""
       line = f"  wave {wave['index']} ({wave['done']}/{wave['total']}){suffix}: {rendered}"

   # after
   for step in steps[:_MAX_STEPS_PER_FLOW]:
       step_id = str(step.get("step_id") or "")
       status = str(step.get("status") or "ready")
       line = f"  step {step_id} ({status}): {rendered}"
   ```

5. 更新 `tests/agent/tools/taskflow/test_progress_push.py` 的 payload 断言。

**验证：**

```bash
uv run --no-sync pytest tests/agent/tools/taskflow/ tests/agent/middlewares/test_plan_continuation.py -q
uv run --with ruff ruff check agent/tools/taskflow/progress_push.py agent/middlewares/todo_continuation/core.py
uv run --no-sync basedpyright agent/tools/taskflow/progress_push.py
```

**出口条件**：后端全绿；非测试、非 waves.py 代码里 `grep -rn "compute_waves"` 零命中。

### 阶段 B.2 —— 前端重写（跟 B.1 原子部署）

**动作：**

1. `client/app/stores/taskflow.ts`：
   - 删除 `FlowWave` 接口（`:29-35`）。
   - 在 `FlowProgress` 里，把 `current_wave: number; waves: FlowWave[]` 替换为 `steps: FlowStep[]`。
   - 在 `ProgressTotals` 里，去掉 `current_wave` 和 `waves`（`:58`）。
   - 更新 `EMPTY_TOTALS`（`:62`）去掉这两个字段。
2. `client/app/pages/home/components/ProgressFloat.vue`：
   - 删除 `import type { FlowWave }`（`:167`）。
   - 把 `v-for="wave in flow.waves"` 块（`:105-130`）替换为扁平的 `v-for="step in flow.steps"` 块。每个 step 行显示 `step.task`、status chip 和 `step_id`。
   - 把内层 `v-for="step in wave.steps"`（`:134`）移出来 —— 它变成外层循环。
   - 把 `waveChipLabel`（`:222-235`）替换为更简单的 `doneChipLabel`，已开始时返回 `"X / Y done"`，否则空。去掉 "wave X of Y" 分支。
   - 更新 key 收集 helper（`:195-196`）直接遍历 `flow.steps`。
   - 去掉 `data-test="progress-float-waves"` / `data-test="progress-wave"` / `data-test="progress-wave-current"` / `data-test="progress-wave-cyclic"` 测试属性；替换为 `data-test="progress-float-steps"` / `data-test="progress-step"` 等。
3. 更新 `client/app/pages/home/components/__tests__/ProgressFloat.integration.test.ts`（及任何同级测试）到新的选择器和形状。
4. 如果 `client/app/composables/bridge/agent-socket-frames.ts` 断言 `waves` 形状（阶段 B.0 审计），更新它。

**验证：**

```bash
cd client && pnpm test:unit && pnpm test:integration && pnpm run dpdm
cd client && pnpm typecheck
```

**出口条件**：前端全绿；`grep -rn "FlowWave\|current_wave\|flow.waves\|wave.steps" client/app/` 零命中。

### 阶段 B.3 —— 删除 `waves.py`

**动作：**

1. `rm agent/tools/taskflow/waves.py`。
2. 如果 `agent/tools/taskflow/__init__.py` 从 `waves` re-export 任何东西（审计；当前 grep 显示没有），更新它。
3. 跑全后端测试套件确认没有漏网的 import。

**验证：**

```bash
uv run --no-sync pytest tests/agent/tools/taskflow/ -q
uv run --no-sync lint-imports
uv run --with ruff ruff check . && uv run --with ruff ruff format --check .
uv run --no-sync basedpyright agent/tools/taskflow/
grep -rn "from agent.tools.taskflow.waves\|from agent.tools.taskflow import waves" agent/ server/ runtime/ tests/   # 期望：零命中
```

**出口条件**：`waves.py` 已删；全绿；零引用。

### 阶段 B.4 —— 文档与全量验证

**动作：**

1. 更新 `AGENTS.md`：
   - `## TaskFlow Progress` 章节目前详细描述 wave 视图 —— 重写为扁平 step 视图。
   - 架构总览里把 "waves" 标为报告层的那段 —— 去掉 wave 描述，简化为"面板显示扁平 step 列表 + 每个 step 的 status"。
2. 如果文档里有 wave 引用（用 `grep -rn "wave\|FlowWave" docs/` 审计），更新。
3. 审计 `skills/builtin/core/taskflow/SKILL.md` —— 如果它的 `## Reading the board` 章节描述 wave，重写为扁平 step。
4. 重跑所有 gate：

   ```bash
   uv run --no-sync python tests/run_tests_split.py
   uv run --no-sync pytest tests/perf -q -s
   cd client && pnpm test:unit && pnpm test:integration
   ./scripts/verify_feature.sh
   uv run --no-sync python scripts/check_docs_parity.py
   uv run --no-sync python scripts/check_doc_links.py
   ```

**出口条件**：所有 gate 全绿；`grep -rn "wave\|FlowWave\|compute_waves"` 在 `agent/`、`server/`、`runtime/`、`workspace/`、`context_engine/`、`config/`、`skills/`、`tests/`、`client/app/`、`docs/`、`AGENTS.md` 下零命中。

## B.5. 关键风险与缓解

| 风险 | 可能性 | 缓解 |
|---|---|---|
| **B.1 和 B.2 之间线上 payload 形状断裂** | 必然 | 原子部署：B.1 和 B.2 落在同一个 release。它们之间仓库就是断的 —— 这是设计如此，所以是同一个 PR 两个 commit，不是两个 PR。 |
| **cyclic step 展示丢失** | 低 | 单次 `unlock_dependents` 不会产生环（`_shared.py:237-238`）；环只在手动改 `state_json` 时出现。去掉 cyclic 徽标可接受；如果后来发现它重要，在 `_shared.py` 加个小的 `find_cyclic_steps(steps)` helper 和 per-step `cyclic: true` flag —— 但只在真有需求时。 |
| **`current_wave` 曾用于"已开始但未完成"UX 状态** | 中 | 前端 3 态 `waveChipLabel`（`:222-235`）用 `current_wave` 区分"未开始"和"在跑"。变更后，"在跑"是 `store.totals.done < store.totals.total && any step.status === 'dispatched'`。在新的 `doneChipLabel` 里显式实现等价检查。 |
| **现有面向用户的截图/文档展示 wave** | 低 | 阶段 B.4 更新文档。如果存在截图，后续单独重生成。 |
| **某个 `taskflow_refresh` 快照调用方期望 `waves`** | 低 | handler 返回 `progress_payload`（一个函数）；改这个函数的返回形状同时改 `taskflow_updated` 和 `taskflow_refresh` 的答案，原子。没有单独的快照形状要同步。 |
| **隐藏的非 `from` 形式 import `waves`** | 低 | 阶段 B.3 最后的 `grep` 抓全仓库任何 `agent.tools.taskflow.waves` 或 `taskflow import waves` 引用。 |
| **subagent `conftest.py` sys.modules stub 干扰** | 低 | `tests/agent/tools/subagent/conftest.py` stub 的是 subagent 模块，不是 taskflow wave。阶段 B.4 的 split runner（3 进程）是跨套件 stub 污染的护栏。 |

## B.6. 明确不在范围

- 调度器（`_shared.py::unlock_dependents`、`taskflow_dispatch`、`taskflow_resume` auto-dispatch）—— 不动。
- `taskflow_updated` / `taskflow_refresh` 事件名 —— 不变。
- `taskflow` 技能的 `scope: main_only` —— 不受影响。
- 计划 A（evidence-ledger 抽出）—— 独立，可任意顺序跑。
- 任何 todolist 侧改动 —— wave 是 taskflow 专属概念。

## B.7. 回滚

每个阶段是一个 commit（推荐）。阶段 B.1 + B.2 是一个 PR；revert PR 回滚。B.3 是单独一个删除 `waves.py` 的 commit —— 单独 revert B.3 会留下死代码（没人 import），所以没有 B.1+B.2 单独 revert B.3 是 no-op。B.4 只是文档。

## B.8. 验收清单

- [ ] 阶段 B.0 baseline 全绿；前端 ProgressFloat 测试已审计
- [ ] 阶段 B.1：后端 payload 产出 `steps`；PlanContinuationEnforcer 不再调 `compute_waves`；`test_progress_push.py` 已更新
- [ ] 阶段 B.2：`client/app/stores/taskflow.ts` 和 `ProgressFloat.vue` 消费扁平 `steps` payload；前端测试全绿
- [ ] 阶段 B.3：`agent/tools/taskflow/waves.py` 已删除；`grep -rn "compute_waves\|from agent.tools.taskflow.waves"` 空
- [ ] 阶段 B.4：AGENTS.md + taskflow SKILL.md 已更新；ruff / basedpyright / lint-imports / doc gate 全绿；`tests/run_tests_split.py` + perf + `verify_feature.sh` 全绿
- [ ] `agent/`、`server/`、`runtime/`、`workspace/`、`context_engine/`、`config/`、`skills/`、`tests/`、`client/app/`、`docs/`、`AGENTS.md` 下任何地方都不再残留 `wave` / `FlowWave` / `compute_waves` 引用
