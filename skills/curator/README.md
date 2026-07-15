# Curator — 后台技能维护编排器

[**English**](README.en.md) | **中文**

> **Curator** 是 EMA AI Agent 的后台技能维护系统，负责对 Agent 自动创建的技能进行生命周期管理、归档和合并整合。

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
- [分类与对账](#分类与对账)
- [使用记录系统](#使用记录系统)
- [Pin 机制](#pin-机制)
- [报告系统](#报告系统)
- [配置参考](#配置参考)
- [不变量](#不变量)
- [文件结构](#文件结构)

---

## 概述

Curator 是一个**空闲触发**的后台任务。当 Agent 处于空闲状态，且距离上次 Curator 运行已超过 `interval_hours` 时，`maybe_run_curator()` 会启动一次后台审查。

它只操作 Agent 创建的技能（`skills/auto/` 下的技能），**绝不触碰内置技能**（`skills/builtin/`）。默认行为是**归档而非删除**，确保所有操作可恢复。

---

## 核心职责

1. **生命周期自动转换** — 基于技能活跃时间戳自动推进 `active → stale → archived`
2. **合并整合**（可选 LLM Pass） — 将重叠的窄技能合并为类级别伞形技能
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
│                  │     ├── 跳过 pinned / cron-referenced        │
│                  │     └── 按 cutoff 时间标记 stale/archived    │
│                  │                                              │
│                  ├── 2. LLM 合并整合 (可选)                     │
│                  │     ├── _render_candidate_list()             │
│                  │     ├── _run_llm_review(prompt)              │
│                  │     └── 解析结构化 YAML 输出                 │
│                  │                                              │
│                  └── 3. 报告与持久化                             │
│                        ├── _write_run_report() → logs/curator/  │
│                        └── save_state() → .curator_state        │
└─────────────────────────────────────────────────────────────────┘
```

---

## 触发机制

Curator 采用**空闲触发**模式，而非定时 cron：

```
maybe_run_curator(idle_for_seconds=...)
  │
  ├── should_run_now() 检查:
  │     ├── is_enabled() == False  → 跳过
  │     ├── is_paused() == True    → 跳过
  │     ├── 首次运行              → 种子 last_run_at，返回 False（推迟一次间隔）
  │     └── now - last_run_at >= interval_hours → 可运行
  │
  └── idle_for_seconds < min_idle_hours * 3600 → 跳过
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `interval_hours` | 168（7 天） | 两次 Curator 运行的最小间隔 |
| `min_idle_hours` | 2 | Agent 必须空闲至少 N 小时才触发 |

首次调用时，Curator 仅种子 `last_run_at` 时间戳，**不会立即执行**，确保不会在 Agent 刚启动时就运行审查。

---

## 生命周期状态机

```
    active ──────(stale_after_days 无活动)──────► stale
      ▲                                              │
      │            (有新活动 / 重新激活)              │
      └──────────────────────────────────────────────┘
      │                                              │
      │         (archive_after_days 无活动)           │
      └──────────────────► archived ◄─────────────────┘
                              │
                              │ (restore_skill)
                              ▼
                           active
```

| 状态 | 含义 |
|------|------|
| `active` | 技能正常可用 |
| `stale` | 超过 `stale_after_days` 无活动，标记为陈旧 |
| `archived` | 超过 `archive_after_days` 无活动，移入 `.archive/` |

**关键约束**：
- Pinned 技能**永不**被自动转换
- Cron 引用的技能**永不**被自动转换
- 归档是可恢复的（`restore_skill()`），删除是不可逆的

---

## 执行流程

### run_curator_review()

```
run_curator_review(synchronous=True, dry_run=False, consolidate=None)
  │
  ├── 1. 自动转换阶段
  │     ├── dry_run=True → 仅统计，不修改
  │     └── dry_run=False → apply_automatic_transitions()
  │           ├── 标记 stale
  │           ├── 归档（移动到 .archive/）
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
  ├── cron-referenced? → 跳过
  ├── 无 usage record? → seed_record_if_missing(), 跳过
  │
  ├── 从未使用 (use_count==0) 且创建时间 < stale_cutoff?
  │     └── 如果当前 stale → 重新激活为 active
  │
  ├── last_activity <= archive_cutoff 且非 archived?
  │     └── _remove_skill() → 归档或删除
  │
  ├── last_activity <= stale_cutoff 且当前 active?
  │     └── 标记为 stale
  │
  └── last_activity > stale_cutoff 且当前 stale?
        └── 重新激活为 active
```

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

**Dry-run 模式**：LLM 只输出"将要采取的行动"，不实际修改技能库。

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
- 搜索被移除技能名的引用
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
  "last_activity_at": "2026-07-15T12:30:00+00:00",
  "_persisted": true
}
```

| 字段 | 说明 |
|------|------|
| `use_count` | 技能被调用次数 |
| `view_count` | 技能被查看次数 |
| `patch_count` | 技能被修改次数 |
| `activity_count` | 上述所有计数之和 |
| `last_activity_at` | 最后一次活动的时间戳 |
| `_persisted` | 记录是否已持久化到磁盘 |

`bump_usage(name, field)` 是外部入口，每次技能被使用/查看/修改时调用，自动递增计数并更新 `last_activity_at`。

---

## Pin 机制

Pinned 技能享有最高保护级别：

- **双重判定**：usage record 中 `pinned=True` **或** 技能目录下存在 `.pinned` 标记文件
- **保护效果**：跳过所有自动转换（stale/archived 均不触发）
- **操作方式**：`pin_skill(name)` / `unpin_skill(name)`，同时更新 record 和标记文件

---

## 报告系统

每次运行生成一份详细报告，保存在 `logs/curator/{timestamp}/` 下：

| 文件 | 内容 |
|------|------|
| `run.json` | 完整的结构化数据（转换计数、分类结果、tool_calls、LLM 输出等） |
| `REPORT.md` | 人类可读的 Markdown 报告 |

**REPORT.md 包含**：
- 运行元信息（模型、时长、技能数量变化）
- 自动转换统计
- LLM 合并统计（consolidated / pruned）
- 具体的合并和清理列表
- 恢复指引

**恢复方式**：
```bash
curator restore <skill-name>   # 从 .archive/ 恢复
```

---

## 配置参考

配置文件路径：`config/curator.yaml`

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `enabled` | `true` | 是否启用 Curator |
| `interval_hours` | `168`（7 天） | 运行间隔 |
| `min_idle_hours` | `2` | 最小空闲时间 |
| `stale_after_days` | `30` | 标记为 stale 的天数 |
| `archive_after_days` | `90` | 归档的天数 |
| `consolidate` | `false` | 是否启用 LLM 合并整合 |
| `prune_builtins` | `true` | 是否清理内置技能的 usage 记录 |

**环境变量**：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `CURATOR_PROCESS_USELESS_SKILL` | `archive` | 无用技能处理方式：`archive`（归档）或 `delete`（删除） |

---

## 不变量

Curator 遵循以下严格不变量，任何情况下不可违反：

1. **只触碰 Agent 创建的技能**（`skills/auto/`），绝不触碰内置技能（`skills/builtin/`）
2. **永不自动删除** — 默认只归档，归档可恢复（除非 `CURATOR_PROCESS_USELESS_SKILL=delete`）
3. **Pinned 技能绕过所有自动转换**
4. **Cron 引用的技能永不自动转换**

---

## 文件结构

```
curator/
├── __init__.py           # 公共 API 导出
├── constants.py          # 常量定义（路径、状态名、默认值）
├── config.py             # 配置加载（curator.yaml + 环境变量）
├── state.py              # Curator 运行状态持久化（.curator_state）
├── usage.py              # 技能使用记录 CRUD（.usage/{name}.json）
├── transitions.py        # 自动状态转换 + should_run_now 判定
├── orchestrator.py       # 主编排器（run_curator_review / maybe_run_curator）
├── classify.py           # 移除技能分类（合并 vs 清理）+ 三源对账
├── helpers.py            # 工具函数（ISO 解析、原子写入、cron 引用读取）
└── report.py             # 运行报告生成（run.json + REPORT.md）
```

**运行时文件**：
```
skills/
├── .curator_state              # Curator 运行状态
├── .archive/                   # 归档技能目录
└── auto/
    └── .usage/
        └── {skill-name}.json   # 技能使用记录

logs/curator/
└── {timestamp}/
    ├── run.json                # 结构化运行数据
    └── REPORT.md               # 人类可读报告
```
