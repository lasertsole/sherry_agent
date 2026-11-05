# 子 agent 派生上下文交接 — Phase 1（prompt-bound，保 thinking）

> **目标**：优化 plan 经验抽取质量。子 agent 返回结构化 handoff（4 经验字段），plan 抽取机械消费而非从自由文本猜。
>
> **Phase 1 范围**：不 schema-bind、子 agent 保 thinking、handoff 通过 result_text 末尾的 ```json block 返回、`_extract_handoff_json` 正则提取（主路径）。立即修复 plan 抽取的确定性 bug（step_id 错位、动荡污染、LLM 综合丢数据、压缩丢 failed_paths）。Phase 2（schema-bind 升级）见 `计划_子agent派生上下文交接_Phase2.md`。

---

## 四个字段定义

> **方向**：方案 A——四个字段都是 **return↑**（子 agent 自己产生，向上返回）。父不下传值（无 spawn↓）。每个节点填自己执行中发现/锁定的内容，不从父继承。

| 字段 | 类型 | 含义 |
|---|---|---|
| `task_understanding` | `str` | 子 agent 用自己的理解重述任务 |
| `discovered_hard_constraints` | `list[str]` | 执行中发现的硬约束（环境/API/业务规则） |
| `locked_decisions` | `list[{decision, reason}]` | 已锁定决策，必须带理由。各节点独立锁定 |
| `failed_paths` | `list[{path, failure_reason}]` | 试过但失败的路径。子可能重复父的失败（靠父写 task 文本提示缓解） |

**元数据（程序注入，不由 LLM 填）**：`depth`（从 `run.spawn_depth` 读）、`agent_label`（run record 的 label，缺失合成 `{role}-{seq}`）、`outcome`（registry 的 outcome）、`children`（代码装配）。

## 树状结构

- **向下**：不传任何 handoff 值。父只让子 agent 知道「返回时带 4 字段」（Phase 1 通过 system prompt 指示，非 response_format）。父避坑提示写 task 文本。
- **向上**：子 agent 返回 4 字段（本层自己的）。`children` 由 `_assemble_handoff_tree` 按 registry `requester_session_key` parent→child 关系代码装配——LLM 不接触 children，保证每层 4 字段纯粹是本层贡献，不和子层搞混。

完整树节点（代码装配产物，非 LLM 原始输出）= 4 经验字段 + depth + agent_label + outcome + children（递归）。

### 合并语义

`_aggregate_handoffs` 深度优先遍历装配后的树，扁平化：
- `discovered_hard_constraints`：跨树字符串去重，语义相同的由 plan 抽取 LLM 判断合并
- `locked_decisions`：各节点自己的，`decision` 相同保先出现。无「父决策不可覆盖」约束
- `failed_paths`：跨树语义相似保先出现。无「不重复父失败」约束
- `task_understanding`：不合并
- `children`：代码装配，不合并

---

## 压缩器集成（SummaryDoc）

### 字段映射

| SummaryDoc 字段 | 映射 handoff 字段 | 备注 |
|---|---|---|
| `constraints`（已有） | `discovered_hard_constraints` | 增强 prompt 区分 hard/soft |
| `key_decisions`（已有） | `locked_decisions` | 格式已对齐 |
| `goal`（已有） | `task_understanding` | 近似映射，接受漂移（task 在 system prompt 不被压缩） |
| `failed_paths`（**新增**） | `failed_paths` | 新字段 |

### SummaryDoc 新增 `failed_paths`

```python
failed_paths: list[str] = Field(default_factory=list,
    description='Paths tried and failed, each as "<path>: <failure_reason>". '
    "Carry existing entries forward verbatim; append newly discovered. "
    "Empty list when none.")
```
- `_LIST_FIELDS` / `_FIELD_CAPS`（`FAILED_PATHS_MAX_ITEMS`）/ `_ITEM_TOKEN_BUDGETS` / `render_summary_markdown` 条件段同步。

### 压缩 prompt 增强

`_SUMMARY_JSON_RULES`（`summary_generation.py:148-174`）加 `failed_paths` 行 + 增强 `constraints`（HARD constraints vs preferences，carry forward verbatim）。`_SUMMARY_UPDATE_INSTRUCTIONS_STRUCTURED` 加 carry-forward 规则。

### 深度递增预算

`summary_total_max_tokens` 改按 spawn 深度递增（不再扁平）：

```python
# config/features/agent_side/summarization.py
"failed_paths_max_items": 5,
"summary_total_max_tokens_base": 5000,        # 最深层 base（原 4000 → 5000）
"summary_total_max_tokens_depth_step": 1000,  # 每高一层 +1000

def effective_summary_max_tokens(depth, max_spawn_depth):
    return base + max(0, max_spawn_depth - depth) * step
# max_spawn_depth=2: depth 2 = 5000, depth 1 = 6000
```
main agent 压缩沿用原配置不受影响。`summary_item_tokens_short` 50→75。

### 压缩保留机制

子 agent 的 4 字段在执行中散落对话消息，完成时填入返回。压缩时 SummaryDoc carry-forward 保留。残留损失（cap 截断、LLM 漏 carry、多次压缩累积、hard/soft 混）是已知代价——靠深度递增预算 + carry-forward 规则降低，不加 per-run store（压缩作为唯一机制）。

---

## 现状分析

- 当前 spawn schema 无交接字段；子 agent 只收到 task 文本。
- `initial_message.py` 的 `context` 参数从未被填充。
- 子 agent 返回自由文本（Task Result），无结构化 handoff。
- plan 抽取 `_read_subagent_runs` 读 `result_text`（截断 24KB），LLM 从自由文本猜 failure_set/success_path。
- `step_id`/`flow_id` 不在 `SubagentRunRecord`，靠 LLM 语义匹配 todo → 误关联。

---

## 设计方案

### 改动范围

| 改动 | 文件 | 内容 |
|---|---|---|
| ② 系统提示 | `spawn/system_prompt.py` | Output Format 要求子 agent 在 result_text 末尾 append ```json block（4 经验字段，不含 depth/label/children）。**保 thinking，无 response_format** |
| ⑤ SummaryDoc | `summary_doc.py` | 新增 `failed_paths` 字段 + cap + render |
| ⑥ 压缩 prompt | `summary_generation.py` | JSON rules 加 `failed_paths` + 增强 `constraints` |
| ⑦ 压缩配置 | `config/features/agent_side/summarization.py` | `failed_paths_max_items` + 深度递增预算 + `summary_item_tokens_short` 50→75 |
| ⑧ plan 抽取消费链 | `nudges.py` | `_extract_handoff_json` 正则提取（主路径）+ registry 字段 + 装配树 + 快照过滤 + 聚合 + handoff-tree.json + prompt 重写 |
| registry | `types/registry.py` + `run_manager.py` + `store_sqlite.py` + `queries.py` | `SubagentRunRecord` 加 step_id/flow_id/handoff/spawn_depth + `CompletionState.handoff` + SQLite 列 + 回退 |
| knowledge | `knowledge_store.py` + `knowledge_tool.py` + `prompt_block.py` | handoff-tree.json 读写 + `layer="handoff"` + 摘要注入 |

**不改动**：`_extract_result_text`、`delivery.py`、`initial_message.py`（方案 A 无向下值传递）、`completion_judge.py`、`output_caps.py`。无新增 spawn 参数。

### ② 系统提示（prompt-bound）

`build_subagent_system_prompt` Output Format 段：保留 Task Result 自由文本摘要 + 要求子 agent 在末尾 append ```json block，只含 4 经验字段（不含 depth/agent_label/children——程序注入）。thinking 不受影响。

```python
output_format = (
    "## Output Format\n"
    "First provide:\n```\n## Task Result\n**Status**: ...\n**Summary**: ...\n"
    '**Details**: ...\n**Errors**: ...\n**Next Steps**: ...\n```\n\n'
    "Then append a handoff ```json block with YOUR OWN fields (this layer only):\n"
    "```json\n{\n"
    '  "task_understanding": "<YOUR restated understanding>",\n'
    '  "discovered_hard_constraints": ["<hard constraints YOU found>"],\n'
    '  "locked_decisions": [{"decision": "...", "reason": "..."}],\n'
    '  "failed_paths": [{"path": "...", "failure_reason": "..."}]\n'
    "}\n```\n"
    "Rules: fill only what YOU encountered THIS layer — do NOT include child "
    "subagents' data. Empty lists stay empty. Do NOT include depth/agent_label/"
    "children — those are system-injected. Put the JSON block BEFORE lengthy details.\n"
)
```

### ⑧ plan 抽取消费链

**改动 1：`_extract_handoff_json`（主路径，正则）**

```python
def _extract_handoff_json(result_text: str) -> dict | None:
    """正则提取 result_text 末尾的 ```json block，校验含 4 经验字段，返回 dict 或 None（fail-open）。"""
    import re, json
    blocks = re.findall(r"```json\s*\n(.*?)```", result_text, re.DOTALL)
    if not blocks: return None
    try:
        data = json.loads(blocks[-1])
        if any(k in data for k in ("task_understanding", "discovered_hard_constraints",
                                    "locked_decisions", "failed_paths")):
            return data
    except (json.JSONDecodeError, TypeError): pass
    return None
```

**完成时提取存储**：`core.py` 子 agent 完成时，`_extract_result_text` 之后调 `_extract_handoff_json(result_text)`，存入 `run.completion.handoff`（`CompletionState.handoff: dict | None`，与 `result_text` 并列）。`store_sqlite.py` 加 `handoff` 列（JSON blob，`ALTER TABLE` 增量迁移）。

**`_read_subagent_runs`** 直接读 `run.completion.handoff`（不再现场提取）；None 时 fail-open（plan 抽取 LLM 回退读 result_text 自由文本）。输出每个 run 带：`handoff`、`step_id`、`flow_id`、`spawn_depth`、`outcome`、`label`、`requester_session_key`、`child_session_key`。

**改动 2：`SubagentRunRecord` 字段**

- `step_id: str | None`、`flow_id: str | None`（关联 plan task）
- `spawn_depth: int`（spawn 时记录，main=0/子=1/孙=2）
- `CompletionState.handoff: dict | None`
- `register_run` 签名加 `step_id`/`flow_id`/`spawn_depth`；`complete_run` 签名加 `handoff`。

**改动 0：`_assemble_handoff_tree`（代码装配）**

```python
def _assemble_handoff_tree(runs: list[dict]) -> list[dict]:
    children_map = {}
    for run in runs:
        children_map.setdefault(run.get("requester_session_key"), []).append(run)
    run_by_child_session = {run.get("child_session_key"): run for run in runs if run.get("child_session_key")}

    def _build_node(run):
        handoff = run.get("handoff")
        if not handoff: return None
        node = dict(handoff)
        node["depth"] = run.get("spawn_depth")
        node["agent_label"] = run.get("label") or f"{run.get('functional_role','subagent')}-{run.get('run_seq','?')}"
        node["outcome"] = run.get("outcome")
        node["children"] = [n for n in (_build_node(c) for c in children_map.get(run.get("child_session_key"), [])) if n]
        return node

    def _is_root(run):
        req = run.get("requester_session_key")
        if req and "agent:main:session:" in req: return True
        return req not in run_by_child_session  # orphan → promote to root

    roots = [_build_node(r) for r in runs if _is_root(r)]
    return [n for n in roots if n]
```

- depth 从 `run.spawn_depth` 读（不靠树位置递推，孤儿不错位）
- 孤儿（父被快照过滤）提升为 root，保留有效子经验
- 每节点 4 字段纯本层 LLM 产出，元数据 + children 代码注入

**快照过滤（抗 todo/taskflow 动荡）**

`_build_plan_context` 流程：
```
_read_subagent_runs → _read_plan_snapshot → _filter_runs_by_snapshot → _assemble_handoff_tree(filtered) → _aggregate_handoffs(tree_roots)
```

- `_read_plan_snapshot(session_id)`：`get_todos_sync` + `get_active_flows_sync`（当前 todos + flows 的 id/position/status）
- `_filter_runs_by_snapshot(runs, snapshot)`：`_classify_run_relevance` 判定——flow superseded/cancelled 或 todo cancelled → 剔除（不多抽）；completed/done/finished/active → 保留；无 step_id（自由 spawn）或 snapshot 找不到 → 保留（unknown）。`_resolve_position` 按**当前**快照重解析 step_id→position（不漂移）。fail-open：snapshot=None 时不过滤。
- plan context 的 `subagent_runs` = filtered（LLM 也看 filtered，避免多抽）。filtered 内 handoff=None 的 run 仍带 result_text 作 fail-open。

**`_aggregate_handoffs`/`_walk_handoff`**

纯函数，深度优先遍历装配树，扁平化：failed_paths（带 source `depth-N:label > depth-M:label`）、hard_constraints（去重）、locked_decisions（带 source）、handoff_forest（原始树）。

**`handoff-tree.json`（代码写，LLM 前）**

`_run_plan_extraction` 中，LLM 调用**前**代码原子写 `aggregated_handoff` 到 `<plan_key>/handoff-tree.json`（temp + replace）。断电时原始合并树不丢，LLM 综合可重跑。与 LLM 写的 `plan-summary.json` 并列。

**改动 3：plan 抽取 prompt 重写**

`_PLAN_EXTRACTION_PROMPT` Part 1 指示 LLM 消费结构化 handoff：
- failure_set：深度优先遍历 handoff 树 failed_paths；handoff=None 回退 result_text
- success_path：从 locked_decisions 推导（锁定 = 成功）
- hard constraints：跨树 discovered_hard_constraints 聚合
- position mapping：用 `resolved_position`（非 step_id-1）；None 时回退文本匹配

**改动 4：knowledge 存储**

task-level 加 `hard_constraints`/`failed_paths_tree`；plan-level 加 `aggregate_hard_constraints`/`aggregate_failed_paths`。`knowledge_store.py` 渲染 + `knowledge(action="read", layer="handoff")` + `build_knowledge_block` 注入计数摘要（≤5 条显示，否则只计数）。

### ⑨ 返回提取与交付

`_extract_result_text` 提自由文本（Task Result 摘要）。`_extract_handoff_json` 提 handoff。两者并行。`delivery.py` result_text 截断只影响 Task Result 摘要；Phase 1 handoff 在 result_text 的 JSON block 里——**截断风险**：系统提示要求 JSON block 放 Details 前，`delivery_chars`（4000）通常够，但长子 agent 可能截断。这是 Phase 1 已知代价（Phase 2 schema-bind 消除此风险）。

### ⑩ TaskFlow 步骤

Phase 1：TaskFlow step 不额外 schema-bind（保 Phase 1 prompt-bound）。step_id/flow_id 从 spawn 上下文传 registry。

---

## 改动文件清单

| # | 文件 | 改动 |
|---|---|---|
| 1 | `spawn/system_prompt.py` | Output Format 要求末尾 ```json block（4 字段，保 thinking） |
| 2 | `spawn/core.py` | `register_run` 传 step_id/flow_id/spawn_depth + `_extract_handoff_json` 完成时提取存 `run.completion.handoff` |
| 3 | `types/registry.py` | `SubagentRunRecord` 加 step_id/flow_id/spawn_depth；`CompletionState` 加 `handoff` |
| 4 | `registry/store_sqlite.py` | `subagent_runs` 表加 `handoff` 列（ALTER TABLE 增量） |
| 5 | `registry/run_manager.py` | `register_run`/`complete_run` 签名扩展 + 透传 |
| 6 | `registry/queries.py` | `list_runs_for_requester` memory 空时回退 SQLite |
| 7 | `summary_doc.py` | `failed_paths` 字段 + cap + render |
| 8 | `summary_generation.py` | JSON rules + carry-forward + hard/soft |
| 9 | `config/features/agent_side/summarization.py` | `failed_paths_max_items` + 深度递增预算 + `summary_item_tokens_short` 50→75 |
| 10 | `nudges.py` | `_read_subagent_runs` 读 `run.handoff` + `_filter_runs_by_snapshot` + `_assemble_handoff_tree` + `_aggregate_handoffs`/`_walk_handoff` + `_build_plan_context` 流程 + `_run_plan_extraction` 写 handoff-tree.json + `_PLAN_EXTRACTION_PROMPT` 重写 |
| 11 | `knowledge_store.py` | `write_handoff_tree` + `read_all` 识别 + `_format_task`/`_format_plan` 渲染 |
| 12 | `knowledge_tool.py` | `layer="handoff"` |
| 13 | `prompt_block.py` | handoff 摘要计数注入 |

不改动：`_extract_result_text`、`delivery.py`、`initial_message.py`、`completion_judge.py`、`output_caps.py`。

---

## 测试计划

| 测试 | 覆盖点 |
|---|---|
| `test_system_prompt.py` | Output Format 含 ```json block 模板 + 4 字段 + 不提 depth/label/children |
| `test_extract_handoff_json.py`（新增） | 正则提取成功/失败/无 block/格式错；含 depth/label/children 时剥离（Phase 1 LLM 可能误填，提取时丢弃） |
| `test_run_manager.py` | `register_run` 传 step_id/flow_id/spawn_depth；`complete_run` 后 `run.handoff` 存储 |
| `test_summary_doc.py` | `failed_paths` 默认空、cap、render |
| `test_summary_generation.py` | JSON rules 含 `failed_paths`；constraints 含 hard/soft |
| `test_summarization_config.py` | base=5000、step=1000、effective 计算 |
| `test_plan_extraction.py` | `_read_subagent_runs` 读 `run.handoff`；`_filter_runs_by_snapshot` 剔除 cancelled/superseded + resolved_position；`_assemble_handoff_tree` 孤儿提升 + depth 从 spawn_depth；`_aggregate_handoffs` 合并；流程 read→snapshot→filter→assemble→aggregate |
| `test_queries.py` | memory 空回退 SQLite |
| `test_knowledge_store.py` | `write_handoff_tree` + `read_all` + `layer="handoff"` |
| `test_prompt_block.py` | 摘要计数注入 |

---

## 风险

| 风险 | 等级 | 缓解 |
|---|---|---|
| LLM 不按格式输出 ```json block | 中 | `_extract_handoff_json` fail-open 返回 None → 回退 result_text 自由文本（原行为）。观察期统计成功率 |
| result_text 截断丢失 JSON block | 中 | 系统提示要求 JSON block 放 Details 前；`delivery_chars`(4000) 通常够。长子 agent 风险——Phase 2 schema-bind 消除 |
| 压缩损失早期发现 | 中 | carry-forward + hard/soft 区分 + 深度递增预算。接受压缩作唯一机制，不加 per-run store |
| `task_understanding` 压缩漂移 | 低 | task 在 system prompt 不被压缩成经验字段，漂移低。接受 |
| step_id/flow_id 缺失 | 低 | 可选默认 None；prompt 指示 None 时文本匹配 |
| registry 内存重启丢失 | 低 | `init_registry` 同步恢复；`list_runs_for_requester` 回退 SQLite |
| plan context 体积线性增长 | 中 | 不注入 handoff_forest，只 cap 后扁平列表（10 条/字段），增量 ~500-800 token |
| 断电 handoff-tree.json vs plan-summary.json 不一致 | 低 | handoff-tree.json 在 LLM 前写（原子）；断电可重跑，幂等覆盖 |

---

## 实施顺序

1. `config/features/agent_side/summarization.py` — `failed_paths_max_items` + 深度递增预算（独立）
2. `summary_doc.py` — `failed_paths` 字段 + cap + render
3. `summary_generation.py` — prompt 增强
4. `system_prompt.py` — Output Format 加 ```json block 模板（保 thinking）
5. `spawn/core.py` — `register_run` 传 step_id/flow_id/spawn_depth + `_extract_handoff_json` 完成时提取
6. `types/registry.py` + `run_manager.py` + `store_sqlite.py` + `queries.py` — record 字段 + CompletionState.handoff + SQLite 列 + 回退
7. `nudges.py` — `_read_subagent_runs` 读 handoff + 快照过滤 + 装配树 + 聚合 + handoff-tree.json + prompt 重写
8. `knowledge_store.py` + `knowledge_tool.py` + `prompt_block.py`
9. 测试编写（含 `_extract_handoff_json` 成功率 baseline）
10. 验证：`uv run pytest tests/agent/middlewares/summarization -q` + `tests/agent/tools/subagent -q` + `tests/agent/tools/todolist/knowledge -q`
11. **观察期**：统计 `_extract_handoff_json` 提取成功率。>80% 则 Phase 1 已够；漏提取多则进 Phase 2。

---

## 推演：强化效果

| 环节 | 强化前 | Phase 1 强化后 |
|---|---|---|
| subagent 返回 | 自由文本 | 自由文本 + ```json block（4 字段） |
| plan context 读取 | result_text 自由文本 | `run.handoff`（正则提取，None 回退） |
| task→run 关联 | LLM 语义匹配 | step_id/resolved_position 确定性映射 |
| failure_set | LLM 从文本猜 | LLM 从 handoff 树结构化遍历 |
| success_path | LLM 从文本猜 | 从 locked_decisions 推导 |
| knowledge | 一句话摘要 | + hard_constraints + failed_paths_tree（可追溯） |
| 动荡抗性 | 无 | 快照过滤 cancelled/superseded + position 重解析 |

三个机械化的最大杠杆：① `_extract_handoff_json` 正则提取（从猜 → 结构化数据）；② step_id/flow_id（语义匹配 → 确定性映射）；③ knowledge 保留 failed_paths_tree（摘要 → 可追溯树）。LLM 仍负责 wave/plan 层模式综合 + 语义去重（不可机械化），但输入质量从自由文本 → 结构化 handoff。

---

## 断电保护

- subagent 完成 → `complete_run` 写 `run.handoff` + SQLite 落盘。
- plan 完成 → `_build_plan_context`（read→snapshot→filter→assemble→aggregate）→ **LLM 前**代码原子写 `handoff-tree.json` → LLM 跑（写 plan-summary.json）→ 写 snapshot hash。
- 断电在 LLM 前：handoff-tree.json 已落盘，重跑幂等覆盖。
- 断电在 LLM 中：原始树在，LLM 综合重跑。
- `list_runs_for_requester` memory 空回退 SQLite。
- 全链路纯函数 + 覆盖写，幂等。

---

## 开放问题

- completion judge 是否看 handoff？MVP 不做。
- `_read_subagent_runs` requester key 硬编码 `agent:main:session:{sid}`——孙 agent 经子 agent 的 children 数组间接到达（树递归），只收集直接子。
- SummaryDoc `constraints` 是否拆 hard/soft？MVP 不拆，靠 prompt 区分。
- plan context 不注入 handoff_forest（控体积），按需 `knowledge(action="read", layer="handoff")`。

---

## 下游应用（PTC / langchain-dynamic-workflow）

- **PTC**：plan 抽取 Part 2 可生成 `scripts/<method>.py`，后续 agent `skill_view` → `execute_code` 一次调用完成多步。handoff 的 hard_constraints 作 guard 断言、failed_paths 作注释。MVP 内低成本路径。
- **langchain-dynamic-workflow**：后续阶段。plan 抽取的 success_path + hard_constraints + failed_paths 编译为 ldw 编排脚本，journal 零成本 replay。本计划的结构化 handoff 是编译器输入。MVP 不集成。

Phase 1 的结构化 handoff 是两者的**数据基础设施**。


---

# 子 agent 派生上下文交接 — Phase 2（schema-bind，条件性升级）

> **前置**：建立在 Phase 1（`计划_子agent派生上下文交接_Phase1.md`）基础上。共享基础设施（registry 字段、`_assemble_handoff_tree`、`_aggregate_handoffs`、`handoff-tree.json`、快照过滤、SummaryDoc、深度预算、plan 抽取 prompt）全部不变——见 Phase 1 文件。
>
> **Phase 2 只改 handoff 的返回与提取方式**：从 Phase 1 的「result_text 末尾 ```json block + 正则提取」升级为「`response_format` 强制结构化输出 + `_extract_handoff_structured` 提取」。handoff 数据形状（4 经验字段）不变，`run.handoff` 存储格式不变，下游全透明。
>
> **启用条件**：① provider 支持原生 response_format（OpenAI 等，thinking 与 schema-bind 共存）；或 ② TaskFlow step（已 thinking OFF，无边际成本）。**DeepSeek 非 TaskFlow 的计划激活期自由 spawn 保持 Phase 1**（保 thinking）。

---

## 启用判定

spawn 时（`spawn_subagent_direct`）查两个条件：
1. **requester session 计划激活**（todos 非空 OR 活跃 flow）—— Phase 1 也查这个，未激活不合入。
2. **provider 支持原生 response_format** OR **该 spawn 是 TaskFlow step**（已有 response_schema，已 thinking OFF）。

| 场景 | Phase 2 合入？ | thinking |
|---|---|---|
| 原生 response_format provider + 计划激活 | ✅ 合入 | 共存 |
| DeepSeek + TaskFlow step（计划激活） | ✅ 合入 | OFF（原本就 OFF，无边际） |
| DeepSeek + 非 TaskFlow 自由 spawn（计划激活） | ❌ 保持 Phase 1 | ON（保 thinking） |
| 计划未激活 | ❌ 不合入 | 自由 |

**handoff 优先级高于 thinking**：计划激活期若合入（原生 provider 或 TaskFlow step），handoff 字段一定在 output_schema 里，DeepSeek 上 thinking 让位。

---

## HANDOFF_SCHEMA（合入 output_schema 的 schema，LLM 只填 4 经验字段）

```python
# LLM 填的 schema——只有 4 个经验字段
# depth/agent_label/outcome/children 都不由 LLM 填（程序生成，消除不确定性）
HANDOFF_SCHEMA = {
    "type": "object",
    "properties": {
        "task_understanding": {"type": "string"},
        "discovered_hard_constraints": {"type": "array", "items": {"type": "string"}},
        "locked_decisions": {"type": "array", "items": {"type": "object",
            "properties": {"decision": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["decision", "reason"]}},
        "failed_paths": {"type": "array", "items": {"type": "object",
            "properties": {"path": {"type": "string"}, "failure_reason": {"type": "string"}},
            "required": ["path", "failure_reason"]}},
    },
    "required": ["task_understanding", "discovered_hard_constraints",
                 "locked_decisions", "failed_paths"],
}

# 代码装配的树节点（HANDOFF_TREE_NODE）= 本层 4 经验字段 + 程序注入元数据
# 见 Phase 1 的 _assemble_handoff_tree
HANDOFF_TREE_NODE = {**HANDOFF_SCHEMA["properties"],
                     "depth": {"type": "integer"},
                     "agent_label": {"type": "string"},
                     "outcome": {"type": "string"},
                     "children": {"type": "array", "items": {"type": "object"}}}
```

**LLM 只填 4 经验字段。** `depth`/`agent_label`/`outcome`/`children` 全部程序注入——消除 LLM 填元数据的不确定性。每层 4 字段纯粹本层贡献，不和子层搞混。

---

## 改动（Phase 1 基础上的增量）

### ① spawn 合入 HANDOFF_SCHEMA

`spawn_subagent_direct`（`core.py`）：计划激活 +（原生 provider OR TaskFlow step）时，把 `HANDOFF_SCHEMA` 合入 `output_schema`：
- caller 提供自定义 output_schema（TaskFlow step 的 response_schema）→ 取并集，字段名冲突 handoff 优先（检测冲突报 warning）
- caller 未提供 → 用 `HANDOFF_SCHEMA` 作默认 output_schema
- 合并后传 `create_agent(response_format=<merged>)`，子 agent schema-bound

DeepSeek 非 TaskFlow spawn：跳过合入，保持 Phase 1（prompt-bound，保 thinking）。

**不加向下值传递参数**（方案 A）。`SessionsSpawnSchema`/runtime tool 无新增字段（`output_schema` 已存在）。

### ② 系统提示（schema-bound 版，替换 Phase 1 的 ```json block 模板）

`build_subagent_system_prompt` Output Format：结构化返回由 `response_format` 强制，**移除 ```json block 模板**。只留字段语义说明 + Task Result 自由文本摘要：

```python
output_format = (
    "## Output Format\n"
    "Your structured output is enforced by the response_format schema — it "
    "already includes the handoff fields below. Fill them with what YOU "
    "discovered; do NOT emit a separate JSON code block.\n\n"
    "First provide:\n```\n## Task Result\n**Status**: ...\n**Summary**: ...\n"
    "**Details**: ...\n**Errors**: ...\n**Next Steps**: ...\n```\n\n"
    "Then fill the structured handoff fields (YOUR OWN this layer):\n"
    "- task_understanding / discovered_hard_constraints / locked_decisions / failed_paths\n"
    "Rules: fill only what YOU encountered THIS layer; do NOT include child data. "
    "Empty lists stay empty. Do NOT fill depth/agent_label/children — system-injected.\n"
)
```

按是否 schema-bound 条件渲染：schema-bound 子 agent 用此版（无 ```json block）；Phase 1 子 agent（DeepSeek 自由 spawn）仍用 Phase 1 的 ```json block 模板。

### `_extract_handoff_structured`（替代 `_extract_handoff_json` 主路径）

`core.py` 新增，紧邻 `_extract_result_text`：

```python
def _extract_handoff_structured(agent_result, output_schema) -> dict | None:
    """从子 agent 最终 AIMessage 提取结构化 handoff（4 字段）。

    两种 response_format 策略：
    - 原生 structured output（OpenAI 等）：final AIMessage.content 是 JSON 文本 → 解析
    - forced tool_choice（DeepSeek 等）：final AIMessage.tool_calls[0].args 是 dict → 直读

    校验含 4 经验字段，剥离 depth/label/children（若 LLM 误填）。
    返回 4 字段 dict 或 None（fail-open → _read_subagent_runs 回退 _extract_handoff_json）。
    """
    if not agent_result or not isinstance(agent_result, dict) or not output_schema:
        return None
    for msg in reversed(agent_result.get("messages") or []):
        # Path A: forced tool_choice
        tool_calls = getattr(msg, "tool_calls", None) or []
        if tool_calls:
            args = getattr(tool_calls[-1], "args", None) or (tool_calls[-1].get("args") if isinstance(tool_calls[-1], dict) else None)
            if isinstance(args, dict) and any(k in args for k in ("task_understanding","discovered_hard_constraints","locked_decisions","failed_paths")):
                return {k: args[k] for k in ("task_understanding","discovered_hard_constraints","locked_decisions","failed_paths") if k in args}
        # Path B: native response_format
        content = getattr(msg, "content", None)
        if isinstance(content, str) and content.strip().startswith("{"):
            try:
                import json
                data = json.loads(content)
                if isinstance(data, dict) and any(k in data for k in ("task_understanding","discovered_hard_constraints","locked_decisions","failed_paths")):
                    return {k: data[k] for k in ("task_understanding","discovered_hard_constraints","locked_decisions","failed_paths") if k in data}
            except (json.JSONDecodeError, TypeError): pass
    return None
```

`complete_run` 路径：`_extract_handoff_structured` 替代 `_extract_handoff_json` 作主路径（schema-bound 子 agent）。`_extract_handoff_json` 正则降为**旧 checkpoint / Phase 1（DeepSeek 自由 spawn）兼容回退**。`_read_subagent_runs` 读 `run.completion.handoff`（来源是 structured 或 regex，存储格式一致）。

### ⑧ plan 抽取消费链

**不变**——`_read_subagent_runs` 仍直接读 `run.completion.handoff`；`_filter_runs_by_snapshot`/`_assemble_handoff_tree`/`_aggregate_handoffs`/`handoff-tree.json`/`_PLAN_EXTRACTION_PROMPT` 全部不变。handoff 数据形状（4 字段）与 Phase 1 一致，下游透明。

### ⑨ 返回提取与交付（Phase 2 收益）

handoff 是结构化输出字段，不在 result_text 自由文本里。`delivery_chars`（4000）截断只影响 Task Result 摘要，**不丢 handoff**——消除 Phase 1 的截断风险。

### ⑩ TaskFlow step

TaskFlow step 处于活跃 flow（计划激活），且原本就 schema-bound（response_schema + thinking OFF）。Phase 2 把 4 handoff 经验字段合入其 response_schema（取并集）——无边际 thinking 成本。`depth`/`agent_label`/`outcome`/`children` 仍代码装配注入。

---

## 改动文件清单（Phase 2 增量）

| # | 文件 | 改动 |
|---|---|---|
| 1 | `spawn/core.py` | spawn 查计划激活 +（原生 provider OR TaskFlow step）→ 合入 `HANDOFF_SCHEMA` 到 output_schema；新增 `_extract_handoff_structured` 替代主路径 |
| 2 | `spawn/system_prompt.py` | schema-bound 子 agent 用字段语义说明版（移除 ```json block）；Phase 1 子 agent 保留 ```json block 模板（条件渲染） |
| 3 | `nudges.py` | `_read_subagent_runs` 读 `run.completion.handoff`（来源 structured/regex 统一）；下游不变 |

**不变的（Phase 1 已实现）**：registry 字段、`CompletionState.handoff`、SQLite 列、`_assemble_handoff_tree`、`_aggregate_handoffs`/`_walk_handoff`、`handoff-tree.json`、快照过滤、SummaryDoc、深度预算、`_PLAN_EXTRACTION_PROMPT`、knowledge 读写。

---

## 测试计划（Phase 2 增量）

| 测试 | 覆盖点 |
|---|---|
| `test_extract_handoff_structured.py`（新增） | 原生 content JSON 路径；forced tool_choice tool_calls 路径；缺字段返回 None；含 depth/label/children 时剥离；非 schema-bound（Phase 1）返回 None |
| `test_spawn_handoff.py`（扩展） | 计划激活 + 原生 provider → 合入；TaskFlow step → 合入；DeepSeek 自由 spawn → 不合入（保 Phase 1）；caller 自定义 schema 取并集、冲突 handoff 优先 |
| `test_system_prompt.py`（扩展） | schema-bound 版无 ```json block；Phase 1 版有；条件渲染正确 |
| Phase 1↔2 共存 | DeepSeek 自由 spawn 走 Phase 1（```json block + 正则）、TaskFlow step 走 Phase 2（structured），同一 plan 内混合正确 |

---

## 风险（Phase 2 增量）

| 风险 | 等级 | 缓解 |
|---|---|---|
| provider 不支持结构化输出 | 中 | 极罕见。spawn 检测 response_format 不可用时回退 Phase 1（```json block + 正则）。DeepSeek 走 forced tool_choice 已验证 |
| output_schema 合入副作用 | 中 | ① schema-bound → DeepSeek forced tool_choice thinking OFF（仅对合入的 spawn：原生 provider 共存、TaskFlow step 原本 OFF、DeepSeek 自由 spawn 不合入保 thinking）；② 字段名冲突 handoff 优先（warning）。冲突罕见（handoff 字段名语义专属） |
| result_text 截断丢失 handoff | 低 | Phase 2 消除：handoff 是结构化字段不在 result_text。仅 Phase 1（DeepSeek 自由 spawn）仍有此风险 |

---

## 实施顺序（Phase 1 验收后）

12. `spawn/core.py` — spawn 查计划激活 +（原生 provider OR TaskFlow step）合入 `HANDOFF_SCHEMA`；新增 `_extract_handoff_structured` 替代主路径
13. `system_prompt.py` — schema-bound 版字段语义说明（移除 ```json block）；条件渲染（Phase 1/2 共存）
14. `_extract_handoff_json` 正则降为旧 checkpoint / Phase 1 兼容回退
15. 测试扩展：`_extract_handoff_structured` + Phase 1↔2 共存
16. 验证：`uv run pytest tests/agent/tools/subagent -q` + 确认 DeepSeek 自由 spawn thinking ON、TaskFlow step thinking OFF

---

## Phase 1 ↔ Phase 2 对照

| 维度 | Phase 1（prompt-bound） | Phase 2（schema-bind） |
|---|---|---|
| handoff 返回 | result_text 末尾 ```json block | response_format 结构化输出 |
| 提取 | `_extract_handoff_json` 正则（主） | `_extract_handoff_structured`（主） |
| thinking | 全程 ON | 原生 provider 共存；DeepSeek 非 TaskFlow 仍 Phase 1；TaskFlow step OFF（原本） |
| 截断风险 | 中（JSON block 可能被 delivery_chars 截） | 无（结构化字段不在 result_text） |
| 可靠性 | 中（依赖 LLM 自觉 + 正则） | 高（provider 强制） |
| 启用 | 默认 | 原生 provider 或 TaskFlow step |
| 共享基础设施 | registry / assemble / aggregate / handoff-tree / 快照过滤 / SummaryDoc / 深度预算 / plan 抽取 prompt | 同左（不变） |

**Phase 2 是 drop-in 升级**：handoff 数据形状（4 字段）与 Phase 1 完全一致；`run.completion.handoff` 存储格式不变；`_assemble_handoff_tree`/`_aggregate_handoffs`/`handoff-tree.json`/`_PLAN_EXTRACTION_PROMPT` 全部不变。切换只改提取函数 + spawn 合入 + system prompt 条件渲染，下游透明。

---

## 推荐路径

1. **先做 Phase 1**（见 Phase 1 文件）——立即修复确定性 bug，保 thinking，性价比高。
2. **观察 Phase 1 `_extract_handoff_json` 成功率**——>80% 则 Phase 1 已够，Phase 2 可缓。
3. **Phase 2 在原生 response_format provider 上启用**——DeepSeek 上只对 TaskFlow step 启用（已 thinking OFF），自由 spawn 保持 Phase 1。

---

# 附录 A：推演——对 plan 经验抽取的强化效果

## 当前 plan 抽取的弱点（逐环节）

| 环节 | 代码位置 | 弱点 |
|---|---|---|
| subagent 返回 | `core.py:_extract_result_text` | 只提取最后一条 AIMessage 的自由文本，无结构化字段 |
| registry 存储 | `run_manager.py:complete_run` | `result_text` 字节级 cap（~24KB），超出截断 |
| plan context 读取 | `nudges.py:_read_subagent_runs` | 只返回 `{task_name, task, result_text, outcome, error}`——result_text 是自由文本，无结构化拆分 |
| task→run 关联 | 无 | **step_id/flow_id 不在 SubagentRunRecord 上**，只盖在 child session state。plan 抽取靠 LLM 用 task 文本与 todo content 语义匹配——描述差异大则误关联 |
| plan context 注入 | `nudges.py:_build_plan_context` | subagent_runs 整体 JSON 序列化注入 prompt，result_text 埋在数组项的文本字段中 |
| failure_set 提取 | `_PLAN_EXTRACTION_PROMPT` Part 1 | LLM 从自由文本中"猜"失败模式——如果失败信息散落在对话细节中，提取质量取决于 LLM 阅读理解能力 |
| knowledge 写入 | `knowledge_store.py` | schemaless JSON，task 层只有 `failure_set`/`success_path`/`method`——不保留失败路径的树状来源、不区分硬约束、不保存原始 handoff 树 |

## 强化后的效果（逐环节对照）

| 环节 | 强化前 | 强化后 | 提升幅度 |
|---|---|---|---|
| subagent 返回 | 自由文本 | 自由文本 + handoff JSON 树（4 字段 + children 递归） | **结构化失败数据从无到有** |
| registry 存储 | result_text 字节 cap | 同（不变），但 handoff 在 `CompletionState.handoff` 独立存储 | 无变化（handoff 不再埋在 result_text） |
| plan context 读取 | `{result_text, outcome}` | `{result_text, outcome, handoff, step_id, flow_id, spawn_depth}`——handoff 机械读 | **从文本猜 JSON → 代码直接读结构化数据** |
| task→run 关联 | task 文本语义匹配 | step_id / resolved_position 直接映射 todo position | **从模糊匹配 → 精确映射** |
| failure_set 提取 | LLM 从自由文本猜 | LLM 从 handoff 树深度优先遍历 failed_paths | **从阅读理解 → 结构化遍历** |
| success_path 提取 | LLM 从自由文本猜 | LLM 从 locked_decisions 推导（锁定决策 = 成功路径） | **从猜 → 推导** |
| knowledge 写入 | failure_set/success_path/method 三字段 | + hard_constraints + failed_paths_tree（保留原始树） | **从摘要 → 可追溯的完整经验树** |

## 关键强化的因果链

**强化点 1：机械提取（最大杠杆）**

当前 LLM 要在一个 24KB 文本块里找到 ```json 围栏、解析树状 JSON、提取 failed_paths。这一步的失败率取决于：
- subagent 的 LLM 是否按格式输出了 JSON（可能没有）
- JSON 是否被截断（可能被 result_text cap 切掉）
- plan 抽取的 LLM 是否能从文本中正确找到并解析 JSON（可能忽略）

机械提取把这三个不确定环节缩减为一个：`_extract_handoff_json`（Phase 1）或 `_extract_handoff_structured`（Phase 2）提取，失败返回 None 走回退。plan 抽取 LLM 拿到的是干净的 `handoff` 字段（dict 或 None），不再需要做 JSON 解析。

**效果**：当 subagent 正确输出 handoff 时（Phase 1 预期 >80%，Phase 2 provider 强制 ~100%），plan 抽取的 failure_set 提取从"LLM 阅读理解"变为"LLM 结构化遍历"——准确率和召回率都显著提升。

**强化点 2：step_id/flow_id + resolved_position 精确映射**

当前 plan 抽取写 task-level knowledge 时 `position` 参数靠 LLM 猜测"这个 subagent 对应第几个 todo"。如果 todo content 是"实现数据库迁移"而 subagent task 是"用 Alembic 实现 SQLite 到 PostgreSQL 的 schema 迁移"，LLM 可能正确匹配，也可能误关联到另一个涉及数据库的 todo。

有了 step_id + resolved_position（按当前快照重解析，抗 todo 漂移），`position = resolved_position` 是确定性映射。LLM 不再需要猜。

**效果**：task-level knowledge 的 position 准确率从"LLM 语义匹配"提升到"确定性映射 + 抗动荡"。

**强化点 3：failure_set 来源可追溯**

当前 failure_set 是一句话列表："SQLite WAL 不支持并发写入"。读者无法知道这个失败是哪个 subagent 在哪个深度遇到的。

强化后，`failed_paths_tree`（handoff-tree.json）保留原始 handoff 树——每个失败路径知道来源 agent、深度、以及是否有子 agent 尝试了相关变体（`source: depth-1:researcher-1 > depth-2:executor-1`）。future plan 查询（`knowledge(action="read", layer="handoff")`）可以追溯失败的完整上下文。

**效果**：knowledge 从"摘要"升级为"可追溯的经验树"。

**强化点 4：success_path 从 locked_decisions 推导**

当前 success_path 完全靠 LLM 从自由文本中识别"什么做法成功了"。这是最难的一步——成功路径散落在对话各处，LLM 需要阅读理解。

handoff 的 `locked_decisions` 是"我锁定了用 X 方案，因为 Y"——这隐含了"X 方案成功了"（否则不会锁定）。plan 抽取可以从 locked_decisions 推导 success_path：每条 `{decision}` 对应一个成功路径步骤，`{reason}` 解释为什么有效。

**效果**：success_path 提取从"阅读理解"变为"从结构化决策推导"——准确率提升，且 success_path 的每步自带理由。

## 仍然依赖 LLM 的部分（无法机械化的）

| 环节 | 为什么仍需 LLM | 风险 |
|---|---|---|
| wave-level pattern 综合 | 需要跨 task 识别共性失败/成功模式——这是语义抽象，代码做不了 | LLM 可能漏掉跨 task 模式。但 handoff 树的结构化数据让 LLM 的输入质量更高 |
| plan-level key_failures/key_successes | 需要判断"最有影响力的"失败/成功——这需要评估，不是提取 | 同上，输入质量提升间接提升输出质量 |
| failed_paths 语义去重 | "WAL 不支持并发写"和"SQLite WAL 多进程写入触发 BUSY"是否相同——需要语义判断 | 机械提取后 LLM 做去重，比从自由文本中提取再去重少一步 |
| handoff JSON 生成质量 | subagent 的 LLM 是否正确填写四个字段 | 提取 fail-open 到 None；系统提示给模板；整体可靠性取决于模型能力 |

## 结论

**能显著强化 plan 经验抽取**，核心在于三个机械化的改动：

1. **`_read_subagent_runs` 机械读 handoff**（Phase 1 正则 / Phase 2 结构化）— 把 plan 抽取 LLM 的任务从"从自由文本猜 JSON"变为"消费干净的结构化数据"。这是最大杠杆。
2. **`SubagentRunRecord` 加 step_id/flow_id + resolved_position** — 把 task→run 的关联从"语义匹配"变为"确定性映射 + 抗动荡"。
3. **knowledge 存储保留 failed_paths_tree** — 把经验产物从"一句话摘要"变为"可追溯的经验树"。

压缩器的 SummaryDoc 改造（复用 `constraints`/`key_decisions` + 新增 `failed_paths`）保证 subagent 自身对话被压缩后，handoff 信息不丢失——这是上述强化的前提条件（如果子 agent 对话压缩后丢失了约束/失败信息，它生成的 handoff 就是空的）。

LLM 仍负责 wave/plan 层的模式综合和语义去重——这是不可机械化的语义抽象任务，但输入质量从"自由文本"升级到"结构化 handoff 树"，间接提升输出质量。

---

# 附录 B：断电保护

## 持久化时间线

```
subagent 完成
  → complete_run: memory.set_run + store_sqlite.upsert_run_sync  ← SQLite 落盘 ✓
  → CompletionState.handoff 持久化在 subagent_runs handoff 列（JSON blob）

plan 完成 → _run_plan_extraction 触发
  → _build_plan_context: read → snapshot → filter → assemble → aggregate
  → 写 handoff-tree.json（代码，原子写）  ← 文件落盘 ✓
  → plan 抽取 LLM 跑（写 plan-summary.json via knowledge tool）  ← 文件落盘 ✓
  → 写 snapshot hash  ← 标记完成 ✓
```

## 逐环节断电分析

| 断电时刻 | handoff-tree.json | plan-summary.json | snapshot hash | 后果 | 恢复 |
|---|---|---|---|---|---|
| subagent 完成后、plan 抽取前 | 不存在 | 不存在 | 未写 | 无经验抽取发生 | 重启后 `_detect_todo_all_complete` 仍返回 True（snapshot 未写），下次 turn 边界重试抽取 |
| `_aggregate_handoffs` 后、写 handoff-tree.json 前 | 不存在 | 不存在 | 未写 | 聚合体只在内存中 | 同上——重试时重新聚合（幂等：相同 runs → 相同聚合） |
| 写 handoff-tree.json 后、LLM 调用中 | ✅ 存在 | 不存在 | 未写 | 原始合并树已落盘，LLM 综合丢失 | 重试时 LLM 重新跑；handoff-tree.json 被覆盖写（幂等） |
| LLM 写 plan-summary.json 后、写 snapshot hash 前 | ✅ 存在 | ✅ 存在 | 未写 | 两层产物都在，但未标记完成 | 重试——两个文件被幂等覆盖写，snapshot hash 最终写入 |
| 写 snapshot hash 后 | ✅ 存在 | ✅ 存在 | ✅ 已写 | 完整状态 | 不重试（已完成） |

## 关键设计：写 handoff-tree.json 在 LLM 调用之前

```python
# _run_plan_extraction 中（nudges.py）：
context = _build_plan_context(session_id)  # read→snapshot→filter→assemble→aggregate
if not context:
    return

# NEW: 写 handoff-tree.json BEFORE LLM runs
aggregated = context.get("aggregated_handoff", {})
if aggregated.get("handoff_forest"):
    _write_handoff_tree_safely(session_id, context["plan_name"], aggregated)

# LLM 调用（可能失败/超时/断电）
result = await extraction_agent.ainvoke(...)

# LLM 成功后才写 snapshot hash
state_register_db.set_state(session_id, _PLAN_SNAPSHOT_HASH_KEY, ...)
```

这样即使 LLM 调用中断电，原始合并树已落盘。LLM 综合是"锦上添花"——丢了可以重跑，但原始数据不丢。

## 原子写保护

```python
def _write_handoff_tree_safely(session_id, plan_name, aggregated):
    """Atomic write of handoff-tree.json — temp file + rename."""
    identity = resolve_plan_identity(session_id, plan_name)
    if identity is None:
        return
    dest = _KNOWLEDGE_ROOT / identity.key / "handoff-tree.json"
    payload = json.dumps({
        "schema_version": 1,
        "plan_name": plan_name,
        "written_at": _timestamp(),
        **aggregated,
    }, ensure_ascii=False, indent=2)
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(dest)  # atomic on POSIX; on Windows atomic-ish (rename over existing)
```

断电在 `tmp.write_text` 中 → tmp 不完整，dest 不受影响（旧文件或不存在）
断电在 `tmp.replace(dest)` 中 → 极短窗口，Windows 上可能两者都存在但 dest 要么是旧的要么是新的，不会是半写的

## registry 内存层断电恢复

`_read_subagent_runs` 调 `list_runs_for_requester`（`queries.py`）只读 `memory.values()`。但进程重启时 `init_registry()` 会调 `load_runs_from_sqlite()` 从 SQLite mirror 恢复内存。所以重启后 memory 是有数据的。

**回退保护**（纳入实施范围）：`list_runs_for_requester` 在 memory 为空时回退读 SQLite：

```python
def list_runs_for_requester(requester_session_key: str) -> list[SubagentRunRecord]:
    runs = [run for run in memory.values() if run.requester_session_key == requester_session_key]
    if not runs:
        # Fallback: memory may not have been restored from SQLite yet
        from . import store_sqlite
        all_runs = store_sqlite.load_runs_from_sqlite()
        runs = [r for r in all_runs if r.requester_session_key == requester_session_key]
    return runs
```

## 幂等性保证

| 操作 | 幂等机制 |
|---|---|
| `complete_run` 写 result_text + handoff | `INSERT OR REPLACE`（SQLite upse
rt），memory.set_run 覆盖 |
| `_filter_runs_by_snapshot` + `_assemble_handoff_tree` | 纯函数——相同输入 → 相同输出，无副作用 |
| `_aggregate_handoffs` | 纯函数 |
| 写 `handoff-tree.json` | temp + replace——覆盖写，不追加 |
| LLM 写 `plan-summary.json` | `KnowledgeStore.write` 覆盖写文件 |
| 写 snapshot hash | `state_register_db.set_state` 覆盖写 |
| plan 抽取 gating | snapshot hash 不匹配 → 重试；匹配 → 跳过 |

**重试不会产生两份 handoff-tree.json 或两份 plan-summary.json**——都是覆盖写。

## 失败回退链

如果整个 plan 抽取失败（LLM 超时、API 错误等）：

1. handoff-tree.json 已落盘（在 LLM 之前写的）→ **原始合并数据不丢**
2. plan-summary.json 可能不存在 → `build_knowledge_block` 读不到 plan summary → 注入空字符串（fail-open，已有行为）
3. snapshot hash 未写 → 下次 turn 边界 `_detect_todo_all_complete` 返回 True → 重试
4. 重试时 `_read_subagent_runs` 重新读 registry（幂等）→ filter/assemble/aggregate 重新跑（幂等）→ 覆盖写 handoff-tree.json（幂等）→ LLM 重跑

**最坏情况**：LLM 持续失败，plan-summary.json 始终不存在。但 handoff-tree.json 存在（首次成功写入）。主 agent 可以通过 `knowledge(action="read", layer="handoff")` 主动查询原始合并树——不依赖 LLM 综合。

---

# 附录 C：最新快照感知的递归汇合（plan-aware aggregation，抗 todo/taskflow 动荡）

> **动机**：`_aggregate_handoffs` 的输入是「该 requester 全量 runs」，不感知 plan 的 todo 节点变化。todo 中途 cancel、flow supersede 后，其 subagent 已返回的 handoff 仍被聚合（多抽）；todo 增删导致 step_id→position 漂移，静默写错 position（少抽/错位）。
>
> **核心原则**：汇合前先取当前 plan 最新快照，据此过滤 cancelled/superseded 的 run（不多抽）+ 按当前 todo 重解析 step_id→position（不少抽、不错位）。四字段合并规则不变——改的是**输入子集的确定**。

## S1. 快照获取 + 过滤：在 `_assemble_handoff_tree` 之前

`_build_plan_context` 流程：`_read_subagent_runs` → **快照过滤** → `_assemble_handoff_tree` → `_aggregate_handoffs`。过滤在装配前——cancelled/superseded 的 run 不进装配树，其 children 也跟着剔除（有效孤儿除外，见 S2）：

```python
def _build_plan_context(session_id):
    runs = _read_subagent_runs(session_id)
    plan_snapshot = _read_plan_snapshot(session_id)
    filtered = _filter_runs_by_snapshot(runs, plan_snapshot)
    tree_roots = _assemble_handoff_tree(filtered)
    aggregated = _aggregate_handoffs(tree_roots)
    return {..., "subagent_runs": filtered, "aggregated_handoff": aggregated, "plan_snapshot": plan_snapshot}
```

`_read_plan_snapshot(session_id)` 读：
- todos：`get_todos_sync(session_id)`（`agent/tools/todolist/registry/store_sqlite.py:482`），取 `id`/`position`/`status`/`content`
- 活跃 flows + steps：`get_active_flows_sync`/`get_non_terminal_flows`（`agent/tools/taskflow/registry/store_sqlite.py:714`/`:477`），取 `flow_id`/`status` + `step_id`/`status`

## S2. 过滤：装配前剔除 cancelled/superseded

```python
def _filter_runs_by_snapshot(runs, plan_snapshot=None):
    if plan_snapshot is None:
        return runs  # fail-open
    kept = []
    for run in runs:
        relevance = _classify_run_relevance(run, plan_snapshot)
        if relevance in ("cancelled", "superseded"):
            continue
        run["relevance"] = relevance
        run["resolved_position"] = _resolve_position(run, plan_snapshot)
        kept.append(run)
    return kept
```

`_classify_run_relevance(run, snapshot)`：
- run 有 `flow_id` 且该 flow 状态为 `superseded`/`cancelled` → `"superseded"`
- run 有 `step_id` 且该 step 对应的 todo 状态为 `cancelled` → `"cancelled"`
- step_id/flow_id 在 snapshot 中能找到且状态有效（`completed`/`done`/`finished`/`running`/`waiting`）→ `"active"`
- run 无 step_id/flow_id（自由 spawn）→ `"unknown"`，保留
- step_id/flow_id 在 snapshot 中找不到 → `"unknown"`，保留并标记（S3）

**孤儿处理**：被剔除 run 的 children 若自身 relevance 有效，作为孤儿提升为 root（`_assemble_handoff_tree` 的 `_is_root` 兜底）。plan context 的 `subagent_runs` = filtered（LLM 也看 filtered，避免多抽）。filtered 内 handoff=None 的 run 仍带 result_text 作 fail-open。

**为什么孤儿提升 root 而非丢弃**：孤儿 run（父被快照过滤，但自身有效）的经验不是噪音，是**世界事实 + 取消证据**：
- `discovered_hard_constraints` 是环境/API 客观限制，与分支是否取消无关——未来任何 plan 撞上同样限制。丢掉 = 少抽世界事实。
- `failed_paths` 是「分支为什么被取消」的证据（todo 取消往往因为某条路走不通）。丢掉 = 销毁取消分支的复盘证据。
- filter 已按快照判过一次子有效；装配阶段再因「父被滤」丢子是第二次矛盾过滤。
- `_aggregate_handoffs` 扁平化带 source 标签，plan 抽取 LLM 能逐条判：hard_constraints/failed_paths（世界事实）保留、locked_decisions（可能绑死在取消方案）降权。

## S3. 重映射：step_id → 当前 position

`_resolve_position(run, snapshot)`：
- run 的 `step_id` 对应的 todo 在当前 snapshot 中存在 → `position = snapshot_todo.position`（当前实际位置）
- todo 已被删除 → `position = None`，prompt 指示 LLM 回退 task 文本匹配，source 标记 `orphaned`

plan context 和 prompt 用 `resolved_position` 而非 `step_id - 1`。

## S4. 「不多抽不少抽」对照

| 场景 | 无快照 | 有快照 |
|---|---|---|
| todo 中途 cancel，其 subagent 已返回 handoff | 进聚合（多抽） | relevance=cancelled，不进装配树 |
| flow 被 supersede | 进聚合（多抽） | relevance=superseded，不进装配树 |
| todo 增删导致 position 漂移 | step_id-1 静默写错（错位） | resolved_position 按当前快照重解析 |
| todo 被删除，step_id 找不到 | 越界/错误 position | resolved_position=None，回退文本匹配 + orphaned |
| replan 新增 todo 的 run | 映射错位 | 按当前快照解析到正确新 position（不少抽） |

## S5. 实施位置

| 单元 | 文件 | 内容 |
|---|---|---|
| S1 | `nudges.py` `_build_plan_context` | `_read_plan_snapshot` |
| S2 | `nudges.py` `_filter_runs_by_snapshot` | `_classify_run_relevance`；cancelled/superseded 剔除 |
| S3 | `nudges.py` `_resolve_position` + prompt | resolved_position；prompt 第 4 条改用 resolved_position |
| S4 | `test_plan_extraction.py` | 四种判定；剔除；重映射；快照 None 回退；孤儿保留 |

## S6. 不冲突点

- 四字段合并规则不变（`_walk_handoff` 去重、保先出现、children 代码装配）
- `handoff_forest` 仍原样收集（被过滤的不进，有效孤儿保留）
- `handoff-tree.json` 仍代码写、幂等覆盖
- plan 抽取 LLM 任务不变（模式综合 + 语义去重）
- fail-open 链完整（snapshot=None 不过滤；step_id 找不到→文本匹配）

## S7. 断电/幂等 + 终态相关性

`_read_plan_snapshot` 纯读，无写入。全链路纯函数——相同 runs + 相同快照 → 相同 aggregate，幂等覆盖。

**终态相关性边界**：plan 完成 extraction 时，所有 todo 已 completed/cancelled、flow 已 finished/cancelled。按「终态相关性」判定（非「活跃」）：`completed`/`done`/`finished` 保留（有效经验来源），只过滤 `cancelled`/`superseded`：

| todo/flow 状态 | relevance | 处理 |
|---|---|---|
| completed / done / finished | active（有效终态） | 汇合 |
| running / waiting / in_progress | active | 汇合（防御性） |
| cancelled | cancelled | 不汇合 |
| superseded | superseded | 不汇合 |
| snapshot 中找不到 | unknown | 保留 + resolved_position=None |

---

# 附录 D：plan 抽取 prompt 全文 + `_walk_handoff` + cap 代码

## D1. `_PLAN_EXTRACTION_PROMPT` Part 1 重写全文

```
"The subagent_runs in the plan context now carry a structured 'handoff' field
alongside 'result_text'. The handoff is a tree node with these fields:

  - task_understanding: the subagent's own restated understanding of its task
  - discovered_hard_constraints: list of hard constraints it found (inviolable)
  - locked_decisions: list of {decision, reason} it locked
  - failed_paths: list of {path, failure_reason} it tried and failed
  - children: array of child subagent handoff nodes (recursive tree, code-assembled)
  - resolved_position: maps to the current todo position (re-resolved from latest
    plan snapshot, NOT the spawn-time position)

Extraction strategy (PREFER structured handoff over free-text result_text):

1. failure_set: Walk each subagent's handoff tree DEPTH-FIRST. Collect every
   failed_paths entry across the tree. Each {path, failure_reason} becomes one
   failure_set entry: '<path>: <failure_reason>'. Deduplicate semantically
   similar entries. If handoff is None for a run, fall back to reading
   result_text for failure signals.

2. success_path: Derive from locked_decisions in the handoff tree — a locked
   decision implies the chosen path worked. Collect {decision} entries that
   describe an approach, ordered by depth (parent decisions first). Supplement
   with result_text if the handoff's locked_decisions are sparse.

3. hard constraints: Carry discovered_hard_constraints from ALL handoff nodes
   into the task-level knowledge as a 'hard_constraints' field. These are
   inviolable — future plans must respect them.

4. position mapping: When writing task-level knowledge, use the subagent run's
   resolved_position to determine position. If resolved_position is None (the
   todo was deleted/reordered away), fall back to matching the run's task text
   against the todos array. Do NOT compute position as step_id - 1 — todos may
   have shifted.

5. children failures: A child subagent's failure may be a sub-path of the
   parent's attempt. Preserve this relationship: if a parent's failed_paths
   entry and a child's failed_paths entry are related (the child tried a
   variation of the parent's approach), note this in the failure_set as
   '<parent_path> -> <child_path>: <failure_reason>'."
```

plan 抽取 LLM 综合后仍走既有三路由：`knowledge(action="write")`（plan-summary.json + task/wave 级）+ `skill_manage`（skills/auto）+ `memory(target="facts")`（facts target）。本计划只在 knowledge 目录加 `handoff-tree.json`（代码写）+ 增强 plan-summary 字段（hard_constraints/failed_paths_tree），不改 facts/skills 路由。

## D2. `_walk_handoff` 完整代码

```python
def _walk_handoff(node: dict, aggregate: dict, seen: set, source_prefix: str) -> None:
    """Depth-first walk of a handoff tree node, collecting into the aggregate."""
    depth = node.get("depth", "?")
    label = node.get("agent_label", "?")
    source = f"{source_prefix}depth-{depth}:{label}" if source_prefix else f"depth-{depth}:{label}"

    for fp in node.get("failed_paths", []):
        aggregate["failed_paths"].append({
            "path": fp.get("path", ""),
            "failure_reason": fp.get("failure_reason", ""),
            "source": source,
        })
    for c in node.get("discovered_hard_constraints", []):
        if c not in seen:
            seen.add(c)
            aggregate["hard_constraints"].append(c)
    for d in node.get("locked_decisions", []):
        aggregate["locked_decisions"].append({
            "decision": d.get("decision", ""),
            "reason": d.get("reason", ""),
            "source": source,
        })
    for child in node.get("children", []):
        _walk_handoff(child, aggregate, seen, source_prefix=source + " > ")
```

## D3. plan context 体积控制 cap 代码

plan context 不注入 `handoff_forest`（原始树），只注入 cap 后的扁平聚合列表：

```python
def _aggregate_handoffs_for_plan_context(runs: list[dict]) -> dict:
    """Compact aggregate for plan context injection — capped, no forest."""
    full = _aggregate_handoffs(runs)
    return {
        "failed_paths": full["failed_paths"][:10],
        "hard_constraints": full["hard_constraints"][:10],
        "locked_decisions": full["locked_decisions"][:10],
        "total_failed_paths": len(full["failed_paths"]),
        "total_hard_constraints": len(full["hard_constraints"]),
        # handoff_forest OMITTED — full trees only in handoff-tree.json
    }
```

LLM 看到 cap 后的扁平列表 + 总计数。完整树在 `handoff-tree.json` 中，通过 `knowledge(action="read", layer="handoff")` 按需查询。plan context 增量固定 ~500-800 token，不随 subagent 数量线性增长。

---

# 附录 E：Token 消耗分析

## 消耗点清单

handoff 数据在生命周期中经过以下 token 消耗点：

| # | 消耗点 | 消费者 | 频率 | 估算 token | 缓解机制 |
|---|---|---|---|---|---|
| 1 | 子 agent 系统提示 Output Format 模板 | 子 agent LLM | 每个子 agent turn | ~400 | **前缀缓存**——系统提示同会话内稳定，首 turn cache miss 后续 cache hit |
| 2 | ~~初始消息 `[Handoff Context]` 段~~ | — | — | — | **方案 A 已移除**：父不下传值，无 `[Handoff Context]` 输入段。handoff 值在执行中产生、最终入结构化返回（#3） |
| 3 | 子 agent handoff 字段（Phase 1: ```json block / Phase 2: 结构化输出） | 子 agent LLM（生成） | 一次 | 200-1000 | 已计入子 agent 输出预算（`max_output_tokens`） |
| 4 | 父 agent 读完成消息（result_text = Task Result 摘要） | 父 agent LLM | 一次 | 200-500 | 一次性，`delivery_chars`（4000）截断只影响 Task Result 摘要（Phase 2 handoff 不在 result_text；Phase 1 JSON block 可能被截，放 Details 前缓解） |
| 5 | plan 抽取 prompt 分层消费策略 | plan 抽取 LLM | 一次 per plan 抽取 | ~300 | 一次性 |
| 6 | plan context 中的 `aggregated_handoff`（cap 后） | plan 抽取 LLM | 一次 per plan 抽取 | 可变 | 见下方"plan context 体积控制"（附录 D3） |
| 7 | `build_knowledge_block` 注入 handoff 摘要计数 | 主 agent LLM | 每个主 agent turn（抽取后） | ~50 | **前缀缓存** + 极小体积（一行计数摘要，如 `Handoff: 8 failed paths, 5 hard constraints — use knowledge(action='read', layer='handoff') for details`） |

## 是否"反复消耗"

**不会无意义反复消耗。** 方案 A 后只有两个 recurring 点（#1、#7），都有缓解机制：

- **#1 系统提示**：前缀缓存。系统提示在同会话内字节稳定，首 turn cache miss，后续 turn cache hit。~400 token 的实际成本是 ~400 token 的 cache-prefill 一次 + ~0 后续。与现有 Output Format 段（~150 token）相比，增量 ~250 token 一次性 prefill。
- **#2 已移除**：方案 A 无 `[Handoff Context]` 输入段，输入侧无 handoff 反复消耗。子 agent 执行中发现的约束/决策/失败路径散落对话消息，超压缩线后由 SummaryDoc 接管（`constraints`/`key_decisions`/`failed_paths` 扁平列表，~100 token），最终填入返回（#3，一次性）。
- **#7 系统提示注入**：极小。`build_knowledge_block` 不注入完整 handoff 树——只一行计数摘要（~50 token）。完整数据通过 `knowledge(action="read", layer="handoff")` 按需查询。

## 同一数据被多个消费者读取

handoff 被 3 个不同 LLM 消费：

| 消费者 | 读什么 | 为什么需要 | 是否冗余 |
|---|---|---|---|
| 父 agent LLM | 完成消息中的 result_text（Phase 1 含 handoff JSON block） | 决定下一步（继续/收尾/派新子 agent） | 否——父 agent 需知道子的结果和发现 |
| plan 抽取 LLM | plan context 中的 aggregated_handoff（cap 后） | 提取经验写入 knowledge | 否——经验抽取的输入 |
| 主 agent LLM | system prompt 中的计数摘要（~50 token） | 未来 turn 中避免重复失败 | 否——被动提示，极小体积 |

三者消费同一数据的不同视图（完整 / cap 后聚合 / 计数摘要），在不同时间、不同目的下消费。不是冗余——是多用途复用。

---

# 附录 F：下游应用评估（PTC 与 langchain-dynamic-workflow）

## 评估 1：抽取后的 skill 能否用 PTC 简化执行步骤？

### PTC 能力回顾

PTC（`execute_code`，`agent/tools/ptc/`）是 EXECUTOR-only 工具，让子 agent 写一段 Python 脚本，通过 RPC 桥同步调用白名单工具（`read_file`/`write_file`/`patch_file`/`terminal`/`search_files`/`web_search`），只有最终 `print(...)` 回到 agent context。预算 50 次工具调用、120s 超时。

**核心价值**：把"调用→处理→再调用"的 N 次 LLM turn 压缩成一次 `execute_code`，子进程内部同步循环，中间推理不占 context window。

### 抽取后的 skill 如何喂给 PTC

plan 抽取 Part 2 已能向 `skills/auto/` 写入 `scripts/` 目录下的可执行脚本（`nudges.py:236-238`）。关键链路：

```
plan 抽取 → success_path（有序步骤）+ method（标识符）
  → skill_manage(action="write_file", file_path="scripts/<method>.py", file_content=...)
  → 写入 skills/auto/<skill>/scripts/<method>.py

后续 agent 遇到类似任务
  → skill_view(name="<skill>") → 读 SKILL.md，看到 when_to_use 匹配
  → skill_view(name="<skill>", file_path="scripts/<method>.py") → 读脚本模板
  → 适配参数 → execute_code(code=<adapted script>) → 一次调用完成 N 步
```

### 可行性：✅ 可行，需增强 plan 抽取 prompt

`_PLAN_EXTRACTION_PROMPT` Part 2（`nudges.py:195-328`）新增 PTC 脚本生成指示：

```
"If the task's success_path is a sequence of 3+ tool calls with processing logic
between them (filtering, branching, aggregation), generate a PTC script template
in scripts/<method>.py that implements the success_path as a single execute_code
script. The script should:
- Use 'from sherry_tools import <tool>' for each needed tool
- Encode the success_path steps as sequential function calls
- Use the hard_constraints from the handoff tree as guard assertions
  (e.g., if a constraint says 'max 100 API calls', assert len(results) <= 100)
- Encode failed_paths as comments: '# AVOID: <path> — failed because <reason>'
- Parameterize variable inputs as function arguments at the top
- Print the final result as JSON to stdout

The SKILL.md body should reference the script:
'For batch execution, use execute_code with the template in scripts/<method>.py
(adapt the parameter section to your specific inputs).'"
```

**效果**：后续 agent 遇同类任务时，读 skill → 拿脚本模板 → 一次 `execute_code` 完成，而非 N 次 turn。从"LLM 逐步推理"变成"脚本确定性执行"。

**限制**：
- PTC 脚本不能 `import` skill 目录文件——agent 必须通过 `skill_view(file_path="scripts/...")` 读脚本内容，粘贴到 `execute_code(code=...)`。
- PTC 是 EXECUTOR-only——只有 executor 角色子 agent 能用。
- 脚本预算 50 次工具调用——复杂 plan 的 success_path 超 50 步会截断。
- 脚本模板是"参考"不是"可执行"——LLM 需根据当前任务参数适配。

## 评估 2：能否用 langchain-dynamic-workflow 简化执行步骤？

### ldw 能力回顾

`langchain-dynamic-workflow`（v0.3.0 alpha）是确定性多智能体编排库：
- **控制流反转**：loop/branch/fan-out 在编排脚本里，不在 LLM 脑子里。只有叶子 `agent()` 调用委托给 agent。
- **内容哈希 journal**：每个叶子调用结果按 SHA-256 内容哈希缓存。resume 时已完成叶子零模型调用 replay。
- **原语**：`agent()`/`parallel()`/`pipeline()`/`dag()`/`race()`/`loop_until()`/`batch_map()`/`checkpoint()`。
- **集成**：可作纯库调用（`run_workflow(orchestrate, roster=...)`），或通过 `create_workflow_middleware` 嵌入 host agent。

### 与 sherry_agent 契合点

| sherry_agent 现状 | ldw 对应 | 契合度 |
|---|---|---|
| subagent spawn（LLM 决定何时/多少/什么任务） | `ctx.agent(prompt, agent_type=...)` 确定性调用 | **高**——plan 抽取的 success_path 可编译为编排脚本 |
| sessions_yield + push-based 完成通知 | 后台运行 + 完成通知注入下一 turn | **高**——相同异步模型 |
| 多级 spawn（MAIN→ORCHESTRATOR→LEAF） | `ctx.workflow(name)` 嵌套（深度上限 8） | **中**——sherry 深度 2，ldw 深度 8 |
| 无 journal——重跑全量 | 内容哈希 journal——零成本 replay | **高**——plan 重跑最大优化点 |
| TaskFlow DAG 调度 | `ctx.dag(nodes)` 拓扑序依赖图 | **中**——功能重叠，TaskFlow 有 evidence gate |
| handoff 树（本计划 failed_paths） | 无直接对应 | **低**——ldw 无经验交接机制 |

### 可行性：⚠️ 可行但工程量大，建议后续阶段

**可行路径——"plan replay"模式**：

```
plan 抽取 → knowledge JSON（success_path + failed_paths + hard_constraints）
  → 编译为 ldw 编排脚本
  → ctx.agent(step_1_prompt, ...) → ctx.agent(step_2_prompt, ...) → ...
  → 每个叶子是一个 sherry subagent（通过 Roster 注册）
  → journal 缓存：同 plan 重跑时已完成步骤零模型调用
  → failed_paths 编码为脚本注释 / race 的候选排除
  → hard_constraints 编码为 guard 断言
```

**工程挑战**：

| 挑战 | 等级 | 说明 |
|---|---|---|
| deepagents 依赖冲突 | 中 | ldw 要求 `deepagents>=0.6`，sherry 用自定义 agent builder，需适配层 |
| LangGraph 版本兼容 | 低 | sherry 用 1.3，ldw 要求 `>=1.0`——兼容 |
| 工具 session 隔离 | 高 | sherry 工具进程级单例带 session-scoped state，ldw 叶子隔离 context 运行——需确保 session_id 正确注入 |
| evidence ledger / completion judge | 中 | sherry 的完成门在 ldw 无对应，需在叶子 agent 内部完成 |
| TaskFlow 功能重叠 | 中 | TaskFlow 已有 DAG/budget/evidence gate，需决定替换还是并存 |

### 建议

1. **MVP 不集成 ldw**——当前计划聚焦 handoff + 机械提取 + knowledge 存储。
2. **PTC 脚本模板是 MVP 内低成本简化路径**——增强 plan 抽取 Part 2 生成 `scripts/<method>.py`，零新依赖。
3. **ldw 集成作为后续阶段**——plan 抽取积累足够 success_path 数据后，写 `compile_plan_to_workflow` 编译器。handoff 树的 `hard_constraints`/`failed_paths` 直接成为脚本 guard 断言和排除注释——本计划的结构化数据正好喂给编译器。

本计划是两者的**数据基础设施**——没有结构化的 `success_path` + `hard_constraints` + `failed_paths`，PTC 脚本模板和 ldw 编排脚本都没有编译输入。

---

# 附录 G：链路推演 + 数据流图 + 存储决策 + 前置缺口修复

## G1. 链路推演（三流程对照）

**当前流程（无 handoff）**：
```
subagent 执行 → result_text（自由文本）→ registry → _read_subagent_runs（截断 24KB）
→ plan context（result_text 埋在数组项）→ _PLAN_EXTRACTION_PROMPT
→ LLM 从自由文本猜 failure_set/success_path/method → knowledge(action="write")
```

**仅加 prompt 指示（不够）**：
```
subagent 执行 → result_text（自由文本 + 内嵌 handoff JSON）→ registry → _read_subagent_runs（截断）
→ plan context（handoff JSON 仍埋在 result_text 文本中）→ prompt 说"prefer structured fields"
→ LLM 仍需从文本中找 JSON 并解析 → 不可靠
```

问题：LLM 要在 24KB 文本块里找 ```json 围栏、解析树状 JSON、提取 failed_paths。JSON 格式不完美/被截断/LLM 忽略指示 → 强化归零。

**强化后流程（Phase 1 正则 / Phase 2 结构化）**：
```
subagent 执行 → handoff（Phase 1: result_text 末尾 JSON block / Phase 2: response_format 结构化输出）
→ 完成时 _extract_handoff_json / _extract_handoff_structured 提取存 run.completion.handoff
→ _read_subagent_runs 直接读 run.handoff → 返回 {result_text, handoff} 分离结构
→ 快照过滤 → _assemble_handoff_tree（装配 children）→ _aggregate_handoffs（扁平化）
→ plan context（handoff 是独立结构化字段，cap 后聚合列表）
→ _PLAN_EXTRACTION_PROMPT 消费干净 handoff 树 → LLM 做模式综合而非 JSON 解析
→ knowledge(action="write") + 代码写 handoff-tree.json
```

handoff 不再埋在 result_text 自由文本里——Phase 2 它是 response_format 强制的结构化返回；Phase 1 它是正则提取的独立 JSON block（存 `CompletionState.handoff`）。代码直接读，不依赖 plan 抽取 LLM 解析 JSON。

## G2. 数据流总结图

```
subagent 完成 → result_text + handoff（CompletionState，SQLite 落盘）
                                                                    │
plan 完成 → _read_subagent_runs 读 run.completion.handoff ←─────────┘
                │
                ▼
         _read_plan_snapshot（当前 todos + flows）
                │
                ▼
         _filter_runs_by_snapshot（剔除 cancelled/superseded + resolved_position）
                │
                ▼
         _assemble_handoff_tree（depth 从 spawn_depth、孤儿提升 root、注入 label/outcome/children）
                │
                ├──→ plan context（cap 后聚合列表，注入 prompt，LLM 做模式综合）
                │       │
                │       ▼
                │   LLM 写 plan-summary.json（综合摘要 + hard_constraints/failed_paths_tree）
                │
                └──→ 代码写 handoff-tree.json（LLM 前，原子写——原始合并树）
                            │
                            ▼
                    build_knowledge_block 注入 system prompt（被动，计数摘要 ~50 token）
                    knowledge(action="read", layer="handoff") 主动查询（完整树）
                            │
                            ▼
                    main_agent 被动获知 + 主动查询
```

**两层产物**：
- `plan-summary.json`（LLM 写）— 综合摘要，main agent 被动注入 system prompt
- `handoff-tree.json`（代码写）— 原始合并树，main agent 主动查询

LLM 综合可能丢数据，代码合并不会。两层并列，互为补充。

## G3. 为什么不用 SQLite 存合并结果

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| todos.db 新增字段 | 与 plan 状态同库 | 混生命周期——todo 是执行状态，经验是完成产物。todos 行在 plan 重做时被覆写 | ❌ |
| subagent_registry.db 新增表 | 与原始数据同库 | per-run 存储，不是 plan 级聚合。聚合体跨多个 run，不属于单条 run | ❌ |
| taskflow_registry.db 新增字段 | 与 DAG 同库 | DAG 是执行调度，不是经验。task_flows 行在 flow 完成后变 terminal | ❌ |
| 新建独立 SQLite | 可查询、事务性 | 与已有 knowledge 文件存储不一致——knowledge 始终走文件系统 sidecar JSON | ❌ |
| **knowledge 目录文件** | 与已有设计一致、人可读、可调试、无 schema 迁移、`build_knowledge_block` 已读此目录 | 不可 SQL 查询 | ✅ |

knowledge 存储是**文件系统 JSON**——项目已有设计决策（`knowledge_store.py` 无 SQLite，`build_knowledge_block` 和 `knowledge(action="read")` 都走文件）。合并结果作为 knowledge 的一部分，放同一个 `<plan_key>/` 目录，最一致。未来需跨 plan 查询经验时，可加一层 SQLite 索引指向 knowledge 文件——后续增强，MVP 不需要。

## G4. 前置缺口修复（阻断性）：结构化 handoff 的捕获与存储

> 当前完成路径 `_extract_result_text`（`spawn/core.py:101`）只从最终 AIMessage 的 **text content** 提取自由文本，存进 `run.completion.result_text`（`CompletionState`，`types/registry.py`）。但 response_format 的结构化 handoff 在 AIMessage 的位置因 provider 策略而异：

| provider 策略 | 结构化输出位置 | `_extract_result_text` 能拿到？ |
|---|---|---|
| 原生 response_format（OpenAI 等） | `message.content`（JSON 文本） | 能——`result_text` 即 JSON |
| forced tool_choice（DeepSeek 等） | `message.tool_calls[0].args` | **不能**——tool-call-only message 的 content 空，被跳过 |

不补这个缺口，`_read_subagent_runs` 无可靠结构化 handoff 可读，整条机械链断在捕获环节。

### 修复（三步）

**1. `CompletionState` 新增 `handoff` 字段**（`types/registry.py`）：

```python
class CompletionState(BaseModel):
    required: bool = True
    result_text: str | None = None        # 自由文本（Task Result 摘要）
    handoff: dict | None = None           # NEW — 结构化 handoff（4 经验字段）
    captured_at: float | None = None
```

`result_text` 与 `handoff` 分离：前者给父 agent / plan 抽取 LLM 读自由文本摘要，后者给机械消费的结构化经验。`store_sqlite.py` 的 schema 同步加 `handoff` 列（JSON blob，`ALTER TABLE` 增量迁移，旧行 NULL）。

**2. `_extract_handoff_structured`**（`core.py`，Phase 2 主路径）— 见 Phase 2 文件改动②。Phase 1 用 `_extract_handoff_json` 正则。

**3. `complete_run` 路径捕获并存储**：`_finalize_child_run`（`spawn/core.py`）调 `complete_run` 前，调提取函数（Phase 1 `_extract_handoff_json(result_text)` / Phase 2 `_extract_handoff_structured(agent_result, output_schema)`），结果作为 `handoff` 参数传给 `complete_run`，写入 `run.completion.handoff` + SQLite mirror。`_read_subagent_runs` 直接读 `run.completion.handoff`（不再现场提取）——提取在完成时一次性做，plan 抽取只读。

### 与 `_extract_result_text` 的关系

两者并行——`_extract_result_text` 提自由文本摘要（Task Result，给父 agent / plan 抽取 LLM 读）；提取函数提结构化 handoff（给机械聚合读）。原生 response_format 路径下两者可能重叠（content 既是 JSON 又被当 result_text）——此时 `result_text` 是 JSON 字符串、`handoff` 是解析后的 dict，不冲突。父 agent 读 result_text 看到 JSON（可忽略），plan 抽取 LLM 读 details 时可忽略 JSON，机械聚合读 handoff dict。
