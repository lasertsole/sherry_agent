# SWE-Bench（host harness）

SWE-Bench 通过仓库自己的 host harness 运行，而不是官方 Docker 评测。harness 固定每个
实例的 base commit、创建环境，并用实例自己的 FAIL_TO_PASS / PASS_TO_PASS 判定结果。
它回答的是“这个 agent 在这台机器上是否修好了这两个 HARD 实例”，而不是“排行榜名次是多少”。

## 测量内容

- 通过 `server.service.messages.async_generate` 运行**主 agent**，使用真实中间件链和
  选定的工具面。
- 它派生的**子代理**，包括它们的文件写入，以及 harness 读取 diff 之前的结算。
- **harness**：实例初始化、官方测试补丁和精确测试判定。harness 错误记为 invalid，
  绝不记为修复失败。

## Harness 与切片

- 数据集：`princeton-nlp/SWE-bench_Lite`，test split。
- HARD 切片：`django__django-17051` 与 `django__django-16820`。`sympy__sympy-20639` 已于 2026-11-08 从可运行切片移除：其 test patch 改变了 NumberSymbol 与 Symbol 分母的旧语义，而该选择无法从 issue 和修复前测试推导出来。Sympy 历史运行仍保留在 `evals/swebench/RESULTS.md`。
- 实例缓存：`--instance-cache-dir /tmp/swebench-instance-cache`。
- Arm：中性提示词、`--persona coding`、delegation pressure
  （`SHERRY_EVAL_DELEGATION_PRESSURE=1`）。delegate-only arm 及其中间件已于
  2026-11-02 删除；旧结果行只保留为历史。
- 报告位于 `evals/results/swebench/<run_id>/eval_report.json`；各实例 diff 在同目录。

- 自 2026-11-04 起，任务 prompt 会附带实例的 test patch 和 FAIL_TO_PASS 名称
  （只读）。checkout 会在运行前预先打上 test patch，供 agent 运行目标测试；prompt 现在还给出两组测试的精确命令（Django 为 `python tests/runtests.py <labels>`，其他为 `python -m pytest <node ids>`）。判题时先重置这些文件再重新应用补丁，实例报告记录 `target_tests_exposed` 与 `tests_preapplied`。
- 确定性解码：每个 eval turn 都通过固定 temperature 0 的 `built_agent` 构建。
  `SHERRY_EVAL_MAX_ITERATIONS` 可选地提高主模型调用上限（未设置/0 保持出厂 180）；
  报告记录 `model.temperature` 与 `budget.main_max_iterations`。

## 失败历史（2026-11-01）

| 运行 | Arm | 结果 | 发生了什么 |
| :--- | :--- | :--- | :--- |
| 2026-11-01 000025 | 提示词 + no-edit 门 | 1/2 脏 | Django 新增测试文件；Sympy 726B patch 破坏 P2P |
| 2026-11-01 002305 | delegate-only | 2/2 干净 | 主代理无直接工具；子代理实现并验证 |
| 2026-11-01 143952 | 首版软门（10/20） | 1/2 | 门迫使 spawn，之后主代理反复 patch 并破坏 P2P |
| 2026-11-01 174750 | 软门（8/16，累计） | 2/2 干净 | 主代理在 16 次读后被禁止读写，转交实现 |
| 2026-11-03 010944 | 7/14 建议式（累计） | 0/2 | Django 只改条件仍触发唯一约束错误；Sympy 未产出 diff，90 次调用预算耗尽 |
| 2026-11-03 194320 | 网络桥接 + 签名连击（仅 Sympy） | 0/1 | 有非空 diff，但 `test_pretty_sqrt` P2P 回归；连击仅 WARN，90 次预算耗尽 |
| 2026-11-03 203547 | + 精确签名 BLOCK（仅 Sympy） | 0/1 | 约 20 次时拦截生效，但 agent 改用 `probe1.py`…`probe19.py`（输出相同、参数不同），仍耗尽 90 次 |
| 2026-11-03 205218 | + 结果重复（仅 Sympy） | 0/1 | terminal/python_repl 在 6 次相同结果后被拦截；agent 到 72/90 才 patch，之后无法验证 |
| 2026-11-03 210023 | + patch_file 成功后解除拦截（仅 Sympy） | 0/1 | 28 次调用的干净运行，未触发护栏；P2P 通过，F2P 未命中 NumberSymbol vs Symbol 的隐藏语义（仓库现有 `test_issue_6285` 断言的是旧行为） |
| 2026-11-04 003521 | + 目标测试写进 prompt（HARD 2） | 0/2 | 测试可见但不可运行；两个实例都耗尽 90 次调用 |
| 2026-11-04 004714 | + 目标测试预应用到 checkout（HARD 2） | 1/2 | Django 通过；Sympy 用 `e.is_Symbol`（对 `1/x_17` 判断错误）并在 repro 脚本上循环 |
| 2026-11-04 005721 | 强化失败后提示（仅 Sympy） | 0/1 | 仍是同样的错误条件；F2P 仍为 0 |
| 2026-11-08 111407 | 仅 HARD Django；prompt 列出 F2P+P2P | 1/1 | Django 通过；首跑 0/1 因 P2P `NoneType` 回归，第二次通过 |
| 2026-11-08 133112 | temperature=0 + prompt 列出 F2P/P2P | 隔离失效 | 温度 pin 在 workspace 覆盖之前导入了 `config.path`，运行落在 live workspace；其 1/1 不计入 |
| 2026-11-08 133743 | 修正 pin 顺序后 temperature=0 | 1/1 | Django 通过；17 次调用 / 60.6 秒；F2P/P2P 全通过，无临时文件 |
| 2026-11-08 135604 | temperature=0，累计四次有效重复 | 4/4 | 全部通过；调用量 16～82 次（60.6～286.3 秒），最长一次用掉 90 次预算中的 82 次 |
| 2026-11-08 145011 | 新 HARD 实例 `django__django-16820`，temperature=0 | 0/1 | gold check 通过（5 F2P + 91 P2P）；agent 在 92 次调用时耗尽 90 次预算，F2P 通过、P2P 未通过 |
| 2026-11-08 184144 | 同实例，`SHERRY_EVAL_MAX_ITERATIONS=180` | 1/1 | 24 次调用 / 96.5 秒通过；更大的上限消除了截断风险，但不是修复成功的原因（同温度下轨迹差异很大） |

## 暴露的问题

1. **旧迭代预算把工具对算了两次。** 工具调用为其 `AIMessage` 扣 1，又为其
   `ToolMessage` 扣 1，所以 90 看起来只有约 45 次工具循环。现在只计模型调用。
2. **`terminal` 是循环监测盲区。** `ToolGuardrails` 只对幂等工具做哈希；
   `terminal` 非幂等，32 次相同命令也不会得到 no-progress 判定。
3. **“信息足够了”没有收敛点。** 模型在定位改动后仍持续验证，或转去搜索上游 PR；
   没有任何机制强制它进入编辑与验证。
4. **评测 harness 没有拒绝测试文件改动。** 干净 diff 是产品契约的一部分，但
   `changed_paths` 只是被记录。测试文件可以通过目标测试，却仍是脏成功。该缺口
   已关闭：`dirty_test_edits` 会标出 agent 在 harness 补丁之外改动的测试文件，
   并强制 `resolved=False`，无论测试是否通过。
5. **写不等于验证。** 第一版 no-edit arm 产出了 patch，却没有运行既有测试文件；
   P2P 回归直到判题时才被发现。
6. **子代理结果与 token 难以观察。** 超时子代理的 checkpoint 可能已被清理；主代理
   有时为了确认状态重复 spawn，报告也只统计主 session 的 token。
7. **测量用的 pin 也能破坏沙箱。** 在 `SHERRY_WORKSPACE_DIR` 覆盖之前导入 eval
   接缝，会把 `WORKSPACE_DIR` 冻结到 live persona 目录。该次运行仍报告 1/1，
   但 `eval_workspace_has_sessions=false`、`live_sessions_unchanged=false` 暴露了
   问题；报告的 isolation 块就是阻止这种运行进入能力统计的门。

### 当前解决

1. **已修复。** `IterationBudget` 只按模型调用计 1；工具结果免费。
2. **已修复。** `ToolGuardrails` 增加了不看状态签名的连击检测（工具 + 参数哈希；3 次相同调用，terminal 用自身 metadata 声明的 5 次），每回合最多警告 3 次，之后封锁该精确签名；另有非幂等工具的结果重复检测（3 次警告、6 次连续相同结果封锁工具）。`patch_file` 成功（metadata `clears_guardrail_blocks`）会清空 block、连击和失败计数，让验证可以继续。不再需要只读命令分类器。
3. **由工具护栏缓解，不是硬性收尾门。** 被封锁的签名会迫使 agent 换方法；20261103_210023 的 Sympy 运行在 28 次模型调用内收敛，并运行了邻近测试文件，prompt 现在也要求这样做。独立的“收尾前测试必须通过”门仍不存在。
4. **仍未解决。** harness 记录 `changed_paths`，但不会把 `tests/` 改动判 invalid；软门降低概率，但不是官方守卫。
5. **部分缓解。** escalated 提醒推动主代理停止 spawn 后的 patch 循环并转交实现；还没有“收尾前测试必须通过”的硬门。
6. **部分修复。** 每回合 token 预算现在把主代理 + 所有层级子代理归到根回合；eval 报告仍只统计主 session，子代理 checkpoint/结果回读仍未解决。

## no-edit 门及其失败原因

旧评测门在 15 次工具调用后警告，在迭代预算 70% 后阻断研究；任何写都会重置它。
这带来三个可预期的失败：

- 它按**工具名**（`patch_file` / `write_file`）识别写入，`terminal cp` 或 heredoc
  编辑完全不可见。Django 正是用 `cp` 创建了测试文件。
- 它把**任何**写都视为成功，包括写到 `tests/` 下、或马上被撤回的 patch。
- 第一次写会关闭本回合剩余时间的门，因此 Sympy 第一个错误 patch 之后约 30 次
  terminal 探针和 P2P 回归都没有被约束。

它已被删除；不能作为收敛机制。

## 为什么 read-before-write 许可证没拦住首轮软门循环

`agent/tools/pub_base/read_state.py` 的许可证保护的是 **`write_file` 覆盖**：已存在
的文件只能被读过它的 session 替换，读取 revision 作为 CAS 前置条件。它**不适用于**
`patch_file`：后者用自己的 old_string/new_string、模糊匹配和 stale-write CAS 完成
替换，不要求先读，而且两个工具都不知道 patch 是否正确、测试是否通过。

首版软门（10/20）在任何产品写入后立即解除阻断，且没有把 `patch_file` 纳入目标。
主代理 spawn 了一个 researcher，门复位，随后主代理在 `compiler.py` 上连续约 70 次
`patch_file`。这是**patch 重试病理**，不是未授权的 `write_file` 覆盖，所以许可证
从未触发。早先的 8/16 门也会阻断 `patch_file`，2026-11-01 的运行正是这样收尾的；当前 7/12 建议式设计已刻意移除阻断。

## 当前设计：DelegationPressureGate（7/12 建议式）

2026-11-01 的运行测量的是早先的 8/16 硬阻断版本；下面的 7/12 行为是当前设计；2026-11-03 的 0/2 重跑测量的是 7/14 设置，HARD 切片未通过——详见 `evals/swebench/RESULTS.md`。

`agent/middlewares/delegation_pressure/core.py`：

- 在单回合内累计**读/探索**调用。
- 第 **7** 次读时，通过 gate 自己的 mid-turn `abefore_model` 注入一条 `<system-reminder source="delegation_pressure">` 警告。
- 第 **14** 次读时注入语气更强的 escalated 系统提醒；工具始终可用，门只建议、不阻断。
- 浏览器调试工具不计次。
- accepted/pending 的 spawn 成功会重新计数，主 Agent 可以检查/验证，
  之后再次达到 7/12 时门会重新生效；失败 spawn 和写本身不清零，
  `after_agent` 也会在回合边界清零。

它只安装在主 Agent 上；子代理的 middleware builder 不会添加它，`_active` 也会拒绝
`:subagent:`/`:swarm:` 会话。

## 网络状态桥接

失败形态属于网络的 terminal 命令现在会变成工具错误：`curl` / `wget` / `http` /
`https` / `ssh` / `scp` / `sftp` / `ping` / `dig` / `nslookup` 非零退出，或
`git clone|fetch|pull|push|ls-remote|remote|submodule` 非零退出，都会 raise
`ToolException`。`handle_tool_error` 把它转成 `ToolMessage(status="error")`，
exact-failure 计数因此生效。本地命令（pytest、编译器、脚本）的非零退出仍作为
普通文本返回（`status="success"`），agent 可以读取失败并继续迭代。`web_fetch`
的 HTTP 4xx/5xx 同样 raise。

eval harness 还会在运行前探测 GitHub；探测不到网络时，它会向种子 `AGENTS.md`
追加“不要继续探测网络”的 directive。这消除了 Sympy 的 curl/GitHub 探测循环。
Sympy 剩余的 F2P 失败是 NumberSymbol vs Symbol 的隐藏测试语义，不是网络探测。

## 结果

| Arm | Resolved | 时间 | 主 session tokens | 估算成本 |
| :--- | :--- | :--- | :--- | :--- |
| `DelegationPressureGate` 8/16（run 20261101_174750） | 2/2 | 1693.4s | 1,962,778 / 12,673 | $1.000 |
| `DelegationPressureGate` 7/14 建议式（run 20261103_010944） | 0/2 | 669.7s | 8,370,656 / 31,119 | $4.232 |
| Delegate-only（run 20261101_145717） | 2/2 | 3469.0s | 2,561,580 / 20,925 | $1.312 |

Django 只改了产品代码与发布文档；Sympy 只改了一个库文件。两个成功 diff 都不含
测试文件。token/cost 只覆盖主 session；子代理用量未聚合，因此两臂对比是时间与
主上下文信号，不是完整账单。

## 复现

```bash
SHERRY_EVAL_DELEGATION_PRESSURE=1 uv run --no-sync python evals/swebench/suite.py \
  --hard --persona coding --keep-instances \
  --instance-cache-dir /tmp/swebench-instance-cache
```

## 注意事项

- 两个实例是信号，不是基准排名；在宣称普遍提升前应重复切片。
- host harness 不是官方 Docker harness；Python 和依赖版本与基准镜像不同。
- 干净结果没有关闭测试编辑守卫：harness 仍应把 `changed_paths` 中 `tests/` 下的
  条目判为 invalid。
- 浏览器工具按设计被排除；本次运行不能证明浏览器驱动研究的效果。
