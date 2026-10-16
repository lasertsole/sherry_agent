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

### 待决策的修复方向（本次未实施）

1. 模型调用后加一道检查：终答带 `invalid_tool_calls` 时不当终答处理——把解析错误作为
   一条消息喂回模型（限定重试次数），要求它重新发出合法 JSON 的调用；
2. 至少把 `invalid_tool_calls` 记进日志与 trajectory（今天它是静默的）；
3. `terminal.commands` 这类「大段自由文本」参数天然容易坏 JSON——重审其参数形状或
   在提示词中强调转义要求，以降低触发率。
