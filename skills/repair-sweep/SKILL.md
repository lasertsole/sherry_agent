---
name: repair-sweep
description: 修复清扫舰队编排协议,orchestrator 构建修复队列并把批次派给一组各自持有隔离 git worktree 的 worker 批量修复,授权清单内嵌派发 prompt,worker 可反向质疑关闭决定
scope: main_only
---

# repair-sweep:修复清扫舰队编排协议

本技能只由主会话使用(orchestrator),编排 worker 舰队批量修复 issue/PR。每个 worker 在自己的隔离 git worktree 内工作,主工作区不被触碰;worker 对关闭决定有异议时可反向质疑(Challenge)。子代理不可见(scope: main_only)。

## 1. 参数

### 1.1 scope(三档)

- `refs`:给定符号/引用清单,从代码引用反查修复点。
- `discovery`:主动扫描失败项与待修 issue(默认档)。
- `queue`:从外部队列文件灌入,如 `.omo/repair-sweep/queue.md`。

### 1.2 batch_size

- 默认 5,上限 20;请求超过 20 时编排者直接拒绝该轮并提示拆批,不产生任何派发。

### 1.3 workers

- 按 scope 默认:refs 8 / discovery 8 / queue 64。
- 受 swarm 容量约束:每 5 workers 一组(max_children_per_group=5,agent/tools/subagent/types/swarm.py:27),组内并发 3(max_concurrent=3,agent/tools/subagent/types/swarm.py:29);queue 档 64 workers 约拆 13 组。

## 2. 流程(六步)

1. **队列构建**:scope 决定队列来源与去重键;refs 按符号/引用清单反查,discovery 主动扫描失败项与待修 issue,queue 读外部队列文件。
2. **批次切片**:按 batch_size 切片;切片内按文件/目录去重,同一文件不进两个 worker;scope=queue 时批次数 = ceil(条目数 / batch_size),切片之间无重叠。
3. **per-worker spawn**:每 worker 一次 sessions_spawn,spawn prompt 内嵌专属 worktree 路径与授权清单(见第 3 章),按 swarm 组派发。
4. **收集**:worker 结束后经 announce 回流,主会话逐一核对回报并更新台账。
5. **质疑处理**:worker 对关闭决定有异议时,经 sessions_send 反向质疑;orchestrator 必须回应,回应内容在台账留痕。
6. **汇总与 worktree 清理**:核对台账、汇总各 worker 分支产出,执行第 4 章的 worktree 生命周期收尾。

## 3. worker prompt 模板(spawn 时填充)

每次 sessions_spawn 按下列模板填充派发 prompt:

```text
你是 repair-sweep worker,负责 item <item-id>。
工作目录(隔离 worktree):../repair-sweep-wt/<item-id>
全程只在该路径内工作,禁止改主工作区。
授权清单(调用即授权,无需逐项确认):
- 直接授权:investigate / fix / commit / push / PR / comment
- 需复核:land 与 close 默认必须回传 orchestrator 复核,复核通过才执行
产出:在隔离 worktree 内创建分支 fix/<item-id> 并提交;回报必含:分支名、变更摘要、自验命令及输出。
对关闭决定有异议:经 sessions_send 向 orchestrator 发起质疑(Challenge)。
```

与 openclaw 的差异:openclaw 调用即全量授权(含 land/close);本移植收紧 land/close 两项为需 orchestrator 复核。理由:land 直接进主干、close 终结 issue,两者不可逆且改变仓库公共状态;sherry 无人工确认链,默认收紧更安全,需全自动时显式打开并自担风险。

## 4. worktree 生命周期(必做步骤)

- sweep 开始:编排者经 terminal 批量执行 `git worktree add ../repair-sweep-wt/<item-id> -b fix/<item-id>`,每 worker 一个专属 worktree。
- sweep 结束:批量 `git worktree remove <path>`(存在未合并产出时先标记保留)后执行 `git worktree prune`。
- 清理验证:`git worktree list` 输出中不再含本 sweep 创建的路径,作为汇总步骤的收尾检查项;清理验证未完成,不得宣布 sweep 结束。

## 5. 兜底

- 失控 worker:用 sessions_kill 终止(agent/tools/subagent/control/kill.py:28-78;cascade 级联 :81-133)。
- 任务偏离:swarm run 不接受 steer(control/steer.py:44-46 拒绝 swarm run),worker 纠偏以 sessions_kill 加重派为准,不使用 steer。

## 6. 台账

- 路径:`.omo/repair-sweep/ledger.md`,由主会话(orchestrator)维护。
- 每 item 记录:item-id、worker、worktree 分支、结果状态、质疑内容与 orchestrator 回应;回应必须留痕于台账。
- land/close 复核结论同样记入台账;复核未通过的任务记为 blocked,不进入 land/close。
