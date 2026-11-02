# 评测

本目录记录 Agent 如何被**外部**基准测量，以及每个结果能证明什么、不能证明什么。
它刻意与产品文档分开：基准同时测量 harness、模型和 agent，因此分数不是能力百分比。

## 套件

| 套件 | 测量内容 | 入口 |
| :--- | :--- | :--- |
| [SWE-Bench](SWE-Bench/README.md) | 真实仓库 issue，由实例自己的 FAIL_TO_PASS / PASS_TO_PASS 判定 | `evals/swebench/suite.py` |
| 编码能力 | 自建编码任务与内置提示词 arm | `evals/coding_capability/` |
| 外部基准 | 桥接的编码基准与 harness 适配器 | `evals/` |

## 测量规则

- 在选定切片上报告 **N of M**，绝不把百分比说成排行榜名次。
- 区分 **harness** 结果与 **模型** 结果：provider 错误、测试补丁未应用或超时都不是“修复失败”。
- 记录 **arm**（中性提示词、编码模板、delegation-pressure）、模型、git 版本和完整命令。
- 干净 diff 是判定的一部分：`tests/` 下的改动或破坏 PASS_TO_PASS 都不算成功，即使目标测试通过。
- 只覆盖主 session 的 token/cost 数字必须注明；目前并非每份报告都聚合子 session 用量。

## 运行

```bash
# SWE-Bench HARD 切片，通过环境变量选择当前评测 arm：
SHERRY_EVAL_DELEGATION_PRESSURE=1 uv run --no-sync python evals/swebench/suite.py \
  --hard --persona coding --keep-instances \
  --instance-cache-dir /tmp/swebench-instance-cache
```

每次运行写入 `evals/results/swebench/<run_id>/`，包含 `eval_report.json`、各实例
diff 和 `sandbox/`。该目录被 gitignore；本 wiki 记录稳定的结论。

## 结果

- [SWE-Bench](SWE-Bench/README.md) —— 失败历史、no-edit 门复盘、为什么
  read-before-write 许可证没拦住首轮软门 patch 循环，以及 8/16
  `DelegationPressureGate` 的结果。
