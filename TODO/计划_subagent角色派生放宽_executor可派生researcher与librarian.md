# 计划：放宽 subagent 角色派生 —— executor 可派生 researcher 与 librarian

> 状态：方案设计 / 待评审
> 创建：2026-10-28
> 范围：subagent 角色遏制（capability containment）规则放宽，仅针对 executor 角色

---

## 1. 背景与目标

### 现状

当前 subagent 的角色遏制规则是**严格同角色派生**：一个专用子代理（researcher / executor /
reviewer / librarian）只能派生**自己同角色**的子代理。MAIN（编排根）与 general（inherit = 全工具）
不受限制。

- 核心实现：`agent/tools/subagent/spawn/core.py::_check_role_containment`（166-196 行）
- 模型侧唯一提示：`agent/tools/subagent/tools/runtime_tools.py` 里 `functional_role` 字段描述中的
  `"A specialized caller may only spawn its own role."`（63 行）

设计初衷（来自 `test_spawn_role_containment.py` 开头 docstring）：子代理的工具集来自**主代理全集按
子代理自己的角色过滤**，若不加遏制，一个只读 researcher 在 ORCHESTRATOR 深度可以雇一个可写的
executor 替它干活——升级路径由此产生。同角色派生（同角色 = 同 allow-list）消除了升级路径。

### 目标

放宽 **executor** 角色的派生限制：executor 除了派生同角色 executor 外，**额外可派生 researcher
与 librarian**。其余专用角色（researcher / reviewer / librarian）仍保持同角色派生不变。

### 动机

executor 在执行写入/运行任务前，常需要先做只读调研：用 researcher 搜本仓库代码、用 librarian
查外部第三方库实现。当前必须回到主代理或 general 子代理中转，割裂了"先调研后执行"的编排链路。
放宽后 executor 可自行派生只读调研子代理，闭环更短。

---

## 2. 安全性分析（为什么 executor → researcher/librarian 是安全的）

角色工具集（来自 `roles/definitions/*/AGENTS.md` frontmatter + 程序化注入）：

| 角色 | 写能力 | 只读工具 | 程序化注入 |
|------|--------|----------|-----------|
| executor | `write_file` / `patch_file` / `python_repl` | `read_file` / `terminal` | ast-grep + `execute_code`(PTC) |
| researcher | 无 | `read_file` / `search_files` / `terminal` / `web_search` | ast-grep + code-intel + 8 LSP |
| librarian | 无 | `read_file` / `search_files` / `terminal` / `web_search` | ast-grep + code-intel + 8 LSP |

**结论：安全。** researcher 与 librarian 都是**纯只读角色**，没有任何写/执行能力。让 executor 派生它们：

- **不产生写能力升级**：子代理的写能力 ≤ 父代理（executor）的写能力。原 containment 哲学（防止
  只读角色借壳获得写能力）不被破坏——这里方向相反，是写角色向下派生只读角色。
- researcher/librarian 相对 executor 多出的工具（`search_files` / `web_search` / code-intel /
  LSP）全部是**只读检索与代码理解工具**，正是 executor 执行前需要的调研能力，属于期望的"横向
  只读扩展"，不是能力升级。
- executor 派生 general 仍然禁止（general = inherit 全工具，是升级）；派生 reviewer 不放开
  （reviewer 是审计角色，语义不匹配 executor 的执行链路，保持拒绝）。

> 与原设计哲学的偏差点：原规则要求"子代理工具集 ⊆ 父代理工具集"。本方案下 researcher/librarian
> 的工具集**不是** executor 的子集（多了 search_files/web_search/code-intel/LSP）。但 containment
> 的本质是遏制**写能力升级**，而非禁止只读信息获取扩展——这个偏差是刻意的、安全的、正是需求所要。

---

## 3. 设计方案：显式白名单映射

采用**显式可派生角色映射**，而非"写角色可派生只读角色"的规则推导。理由：

1. 显式、可测试，与现有 `CODE_INTEL_ROLES` / `PTC_ROLES`（`frozenset` 常量）风格一致；
2. 未来要给别的角色放宽时只改一处，单一来源；
3. 不引入"哪些是只读角色"的隐式判定（隐式判定需要维护，容易漂移）。

### 3.1 新增常量

**文件**：`agent/tools/subagent/types/functional_role.py`

在 `PTC_ROLES` 之后新增：

```python
#: Roles a specialized parent may spawn BESIDES the same-role default.
#: Absent key = same-role only (the legacy rule). MAIN (no run record) and
#: GENERAL (inherit = every tool) are unrestricted and read no entry here —
#: they short-circuit in _check_role_containment before this lookup. An entry
#: MUST list the same role too (same-role fan-out stays explicit), and every
#: permitted child MUST be a read-only or same-write role so no write-capable
#: escalation path opens. spawn/core.py reads this single map so the gate and
#: its documentation cannot drift.
SPAWN_ALLOWED_CHILD_ROLES: dict[FunctionalRole, frozenset[FunctionalRole]] = {
    FunctionalRole.EXECUTOR: frozenset(
        {
            FunctionalRole.EXECUTOR,   # same-role fan-out (explicit)
            FunctionalRole.RESEARCHER, # read-only codebase/web research before writing
            FunctionalRole.LIBRARIAN,  # read-only external codebase lookup
        }
    ),
}
```

> 约束注释：映射键的值**必须**包含同角色（同角色派生显式保留）；允许的子角色**必须**是只读或
> 同写角色，避免开升级路径。这两条由测试钉死。

### 3.2 改写遏制检查

**文件**：`agent/tools/subagent/spawn/core.py`（166-196 行）

`_check_role_containment` 改为读映射：

```python
def _check_role_containment(
    requester_session_key: str,
    child_functional_role: FunctionalRole,
) -> str | None:
    """Reject escalation-capable spawning from a specialized parent.

    MAIN (no run record) and general (``tools: inherit`` = every tool) impose no
    restriction: every specialized role's whitelist is a subset of theirs, so no
    escalation is possible. A specialized parent may spawn its OWN role plus any
    roles listed in SPAWN_ALLOWED_CHILD_ROLES (read-only / same-write roles only,
    so no write-capable escalation path opens). A role absent from the map keeps
    the legacy same-role-only rule.

    Returns the refusal text, or ``None`` when the spawn is allowed.
    """
    from ..registry import get_run_by_child_session_key
    from ..types.functional_role import SPAWN_ALLOWED_CHILD_ROLES

    parent_run = get_run_by_child_session_key(requester_session_key)
    if parent_run is None:
        return None  # MAIN — the orchestrating root is not contained
    parent_role = getattr(parent_run, "functional_role", FunctionalRole.GENERAL)
    if parent_role == FunctionalRole.GENERAL:
        return None  # inherit mode: every specialized whitelist is a subset
    allowed = SPAWN_ALLOWED_CHILD_ROLES.get(
        parent_role, frozenset({parent_role})
    )
    if child_functional_role in allowed:
        return None
    allowed_names = " / ".join(r.value for r in allowed)
    return (
        f"Capability containment: a {parent_role} subagent can only spawn "
        f"these roles: {allowed_names} (requested {child_functional_role}). "
        f"Pass functional_role=\"<one of the allowed roles>\"; use a general "
        "subagent or the main agent to spawn a different role."
    )
```

要点：
- `allowed_names` 把允许的角色列出来，保持"refusal names the way out"原则（原测试断言这条）。
- 不在映射里的角色（researcher / reviewer / librarian）走 `frozenset({parent_role})` 默认，
  行为与今天逐字节一致。

---

## 4. 涉及修改的文件清单

### 4.1 代码（必改）

| # | 文件 | 改动 |
|---|------|------|
| 1 | `agent/tools/subagent/types/functional_role.py` | 新增 `SPAWN_ALLOWED_CHILD_ROLES` 映射常量 |
| 2 | `agent/tools/subagent/spawn/core.py`（166-196） | `_check_role_containment` 改读映射；更新 docstring 与拒绝消息 |
| 3 | `agent/tools/subagent/tools/runtime_tools.py`（56-65） | 更新 `functional_role` 字段描述（模型侧唯一 containment 提示） |

### 4.2 提示词（详见第 5 节）

| # | 文件 | 改动 |
|---|------|------|
| 4 | `agent/tools/subagent/tools/runtime_tools.py`（functional_role 描述） | 见 5.1 —— 模型侧核心提示 |
| 5 | `agent/tools/subagent/roles/definitions/executor/AGENTS.md` | 见 5.2 —— 角色定义补一句（可选但推荐） |
| 6 | `agent/tools/subagent/tools/sessions_spawn.py`（50-57） | 见 5.3 —— build-time 工具描述对齐（可选） |

> `agent/tools/subagent/spawn/system_prompt.py` **不需要改**：它只注入角色身份 + 角色 body，
> 不承载 containment 规则。executor 能派生谁的信息由工具描述（5.1）+ 角色 body（5.2）覆盖。

### 4.3 测试（必改）

| # | 文件 | 改动 |
|---|------|------|
| 7 | `tests/agent/tools/subagent/test_spawn_role_containment.py` | 见第 6 节 —— 矩阵更新 + 新增允许用例 + 拒绝消息断言更新 |

### 4.4 文档（四语言 parity，必改）

| # | 文件 | 位置 |
|---|------|------|
| 8 | `AGENTS.md`（根） | 220-225 行 ROLE-CONTAINED 段落 |
| 9 | `agent/tools/subagent/README.md`（EN） | 17-24、89-90、505-511、523 行 |
| 10 | `agent/tools/subagent/README.zh.md` | 13、78、507 行附近 |
| 11 | `agent/tools/subagent/README.ja.md` | 13、79、513 行附近 |
| 12 | `agent/tools/subagent/README.ko.md` | 13、509 行附近 |
| 13 | `wiki/subagent/README.md`（EN） | 角色表 64-72 行 + containment 说明 |
| 14 | `wiki/subagent/README.zh.md` / `.ja.md` / `.ko.md` | 同步 |

> 跑 `scripts/check_docs_parity.py` 与 `scripts/check_doc_links.py` 确认四语言 parity 无破。

---

## 5. 提示词修改明细

### 5.1 runtime_tools.py —— `functional_role` 字段描述（模型侧核心，必改）

**当前**（`runtime_tools.py` 56-65 行）：

```python
functional_role: Annotated[
    str | None,
    Field(
        description="Functional specialization of the child: general (full "
        "access), researcher (read-only search), executor (write + run), "
        "reviewer (read-only audit), librarian (read-only external-codebase "
        "retrieval). Omit for the default. A specialized caller may only "
        "spawn its own role."
    ),
] = None,
```

**改为**：

```python
functional_role: Annotated[
    str | None,
    Field(
        description="Functional specialization of the child: general (full "
        "access), researcher (read-only search), executor (write + run), "
        "reviewer (read-only audit), librarian (read-only external-codebase "
        "retrieval). Omit for the default. A specialized caller may only "
        "spawn its own role, except an executor may also spawn researcher "
        "and librarian children for read-only research before writing."
    ),
] = None,
```

> 这是模型在调用 `sessions_spawn` 时唯一能看到 containment 规则的地方。原句
> "A specialized caller may only spawn its own role." 被测试
> `TestRuntimeToolExposesTheRole::test_the_schema_lists_the_roles` 用
> `assert "own role" in description` 钉住——"own role" 保留在新句中，断言仍过。

### 5.2 executor 角色定义 AGENTS.md —— 补一句派生提示（推荐）

**文件**：`agent/tools/subagent/roles/definitions/executor/AGENTS.md`

当前 body 末尾是 `## Output` 段。在 `## When NOT to use` 与 `## Output` 之间加一段：

```markdown
## Spawning read-only research children

When your task needs investigation before you write code, you may spawn
**researcher** or **librarian** children (read-only) to do that research for
you, in addition to same-role executor children for parallel execution. You
cannot spawn general, reviewer, or any other role — escalate to the parent
agent for those.
```

> 这段会随角色 body 注入 executor 子代理的系统提示（`system_prompt.py` 的 `## Role Instructions`
> 段），让 executor 知道自己有"先调研后执行"的编排能力。四语言无需翻译（角色定义 body 目前是英文，
> researcher/librarian/reviewer 定义 body 同样英文）。

### 5.3 sessions_spawn.py —— build-time 描述对齐（可选）

**文件**：`agent/tools/subagent/tools/sessions_spawn.py`（50-57 行）

build-time `SessionsSpawnSchema.functional_role` 描述当前**不含** containment 句子（只有角色枚举），
与 runtime twin 本就有差异。为保持双胞胎一致，可同步加一句与 5.1 相同的放宽说明。非必须（build-time
工具不是模型主调用面），但建议对齐以免漂移。

---

## 6. 测试矩阵变更

**文件**：`tests/agent/tools/subagent/test_spawn_role_containment.py`

### 6.1 `TestContainmentMatrix::test_cross_role_spawning_is_refused`

当前参数化列表（66-78 行）中，`(FunctionalRole.EXECUTOR, FunctionalRole.RESEARCHER)` 这条
**移除**——executor → researcher 现在放行。同时**新增**拒绝用例钉死边界：

- 保留 `(EXECUTOR, GENERAL)` —— general 是升级，仍拒绝
- 新增 `(EXECUTOR, REVIEWER)` —— reviewer 不在白名单，拒绝
- 新增 `(EXECUTOR, LIBRARIAN)` 放到**允许**测试里（见 6.2），不放这里
- researcher/reviewer/librarian 的跨角色拒绝全部保留不变

> 注意：原列表没有 `(EXECUTOR, LIBRARIAN)`，现在要显式补到允许侧。

### 6.2 新增 `test_executor_may_spawn_researcher_and_librarian`

```python
@pytest.mark.parametrize(
    "child", [FunctionalRole.RESEARCHER, FunctionalRole.LIBRARIAN]
)
def test_executor_may_spawn_researcher_and_librarian(self, parent_run, child):
    """executor may spawn read-only research children before writing."""
    parent_run["run"] = _ParentRun(FunctionalRole.EXECUTOR)

    assert _check_role_containment(_CALLER, child) is None
```

### 6.3 更新 `test_the_refusal_names_the_way_out`（88-95 行）

拒绝消息格式变了（现在列出允许角色集合）。改用 researcher → executor 仍拒绝，断言改为：

```python
def test_the_refusal_names_the_way_out(self, parent_run):
    parent_run["run"] = _ParentRun(FunctionalRole.RESEARCHER)
    refusal = _check_role_containment(_CALLER, FunctionalRole.EXECUTOR)

    assert refusal is not None
    assert "a researcher subagent can only spawn" in refusal
    assert "researcher" in refusal  # 允许角色列表里有 researcher
    assert "(requested executor)" in refusal
    assert "use a general subagent or the main agent" in refusal
```

### 6.4 `TestContainmentInTheSpawnPipeline`

- `test_a_specialized_parent_gets_a_forbidden_result`（researcher → executor）仍过，断言
  `"Capability containment"` 不变。
- 新增 executor → researcher 走到 lane 的 accepted 用例（镜像
  `test_a_same_role_spawn_passes_the_gate`，把 hint 改 `researcher`、parent 改 executor）。

### 6.5 `TestRuntimeToolExposesTheRole`

- `test_the_schema_lists_the_roles` 的 `assert "own role" in description` **仍过**（5.1 保留了 "own role"）。
- 可加一条 `assert "executor may also spawn researcher" in description` 钉死放宽提示存在。

### 6.6 新增：白名单不变量测试

钉死设计约束，防漂移：

```python
def test_spawn_allowed_map_is_safe(self):
    """每个映射键的允许集必须：含同角色 + 全部是只读或同写角色。"""
    from agent.tools.subagent.types.functional_role import (
        SPAWN_ALLOWED_CHILD_ROLES,
        WRITE_CAPABLE_ROLES,  # 见下，或内联 {EXECUTOR}
    )
    write_roles = {FunctionalRole.EXECUTOR}
    for parent, allowed in SPAWN_ALLOWED_CHILD_ROLES.items():
        assert parent in allowed, "同角色派生必须显式保留"
        for child in allowed:
            # 允许的子角色要么只读，要么等于父角色（同写）
            assert child == parent or child not in write_roles, \
                f"{parent} 不允许派生写角色 {child}"
```

---

## 7. 多层派生的覆盖性（逐跳检查够不够）

**门控是逐跳的，不是祖先链遍历。** `_check_role_containment` 调
`get_run_by_child_session_key(requester_session_key)`（`registry/queries.py:69`），只取 requester
自己的 run record、读其 `functional_role`——每次 spawn 只看"直接父角色 vs 子角色"，不回溯祖先。
registry 里有 `list_descendant_runs`（BFS 后代遍历）但 containment 没用它。

**本方案下多层安全，且不需要改成祖先链遍历。** 推演（`max_depth` 默认 3）：

```
MAIN(0) → executor(1, ORCHESTRATOR) → researcher(2, ORCHESTRATOR) → researcher(3, LEAF, 止)
```

- executor 放出 researcher/librarian（方案允许）；
- researcher/librarian **不在 `SPAWN_ALLOWED_CHILD_ROLES` 键里**，走默认 `frozenset({parent_role})`，
  只能同角色派生——链永远停在只读侧，回不到写角色；
- depth 维度（`capabilities/core.py`：MAIN=0、`depth>=max_depth`=LEAF、中间=ORCHESTRATOR；
  `can_spawn_children` 只放 MAIN/ORCHESTRATOR）卡住链长度，LEAF 结构性不能再派生。

**安全性来自一条单跳不变量，而非多层遍历**：只要"白名单里允许的非同角色 child 必须是只读角色"
（6.6 的 `test_spawn_allowed_map_is_safe` 钉死），加上"只读角色不在白名单键里、默认同角色（不放出
写角色）"，数学上每一跳放出的非同角色都是只读，回不到写角色——**逐跳 = 多层**，无需祖先链遍历，
避免过度工程化。

**逐跳的盲区是环。** 若未来把 researcher 也加进白名单放出 executor（`RESEARCHER: {RESEARCHER, EXECUTOR}`），
则 executor → researcher → executor 每跳单看都合法，逐跳发现不了环，中间已生出写角色。6.6 的不变量
（允许的非同角色必须只读）正好挡住这种扩展——researcher 想放出 executor 会因 executor 是写角色而
断言失败。**守住这条不变量 = 守住多层安全，不需要额外加祖先链遍历或环检测。**

---

## 8. 风险与注意事项

1. **researcher/librarian 比 executor 多 code-intel + LSP 工具**：这是期望的横向只读扩展，
   非 bug。但要在文档里说清"放宽的是只读派生，子代理获得的是只读检索能力，不是写能力"。
2. **general 仍禁止**：executor → general 是升级（general=全工具），白名单不含 general，保持拒绝。
   测试 `(EXECUTOR, GENERAL)` 钉死。
3. **reviewer 不放开**：仅 executor 放宽到 researcher/librarian，reviewer 不在内（审计语义不匹配）。
4. **四语言文档 parity**：subagent 的 README.{md,zh,ja,ko} 与 wiki/subagent/README.* 都要同步，
   跑 `scripts/check_docs_parity.py` 与 `check_doc_links.py`。
5. **AGENTS.md 根说明同步**：220-225 行的 ROLE-CONTAINED 段落是设计权威描述，必须更新，否则
   与代码漂移。
6. **不引入配置开关**：本方案不加 `config/features/` 开关——放宽是确定的行为变更，不是可选特性，
   避免过度工程化（与项目近期 over-engineering audit 取向一致）。
7. **深度角色（depth role）不受影响**：`can_spawn_children`（MAIN/ORCHESTRATOR 可派生，LEAF 不可）
   是独立维度，本方案不动。executor 子代理能否派生仍由其 depth role 决定。

---

## 9. 落地顺序

1. `functional_role.py` 加 `SPAWN_ALLOWED_CHILD_ROLES`
2. `spawn/core.py` 改 `_check_role_containment` + 拒绝消息
3. `runtime_tools.py` 改 `functional_role` 描述（5.1）
4. `executor/AGENTS.md` 补派生提示（5.2）
5. 改测试矩阵（第 6 节），跑 `tests/agent/tools/subagent/test_spawn_role_containment.py` 全绿
6. 四语言文档同步（AGENTS.md 根 + subagent README×4 + wiki×4）
7. 跑 `scripts/check_docs_parity.py` + `check_doc_links.py` + `ruff check .` + `basedpyright agent/`
8. 全量 subagent 测试：`uv run pytest tests/agent/tools/subagent -q -k "not llm_e2e"`
