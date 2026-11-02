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
- HARD 切片：`django__django-17051`、`sympy__sympy-20639`。
- 实例缓存：`--instance-cache-dir /tmp/swebench-instance-cache`。
- Arm：中性提示词、`--persona coding`、delegate-only
  （`SHERRY_EVAL_DELEGATE_ONLY=1`）、delegation pressure
  （`SHERRY_EVAL_DELEGATION_PRESSURE=1`）。
- 报告位于 `evals/results/swebench/<run_id>/eval_report.json`；各实例 diff 在同目录。

## 失败历史（2026-11-01）

| 运行 | Arm | 结果 | 发生了什么 |
| :--- | :--- | :--- | :--- |
| 2026-11-01 000025 | 提示词 + no-edit 门 | 1/2 脏 | Django 新增测试文件；Sympy 726B patch 破坏 P2P |
| 2026-11-01 002305 | delegate-only | 2/2 干净 | 主代理无直接工具；子代理实现并验证 |
| 2026-11-01 143952 | 首版软门（10/20） | 1/2 | 门迫使 spawn，之后主代理反复 patch 并破坏 P2P |
| 2026-11-01 174750 | 软门（8/16，累计） | 2/2 干净 | 主代理在 16 次读后被禁止读写，转交实现 |

## 暴露的问题

1. **旧迭代预算把工具对算了两次。** 工具调用为其 `AIMessage` 扣 1，又为其
   `ToolMessage` 扣 1，所以 90 看起来只有约 45 次工具循环。现在只计模型调用。
2. **`terminal` 是循环监测盲区。** `ToolGuardrails` 只对幂等工具做哈希；
   `terminal` 非幂等，32 次相同命令也不会得到 no-progress 判定。
3. **“信息足够了”没有收敛点。** 模型在定位改动后仍持续验证，或转去搜索上游 PR；
   没有任何机制强制它进入编辑与验证。
4. **评测 harness 没有拒绝测试文件改动。** 干净 diff 是产品契约的一部分，但
   `changed_paths` 只是被记录。测试文件可以通过目标测试，却仍是脏成功。该缺口
   仍然存在；本次成功只是恰好没有改测试。
5. **写不等于验证。** 第一版 no-edit arm 产出了 patch，却没有运行既有测试文件；
   P2P 回归直到判题时才被发现。
6. **子代理结果与 token 难以观察。** 超时子代理的 checkpoint 可能已被清理；主代理
   有时为了确认状态重复 spawn，报告也只统计主 session 的 token。

### 当前解决

1. **已修复。** `IterationBudget` 只按模型调用计 1；工具结果免费。
2. **已缓解。** `DelegationPressureGate` 在 16 次读后本回合阻断 `terminal`/读工具；`ToolGuardrails` 仍不对非幂等 terminal 做哈希，只读命令分类器仍待实现。
3. **已缓解。** 同一门在 16 次后阻断读写并强制 `sessions_spawn`；要求运行目标测试的专门验证门仍未实现。
4. **仍未解决。** harness 记录 `changed_paths`，但不会把 `tests/` 改动判 invalid；软门降低概率，但不是官方守卫。
5. **部分缓解。** 门阻止主代理 spawn 后继续 patch 循环并转交实现；还没有“收尾前测试必须通过”的硬门。
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
从未触发。新门在超过读取阈值后也会阻断 `patch_file`。

## 成功设计：DelegationPressureGate

`agent/middlewares/delegation_pressure/core.py`：

- 在单回合内累计**读/探索**调用。
- 第 **8** 次读时注入一次委派指令。
- 第 **16** 次读后，本回合剩余时间阻断所有读写工具。每次被阻断的调用都返回错误，
  并指引 `sessions_spawn`（只读研究用 researcher，实现用 executor）。
- 浏览器调试工具既不计次也不阻断。
- accepted/pending 的 spawn 成功会解除阻断并重新计数，主 Agent 可以检查/验证，
  之后再次达到 8/16 时门会重新生效；失败 spawn 和写本身不清零，
  `after_agent` 也会在回合边界清零。

它只安装在主 Agent 上；子代理的 middleware builder 不会添加它，`_active` 也会拒绝
`:subagent:`/`:swarm:` 会话。

## 结果

| Arm | Resolved | 时间 | 主 session tokens | 估算成本 |
| :--- | :--- | :--- | :--- | :--- |
| `DelegationPressureGate` 8/16（run 20261101_174750） | 2/2 | 1693.4s | 1,962,778 / 12,673 | $1.000 |
| Delegate-only（run 20261101_145717） | 2/2 | 3469.0s | 2,561,580 / 20,925 | $1.312 |

Django 只改了产品代码与发布文档；Sympy 只改了一个库文件。两个成功 diff 都不含
测试文件。token/cost 只覆盖主 session；子代理用量未聚合，因此两臂对比是时间与
主上下文信号，不是完整账单。

## 复现

```bash
SHERRY_EVAL_DELEGATION_PRESSURE=1 uv run --no-sync python evals/swebench/suite.py \
  --hard --persona coding --keep-instances \
  --instance-cache-dir /tmp/swebench-instance-cache

SHERRY_EVAL_DELEGATE_ONLY=1 uv run --no-sync python evals/swebench/suite.py \
  --hard --persona coding --keep-instances \
  --instance-cache-dir /tmp/swebench-instance-cache
```

## 注意事项

- 两个实例是信号，不是基准排名；在宣称普遍提升前应重复切片。
- host harness 不是官方 Docker harness；Python 和依赖版本与基准镜像不同。
- 干净结果没有关闭测试编辑守卫：harness 仍应把 `changed_paths` 中 `tests/` 下的
  条目判为 invalid。
- 浏览器工具按设计被排除；本次运行不能证明浏览器驱动研究的效果。
