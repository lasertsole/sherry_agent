# SWE-bench HARD 运行分析、问题与整改方案

- 日期：2026-10-26
- 范围：Sherry 自带 `evals/swebench`（**host harness**，不是官方 Docker harness）
- 切片：`HARD_INSTANCES = django__django-17051, sympy__sympy-20639`
- 结论：**1 / 2 resolved（50%）**；Django 通过，Sympy 在 90/90 迭代耗尽时**没有产生任何代码修改**。
- 说明：这是 2 个实例的小样本，不能当成 SWE-bench 排行榜成绩；它用于暴露 agent/harness 的具体缺陷。

---

## 1. 运行方式与产物

### 1.1 命令

第一次完整 HARD：

```bash
uv run --no-sync python evals/swebench/suite.py --hard --keep-instances
```

Django 跑完后 harness 在 `revert` 阶段崩溃，Sympy 尚未执行。随后：

1. 修复 `evals/swebench/instances.py::revert` 的 `.sherry` 清理问题；
2. 复用第一次已经生成好的 Sympy checkout + venv，单独补跑：

```bash
uv run --no-sync python /tmp/run_sympy_recovery.py
```

Sympy 的第二次完整 materialize 曾因 PyPI `setuptools` 解析超时失败（不是模型失败），所以改用已有 venv。

### 1.2 原始日志与数据

| 内容 | 路径 |
|---|---|
| Django 运行日志（含崩溃堆栈） | `evals/results/swebench/manual_hard_20261026/django-run.log` |
| 证据归档目录（日志 / diff / DB / 报告） | `evals/results/swebench/manual_hard_20261026/` |
| Django agent diff | `evals/results/swebench/manual_hard_20261026/django-diff.patch` |
| Sympy 补跑日志 | `evals/results/swebench/manual_hard_20261026/sympy-run.log` |
| Sympy 报告 | `evals/results/swebench/manual_hard_20261026/sympy-report.json` |
| 汇总数据（本次手工整理） | `evals/results/swebench/manual_hard_20261026/summary.json` |
| token/cost 状态库 | `manual_hard_20261026/django-state.db`、`sympy-state.db` |
| 逐消息 token | `manual_hard_20261026/django-messages.db`、`sympy-messages.db` |

### 1.3 环境

- 宿主机：Ubuntu 24.04 aarch64（PRoot 环境），8 vCPU，15 GiB
- `uv` Python 3.11 用于实例 venv；主 agent 走仓库 `.venv`（Python 3.13）
- 模型：`deepseek-flash`，thinking=off
- 实例 Python：3.11
- `terminal` 沙箱后端不可用，日志有 `sandbox requested but no backend available (policy=auto) — degrading to unsandboxed shell execution`

---

## 2. 结果与分数

| 实例 | 结果 | F2P | P2P | 修改文件 | 迭代 | 工具调用 | 回合时长 |
|---|---|---:|---:|---|---:|---:|---:|
| `django__django-17051` | **resolved**（手工补判） | 5/5 | 37/37 | 2 | 90/90 | 47 | 102.9 s |
| `sympy__sympy-20639` | **unresolved** | 0/2 | 139/139 | 0 | 90/90 | 49 | 110.6 s |

Django 的 harness 在 `revert` 阶段崩溃，原始 `eval_report.json` 没有生成。补救方式是使用已保存的 agent diff：

1. 清理实例仓库到 pinned base；
2. `git apply` agent diff；
3. `git apply` 实例 test patch；
4. 用实例自己的 venv 跑该实例的 `FAIL_TO_PASS` / `PASS_TO_PASS`。

结果：FAIL_TO_PASS `5/5`，PASS_TO_PASS `37/37`，判定 resolved=true。

Django 的补丁内容集中在：

- `django/db/models/query.py`：`bulk_create(on_conflict=UPDATE)` 继续走 returning 路径；
- `django/db/models/sql/compiler.py`：修正 returning 与 on-conflict 的兼容条件。

---

## 3. Token 与成本

| 实例 | input | output | cache_read | 估算 cost |
|---|---:|---:|---:|---:|
| Django | 1,240,909 | 6,023 | 1,205,248 | $0.629485 |
| Sympy | 1,182,879 | 6,379 | 1,156,352 | $0.601008 |
| **合计** | **2,423,788** | **12,402** | **2,361,600** | **$1.230493** |

- 总 token：**2,436,190**
- 缓存读取占 input 的 **97.4%**（2,361,600 / 2,423,788）
- 两个实例都只有约 6k 输出 token；成本主要来自 agent 每步携带的长工具结果和上下文。
- Django 的最终 AI 文本是预算耗尽提示，不是补丁总结；Sympy 同样如此。两者的 `reply` 字段还只捕获了首条 streamed text（见 §4.4）。

---

## 4. 发现的问题

### 4.1 P0：`.sherry` 快照让 harness 的 revert 崩溃

**现象**：Django agent 在实例仓库里创建了 `.sherry/shadow-git/objects/...`。`revert()` 执行 `git clean -qfd` 时，对部分对象报：

```text
warning: 不能对 .sherry/shadow-git/objects/... 调用 lstat
: 不允许的操作
RuntimeError: git failed (1)
```

整个 HARD run 因此退出；已完成的 Django 分数、token、报告都没有落盘。

**根因**：harness 与 agent 共享同一个 worktree，而 turn 返回只代表主图结束，不保证 detached 子代理、后台终端或被取消/超时的工具线程已经停止；`.sherry` 是 untracked 目录，`git clean` 会直接遍历它，任一并发写者都可能让 `lstat`/清理失败。shadow-git 快照本身在写工具线程内同步执行，跨 turn 存活的是上述后台写者。`revert` 没有隔离、没有 quiesce，也没有排除 `.sherry`。

**当前修复**（已落到代码并提交，`3651545f`）：

- `revert()` 先删除 `.sherry`（最多 3 次、每次 200ms），再 `git checkout -- .`；
- `git clean -qfd -e .sherry` 排除残留；
- 新增单测 `tests/evals/test_swebench_instances.py::test_revert_drops_the_shadow_snapshot_and_excludes_it_from_git_clean`。

> 竞争问题的完整拆解与 quiesce 方案已落地（`evals/swebench/teardown.py`、`shadow_git.freeze` / `wait_for_idle`）；执行计划文件已删除。

**仍需补强**：

- `_run_instances` 对单个 instance 做 `try/except`，把异常写成 `harness_error` 结果并继续下一个实例；
- 每个实例完成后立即写 `instance_result.json`，不要再等整个切片结束；
- 最终报告在 `finally` 里也要落盘。

### 4.2 P0：Sympy 90/90 迭代耗尽，但仍无任何修改

**证据**：

- `changed_paths = []`；
- `diff-sympy__sympy-20639.patch` 为 0 字节；
- 工具分布：`terminal` 44 次、`read_file` 5 次，`patch_file` / `write_file` **0 次**；
- 90 次 iteration 全部花在探索，最终收到：

  `The iteration budget (90) has been exhausted. I must stop here...`

Django 也耗尽 90 次，但在耗尽前已写出补丁并通过。Sympy 的问题不是“测试环境坏了”，而是**探索没有收敛到编辑**。

**根因假设**：

1. 任务上下文很大（Sympy 相关打印器很多），模型用 `terminal` 反复 grep/find，缺少“先形成假设、再最小编辑”的强约束；
2. prompt 只要求探索与修复，没有“到达预算前必须产出最小 patch”的硬门；
3. 没有 no-edit / no-progress 检测，也没有在预算中段提示“停止搜索，开始修改”；
4. 90 次 budget 对 HARD 实例偏紧，尤其当工具输出很长时。

**建议**：

- 在 SWE-bench arm 增加 **no-edit gate**：例如完成 N 次工具调用（N≈20）仍无写工具调用时，注入“停止广泛搜索，选择一个最小修复并编辑”的 directive；到预算 70% 仍无 diff，强制要求先写 patch 再继续验证。
- 对 HARD 切片使用独立的主 agent 预算（如 120–160），或实现“有进展则小幅续期、无进展则停止”的 budget 策略；executor 的 90 不适用于这个 main-agent 路径。
- prompt/工具策略：先读目标测试文件与该文件相关的 printer，再限定搜索范围；禁止把 `find /`、全仓库 grep 当成默认动作。
- 在 harness 层把 `changed_paths == []` 标记为 `no_edit` 失败类型，单列统计，而不是普通 unresolved。

### 4.3 P1：工具结果被上下文裁剪替换为 “tool result missing after context trim.”

Sympy 消息序列中出现两次：

```text
tool terminal status=error -> "tool result missing after context trim."
```

这不是工具本身失败，而是上下文裁剪后原始结果不可见。模型因此丢失了刚拿到的搜索结果，只能重新搜索，进一步消耗预算。

> 复核（2026-10-28，归档 `sympy-messages.db`）：这两条占位符其实是**并行 grep 的空输出**（`simplify.py` 没有 `def fraction`；`printer.py`/`codeprinter.py` 没有 `_print_Pow`），不是 compaction（`compacted=0` / `compaction_checkpoint_id=None`）。空的 ToolMessage 被配对 sanitizer 丢掉，再补成 missing。现在的三类处理：空结果保留为 `(no output)`（`sherry_repair=empty_result`）；驱逐结果保持 `[evicted to: <path>]` + `read_file` 提示；真正缺失的结果改为 `tool result missing (not recorded; re-run the tool if you still need it).`（`sherry_repair=missing_tool_result`，`status=success`），不再谎称 context trim。SWE 报告新增 `tool_result_stats`，分别统计 empty / evicted / missing / deduped / pruned / ttl / sliced。

**建议**：

- 区分“工具真实错误”和“裁剪占位符”，后者不要写成 `tool_status=error`；
- 被裁剪的超大结果保留首尾摘要/引用（或让其可回读），避免模型重复检索；
- 对该占位符的出现次数做指标，纳入 eval 报告。

### 4.4 P1：报告可观测性不足

- `eval_report.json` 的 `reply` 只保存首条 streamed text（Django/Sympy 都显示 “I'll start by exploring…”），不是最终回答；
- 崩溃时没有逐实例结果，Django 的分数/token 只能从日志与 DB 手工恢复；
- 聚合只有 token（本次已补），没有 cost；单实例 token 有 input/output，但没有 cache_read；
- materialize 过去只打印“开始”，没有 fetch/checkout/venv/install 阶段（本次已补日志）。

**建议**：

- `reply` 改为从最终 assistant message（state/checkpoint）读取 `final_text`；
- 报告增加 `final_text`、`tokens.cache_read`、`cost_usd`、`harness_error`；
- 每个 instance 完成即写结果；失败也写；
- 运行入口写 `run_meta.json`（命令、commit、模型、slice、环境）。

### 4.5 P1：路径解析错误

Django 第一次工具调用：

```text
read_file error: Path is not a directory: /django/db/models/query.py
```

模型把仓库内相对路径写成了根路径 `/django/...`。这次它自行纠正了，但在更大任务里会浪费预算。

**建议**：

- `read_file` / `write_file` / `patch_file` 对“看起来像仓库路径但落在根目录外”的输入给出明确提示（“项目根是 …，请使用相对路径”），或在安全范围内尝试 `project_root / lstrip('/')`；
- SWE-bench prompt 明确说明项目根与路径约定。

### 4.6 P1：materialize 网络脆弱，失败即整轮退出

- Django `git fetch` 耗时 **693.6 s**；
- Sympy 第二次 materialize 在 `uv pip install` 时 `setuptools` PyPI 超时，整轮直接失败；
- 第一次 Sympy materialize 成功（86.7s）后复用 venv 才跑通。

**建议**：

- fetch / uv install 增加有界重试与更清晰的阶段日志；
- checkout + venv 按 instance id + base commit 缓存，失败后可复用；
- 网络失败与模型失败分开计入 `invalid_run`，不要丢掉整轮；
- 设一个可配置的 `--instance-cache-dir`。

### 4.7 P2：沙箱告警

日志反复出现：

```text
terminal: sandbox requested but no backend available (policy=auto) — degrading to unsandboxed shell execution
```

这是宿主环境缺少 bwrap，不是代码逻辑错误；但 SWE-bench 的 terminal 实际在 unsandboxed 模式下运行。需要在 eval 报告里记录 `sandbox_backend=none`，避免把结果误读成“已隔离环境”。

### 4.8 P2：预算与模型选择

- 两个实例都是 90/90 耗尽；Django 恰好在耗尽前完成，Sympy 完全没开始编辑；
- 输出 token 只有约 6k，input 2.4M；增加预算会增加成本，但当前成本 $1.23/2 实例，仍远低于因无 patch 而浪费的探索成本；
- 建议在 SWE-bench arm 用更容易遵守“先修后验”的模型/提示组合，并将预算与“是否已产生 diff”绑定。

---

## 5. 整改方案（按优先级）

### P0（下一轮运行前必须完成）

1. **harness 单实例隔离**
   - `_run_instances` 包 `try/except`，失败写 `{"harness_error": ...}`，继续；
   - 每实例结束立即写 `instance_result.json`；
   - `finally` 写最终报告（即使部分实例失败）。
2. **revert 健壮化**（已实现并提交，`3651545f`）
   - `.sherry` 删除 + `git clean -e .sherry` + 单测。
   - 计时重试只是权宜；竞争问题的完整拆解、quiesce、逐实例结果与 diff 保真已落地，执行计划文件已删除。
3. **no-edit gate**
   - 在 SWE-bench arm 统计 `write_file` / `patch_file` 调用与 `git diff`；
   - 到阈值注入“停止搜索并编辑”的 directive；
   - 到预算 70% 仍无 diff，强制进入最小 patch 分支；
   - 报告把无 diff 标成 `no_edit`。
4. **预算**
   - HARD 切片 main agent 预算提高到 120–160，或按进展续期；
   - 保留普通任务 90 不变。

### P1（下一轮迭代）

5. **context trim 占位符**
   - 不把裁剪占位符标为工具错误；
   - 保留摘要/首尾或可回读引用；
   - 统计占位符出现次数。
6. **报告补全**
   - `final_text`、`cache_read`、`cost_usd`、`sandbox_backend`、`harness_error`；
   - 逐实例增量落盘；
   - 运行元数据文件。
7. **materialize 稳定性**
   - fetch/install 重试；
   - 按 instance + commit 缓存 checkout/venv；
   - 网络失败计 invalid，不终止整轮。
8. **路径提示**
   - 文件工具对 `/django/...` 这类输入返回项目根提示或安全重写。

### P2（体验/成本）

9. **模型与 prompt**
   - SWE-bench arm 的 prompt 增加“先找目标测试/相关 printer，再最小编辑”；
   - 在 20 次调用内没有写操作时给出强提示。
10. **成本/缓存指标**
    - 报告加入 cache hit、每实例 cost、总 cost；
    - 对 cache_read 异常低的实例单独标记。

---

## 6. 验收标准

- HARD 2 实例重复运行不再因 `.sherry` 崩溃；
- Django 保持 resolved；
- Sympy 至少产生非空 diff；若仍 unresolved，报告必须给出 `no_edit` / `f2p` 的具体原因；
- 每实例报告含 input/output/cache_read/cost、最终文本、harness_error；
- 单个实例失败不会丢掉其他实例的结果；
- 现有 `tests/evals/test_swebench_instances.py` 与新增 revert 用例通过。

---

## 7. 复现

```bash
# 完整 HARD（需要网络；建议先缓存 checkout/venv）
uv run --no-sync python evals/swebench/suite.py --hard --keep-instances

# 只跑一个实例
uv run --no-sync python evals/swebench/suite.py --hard sympy__sympy-20639 --keep-instances

# 本次 Django 的补判方式：见 /tmp/judge_django.py（复用已保存 diff + test patch）
```

原始日志与汇总数据在 `evals/results/swebench/`（gitignored）；本文件只保留可提交的分析与整改项。

---

## 8. 执行结果（2026-10-27）

- **竞争修复**：shadow store 可移出实例项目（`SHADOW_GIT["store_dir"]` / `SHERRY_SHADOW_GIT_DIR`）；实例 `.git/info/exclude` 排除 `.sherry/`；`evals/swebench/teardown.py::quiesce_session` 在 diff / test patch / pytest / revert 前取消 turn、级联杀子代理、杀后台终端、等待并冻结 shadow-git 写者；每实例即时写 `instance_results/<id>.json`，单实例失败不再中止整轮；`revert` 改为原子 quarantine；`diff` 包含 untracked 新文件（`git add -N` + `git diff --binary HEAD` + `git reset`），并排除 `.sherry`。
- **4.2 暂未应用（已撤回）**：无编辑 repair turn、HARD 150 预算、maintainer prompt 已撤回。结论是“问题不在 main agent turn 太短”，需要重新讨论 4.2 的根因与方案；未加载/未自动路由 researcher 是当前更可疑的方向。
- **其余可观测性/稳定性（4.3–4.6）**：报告补 `final_text`、`cache_read`、`cost_usd`、`sandbox_backend`、`harness_error`、`run_meta.json`；空结果保留为 `(no output)`（`sherry_repair=empty_result`），真正缺失的占位符改为 `tool result missing (not recorded; ...)`（`status=success` + `sherry_repair=missing_tool_result`），驱逐 preview 带 `[evicted to: <path>]` + `read_file` 提示，报告新增 `tool_result_stats`；materialize 增加有界重试与 `--instance-cache-dir` 复用；`/django/...` 这类仓库根风格路径在文件工具内安全重写。
- **验证**：完整三组门禁 PASS（unit 7314 passed / 1 skipped；integration+module+system 1378 passed / 12 skipped；regression 86 passed）；`tests/full/test_main_agent_e2e.py` + `tests/full/test_session_isolation_e2e.py` 共 7 passed；文档 link/parity gates PASS。
