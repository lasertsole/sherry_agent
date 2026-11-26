# 证据：DeepSeek 真跑 DeepSWE L1（2026-11-25）

- run：`20261125_222727`（`evals/results/deepswe/20261125_222727`）
- 模型：DeepSeek `deepseek-flash`；题集：L1 12 题；总耗时 16248.3 s
- 结果：resolved 4 / provider 中止 4（402）/ 其余 4 题 F2P 未满
- 状态：**原始数据存档**。当时据此写的两份问题记录（`问题_DeepSeek真跑L1_20261125.md`）
  与修复计划已删除——修复已落地、回归用例把当时的数字钉住。

## 内容

| 文件 | 说明 |
|---|---|
| `run.log` | 13 MB 完整 agent + verifier 日志；provider 错误 `grep -c 'Insufficient Balance' = 49` |
| `eval_report.json` | harness 原始报告（56 KB）；`aggregate`、逐题 `orchestration`、`experience_extraction` |
| `patch-<task>.patch` | 12 份 model patch；其中 4 份为 0 字节（provider 中止），`patch-abs-module-cache-flags.patch` 为 8.3 MB（含 11.5 MB 重建二进制 `abs`） |

## 它支撑哪些代码结论

代码注释与回归用例里的 "measured on run 20261125_222727" 指向这里：

- 计划读取跨 session（`evals/deepswe/suite.py::_open_plan_items`）——第 2 题结束时留下 5 条未完成 todo；
- 编排门禁进程级解锁（`evals/external_bench.py::install_orchestration_gate`）——第 2 题 `taskflow_calls=0` 仍跑通 `terminal`，整 run `blocked=1`；
- 报告口径（`_changed_paths` / `_aggregate` / 合规三态 / 抽取三分）——11.5 MB 二进制不在 `changed_paths`、4/8 与 4/12 两套分母；
- provider 终态中止（`_ProviderAbort`）——4 道题在余额耗尽后仍跑完整链路；
- 整次驱动预算（`_next_drive_turn` / `budget_s`）——arktype 9434 s（默认 3600）。

回归用例：`tests/evals/test_deepswe_{plan_scope,gate_scope,report,provider_abort,deadline}_regression.py`。
