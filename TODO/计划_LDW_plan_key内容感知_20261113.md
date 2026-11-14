# 计划：LDW plan_key 内容感知——不同任务不共享 replay 计数器

- 日期：2026-11-13
- 状态：**待评审**
- 来源：过度工程复审 → 评估"LDW/PTC 抽取加泛化判断" → 结论：LDW 的 `min_replays=3` 已是隐式频率过滤器，真正的盲区是 `plan_key` 基于文件路径而非任务内容 → 修 plan_key 比加 LLM 泛化判断更精准、更便宜
- 风险等级：**低** — 改 1 个函数的 hash 输入 + 几个调用点传参；纯 hash 计算，零 LLM 调用
- 依赖：无（LDW 已 enabled，min_replays=3 已生效）

---

## 0. 问题

### 现状

`agent/tools/todolist/knowledge/identity.py:68` 的 `plan_key` 派生方式：

```python
def plan_key(plan_ref) -> str:
    # sha1(repo-relative normalized plan FILE path)[:12]
    return sha1(_normalize_path(plan_ref).encode()).hexdigest()[:12]
```

`plan_key` 是 LDW replay 计数器的存储 key（`PLAN_KNOWLEDGE_DIR/<plan_key>/ldw-replay.json`）。`min_replays=3` 的门槛基于这个计数器。

### 盲区

`plan_key` = plan **文件路径**的 hash，不是任务内容的 hash。导致：

- **不同任务复用同一 plan 文件** → 共享计数器 → 3 次后编译一个不该编译的 workflow
- **同一任务换 plan 文件** → 不同计数器 → 永远达不到 3 次门槛

例子：一个通用的"项目初始化"plan 文件被用于 3 个不同项目，第 3 次触发 LDW 编译——但 3 个项目的任务内容完全不同，编译出的 workflow 对第 4 个项目没有复用价值。

### 为什么不加 LLM 泛化判断

- `min_replays=3` 已经是隐式频率过滤器——真正的一次性任务不会走到 replay 3 次
- LLM 泛化判断增加延迟和成本，且"普适性"是模糊判断，准确率不足以证明成本
- 修 plan_key 以零 LLM 成本解决了真正的盲区（不同任务共享计数器）

---

## 1. 设计

### 1.1 plan_key 混入 goal 文本

`agent/tools/todolist/knowledge/identity.py`，`plan_key` 函数改为接受可选的 `goal_text` 参数：

```python
def plan_key(plan_ref, goal_text: str | None = None) -> str:
    """Stable key for a plan. When goal_text is provided, different goals
    sharing the same plan file get separate keys — preventing unrelated tasks
    from accumulating a shared replay counter."""
    path_part = _normalize_path(plan_ref)
    if goal_text:
        # 不同 goal 用同一 plan 文件 → 不同 key
        content_part = goal_text.strip().lower()[:500]  # 截断防超长 hash 输入
        raw = f"{path_part}:{content_part}"
    else:
        raw = path_part
    return sha1(raw.encode()).hexdigest()[:12]
```

设计决策：
- **goal_text 截断到 500 字符**——goal 通常很短，截断防恶意超长输入；lowercase 做大小写归一化
- **goal_text=None 时行为不变**——向后兼容，不传 goal_text 的调用者拿到和今天一样的 key
- **不用 goal 的完整 hash**——goal 可能被 agent 微调措辞（"Fix the login bug" vs "Fix login bug"），完全 hash 会导致同一任务因措辞微调而分离。但这个问题比"不同任务共享计数器"轻微得多，且 500 字符截断 + lowercase 已经做了基本归一化

### 1.2 fallback_plan_key 同样混入

`identity.py:84` 的 `fallback_plan_key`（无 plan 文件时用 session_id）也加可选 goal_text：

```python
def fallback_plan_key(session_id: str, goal_text: str | None = None) -> str:
    raw = f"session:{session_id}"
    if goal_text:
        raw = f"{raw}:{goal_text.strip().lower()[:500]}"
    return sha1(raw.encode()).hexdigest()[:12]
```

### 1.3 调用点传 goal_text

需要找到所有调用 `plan_key()` / `fallback_plan_key()` 的地方，确认能拿到 goal 文本：

LDW 路径的关键调用点：
- `agent/tools/ldw/replay.py` — `replay_plan` 和 `plan_knowledge_from_flow` 调 `plan_key`。这些函数能从 TaskFlow flow 的 `description` 字段拿到 goal。
- `agent/tools/ldw/compiler.py` — 可能调 `plan_key`。
- `agent/tools/ldw/tool.py` — `workflow_replay` tool 的 `status`/`compile`/`replay` action。
- `agent/tools/subagent/spawn/ptc_prefill.py` — PTC prefill 用 `plan_key` 查找脚本（但 PTC 当前关闭）。
- `agent/middlewares/summarization/ptc_compile.py` — PTC 编译用 `plan_key`（同样关闭）。

对每个调用点：
- 如果能从 flow 的 `description` / `state.description` 拿到 goal → 传 `goal_text`
- 如果拿不到（纯路径上下文）→ 传 `None`（向后兼容，行为不变）

### 1.4 存储 path 不变

`PLAN_KNOWLEDGE_DIR/<plan_key>/ldw-replay.json` 的目录结构不变——只是 `<plan_key>` 的值变了。已有的旧计数器文件（基于纯路径 hash）会自然废弃：新 key 不同 → 新文件 → 计数器从 0 开始。这是期望行为——旧计数器是跨任务混合的，不可信。

不需要迁移脚本。旧的 `ldw-replay.json` 文件可以被安全删除（它们只是计数器，不存不可恢复的数据）。

---

## 2. 变更清单

| 文件 | 变更 |
|---|---|
| `agent/tools/todolist/knowledge/identity.py` | **改**：`plan_key` 和 `fallback_plan_key` 加 `goal_text` 可选参数，混入 hash |
| `agent/tools/ldw/replay.py` | **改**：`replay_plan` / `plan_knowledge_from_flow` 调 `plan_key` 时传 flow 的 `description` 作为 `goal_text` |
| `agent/tools/ldw/tool.py` | **改**：`workflow_replay` tool 的 `status`/`compile`/`replay` action 传 goal_text（从当前 flow 获取） |
| `agent/tools/ldw/compiler.py` | **可能改**：如果调 `plan_key`，传 goal_text |
| `agent/tools/subagent/spawn/ptc_prefill.py` | **不改**（PTC 关闭；启用时一并加） |
| `agent/middlewares/summarization/ptc_compile.py` | **不改**（PTC 关闭；启用时一并加） |

**不改**：LDW config（`min_replays` 不变）、compiler 逻辑、replay 运行逻辑、存储结构。

---

## 3. 测试

| 文件 | 测试 |
|---|---|
| `tests/agent/tools/todolist/knowledge/test_identity.py`（或新建） | `plan_key` 无 goal_text 时 == 旧值（向后兼容）；有 goal_text 时不同 goal → 不同 key；同 goal 不同 plan 文件 → 不同 key；同 goal + 同 plan 文件 → 同 key；goal_text 截断 + lowercase 归一化 |
| `tests/agent/tools/ldw/test_replay.py` | `replay_plan` 传了 flow description 作为 goal_text；不同 goal 的 flow 不共享计数器 |
| 现有 LDW 测试 | 全部通过（向后兼容：不传 goal_text 的路径行为不变） |

---

## 4. 前置调查

- [ ] 盘点 `plan_key()` / `fallback_plan_key()` 的所有调用点，确认每个能否拿到 goal 文本
- [ ] 确认 `replay_plan` 调用时 flow 的 `description` 字段是否一定有值（空 description 的 flow 怎么处理 → 传 None，行为不变）
- [ ] 确认 `workflow_replay` tool 的 `status` action 如何获取当前 flow 的 description（可能需要从 flow 状态读）

---

## 5. Kill criteria

- 若 `plan_key` 的调用点大多数拿不到 goal 文本（flow description 经常为空），则改动无效——回退为纯路径 hash，记录结论"plan_key 内容感知需要先确保 flow description 非空"
- 若现有 LDW 测试因 key 变化而批量失败且修复成本高（>10 个测试文件），评估是否值得——可能需要测试也传 goal_text 参数
