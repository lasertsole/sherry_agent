# 评测

本目录记录 Agent 如何被**外部**基准测量，以及每个结果能证明什么、不能证明什么。
它刻意与产品文档分开：基准同时测量 harness、模型和 agent，因此分数不是能力百分比。

## 套件

| 套件 | 测量内容 | 入口 |
| :--- | :--- | :--- |
| [DeepSWE](../../evals/deepswe/README.md) | 外部编码任务，隐藏测试补丁 + benchmark 自带 grader；默认路径通过 docker 或 podman 运行基准的任务镜像 | `evals/deepswe/suite.py` |
| [DevBench（SWE-bench Verified）](../../evals/devbench/README.md) | 官方 F2P/P2P 测试清单经镜像内 pytest 判分；agent 编辑宿主侧 `/testbed` 副本 | `evals/devbench/suite.py` |
| [Multi-SWE-bench](../../evals/multi_swe_bench/README.md) | devbench 核心跑字节跳动多语言数据（`--lang` 过滤；镜像来自数据集字段） | `evals/multi_swe_bench/suite.py` |
| [TAU-bench](../../evals/tau_bench/README.md) | 模拟业务 API 包装为 sherry 工具；按记录的调用与输出做通用判分 | `evals/tau_bench/suite.py` |
| [GAIA](../../evals/gaia/README.md) | 通用助手任务，宿主模式、全工具面；官方答案归一化 | `evals/gaia/suite.py` |
| [CyberSecEval 风格](../../evals/cybersec/README.md) | 安全选择题/代码分析，宿主模式，选项字母匹配 | `evals/cybersec/suite.py` |
| [WebArena](../../evals/webarena/README.md) | docker-compose web 栈常驻；保留 browser 工具；URL 型评估器原生判分 | `evals/webarena/suite.py` |
| [VisualWebArena](../../evals/visualwebarena/README.md) | WebArena + 视觉增量（JS-evaluate 打开；报告带 vision-model 证据） | `evals/visualwebarena/suite.py` |
| [OSWorld](../../evals/osworld/README.md) | Ubuntu 任务容器，TERMINAL-FIRST（按计划降级 VNC）；vm_* 状态评估器原生判分 | `evals/osworld/suite.py` |

共享基础设施在 `evals/shared/`：数据集解析、分批+断点续跑、统一报告（含计划的 token / 上下文命中率 / code-intel 汇总）、逐套件容器生命周期（含 compose 工程）、三个追踪器（`token_tracker`、`context_analyzer`、`code_intel_tracker`）。每个套件按契约"数据可选"：缺数据集、缺容器运行时或缺密钥都大声跳过（exit 3），绝不静默通过。
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
# DeepSWE 容器模式（benchmark 任务镜像；docker 或 podman）：
uv run --no-sync python evals/deepswe/suite.py --preflight-only  # 拉镜像 + 构建 verifier + GOLD 自检，不调用模型
SHERRY_EVAL_CONTAINER_BIN=podman uv run --no-sync python evals/deepswe/suite.py --timeout 1800

# DeepSWE 默认切片，无 Docker 宿主机模式：
```

`SHERRY_EVAL_CONTAINER_BIN` 指定引擎（`docker` / `podman`；默认先找 docker，再退回 podman）。
`--preflight-only` 在不消耗模型调用的情况下验证整条容器链路——拉镜像、构建 verifier、GOLD
自检；评分运行仍需要 `MAIN_LLM_API_KEY`。在 podman 上，任务容器与 verifier 用
`--http-proxy=false` 创建（podman 会把宿主代理注入每个容器，死代理会让基准自身的 localhost
测试失败），verifier 用 `--network none` 构建（WSL2 没有 nftables，构建期的 bridge 建不起来）。

每次运行写入 `evals/results/deepswe/<run_id>/`，包含 `eval_report.json` 和各任务 diff。该目录
被 gitignore；本 wiki 记录稳定的结论。

## Harness 强制的东西

DeepSWE 驱动的是真实主 agent，因此 harness 把规则说出来并**核验**，而不是只靠提示词：

- **编排门禁** —— 任何文件改动或派生子代理之前，必须先调过 `todowrite` 并开一个 TaskFlow
  flow（`taskflow_flow` 的 `action="plan"`），否则写操作会收到规则提示。用量按工具计数并
  汇报（`orchestration_compliant`）。
- **验收清单** —— 任务文本里可判定的条款（*must* / *raise* / *reject* / *only* / *charset*
  等规范性关键词）会变成带编号的清单写进提示词，门禁还会把清单追加到每个
  `sessions_spawn(functional_role="reviewer")` 的任务里，让验收按条给 PASS/FAIL，而不是
  “读一遍计划表示同意”。
- **计划续跑** —— 生产环境的自动续跑触发器是 server 的 hook，所以 harness 自己驱动：每轮结束
  读沙箱里的计划注册表，只要有未完成项就发一条有界的 `[PLAN CONTINUATION]` 回合（最多 3 次）。
  若某次续跑对 diff 没有任何改变，循环立即停止（`no_progress_stop`）。
- **报告字段** —— `orchestration`（策略、逐任务计数、门禁计数、`acceptance_checklist`、
  `reviewer_checklist_injections`）、`continuations`、`no_progress_stop`，以及 verifier 判定
  和补丁。

已知缺口：容器模式用 `asyncio.wait_for` 约束每个回合，对进行中的 provider 调用没有
硬杀，可能超过截止时间（已删除的 `--local` 模式的 worker 进程硬杀是修复的参考）。

## 结果

- [DeepSWE](../../evals/deepswe/README.md) —— 运行记录与失败分析在
  `evals/deepswe/RESULTS.md`。
