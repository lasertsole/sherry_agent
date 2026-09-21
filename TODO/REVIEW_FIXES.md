# 审查修补清单 (Review Fixes)

> 本文档记录对 `DEEPAGENTS_BORROWING_PLAN.md` P1-6 和 `subagent-role-migration.md` 的高质量审查中发现的问题及修补措施。

---

## 审查范围

| 文件                                                            | 审查内容                                               |
| --------------------------------------------------------------- | ------------------------------------------------------ |
| `TODO/DEEPAGENTS_BORROWING_PLAN.md` — P1-6 中间件脚手架保护     | scaffolding.py 代码正确性、调用点文本、测试覆盖        |
| `TODO/subagent-role-migration.md` — 功能角色分工 + 质量门禁迁移 | AGENTS.md 工具名、工具策略逻辑、verdict 解析、下游依赖 |

---

## P1-6 修补项

### FIX-P1.1 — scaffolding.py 导入缺失

- **问题**: `scaffolding.py` 代码块中直接使用 `ToolGuardrails`、`HumanInTheLoop`、`Summarization` 等类名，但缺少对应的 import 语句。如果按原样实现，会触发 `NameError`。
- **修复**: 在 `from loguru import logger` 之后添加从子模块的显式导入（不从 `__init__.py` 导入以避免循环依赖——`__init__.py` 在末尾 re-export scaffolding，此时所有中间件类已定义完毕）。
- **位置**: `DEEPAGENTS_BORROWING_PLAN.md` P1-6 scaffolding.py 代码块开头
- **状态**: 已修复

### FIX-P1.2 — 死变量 `missing_classes` / `missing_names`

- **问题**: `validate_required_middleware()` 中计算了 `missing_classes = required_classes - actual_classes` 和 `missing_names = required_names - actual_names`，但这两个变量从未被使用——函数通过遍历 `entries` 重新计算缺失项。死代码误导读者以为这些变量参与了判定。
- **修复**: 移除两个死变量，直接在 `entries` 循环中通过 `cls_present` + `any_name_present` 判定。
- **位置**: `DEEPAGENTS_BORROWING_PLAN.md` P1-6 `validate_required_middleware()` 函数体
- **状态**: 已修复

### FIX-P1.3 — `chain` 硬编码选择 entries

- **问题**: `validate_required_middleware()` 接收 `required_classes` 和 `required_names` 参数，但函数体中用 `chain == "main"` 选择 `_MAIN_REQUIRED` 或 `_SUBAGENT_REQUIRED` 来遍历 entries，完全忽略了传入的参数。调用者无法传入自定义 entries 集合（如未来按功能角色拆分的集合）。
- **修复**: 将函数签名改为接收 `entries: tuple[RequiredMiddlewareEntry, ...]` 参数，调用者传入 `_MAIN_REQUIRED` 或 `_SUBAGENT_REQUIRED`。同步更新 `agent/core.py` 和 `spawn/core.py` 两处调用代码。
- **位置**: `DEEPAGENTS_BORROWING_PLAN.md` P1-6 — scaffolding.py 函数签名 + core.py 调用块 + spawn/core.py 调用块 + `__init__.py` re-export
- **状态**: 已修复

### FIX-P1.4 — 文本 "after create_agent" → "before"

- **问题**: 两处调用说明文本写 "在 `create_agent(...)` 调用**之后**插入"，但实际意图是在调用**之前**校验中间件列表（fail fast，避免昂贵的 agent 构建后才报错）。
- **修复**: 改为 "在 `create_agent(...)` 调用**之前**插入校验"，并在注释中说明 fail fast 理由。
- **位置**: `DEEPAGENTS_BORROWING_PLAN.md` P1-6 — main agent 调用块 + subagent 调用块（共 2 处）
- **状态**: 已修复

### FIX-P1.5 — `test_subclass_satisfies_parent_requirement` docstring 与 test body 矛盾

- **问题**: 测试名为 `test_subclass_satisfies_parent_requirement`，docstring 说 "A subclass of a required middleware satisfies the requirement"，但 test body 断言 `pytest.raises(ScaffoldingViolationError)`——即子类**不满足**要求。
- **修复**: 重命名为 `test_subclass_does_not_satisfy_parent_requirement`，修正 docstring 说明子类因 `type().__name__` 不同而**不满足**要求，除非在 `RequiredMiddlewareEntry.names` 中注册别名。
- **位置**: `DEEPAGENTS_BORROWING_PLAN.md` P1-6 — `TestScaffoldingDriftGuard` 测试类
- **状态**: 已修复

### FIX-P1.6 — 两个完全相同的漂移守卫测试

- **问题**: `test_main_names_cover_all_classes` 和 `test_subagent_names_cover_all_classes` 都调用 `verify_required_names_coverage()`（检查两者的 names coverage），是完全相同的测试——重复且缺乏针对性。
- **修复**: 拆分为 3 个独立测试：
  - `test_main_names_cover_all_classes` — 遍历 `_MAIN_REQUIRED` entries，逐条断言 class name + aliases 都在 `MAIN_REQUIRED_NAMES` 中
  - `test_subagent_names_cover_all_classes` — 同上，针对 `_SUBAGENT_REQUIRED`
  - `test_verify_required_names_coverage_passes` — 验证 `verify_required_names_coverage()` 对当前集合不抛异常
- **位置**: `DEEPAGENTS_BORROWING_PLAN.md` P1-6 — `TestScaffoldingDriftGuard` 测试类
- **状态**: 已修复

---

## Migration 修补项

### FIX-M1 — Researcher AGENTS.md 工具名不匹配

- **问题**: researcher 角色定义中 `tools:` 列表使用了不存在的工具名：`grep`、`glob`、`list`、`bash`、`webfetch`。Sherry 实际工具名为 `read_file`、`terminal`、`web_search` 等，无独立 grep/glob/list/webfetch 工具。
- **修复**: 改为正确工具列表：`read_file`、`terminal`（覆盖 grep/find/ls 等终端命令）、`web_search`。同步修改 Capabilities 文本描述。
- **位置**: `subagent-role-migration.md` Step 1.2 — researcher/AGENTS.md
- **状态**: 已修复

### FIX-M2 — Executor AGENTS.md 引用 main_only 的 taskflow 工具

- **问题**: executor 角色定义中 `tools:` 列表包含 `taskflow_run_task` 和 `taskflow_resume`，但这两个工具的 `metadata["scope"] == "main_only"`，子代理在 `apply_tool_policy()` 中会被无条件移除——即使列入白名单也无法获得。此外 `edit` 应为 `patch_file`，`bash` 应为 `terminal`。
- **修复**: 移除 `taskflow_run_task` 和 `taskflow_resume`（executor 无法直接调度 taskflow 任务，应请求父代理代为调度）；`edit` → `patch_file`；`bash` → `terminal`。Capabilities 文本增加说明 "Cannot dispatch taskflow tasks (main-only)"。
- **位置**: `subagent-role-migration.md` Step 1.2 — executor/AGENTS.md
- **状态**: 已修复

### FIX-M3 — Reviewer AGENTS.md 工具名不匹配

- **问题**: reviewer 角色定义中 `tools:` 列表同样使用了不存在的 `grep`、`glob`、`list`、`bash`。
- **修复**: 改为 `read_file` + `terminal`。同步修改 Capabilities 文本。
- **位置**: `subagent-role-migration.md` Step 1.2 — reviewer/AGENTS.md
- **状态**: 已修复

### FIX-M4 — ORCHESTRATOR + 显式白名单时 spawn/yield 处理错误

- **问题**: Phase 8 工具策略代码中，当 ORCHESTRATOR 使用显式白名单（`role_def.tools is not None`）时，`tool_deny` 已被清空为 `[]`。随后 `if role == ORCHESTRATOR:` 从空的 `tool_deny` 中移除 `sessions_spawn`/`sessions_yield`——这是 no-op，不做任何事。但 `sessions_spawn` 和 `sessions_yield` 并不在 `tool_allow` 中（除非角色定义显式列出），导致 ORCHESTRATOR 无法 spawn 子代理。
- **修复**: 区分两种模式：
  - **显式白名单模式**（`tool_allow` 非空）：将 `sessions_spawn`/`sessions_yield` **添加到 `tool_allow`**
  - **继承模式**（`tool_allow` 为空）：从 `tool_deny` 中移除（保持原逻辑）
- **位置**: `subagent-role-migration.md` Phase 8 工具策略代码块
- **状态**: 已修复

### FIX-M5 — `_parse_verify_verdict` 解析过于脆弱

- **问题**: 使用 `"block" in text_lower` 和 `"pass" in text_lower` 做子串匹配，会在 review notes 中任意出现 "pass" 或 "block" 字样时误判 verdict。例如 reviewer 写 "this code block has a bug on line 5" 会被误判为 block；写 "pass statement on line 12 is correct" 会被误判为 pass（取决于哪个 if 先命中）。此外缺少 verdict 行时默认为 pass（不安全——应默认 block）。
- **修复**: 改用正则 `r"\*\*\s*verdict\s*\*\*\s*[:：]\s*(pass|block)"` 精确匹配 verdict 行；无匹配时默认为 `block`（安全失败）。增加测试用例覆盖 "缺少 verdict 行时默认 block"。
- **位置**: `subagent-role-migration.md` Step 2.4 — `_parse_verify_verdict()` + T2.2 测试用例
- **状态**: 已修复

### FIX-M6 — 缺少下游依赖说明

- **问题**: `subagent-role-migration.md` 未说明 PTC_SUBAGENT_PLAN 和 CODE_INTEL_SUBAGENT_PLAN 对本文件的依赖关系，读者无法判断实施顺序。
- **修复**: 新增 "下游依赖" 章节，列出三个下游计划：
  - **PTC_SUBAGENT_PLAN** — 依赖 Phase 1 全部，使用 EXECUTOR 角色
  - **CODE_INTEL_SUBAGENT_PLAN** — 依赖 Phase 1 全部，使用 RESEARCHER 角色
  - **DEEPAGENTS_BORROWING_PLAN P1-6** — 无硬依赖（可独立实施），但未来可按功能角色拆分 entries
- **位置**: `subagent-role-migration.md` 末尾（实施顺序之后）
- **状态**: 已修复

---

## 审查统计

| 类别                | 问题数 | 已修复 | 待修复 |
| ------------------- | ------ | ------ | ------ |
| P1-6 代码正确性     | 6      | 6      | 0      |
| Migration 逻辑/配置 | 6      | 6      | 0      |
| **合计**            | **12** | **12** | **0**  |

---

## 审查方法

1. 逐行阅读 P1-6 scaffolding.py 代码块，对照实际 Sherry 中间件类名和模块路径
2. 检查 `validate_required_middleware()` 的参数传递链：调用者 → 函数体 → 返回值
3. 逐测试检查 docstring 与 test body 的一致性
4. 逐角色定义检查 AGENTS.md 中 `tools:` 列表与 `agent/tools/__init__.py` 实际工具名的对应
5. 对照 `inherited_tool_policy.py::apply_tool_policy()` 的 deny/allow 逻辑验证 ORCHESTRATOR 白名单代码
6. 分析 `_parse_verify_verdict()` 的输入空间和边界条件
7. 检查文档结构完整性（是否有下游依赖说明）
