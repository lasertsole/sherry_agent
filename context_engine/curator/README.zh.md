# Curator — 后台技能维护编排器

[**English**](README) | **中文**

> **Curator** 是 EMA AI Agent 的后台技能维护系统，负责对 Agent 自动创建的技能进行生命周期管理、合并整合和清理。

---

## 目录

- [概述](#概述)
- [核心职责](#核心职责)
- [架构](#架构)
- [触发机制](#触发机制)
- [生命周期状态机](#生命周期状态机)
- [执行流程](#执行流程)
- [自动转换规则](#自动转换规则)
- [LLM 合并整合](#llm-合并整合)
- [伞形技能生成](#伞形技能生成)
- [分类与对账](#分类与对账)
- [使用记录系统](#使用记录系统)
- [孤立记录清理](#孤立记录清理)
- [Pin 机制](#pin-机制)
- [报告系统](#报告系统)
- [配置参考](#配置参考)
- [Curator 状态文件](#curator-状态文件)
- [不变量](#不变量)
- [文件结构](#文件结构)

---

## 概述

Curator 是一个**空闲触发**的后台任务。当 Agent 处于空闲状态，且距离上次 Curator 运行已超过 `interval_hours` 时，`maybe_run_curator()` 会启动一次后台审查。

它只操作 Agent 创建的技能（`skills/auto/` 下的技能），**绝不触碰内置技能**（`skills/builtin/`）。陈旧和未使用的技能会被**删除**（从磁盘移除），LLM 合并整合可在清理前将重叠技能合并为伞形技能。

---

## 核心职责

1. **生命周期自动转换** — 基于技能活跃时间戳自动推进 `active → stale`；删除超过归档截止时间的技能
2. **合并整合**（可选 LLM Pass） — 将重叠的窄技能合并为类级别伞形技能，自动生成内容并迁移文件
3. **持久化状态** — 在 `.curator_state` 文件中保存运行历史

---

## 架构

```
┌─────────────────────────────────────────────────────────────────┐
│  maybe_run_curator()                                            │
│    │                                                            │
│    ├── should_run_now()? ── 否 ──► 返回 None                   │
│    │                                                            │
│    └── 是 ──► run_curator_review()                              │
│                  │                                              │
│                  ├── 1. 自动转换 (apply_automatic_transitions)  │
│                  │     ├── 遍历 agent_created_report()          │
│                  │     ├── 跳过 pinned                         │
│                  │     └── 按 cutoff 时间标记 stale / 删除      │
│                  │                                              │
│                  ├── 2. LLM 合并整合 (可选)                     │
│                  │     ├── _render_candidate_list()             │
│                  │     ├── _run_llm_review(prompt)              │
│                  │     ├── _apply_consolidation()               │
│                  │     │     ├── _generate_umbrella_skill()     │
│                  │     │     └── 迁移支持文件                   │
│                  │     └── 解析结构化 YAML 输出                 │
│                  │                                              │
│                  └── 3. 报告与持久化                             │
│                        ├── _build_rename_summary()              │
│                        ├── _write_run_report() → logs/curator/  │
│                        └── save_state() → .curator_state        │
└─────────────────────────────────────────────────────────────────┘
```

---

## 触发机制

Curator 采用**空闲触发**模式，而非定时调度：

```
maybe_run_curator(idle_for_seconds=..., on_summary=...)
  │
  ├── should_run_now() 检查:
  │     ├── is_enabled() == False  → 跳过
  │     ├── is_paused() == True    → 跳过
  │     ├── last_run_at 为 None    → 可运行（首次运行立即执行）
  │     └── now - last_run_at >= interval_hours → 可运行
  │
  └── idle_for_seconds < min_idle_hours * 3600 → 跳过
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `interval_hours` | 168（7 天） | 两次 Curator 运行的最小间隔 |
| `min_idle_hours` | 2 | Agent 必须空闲至少 N 小时才触发 |

如果 `last_run_at` 从未被设置，首次调用 `should_run_now()` 返回 `True`，审查立即执行（无延迟首次运行的种子机制）。

---

## 生命周期状态机

```
    active ──────(stale_after_days 无活动)──────► stale
      ▲                                              │
      │            (有新活动 / 重新激活)              │
      └──────────────────────────────────────────────┘
      │                                              │
      │         (archive_after_days 无活动)           │
      └──────────────────► deleted ◄──────────────────┘
```

| 状态 | 含义 |
|------|------|
| `active` | 技能正常可用 |
| `stale` | 超过 `stale_after_days` 无活动，标记为陈旧 |

当技能超过 `archive_after_days` 无活动时，会被**删除**（目录和使用记录从磁盘移除）。没有中间的 `archived` 状态——删除是不可逆的。

**关键约束**：
- Pinned 技能**永不**被自动转换或删除
- 创建时间在 stale_cutoff 之后且从未使用（`use_count==0`）的技能，如果当前为 stale，会被**重新激活**

---

## 执行流程

### run_curator_review()

```
run_curator_review(on_summary=None, synchronous=True, dry_run=False, consolidate=None)
  │
  ├── 1. 自动转换阶段
  │     ├── dry_run=True → 仅统计，不修改
  │     └── dry_run=False → apply_automatic_transitions()
  │           ├── 标记 stale
  │           ├── 删除（从磁盘移除）
  │           └── 重新激活
  │
  ├── 2. 保存中间状态
  │     └── last_run_at, run_count, last_run_summary
  │
  ├── 3. LLM 合并整合（_llm_pass）
  │     ├── consolidate=False → 跳过，写入报告
  │     └── consolidate=True:
  │           ├── 快照 before_report (技能列表)
  │           ├── _render_candidate_list() → 候选列表
  │           ├── _run_llm_review(prompt) → LLM 调用
  │           ├── _apply_consolidation(llm_final):
  │           │     ├── 解析结构化 YAML（consolidations + prunings）
  │           │     ├── 为每个伞形技能调用 _generate_umbrella_skill()
  │           │     ├── 迁移支持文件 (references/, templates/, scripts/, assets/)
  │           │     ├── 删除合并源技能
  │           │     └── 删除被清理的技能
  │           ├── 快照 after_report
  │           ├── _build_rename_summary() → 分类变更
  │           └── _write_run_report() → logs/curator/{timestamp}/
  │
  ├── 4. 执行模式
  │     ├── synchronous=True → 当前线程执行
  │     └── synchronous=False → 新建 daemon 线程执行
  │
  └── 5. 返回
        └── { started_at, auto_transitions, summary_so_far }
```

### _run_llm_review()

```
_run_llm_review(prompt)
  │
  ├── 构建 LLM (build_main_llm, temperature=0.3)
  ├── 组装消息 (system prompt + user prompt)
  ├── llm.invoke(messages)
  │
  └── 返回 { final, summary, model, provider, tool_calls, error }
```

LLM 可能调用 `skill_manage` 工具来创建/修改/删除技能，这些 tool_calls 会被记录并用于分类对账。

---

## 自动转换规则

`apply_automatic_transitions()` 对每个 Agent 创建的技能执行以下判定：

```
对每个 agent-created skill:
  │
  ├── pinned? → 跳过
  ├── 无已持久化的 usage record? → seed_record_if_missing(), 跳过
  │
  ├── 从未使用 (use_count==0) 且 anchor > stale_cutoff?
  │     └── 如果当前 stale → 重新激活为 active
  │
  ├── anchor <= archive_cutoff 且非 archived?
  │     └── _remove_skill() → 从磁盘删除
  │
  ├── anchor <= stale_cutoff 且当前 active?
  │     └── 标记为 stale
  │
  └── anchor > stale_cutoff 且当前 stale?
        └── 重新激活为 active
```

其中 `anchor` = `last_activity_at`（若从未活跃则取 `created_at`，否则取 `now` 作为兜底）。

时间截止点：
- `stale_cutoff = now - stale_after_days`（默认 30 天）
- `archive_cutoff = now - archive_after_days`（默认 90 天）

---

## LLM 合并整合

Curator 的 LLM Pass 接收 `CURATOR_REVIEW_PROMPT`，指导 LLM 将窄技能合并为类级别伞形技能：

**合并策略**：
- **a. 合入已有伞形** — 在伞形技能中添加标签段落，归档兄弟技能
- **b. 创建新伞形** — 编写类级别技能，归档兄弟技能
- **c. 降级为引用** — 将窄内容移入伞形技能的支持目录，归档旧技能

**LLM 输出格式**（YAML 结构化摘要）：
```yaml
consolidations:
  - from: old-skill-name
    into: umbrella-skill-name
    reason: why merged
prunings:
  - name: skill-name
    reason: why archived
```

**Dry-run 模式**：LLM 只输出"将要采取的行动"，不实际修改技能库。Dry-run 时会在提示词前添加 `CURATOR_DRY_RUN_BANNER`。

---

## 伞形技能生成

当合并整合产生新的伞形技能时，`_apply_consolidation()` 编排完整的合并流程：

### _generate_umbrella_skill()

通过 LLM 为每个新伞形技能生成合并后的 SKILL.md：

```
_generate_umbrella_skill(umbrella, reasons, source_content, file_inventory)
  │
  ├── 构建 LLM (build_main_llm, temperature=0.3)
  ├── 系统提示词：技能管理员创建伞形技能
  │     - 仅输出 SKILL.md 内容（YAML frontmatter + markdown 正文）
  │     - frontmatter：name, description, created_by: curator
  │     - 合成并去重：合并重叠指令，统一代码模式，去除冗余，保留所有独特技术
  │     - 使用 ## 标题组织每个关注领域
  │     - 包含 "## When to use" 段落
  │     - 使用相对链接引用已迁移的支持文件
  │
  ├── 用户提示词：伞形名称 + 合并原因 + 源技能内容 + 文件清单
  │
  ├── 成功 → 返回生成的 SKILL.md 内容
  └── 失败 → 返回回退骨架（拼接源内容）
```

### 文件迁移

创建伞形技能后，源技能的支持文件会被迁移：

```
对每个合并条目 (from → into umbrella):
  │
  ├── 对每个支持子目录 (references/, templates/, scripts/, assets/):
  │     └── 将每个文件复制到伞形技能的对应子目录
  │
  └── 删除源技能 (delete_skill with absorbed_into=into)
```

### 清理

`prunings` 块中列出的、不属于任何合并条目的技能会被直接删除。

---

## 分类与对账

当 LLM Pass 执行后，一些技能可能被移除。`classify.py` 负责判断每个被移除的技能是**被合并**（consolidated）还是**被清理**（pruned）：

### 三源对账

```
_reconcile_classification(removed, heuristic, model_block, destinations, absorbed_declarations)
  │
  ├── 对每个 removed skill:
  │
  │   1. absorbed_into 声明（LLM 删除时附带）
  │      ├── 目标存在于 destinations → 合并
  │      └── 声明为空 → 清理
  │
  │   2. 模型结构化块（YAML 输出中的 consolidations）
  │      ├── 目标存在 → 合并
  │      └── 目标不存在 → 回退到启发式或标记为清理
  │
  │   3. 启发式审计（tool_call 内容中引用旧技能名）
  │      ├── 有证据 → 合并
  │      └── 无证据 → 清理
  │
  │   4. 无任何证据 → 标记为清理（no-evidence fallback）
  │
  └── 输出: { consolidated: [...], pruned: [...] }
```

**启发式审计**（`_classify_removed_skills`）检查 LLM 的 `skill_manage` tool_calls：
- 遍历 tool_call 参数（file_path, content, new_string 等）
- 搜索被移除技能名的引用（包括 `-`/`_` 变体）
- 对 `file_path` 字段使用 `_needle_in_path_component()` 进行路径感知匹配
- 对内容字段使用单词边界正则匹配
- 若找到引用 → 证据表明该技能被合并到目标伞形

---

## 使用记录系统

每个 Agent 创建的技能在 `skills/auto/.usage/` 下有对应的 JSON 记录文件：

```json
{
  "name": "my-skill",
  "state": "active",
  "pinned": false,
  "use_count": 3,
  "view_count": 5,
  "patch_count": 1,
  "activity_count": 9,
  "created_at": "2026-07-15T10:00:00+00:00",
  "last_activity_at": "2026-07-15T12:30:00+00:00"
}
```

| 字段 | 说明 |
|------|------|
| `use_count` | 技能被调用次数 |
| `view_count` | 技能被查看次数 |
| `patch_count` | 技能被修改次数 |
| `activity_count` | 上述所有计数之和 |
| `last_activity_at` | 最后一次活动的时间戳（从未使用则为 null） |
| `created_at` | 记录创建时间 |
| `_persisted` | 内部标志 — `seed_record_if_missing()` 写入记录后为 `True` |

`_default_record()` 创建新记录，`use_count=0`、`activity_count=0`、`last_activity_at=None`。

---

## 孤立记录清理

`agent_created_report()` 自动调用 `_cleanup_orphan_records()`，移除 `.usage/` 中没有对应技能目录的 JSON 文件。这确保使用记录存储与磁盘上的实际技能目录保持一致。

---

## Pin 机制

Pinned 技能享有最高保护级别：

- **双重判定**：usage record 中 `pinned=True` **或** 技能目录下存在 `.pinned` 标记文件
- **保护效果**：跳过所有自动转换（stale/删除均不触发）；`_pinned_guard()` 阻止任何删除或状态变更操作
- **守卫行为**：`set_state()`、`delete_skill()` 和 `_remove_skill()` 在执行前都会检查 `_pinned_guard()` —— 如果是 pinned，操作会被拒绝并记录警告

当前实现中没有公开的 `pin_skill()` / `unpin_skill()` 函数。固定操作通过外部管理（设置 usage record 中的 `pinned` 字段或创建 `.pinned` 标记文件）。

---

## 报告系统

每次运行生成一份详细报告，保存在 `logs/curator/{timestamp}/` 下：

| 文件 | 内容 |
|------|------|
| `run.json` | 完整的结构化数据（转换计数、分类结果、tool_calls、LLM 输出等） |
| `REPORT.md` | 人类可读的 Markdown 报告 |

**REPORT.md 包含**：
- 运行元信息（模型、提供商、时长、技能数量变化）
- 自动转换统计
- LLM 合并统计（consolidated / pruned）
- 具体的合并和清理列表（各最多 50 条）
- 按名称统计的 tool call 数量
- 自动摘要文本
- LLM 最终摘要文本
- 恢复说明

**恢复方式**：
> **注意**：由于技能是删除而非归档，恢复只能通过版本控制或备份实现。当前实现中没有 `restore_skill()` 函数。

---

## 配置参考

配置文件路径：`curator.yaml`（项目根目录，与 `ROOT_DIR` 同级）

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `enabled` | `true` | 是否启用 Curator |
| `interval_hours` | `168`（7 天） | 运行间隔 |
| `min_idle_hours` | `2` | 最小空闲时间 |
| `stale_after_days` | `30` | 标记为 stale 的天数 |
| `archive_after_days` | `90` | 删除的天数 |
| `consolidate` | `false` | 是否启用 LLM 合并整合 |

配置通过 `_load_config()` 加载，使用 PyYAML 读取 `curator.yaml`。每个 getter 函数（`is_enabled`、`get_interval_hours` 等）在解析错误时回退到常量默认值。

---

## Curator 状态文件

路径：`skills/.curator_state`

```json
{
  "last_run_at": "2026-07-28T10:00:00+00:00",
  "last_run_duration_seconds": 12.34,
  "last_run_summary": "auto: 2 marked stale; llm: skipped",
  "last_run_summary_shown_at": null,
  "last_report_path": "/path/to/logs/curator/20260728-100000",
  "paused": false,
  "run_count": 5
}
```

| 字段 | 说明 |
|------|------|
| `last_run_at` | 上次运行的 ISO 时间戳 |
| `last_run_duration_seconds` | 上次运行时长（秒） |
| `last_run_summary` | 上次运行的可读摘要 |
| `last_run_summary_shown_at` | 摘要上次展示时间 |
| `last_report_path` | 上次运行报告目录的路径 |
| `paused` | 为 `True` 时 Curator 不会运行 |
| `run_count` | 已完成的运行总次数 |

状态通过 `load_state()` 加载（与 `_default_state()` 合并，保留以 `_` 开头的未知键），通过 `save_state()` 保存（原子 JSON 写入）。

---

## 不变量

Curator 遵循以下严格不变量，任何情况下不可违反：

1. **只触碰 Agent 创建的技能**（`skills/auto/`），绝不触碰内置技能（`skills/builtin/`）
2. **Pinned 技能绕过所有自动转换** — 永不标记为 stale 或删除
3. **`_pinned_guard()` 是执行层** — 每个破坏性操作都会检查它

---

## 文件结构

```
curator/
├── __init__.py           # 公共 API 导出
├── constants.py          # 常量定义（路径、状态名、默认值）
├── config.py             # 配置加载（curator.yaml + 环境变量）
├── state.py              # Curator 运行状态持久化（.curator_state）
├── usage.py              # 技能使用记录 CRUD（.usage/{name}.json）+ agent_created_report + 孤立记录清理
├── transitions.py        # 自动状态转换 + should_run_now 判定
├── orchestrator.py       # 主编排器（run_curator_review / maybe_run_curator / _apply_consolidation / _generate_umbrella_skill）
├── classify.py           # 移除技能分类（合并 vs 清理）+ 三源对账
├── helpers.py            # 工具函数（ISO 解析、原子写入、技能描述读取、路径匹配）
└── report.py             # 运行报告生成（run.json + REPORT.md + _build_rename_summary）
```

**运行时文件**：
```
skills/
├── .curator_state              # Curator 运行状态
└── auto/
    └── .usage/
        └── {skill-name}.json   # 技能使用记录

logs/curator/
└── {timestamp}/
    ├── run.json                # 结构化运行数据
    └── REPORT.md               # 人类可读报告
```
