# 评测

本目录记录 Agent 如何被**外部**基准测量，以及每个结果能证明什么、不能证明什么。
它刻意与产品文档分开：基准同时测量 harness、模型和 agent，因此分数不是能力百分比。

## 套件

| 套件 | 测量内容 | 入口 |
| :--- | :--- | :--- |
| [DeepSWE](../../evals/deepswe/README.md) | 外部编码任务，隐藏测试补丁 + benchmark 自带 grader；`--local` 是无 Docker 的宿主机模式 | `evals/deepswe/suite.py` |
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
# DeepSWE 默认切片，无 Docker 宿主机模式：
uv run --no-sync python evals/deepswe/suite.py --local --timeout 1200
```

每次运行写入 `evals/results/deepswe-local/<run_id>/`，包含 `eval_report.json`
和各任务 diff。该目录被 gitignore；本 wiki 记录稳定的结论。Docker 模式使用
benchmark 镜像，与本地模式不是同一条路径。

## 结果

- [DeepSWE](../../evals/deepswe/README.md) —— 本地模式结果与失败分析在
  `evals/deepswe/RESULTS.md`。
