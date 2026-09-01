---
name: auto-qa
description: 主会话编排自动修 QA 失败项的波次协议,按 lane 分组派发子代理,以证据台账验收 verified fixes
scope: main_only
---

# auto-qa:自动修 QA 失败项编排协议

本技能只由主会话使用,编排"自动修 QA 失败项" campaign。子代理不可见(scope: main_only)。

## 1. Campaign 目标

- 默认目标:100 个 verified fixes。
- verified fixes 的计数只认证据台账中的合格条目,其他任何来源一律不采信。
- 修复 agent 自称修完不计入:没有台账合格条目支撑的完成声明,一律视为未完成。

## 2. 波次状态机

修复工作以 wave 为单位推进,每个 wave 依次经过四个状态:

```
dispatching -> collecting -> reviewing -> done
```

- 状态与派发清单落盘到 .omo/evidence/auto-qa/<wave>/wave-state.md。
- wave-state.md 由主会话维护,子代理不得写入该文件。
- 复盘未完成不得进入下一波:reviewing 状态必须完成台账复盘后,才能切换到 done 并开启新 wave。

## 3. lane 划分

按子系统/目录把失败项划分到 lane(例:auth、api、ui),规则如下:

- lane 与 swarm group 一一对应,每个 lane 配置一个 SwarmGroupConfig。
- 单组 lane 数(即组内子代理数)不超过 max_children_per_group=5(agent/tools/subagent/types/swarm.py:27)。
- 组数 = ceil(N/5),N 为本波失败项 lane 总数(例如 N=12 -> 3 组)。
- 组内并发 max_concurrent=3(agent/tools/subagent/types/swarm.py:29)。
- 超订 spawn 停留在 swarm 队列 RESERVED(swarm/collector.py:86-92),由 FIFO 自然接管,波内不追加派发。

## 4. 派发参数模板

每个 lane 一条 sessions_spawn 样例:

```
sessions_spawn(
  task="修复 lane=<lane> 的 QA 失败项:<fix-id 列表>;遵循 references/roles.md 的可写修复 agent 协议,修复后执行自验命令并回填台账",
  task_name="autoqa-w<n>-<lane>",
  mode="run",
  cleanup="keep",
)
```

- task_name 命名为 autoqa-w<n>-<lane>,n 为波次号。
- mode="run":一次性执行,完成后结果经 announce 管线回传主会话。
- cleanup="keep":保留子代理会话与上下文,以便复盘时追查。

## 5. 证据台账

路径:.omo/evidence/auto-qa/<wave>/<fix-id>.md,每个修复项一个文件,五必填字段:

| 字段 | 含义 |
| --- | --- |
| PID | live 进程探查结果,证明修复动作发生在真实进程上下文 |
| baseline SHA | frozen 基线 commit,证明修复针对冻结基线 |
| 复现命令 | 可重复触发该 QA 失败项的命令 |
| 验证输出 | 自验命令的原始输出,原文贴入 |
| 评审结论 | 评审方(只读评审 agent 或降级路径)给出的结论 |

准入规则:PID 与 baseline SHA 缺一即不计入 verified fixes。

## 6. 容量限流

- 按 max_concurrent=3 的分组派发算法执行:每个 swarm group 内同时在跑的子代理不超过 3,其余 spawn 处于 RESERVED 队列等待 FIFO 调度,不丢弃、不加塞。
- 每波复盘先查台账完整性:核对 wave-state.md 派发清单中的每个 fix-id 是否都有台账文件、五必填字段是否齐全,核对通过后再统计 verified fixes。

## 7. 显式前置依赖

前置依赖:sessions_spawn 工具 schema 的 tool_allow/tool_deny 扩参(SUBAGENT_PORT_PLAN 2.2 项)尚未落地。

在扩参落地前,只读评审走降级路径:评审由主会话代查,即主会话自己以只读方式执行检查命令、分析根因,并填写台账条目的"评审结论"字段。此时 references/roles.md 的只读评审 agent 模板暂不派发,仅作为扩参落地后的目标协议保留。

扩参落地后,评审改为派发只读评审 agent(带 tool_deny 清单),按 references/roles.md 执行。
