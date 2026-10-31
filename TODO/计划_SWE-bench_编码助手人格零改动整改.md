# 计划：SWE-bench「编码助手人格」零改动整改

- 建档日期：2026-10-30
- 状态：**待评审**
- 范围：`evals/swebench` 的 `--persona coding` 臂（用内置「编程助手 / 编码助手」预设的系统提示词跑 HARD 切片）。
- 结论（一句话）：**该人格臂 0/2 resolved、两个实例 `changed_paths=[]`**；不是模型不会写 patch，而是**人格提示词禁止主代理编辑、又没有可用的委托通道**，加上 harness 缺"停止探索、开始编辑"的收敛门。本计划只针对这三层，不改生产主代理的工具面。

## 1. 失败现象（run `20261030_031038`）

命令：

```bash
uv run --no-sync python evals/swebench/suite.py --hard --persona coding --instance-cache-dir /tmp/swebench-instance-cache
```

配置：`persona.mode=coding`、`template_lang=zh`（`workspace/template/zh/AGENTS.md`，SOUL/USER/ROLE 空）、编码助手 agent block、`SANDBOX_POLICY=auto`（无后端）、90 迭代、`deepseek-flash` / thinking off。总耗时 273.8 s。

| 实例 | 判定 | 改动文件 | 迭代 | 工具调用 | 工具分布 | tokens in/out/cache | 成本 |
|---|---|---|---:|---:|---|---|---:|
| django__django-17051 | 未解决（**中性臂下 resolved**） | 0 | 90/90 | 48 | terminal 43 / read_file 4 / search_files 1 | 1,504,620 / 4,688 / 0 | $0.759 |
| sympy__sympy-20639 | 未解决 | 0 | 90/90 | 46 | terminal 44 / read_file 1 / skill_view 1 | 1,397,984 / 5,898 / 0 | $0.708 |

两个实例的末条 AI 文本都是预算耗尽文案；`write_file` / `patch_file` / `sessions_spawn` / `taskflow` **0 次**；subagent registry 无任何 run；AI 可见文本中「子代理 / 委派 / ORCHESTRATOR」出现 **0 次**。

对照（分析文档 §2，2026-10-26 中性臂）：django resolved（F2P 5/5、P2P 37/37、改 2 文件、47 次调用），sympy 0 文件（terminal 44 + read_file 5）。所以这里是**两个独立问题叠加**：

1. **sympy「探索不收敛」在中性人格下就已存在**（4.2 原文现象）；
2. **编码助手人格让 django 也退化为零改动**（新增的回归）。

## 2. 根因（分层，均有本次运行证据）

### 2.1 人格层：提示词禁止主代理编辑，且点名了不存在的工具

- `workspace/template/zh/AGENTS.md` 的「编排者信条」原文：*「你是 ORCHESTRATOR… 你写代码，不编辑产品文件。每一份实现工作都必须委派给派生的子代理」*；
- 同文件把委托工具写成历史名 **`task`**，而本构建的委派工具是 **`sessions_spawn`**（`TOOL_GROUPS` 里没有 `task`）；`taskflow_step` 本身存在，但其段落里的 `delegation` / `subagent_id` 字样属于旧 todo payload，同样与当前工具不符；
- 结果：模型既没有编辑（0 次写工具），也没有发起任何委托（0 次 spawn、0 次 taskflow、文本 0 次提及）——它退化成了 90 轮 `terminal` 探索（grep/sed）。

### 2.2 harness 层：即使委托也产不出 patch

- `evals/sandbox.py::SAFE_TOOL_NAMES = {read_file, search_files, question}`：**评测里的子代理是只读的**（主代理保留写工具，这是本 harness 的隔离选择）；
- 所以人格要求的"委派实现"在这条评测链路上不可能完成；而中性臂里让 django 通过的那条规则——*Make the change yourself: read the files, edit them, then run the tests*——**恰好被编码助手模板替换掉了**（模板反过来禁止）。

### 2.3 收敛层：没有任何"停止探索、开始编辑"的门

- `evals/swebench/suite.py::_drive` 只统计 `tool_start` 次数与文本，**不区分读/写工具**；
- `_failure_kind` 对"回合正常结束但没通过"一律返回 `""`（普通 unresolved），`changed_paths==[]` 没有单独分类，报告里看不出"零改动"；
- 分析文档 §5 的 P0-3（no-edit gate）在 2026-10-27 被撤回、至今未实现；本次两个实例（含中性臂的 sympy）再次证明它是真实缺口。

### 2.4 观测层（次要）

- `cache_read=0`（中性臂 97%）：人格臂的提示词变化 + 会话前缀不同，成本翻倍（$1.467/2 实例）。只是观测项，不参与 4.2 判定；
- 失败原因在报告里只有 `resolved=false, failure_kind=""`，读不出"没写 patch"还是"写了但没修对"。

## 3. 整改方案

### P0（下一轮人格臂重跑前完成）

**P0-1 给人格臂一条可执行的编辑路径（默认方案 A）**

- **A（推荐）**：`--persona coding` 时在 `evals/swebench/suite.py::_append_agents_appendix` 追加一条**评测专用覆盖规则**（只加到 eval workspace 的 AGENTS.md 末尾，不改产品模板）：
  > 本评测环境没有可写的子代理（spawned children are read-only），因此"委派实现"不可用。你必须在主会话直接编辑仓库；`sessions_spawn` 只用于只读调研。
- 报告 `persona` 证据加 `"override": "direct-edit"`，任何人格臂的结果都可审计；
- **B（备选）**：放开 eval 子代理写权限（`SAFE_TOOL_NAMES` 增加 `write_file`/`patch_file`/`terminal`），保留"编排者"语义。代价：评测隔离面变大（子代理可执行任意命令）、需要 child 的 HITL/预算策略，且与"主代理是唯一编辑者"的现有 eval 断言冲突。**默认不做**，除非后续要专门评测"多代理协作"。

**P0-2 修正模板里的工具词汇（产品侧，四语言同步）**

- `workspace/template/{zh,en,ja,ko}/AGENTS.md`：`task` → `sessions_spawn`；`taskflow_step(action="run_task"/"dispatch")` 保留（该工具与 action 真实存在）；删除或改写旧 payload 字样（`delegation="self"/"subagent"`、`subagent_id` 字段——按当前 `todowrite`/`taskflow_step` 的实际入参校准），并补一句：*"工具名以系统提示词中的工具列表为准；没有可写的子代理时由主代理直接实现。"*
- 同步：`workspace/AGENTS.md`（线上人格副本）、四语言模板的 bullet 计数 pin（`tests/.../template` 若有断言）、`client/app/composables/persona-catalog.ts` 的注释不涉及；
- 验收：模板文本 grep 不再出现裸 `task` 工具名；四语言 docs parity 通过。

**P0-3 报告里的 `no_edit` 失败类型（可立即落地）**

- `_run_instance` 已有 `changed_paths`；把 `resolved=false and not changed_paths` 归类为 `failure_kind="no_edit"`（优先于现有 `""`；`timeout`/`provider`/`harness` 仍优先）；
- 报告每个实例加 `wrote_paths`（= `changed_paths`）与 `write_tool_calls`（`write_file`+`patch_file` 次数，从 `drive` 的 tool_start 统计里算）；
- 聚合加 `no_edit_instances` 列表，`wrote_paths` 为空不再和"写了但没修对"混在一起；
- 单测：`tests/evals/test_swebench_suite.py` 增加 no_edit 分类用例（完成但零改动 → `no_edit`；通过 → `""`；超时/Provider 优先级不变）。

### P1（同一轮迭代）

**P1-1 任务模板的收敛规则（成本低、见效快）**

- `_TASK_TEMPLATE` 增加两条硬规则：
  1. *Before your 20th tool call, make a minimal edit to the library source*（先形成假设，再最小编辑）；
  2. *Do not use repo-wide grep/find as the default action; read the failing test, then the file it points at.*
- 与 P0-1 的覆盖规则一起写在同一处（`_append_agents_appendix`），只影响 swebench 臂。

**P1-2 真·no-edit gate（mid-turn 注入，进阶）**

- harness 侧无法把消息插进正在跑的 turn；可行路径是复用产品侧已有的**同 turn 内 steer 通道**（`task_intent`/reminder 注入）或 `sessions_spawn` 的 follow-up turn：
  - 阈值：`drive` 统计到第 20 次工具调用仍无写工具 → 用一次 `auto_turn`/steer 注入"停止搜索，写出最小 patch"的 directive；
  - 到预算 70% 仍无 diff → 只允许"写 patch + 跑目标测试"两步。
- 依赖：产品侧要暴露一个"同 turn 内注入 directive"的 API（当前没有）；在该 API 落地前，P1-1 的提示词规则 + P0-3 的分类先覆盖可观测性。

**P1-3 人格臂对照回归**

- `RESULTS.md` 固定记录三列：`persona`（neutral/coding）、`no_edit` 计数、`wrote_paths`；同一 slice 两臂并排，后续改人格/prompt 时可回归对比；
- 基线不被改写：`baseline.json` 仍只由中性臂更新。

### P2（成本/观测）

- 报告给 `cache_read=0` 的实例打标（`cache_miss: true`），避免把人格臂的成本差误读成能力差；
- 若后续允许"按进展续期"预算（分析文档 §5 第 4 项），只在 `wrote_paths` 非空时续期，避免把预算喂给纯探索。

## 4. 验收标准

- 人格臂重跑（`--persona coding`，HARD）：
  - sympy 产出**非空 diff**；若仍 unresolved，报告必须给出 `no_edit` / `f2p` 的具体原因；
  - django 恢复 resolved（中性臂水平）；
  - 报告含 `persona.mode`、`persona.override`、每实例 `wrote_paths` / `write_tool_calls`、聚合 `no_edit_instances`；
- 中性臂基线不回归（`baseline.json` 的 pass rate 不变）；
- `tests/evals/test_swebench_suite.py` 的 no_edit 分类用例通过；四语言模板 pin 通过；`check_docs_parity.py` / `check_doc_links.py` PASS。

## 5. 不做的事

| 项 | 原因 |
|---|---|
| 把编码助手人格改成"直改" | 人格是产品语义；评测侧用覆盖规则（P0-1 A）而不是改产品 |
| 放开 eval 子代理写权限（默认） | 隔离面变大，且会推翻"主代理是唯一编辑者"的现有 eval 断言（仅作为备选 B） |
| 在 harness 里直接往 running turn 塞消息 | 没有产品侧 API；P1-2 依赖 steer 通道，先做提示词 + 分类 |
| 用高分模型掩盖问题 | 本次失败与模型能力无关（无编辑、无委托），换模型不改变提示词/工具面的错配 |
| 提高 HARD 预算到 150 | 两个实例都把 90 次花在探索上；预算不是约束（分析文档 §8 已撤回该假设） |

## 6. 复现

```bash
# 人格臂（本次失败）
uv run --no-sync python evals/swebench/suite.py --hard --persona coding --instance-cache-dir /tmp/swebench-instance-cache
# 中性臂对照
uv run --no-sync python evals/swebench/suite.py --hard --instance-cache-dir /tmp/swebench-instance-cache
```

产物：`evals/results/swebench/<run_id>/eval_report.json`（含 `persona_mode`、`persona.mode/template_lang`、每实例 `diff-*.patch`）；本次为 `20261030_031038`。
