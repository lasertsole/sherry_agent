# 计划：Eval Subagent Completion Backflow 确认与修复

- 日期：2026-11-23
- 状态：**待评审**
- 来源：`evals/sandbox.py:187` 把 `_backflow_shared_memory` 替换成 no-op，禁用了子 agent 完成后的共享内存回写。每个 eval 都禁用。不确定是有意还是遗留。
- 风险等级：**低-中** — 恢复或确认，不改产品侧行为
- 依赖：无
- 可独立执行：✅

---

## 0. 问题

`evals/sandbox.py:187-192`:

```python
agent.middlewares.subagent_completion_drain.core._backflow_shared_memory = lambda *a, **kw: None
```

产品的 `SubagentCompletionDrainMiddleware` 在子 agent 完成后通过 `_backflow_shared_memory` 把完成消息注入主 agent 的共享内存。eval 的 EvalSandbox 把它替换成 no-op。

**可能的原因**（需要确认）：
1. eval 没有 server，共享内存回写依赖 server 侧的 WS/bus 机制，eval 跑不起来 → 有意禁用
2. 历史遗留，eval 早期不需要子 agent 完成回写，后来没恢复 → 意外禁用

**影响**：如果是有意禁用，eval 里子 agent 完成后主 agent 可能看不到完成消息，导致：
- 主 agent 不知道子 agent 已完成，继续等待或重复派发
- TaskFlow 步骤状态不更新
- continuation loop 误判 plan 还有未完成工作

deepSWE 20261102 的记录提到过："压力门在 16 次读后阻断读写，主 agent 看到完成结果却无法修改"——可能是 backflow 被禁用后完成消息走了另一条路径（SteeringQueue），但行为不完全等价。

## 1. 需要先确认

**读 `_backflow_shared_memory` 的实现**，判断它是否依赖 server 侧机制：

| 如果依赖 server | 如果不依赖 server |
|---|---|
| eval 没有 server → no-op 是对的 → 加注释说明原因 | no-op 是遗留 bug → 删掉这行，恢复回写 |

## 2. 改动（取决于确认结果）

### 情况 A：有意禁用（依赖 server）

不改代码，加注释：

```python
# evals/sandbox.py:187
# _backflow_shared_memory depends on the server-side WS/bus for injecting
# completion messages into the main agent's shared memory. The eval harness
# has no server, so this is a no-op. Completion messages reach the main agent
# through the SteeringQueue / drain middleware instead.
agent.middlewares.subagent_completion_drain.core._backflow_shared_memory = lambda *a, **kw: None
```

### 情况 B：意外禁用（不依赖 server）

删除 no-op 行，恢复产品行为：

```python
# evals/sandbox.py:187 — DELETED
# _backflow_shared_memory was no-op'd here, but it does not depend on the
# server. Restoring it so eval subagent completion messages reach the main
# agent the same way they do in production.
```

## 3. 验收

### 情况 A

- 注释说明为什么禁用
- eval 行为不变（no-op 仍在）
- 产品侧不受影响

### 情况 B

- eval 里 `_backflow_shared_memory` 不再被 no-op
- 子 agent 完成后主 agent 能收到完成消息
- 现有 eval 测试通过
- 产品侧不受影响
