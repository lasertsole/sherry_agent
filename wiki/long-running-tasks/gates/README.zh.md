# 🚦 判断门 — 谁决定一个步骤或一个流程算完成

[English](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> [长任务](../README.zh.md)的一部分：从"结果回来了"到"这个步骤/流程算完成"之间的每一道门——调用方声明的结局、Tier 1 schema 门、失败分类器、辅助 LLM 的 StepJudge，以及 `taskflow_finish` 的 A–E——以及它们之间的优先级。

声明不等于事实。子代理说"做完了"，引擎必须判断这句话有没有含义。两个层级、每个边界一次判定：

| 层级 | 问题 | 门 |
| :--- | :--- | :--- |
| **步骤**（`taskflow_resume`） | 这个结果满足调用方要求了吗？ | 声明的 `step_outcome` → Tier 1 schema → 失败分类器 → Tier 2 StepJudge |
| **流程**（`taskflow_finish`） | 这个 DAG 真的完成了吗？ | A DAG 完整性 → B 未决步骤 → C 证据 → E 目标判别 |

**代码来源：** `agent/tools/taskflow/tools/taskflow_resume.py`、`agent/tools/taskflow/tools/taskflow_finish.py`、`agent/tools/taskflow/tools/_retry.py`、`agent/tools/taskflow/step_judge.py`、`agent/tools/taskflow/evidence_collector.py`、`config/features/agent_side/step_judge.py`、`config/features/agent_side/goal_gate.py`。以下每个常量、判定与拒绝文本都逐条对照该代码核验过。

## 优先级（唯一需要记住的规则）

**调用方声明的结局压倒一切门；除此之外没有任何东西会短路别的门。** `step_outcome` 是调用方陈述本次运行如何结束的方式——`failure` 直接把步骤标成 `failed` 并完全跳过判别器，`skipped` 标成 `skipped`（其下游级联 `skipped`），`success` / `partial` 记在结果上而门照常运行。未知取值在任何状态变更之前就被拒绝。以下全部内容适用于调用方没有声明结局的情形。

## 步骤层（`taskflow_resume`）

### 0. 失败检测——这到底算不算失败？

Tier 1 schema 校验**先跑**（见下文），产出 `schema_validated`。该结果是失败分类器消费的两个信号之一——另一个是结果文本：

- **结构。** `schema_validated=false`（Tier 1 的产出）是文本分类看不见的失败，并且在这里就重新派发——**在任何判别调用之前**，所以结构破损永远不会花掉判别器的 token。
- **结果文本。** `classify_failure`（`_retry.py`）是对模式表做小写子串扫描，并预先剔除被否定的措辞（`cannot connect` 不能被读成连接失败）。措辞落在模式表之外的失败不会被捕获——分类器是启发式，不是判决。

带 `retry_policy` 的步骤会把分类出的失败重新派发到一个替换子代理上，而不是就此结算，直到策略预算用尽。`taskflow_wait_all` 无法分类一个没有返回文本的死掉子代理，所以只要预算还剩就一律消耗一份。没有配置策略 = 失败被记录、步骤就此结算（除非调用方另有所述，即 `failed`）。

### Tier 1 — 结构（免费）

当步骤声明了 `response_schema`，结果会被解析（调用方传了 `structured_result` 就用它，否则用结果文本）并按该 schema 校验（`validate_structured_output`）：

- **未通过**记为 `schema_validated=false` 连同错误，并按失败处理、重试类型为 `schema_error`——它永远不花判别调用，且未被重试的未通过会把步骤标成 `failed`。
- **未通过会教会这次重试。** schema 失败在判别之前短路，所以 `judge_feedback` 为空；替换子代理的 task 因此带上校验器自己的消息（`Your previous output failed schema validation: <字段路径, expected/got>.`）作为 `## Previous Attempt Feedback`，同时子代理继承该步骤的整套 spawn 契约（`response_schema` → 它的结构化输出要求、功能角色、模型覆盖、运行超时）——看不到 schema 也看不到失败原因的重试只能靠猜。
- **形状在生成时就已经被约束。** 步骤声明 `response_schema` 时，子代理构建会带上 LangChain 的 `response_format`（原始 schema 解析为 `AutoStrategy`：provider 支持时走原生结构化输出，否则走结构化输出 tool 调用），所以明显的漏项在模型那一层就被拦下，而不是等到这里的校验器。schema 文本仍留在子代理的 system prompt 里作为 prompt 级兜底，而本 Tier 1 校验保留为第二道网——provider 的边角情况、或从不调用结构化输出 tool 的模型，仍会在这里被抓住。`sessions_spawn` 暴露同一个 `output_schema` 字段，工具驱动的子代理也能被同样约束。
- **通过**则存入 `structured_result` + `schema_validated=true`，继续进入 Tier 2。结构本身从不证明内容正确——这正是第二层存在的理由。

### Tier 2 — 语义（辅助 LLM）

步骤上带 `judge_criteria`（或历史字段 `validation_criteria`）时，`judge_step_result`（`step_judge.py`，temperature 0）审阅结果——有已校验的 `structured_result` 就用它，否则用结果文本——并返回三种判定之一：

| 判定 | 效果 |
| :--- | :--- |
| **`pass`** | 步骤标为 `done`，下游解锁 |
| **`retry`** | 通过共享的 `_retry` 缝重新派发该步骤，复用步骤自己的 `retry_count` 预算（`STEP_JUDGE["max_retries"]`，默认 2），并把判别器给出的指引以 `## Previous Attempt Feedback` 追加到替换任务上（`with_judge_feedback`）。预算耗尽则把步骤标成 `blocked` |
| **`block`** | 把步骤标成 `blocked` 并附上判别器的理由（这是留给调用方的决策：修复、取消该分支或重规划） |

会改变行为的细节：

- **证据摘要在场。** 开启 `STEP_JUDGE["evidence_aware"]`（默认开）时，提示词里带着该流程的 `collect_evidence_summary`——子代理记录的验证行及其推导出的陈旧性（见[证据账本](../gates/evidence-ledger/README.zh.md)）。
- **一律 fail-open。** 模型出错或响应无法解析都降级为 `pass`：判别器不可达绝不阻塞进度。代价是明确的——判定是尽力而为，因此"满足验收标准"从不是硬保证。
- **只判已结算的步骤。** 判别器在步骤确实结算时运行（`redispatched_key is None`）；按重试策略重新派发过的步骤保留其标准存储，在最终那次 resume 时才被判定。
- **给 `taskflow_resume` 传 `validation_criteria` 会覆盖**当次调用用的存储值——例如依据子代理实际做了什么来收紧或纠正它。
- **结果文本预算。** `STEP_JUDGE["max_result_chars"]`（8000）限定判别提示词能看到多少。

### 然后是：`done` 意味着"配置的门都过了"

步骤的 `done` 本身从不意味着"活儿是对的"。在**没有**配置任何期望（`response_schema`、`judge_criteria` / `validation_criteria`、`retry_policy`）时，它退化为旧含义——"注入了一个结果"——所以报了错的子代理照样能落到 `done` 并解锁后继。声明一个期望（`response_schema` 免费校验，且在判别调用之前）或上报 `step_outcome="failure"`，闭环才会抓住它：`failed` / `skipped` / `cancelled` 现在是真实的步骤状态，失败永不解锁其下游，而 `taskflow_finish` 拒绝在存在未决步骤时完成。

## 流程层（`taskflow_finish`）

`taskflow_finish` 按顺序用四项检查把守 DONE 转换：

| 门 | 检查 | 拒绝理由 |
| :--- | :--- | :--- |
| **A — DAG 完整性** | 每个步骤都是 `done` 或 `blocked` | `step(s) not done/blocked` |
| **B — 无未决步骤** | 没有步骤是 `failed`、`skipped`、`cancelled` 或 `blocked` | 按 id 列出违规步骤，各自附上记录的理由 |
| **C — 证据** | 流程证据中没有 `FAIL`、没有 `[broken]`、也没有 `[stale]` 行（见[证据账本](../gates/evidence-ledger/README.zh.md)） | `failing verification evidence` / `confirmed regression` / `stale evidence` |
| **E — 目标判别** | 辅助模型判断收集到的结果是否满足流程的 `description` | 拒绝文本会点名 `taskflow_replan` |

每道门都是 **fail-open**：collector 不可读、模型出错或判定无法解析都放行，而不是阻塞完成。Gate E 只在流程有 `description` 且步骤数达到 `GOAL_GATE["min_steps"]`（3）时才运行——一两步的流程已由子级[完成判别器](../../subagent/README.zh.md)覆盖，再对同一份工作给出第二个判定正是那处重叠；`GOAL_GATE["enabled"]` 是它的总开关，`GOAL_GATE["max_result_chars"]`（4000）限定交给它的每个结果。

## 相邻的门（在别的页面，同一个思路）

本页拥有的是 TaskFlow 的门。"声明不等于事实"这条规则在别处同样反复出现，各自记录在它所在的地方：

- **[证据账本](../gates/evidence-ledger/README.zh.md)**——记录的验证轨迹；它的摘要既是 Gate C 的输入，也是 StepJudge 提示词里的证据段。
- **[子代理完成门](../../subagent/README.zh.md)**——子代理级的两层：CompletionJudge 目标循环（DONE / CONTINUE，由 `COMPLETION_JUDGE["goal_max_turns"]` 限界；当子代理的账本已显示一条通过且未陈旧的证据行时跳过首次调用），以及完成 drain 的程序化门（`SubagentCompletionDrainMiddleware`，父侧）。
- **[TodoList 强制层](../todolist/enforcement/README.zh.md)**——计划侧：E1–E7 强制层，含回合结束续跑门及其预算。
- **防循环**——[wiki/loop-prevention](../../loop-prevention/README.zh.md)汇总了各防护的高度矩阵（回合 / 步骤 / 子代理 / 管线）与每层的 fail-open 策略。

## 配置

| 旋钮 | 默认 | 作用 |
| :--- | :--- | :--- |
| `STEP_JUDGE["max_retries"]` | 2 | 判别器可花费的 `retry` 判定次数（即步骤自己的 `retry_count` 预算） |
| `STEP_JUDGE["max_result_chars"]` | 8000 | 判别提示词中的结果文本窗口 |
| `STEP_JUDGE["evidence_aware"]` | True | 是否把证据摘要放进判别提示词 |
| `GOAL_GATE["enabled"]` | True | Gate E 的总开关 |
| `GOAL_GATE["min_steps"]` | 3 | Gate E 生效的最小流程规模 |
| `GOAL_GATE["max_result_chars"]` | 4000 | 目标判别提示词中每个结果的窗口 |

## 测试

`tests/agent/tools/taskflow/` 钉住这一层：`test_validation.py`（标准存储、判定、覆盖、fail-open）、`test_step_judge.py`（解析、判定归一、提示词拼装）、`test_resume_with_judge.py`（resume + 判别：pass / retry / block、重试反馈注入、预算耗尽）、`test_finish_gate.py`（门 A–C）、`test_retry_policy.py`（分类 + 重新派发）、`test_expectation_closure.py`（期望→实际闭环规则）与 `test_chain_smoke.py`（判别 → 重试反馈 → 目标循环 → 完成门，对着 stub 辅助 LLM 的整链冒烟）。
