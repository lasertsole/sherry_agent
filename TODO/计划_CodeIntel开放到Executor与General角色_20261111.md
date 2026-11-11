# 计划：Code Intel 开放范围扩大到 EXECUTOR / GENERAL 角色

- 日期：2026-11-11
- 状态：**待评审**
- 来源：当前 Code Intel（code-intel 6 件 + LSP 8 件）仅注入 RESEARCHER/LIBRARIAN；EXECUTOR 需检索时要 spawn researcher 子代，简单调用图查询也要走子智能体往返。扩大到 EXECUTOR/GENERAL 让其直接用，减少不必要 spawn 开销
- 风险等级：**中** —— 涉及角色权限边界与 LSP 资源压力；但 Code Intel 是本地分析工具（不 spawn 子代），不破坏单跳 allow-list 安全不变量

---

## 0. 现状（已代码核实）

### 0.1 角色门控机制

- `CODE_INTEL_ROLES = {RESEARCHER, LIBRARIAN}`（`agent/tools/subagent/types/functional_role.py:31-33`）
- 注入点单点：`child_agent_factory.py:255-263`，`if functional_role in CODE_INTEL_ROLES:` 块内注入 `build_code_intel_tools()`（6 件 tree-sitter）+ `build_lsp_tools()`（8 件 LSP）
- 这两组 builder **从不进 `_MAIN_TOOLS_BUILDERS`**，主智能体永远拿不到
- `metadata["scope"]="researcher_only"` 是描述性标签，`inherited_tool_policy.py:94` 实际只强制 `scope=="main_only"` 的丢弃，**不检查 `researcher_only`**——真正隔离靠角色条件注入

### 0.2 各角色当前工具可见性

| 工具族 | MAIN | GENERAL 子 | RESEARCHER 子 | LIBRARIAN 子 | EXECUTOR 子 | REVIEWER 子 |
|---|---|---|---|---|---|---|
| code-intel 6 件（explore/callers/callees/impact/path/semantic_search） | 无 | 无 | 有 | 有 | **无** | 无 |
| LSP 8 件（goto_def/references/symbol/call_hierarchy/rename/diagnostics/format/status） | 无 | 无 | 有 | 有 | **无** | 无 |
| ast-grep 2 件（search/rewrite） | 无 | 有 | 有 | 有 | 有 | 有 |
| PTC execute_code | 无 | 无 | 无 | 无 | 有 | 无 |

EXECUTOR 当前只有 ast-grep（结构搜索/重写）+ PTC，**没有**调用图/LSP/语义检索。要查"这个函数的调用方"得 spawn 一个 researcher。

### 0.3 LSP 写工具的只读约束

`lsp_rename`（`dry_run` 默认 `True`）和 `lsp_format`（`write` 默认 `False`）默认预览模式。**代码层不阻止**显式传 `dry_run=false`/`write=true` 真实写盘——只读性靠 `system_prompt.py:225-226` 给 RESEARCHER/LIBRARIAN 的提示词软约束（"Never pass dry_run=false: your role is read-only"）。

### 0.4 设计哲学的当前依据（需认知的张力）

`functional_role.py:58-63` 注释明确：EXECUTOR 的 researcher/librarian 子代"携带父代没有的只读额外工具"是**有意偏差**——写角色应 spawn 只读子代做检索，而非自己做。本计划是**对这个哲学的有限调整**：让 EXECUTOR/GENERAL 直接做轻量检索（调用图、find-references、诊断），减少简单查询的子代往返；重调查仍走 researcher spawn。单跳 allow-list 安全不变量不受影响（Code Intel 是本地分析，不 spawn 子代）。

---

## 1. 目标

| 角色 | 变更 |
|---|---|
| EXECUTOR | 新增 code-intel 6 件 + LSP 8 件；LSP 写工具（rename/format）允许实际应用 |
| GENERAL | 同 EXECUTOR |
| RESEARCHER / LIBRARIAN | 不变（已有，写工具仍预览-only + 提示词约束） |
| REVIEWER | 不变（仅 ast-grep，审计角色不需全量 Code Intel） |
| MAIN | 不变（仍不直接持有，通过子智能体） |

---

## 2. 实施步骤

### 步骤 1：扩大注入集

`functional_role.py:31-33`：

```python
# 原
CODE_INTEL_ROLES: frozenset[FunctionalRole] = frozenset(
    {FunctionalRole.RESEARCHER, FunctionalRole.LIBRARIAN}
)
# 改为
CODE_INTEL_ROLES: frozenset[FunctionalRole] = frozenset({
    FunctionalRole.RESEARCHER,
    FunctionalRole.LIBRARIAN,
    FunctionalRole.EXECUTOR,
    FunctionalRole.GENERAL,
})
```

注入点 `child_agent_factory.py:255-263` 的 `if functional_role in CODE_INTEL_ROLES:` 无需改（条件自动覆盖新角色）。

> 注释更新：说明 EXECUTOR/GENERAL 也注入，但写工具应用范围由提示词区分（步骤 2）。

### 步骤 2：系统提示词按角色分支

`system_prompt.py:186/210/225-226` 当前对 CODE_INTEL_ROLES 统一生成"Code Intelligence Tools"段落 + "Never pass dry_run=false" 约束。改为按角色分支：

- **RESEARCHER / LIBRARIAN（只读）**：保留现有措辞 + "Never pass dry_run=false / write=true: your role is read-only"
- **EXECUTOR / GENERAL（写角色）**：
  - 检索工具（explore/callers/callees/impact/path/semantic_search/goto_def/references/symbol/call_hierarchy/diagnostics） framing 为"编辑前理解影响面 + 定位"
  - 写工具（rename/format）允许实际应用："You may pass dry_run=false / write=true to apply renames/formatting when appropriate — prefer lsp_rename over manual patch_file for cross-file symbol renames (semantically accurate)"
  - 提示轻量检索直接做、重调查仍可 spawn researcher

### 步骤 3：LSP 写工具默认值（不改）

`lsp_rename` `dry_run=True`、`lsp_format` `write=False` 的**默认值保持不变**（安全默认预览）。写角色通过提示词告知可显式传 `false`/`true` 应用，而非改默认值。这样：
- 模型不显式传时仍是预览（安全）
- 写角色显式传时应用（提示词允许）
- 只读角色显式传时...代码层仍会执行（现有缺口，不在本计划修复；如需硬隔离另立任务按角色强制 dry_run）

### 步骤 4：LSP 资源调优

LSP server 是进程级单例（`get_manager()`），按 `(language, cwd)` 共享，`lsp_max_concurrent_servers=2` LRU。开放到更多角色不乘 server 数（共享池），但增加竞争：

- `config/features/agent_side/lsp.py`：`lsp_max_concurrent_servers` 从 `2` 调到 `3`（缓冲多角色竞争，仍控内存——basedpyright/rust-analyzer 每个数百 MB）
- 监控：观察 LRU 逐出频率（manager 可加日志），频繁逐出则考虑 4 或按需懒加载策略
- idle shutdown 300s 保持不变（开放角色不改变空闲回收逻辑）

### 步骤 5：测试更新

`tests/agent/tools/subagent/types/test_functional_role.py:33-38` 当前断言 `EXECUTOR/GENERAL` **不在** `CODE_INTEL_ROLES`。改为断言**在**集合内。新增测试：
- EXECUTOR 子智能体构建后工具集含 code-intel + LSP 工具
- GENERAL 同
- REVIEWER 仍不含
- 注入工具的 `metadata["scope"]` 标签更新（可选：从 `researcher_only` 改为 `subagent_only` 或新增 `code_intel` 标签，反映开放范围；非强制，因标签非门控）

### 步骤 6：文档与 docstring

- `code_intel/tools.py:1` 和 `lsp/tools.py:1` 的模块 docstring 从"RESEARCHER only"/"CODE_INTEL_ROLES only"更新为"CODE_INTEL_ROLES (RESEARCHER/LIBRARIAN/EXECUTOR/GENERAL)"
- `functional_role.py:58-63` 注释更新：说明 EXECUTOR/GENERAL 现直接持轻量 Code Intel，重调查仍走 researcher spawn

---

## 3. 安全性分析（评审重点）

### 3.1 单跳 allow-list 不受影响

`SPAWN_ALLOWED_CHILD_ROLES`（`functional_role.py:64-84`）限制的是**子代 spawn 的角色**。Code Intel 工具是本地分析（调 SQLite 索引 / LSP server / ast-grep CLI），**不 spawn 子智能体**。开放 Code Intel 给 EXECUTOR 不改变它能 spawn 的角色集合（仍 `{EXECUTOR, RESEARCHER, LIBRARIAN}`）。

### 3.2 写权限边界不扩大

EXECUTOR 已是写角色（有 patch_file / ast_grep_rewrite / PTC）。新增的 LSP 写工具（rename/format）是**更安全的**多文件编辑方式：
- `lsp_rename`：LSP server 语义理解，跨文件符号重命名，比 ast_grep_rewrite（ syntactic 结构匹配）更准确
- `lsp_format`：受限的格式化，比手动 patch_file 更窄
- 不引入新的写能力维度，只是更好的写工具

GENERAL 本就是全工具可写（`functional_role.py:11` 注释），新增不改变其写权限。

### 3.3 风险点：只读角色软约束缺口（既有，非本计划引入）

`lsp_rename`/`lsp_format` 代码层不阻止只读角色显式传 `dry_run=false`。本计划不修复此缺口（如需硬隔离另立任务：按角色强制 dry_run）。本计划只是**新增**写角色可合法应用，不改变只读角色的既有约束状态。

---

## 4. 风险与缓解

| 风险 | 缓解 |
|---|---|
| LSP server 竞争加剧（多角色用，max 2 LRU 抖动） | 步骤 4 调到 3 + 监控逐出频率 |
| EXECUTOR 角色职责模糊（既写又检索） | 提示词明确"轻量检索直接做、重调查 spawn researcher"；不强制 |
| LSP 冷启动拖慢 EXECUTOR turn | LSP 惰性 acquire（首次用才启动）+ idle 回收已有；fail-open 不阻塞 |
| 测试断言需更新 | 步骤 5 显式改断言 |
| 语义层（semantic_code_search）依赖 CodeEmbedder profile | 注入不变（`build_code_intel_tools` 内部按 profile 决定是否加第 6 件），开放角色同样受 profile 门控 |

### Kill criteria

- 若 LSP 竞争导致 EXECUTOR turn 明显卡顿（LSP 冷启动 + LRU 抖动），且调到 3 仍不收敛，回退 EXECUTOR 的 LSP 注入（保留 code-intel tree-sitter 6 件 + ast-grep，LSP 仍走 researcher spawn）
- 若实际使用中 EXECUTOR 直接用 Code Intel 的频率低（仍倾向 spawn researcher），记录后不回退但调整提示词弱化引导

---

## 5. 验收

- EXECUTOR 子智能体工具集含 code-intel 6 件 + LSP 8 件（测试断言）
- GENERAL 子智能体同
- REVIEWER 仍仅 ast-grep（不变）
- RESEARCHER/LIBRARIAN 不变（含写工具预览约束提示词）
- EXECUTOR/GENERAL 提示词含"可应用 rename/format"+"轻量直接做、重调查 spawn researcher"
- LSP `lsp_max_concurrent_servers=3`，逐出频率可观测
- 单跳 allow-list 测试全过（`test_functional_role.py` 的 spawn allow-list 断言不变）
- 回归：`uv run pytest tests/agent/tools/subagent -q` + `tests/agent/tools/code_intel` 全过

---

## 6. 实施顺序

| 步骤 | 内容 | 风险 |
|---|---|---|
| 1 | 扩大 `CODE_INTEL_ROLES` | 低（单行改 + 注入自动覆盖） |
| 2 | 系统提示词按角色分支 | 低（提示词调整） |
| 3 | 测试更新 | 低（断言改 + 新增） |
| 4 | LSP 资源调优（max=3） | 低（配置改） |
| 5 | docstring/注释更新 | 无 |
| 6 | 验收 + 观察竞争 | — |

步骤 1-5 可一次提交；步骤 6 跑一轮带 EXECUTOR 编码的 e2e 观察 LSP 逐出频率。
