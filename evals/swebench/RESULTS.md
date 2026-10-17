# SWE-bench 基线运行记录

本目录的 `baseline.json` 是版本化的基线参照（`--update-baseline` 写入）；
每次运行的原始报告（gitignored）在 `evals/results/swebench/<run_id>/`，
含 `eval_report.json`、各实例的 `diff-*.patch` 与完整判据输出。

---

## 20261017_003529 — HARD 切片 @ 1M 上下文窗口

- 命令：`uv run --no-sync python evals/swebench/suite.py --hard --update-baseline`
- 切片：HARD（2 实例，`instances.py::HARD_INSTANCES`）；数据集 `princeton-nlp/SWE-bench_Lite` test
- 模型：`deepseek/deepseek-flash`（thinking off；auxiliary 同为 deepseek-flash）
- 上下文窗口：`MAIN_LLM_MAX_TOKEN` / `AUXILIARY_LLM_MAX_TOKEN` 均为 1000000
  （为本次测量设置；恢复出厂值即改回 131072）
- harness 自检（GOLD patch 必须能让该实例通过，先于任何模型调用）：两实例均 `true`
- 隔离：eval 工作目录与线上 workspace 分离（`live_sessions_unchanged: true`）
- 总耗时 300.5s；`resolved 0/2`，无 invalid run、无超时、无 provider 报错

| 实例 | 判定 | FAIL_TO_PASS | PASS_TO_PASS | 时长 | 工具调用 | 改动文件 |
|---|---|---|---|---|---|---|
| django__django-17051 | ✗ | 5 个全失败 | 37 全过 | 14.7s | 10（8 执行 + 2 被丢弃） | 无（diff 0 字节） |
| sympy__sympy-20639 | ✗ | `test_issue_6285`、`test_issue_17616` 失败 | 全部通过（PASS_TO_PASS ok） | 75.1s | 24 | `sympy/printing/pretty/pretty.py` |

### django：失败原因是工具调用被静默丢弃（非能力差距）

模型（deepseek-flash）两次输出 `terminal` 调用，其 arguments 不是合法 JSON：

1. `{"commands": cd $(pwd) && pwd && git log --oneline -3 && ls}` —— 命令值没有引号；
2. `{"commands": cd /repo 2>/dev/null || cd .; git log --oneline -3; python -c "import django; print(django.__version__)"}`
   —— 值内还带未转义的内层双引号。

LangChain 的消息聚合把这类调用归入 `invalid_tool_calls`（`tool_calls` 保持为空），
而 `create_agent` 的模型→工具路由只看 `tool_calls`：最后一条 AI 消息因此被当作
「无调用的终答」，图直接结束回合。第一条消息里同时发给 `search_files` 的调用是合法
的，所以那一步正常执行——说明丢弃是逐调用发生的，不是整条消息被拦。

证据链（均可在 `evals/results/swebench/20261017_003529/` 复核）：

- `sandbox/src/checkpoints/sqlite.db`：两条消息都带
  `invalid_tool_calls: [{"type": "invalid_tool_call", "id": "call_00_…", "name": "terminal", "args": "<原始串>", "error": null}]`；
- `sandbox/src/store/mes_memory/mes_memory.db`：最后一条 AI 消息 `tool_calls` 为空，
  正文却是「Now I understand the structure. Let me check the git log …」——模型以为自己还要继续；
- 运行日志：两次 `stream: tool start name=terminal` 之后既无 `tool executed`、也无工具结果帧，
  回合在第二个 terminal 调用被丢弃后 0.6s 内结束（`timeout=false`、`error=""`）。

这不是 harness 截断、不是超时、也不是本仓库的中间件拦下的：运行时循环对
`invalid_tool_calls` 不做任何检查（只有 `pub/func/transcript_repair.py` 在重放/修复
路径上会清空它），所以「调用无效」既没有反馈给模型，也没有进日志，失败几乎不可见。

### sympy：真实能力差距（工具链正常走完）

24 次工具调用，完整走了「复现 → 定位 → `patch_file` ×4 → 跑该仓库 pretty 测试（全过）
→ 总结」，产出 813 字节补丁：`_print_nth_root` 以 `fraction(expt)[1]` 取代
`expt.args[0]`。但判据测试 `test_issue_6285` 与 `test_issue_17616` 仍失败——补丁语义
与上游期望不一致（它把 `pi**(1/E)` 改成 E 次根号，判据要求另一种呈现）。
PASS_TO_PASS 未被破坏。附带发现：模型首轮用 `python_repl` 跑
`subprocess.run(['git','log',…])` 被沙箱拒绝（`ImportError('__import__ not found')`），
它自行改用 `terminal` 继续——恢复行为正常。

### 与上一基线的可比性

上一基线 `20261014_082728` 是 SPIKE 切片（3 个 sympy 实例，2/3 解决，窗口 131072，
auxiliary 为 glm-5.3-flash）。实例集合、窗口与 auxiliary 模型均不同，两份数字不可直接对比；
本次是 HARD 切片在 1M 窗口下的首份记录。运行日志同时确认 1M 已生效
（`usable=984000`，压缩门槛随窗口放大；django 全程估算 <8k，未触发压缩）。

### 修复方向（已实施，见下节）

1. 模型调用后加一道检查：终答带 `invalid_tool_calls` 时不当终答处理——把解析错误作为
   一条消息喂回模型（限定重试次数），要求它重新发出合法 JSON 的调用；
2. 至少把 `invalid_tool_calls` 记进日志与 trajectory（今天它是静默的）；
3. `terminal.commands` 这类「大段自由文本」参数天然容易坏 JSON——重审其参数形状或
   在提示词中强调转义要求，以降低触发率。

以上方向 1、2 已落地为
`agent/middlewares/invalid_tool_call_repair/`（语法修复 + 有界重发 + 日志/trajectory
事件；方向 3 未做）。

---

## 20261017_014328 — HARD 切片复跑（修复后验证）

- 命令：`uv run --no-sync python evals/swebench/suite.py --hard`（**不**更新 baseline）
- 条件与上一轮一致：同两实例、同一 `deepseek/deepseek-flash`、1M 窗口；
  harness 自检 `True / True`
- 结果：**`resolved 1/2`（resolve_rate 0.5）**，无 invalid run、无超时、无 provider 报错

| 实例 | 判定 | FAIL_TO_PASS | PASS_TO_PASS | 时长 | 工具调用 | 改动文件 |
|---|---|---|---|---|---|---|
| django__django-17051 | ✓ **解决** | 全过 | 全过 | 137.6s | 46 | `django/db/models/query.py`、`docs/ref/models/querysets.txt` |
| sympy__sympy-20639 | ✗ | 仍失败 | 全过 | 117.7s | 27 | `sympy/printing/pretty/pretty.py` |

- **因果证据**：本轮日志出现且仅出现一次修复记录——

  `[InvalidToolCallRepair] session=evals-swe-django__django-17051-591edd repaired tool call call_00_cYbcqk30bwY1avvix4ZX6223 (terminal) after a JSON parse failure`

  ——正是上一轮「零改动」那类被丢弃的调用形态（未加引号的 `terminal` 参数）。修复后调用
  执行、回合继续：django 从「14.7s、零改动、5 个判据全败」变为「137.6s、46 次调用、
  两个文件改动、判据全过」。
- sympy 未被修复路径影响（其调用本来就合法）：117.7s / 27 次调用，仍为真实能力差距
  （判据测试未过、PASS_TO_PASS 未破坏）。**断言级原因**（由 test patch / gold patch /
  本轮 diff 三方比对得出）：
  - 判据要求的语义是两件事的**组合**：①根号只用于 `1/Rational` 与 `1/Symbol`——
    `1/π`、`1/γ`、`1/e` 必须叠成幂形式（`test_issue_6285` 的期望被 test patch 从
    「π 为指数的根号」改成了幂形式）；②用根号时指数位必须渲染为根的**次数本身**
    （`7**(1/x_17)` → `x₁₇___`，而不是从 `expt.args[0]` 取出的 `-1`，见 `test_issue_17616`）。
  - 本轮补丁只动了条件：`(e.is_Rational or e.is_Pow)`。实测 `1/pi` 的 `is_Pow` 为
    `True` → `pretty(x**(1/pi))` 仍走根号路径（期望幂形式 → 6285 失败）；
    `7**(1/x_17)` 仍从 `expt.args[0]` 取到 `-1`（17616 末两条断言失败）。
    上一轮（`20261017_003529`）的补丁改的恰恰是取数、没动条件——两轮的缺口正好互补，
    但都没有同时覆盖两件事。
  - 上游修法（gold patch）：`_print_nth_root(base, root)` 改为接收并（在单行性检查后）
    pretty 渲染「根的次数」，非单行时退回 `base**(1/root)`；条件为
    `(e.is_Rational or d.is_Symbol)`。
- `baseline.json` 保持 20261017_003529 的记录（本复跑用于对照）。若要把 1/2 记为新基线，
  重跑时加 `--update-baseline` 即可。

---

## 20261017_025620 — HARD 切片第二次复跑：**2/2 全过**

- 命令：`uv run --no-sync python evals/swebench/suite.py --hard`（不更新 baseline）
- 本轮新增的配置（都在报告里可复核）：`bugfix-contract` 技能进入评估技能面
  （`agent_config.skills`）；评估 workspace 的运行守则附加了环境说明附录
  （persona 摘要覆盖该文件）——声明「terminal 可直连公网（curl 可用）、web_search 无后端、
  checkout 是浅克隆」，并给出「修 bug 先合成契约、网络可达时先查上游是否已有修复」的规则。
- 结果：**`resolved 2/2`（resolve_rate 1.0）**，无 invalid run

| 实例 | 判定 | FAIL_TO_PASS | PASS_TO_PASS | 时长 | 工具调用 |
|---|---|---|---|---|---|
| django__django-17051 | ✓ | 5/5 pass | 37/37 pass | 73.9s | 29 |
| sympy__sympy-20639 | ✓ | 2/2 pass | 139/139 pass | 707.3s | 35 |

（逐测试结果来自本轮新增的 `judge_tests` 结构化字段；`judge_detail` 原文保留。）

- **sympy 的过法：联网查到上游修复，补丁与 gold patch 逐字节一致**。transcript 证据链：
  1. `git log` 只显示一条提交（浅克隆）→ 转向网络；
  2. `curl` GitHub 搜索 API（`total_count: 1`，命中 `sympy/sympy#17616`——本实例 PR
     修复的正主）；
  3. 拉取并阅读该 PR（"I have the exact upstream fix… apply it"），中途还明确识别了
     旧测试 `test_issue_6285` 断言的就是被抱怨的旧行为（"the test that the upstream PR
     also updated"）；
  4. 产出的 diff 与 gold patch 逐字节一致（`_print_nth_root(base, root)` 度数渲染 +
     `(e.is_Rational or d.is_Symbol)` 条件）。
- **哪个杠杆起作用了**：`skill_view` 调用数 = **0** —— `bugfix-contract` 技能**没有被加载**；
  起作用的是环境说明附录里「terminal 有网 + 先查上游修复」这条规则（agent 的 AI 消息
  「Let me check the upstream fix」→「I have the exact upstream fix」）。技能保留（通用方法学，
  留给不联网/无上游可查的场景），但本轮数据不能归因于它。
- **探针读数的一个诚实注脚**：报告里 `environment.web` 为
  `{"http_code": "403", "terminal_egress": false}` —— 这是**跑完后**的探针；agent 在运行期间
  的 GitHub 请求把未认证限流（60 次/小时）耗尽，报告期再探即 403。运行内的可达性由
  transcript 的 200 响应证明，字段本身反映的是"报告时刻的瞬时状态"。
- 对照汇总：`003529`（修复前，0/2）→ `014328`（invalid-call 修复后，1/2）→
  `025620`（技能面 + 网络附录，2/2）。三份报告的 `model` / `persona` / `environment`
  字段记录了各自的环境，可直接对比。


