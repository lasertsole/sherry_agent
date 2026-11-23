# 计划：Eval bind_external_session 补 set_session_yolo

- 日期：2026-11-23
- 状态：**待评审**
- 来源：`evals/external_bench.py:1025-1052` 的 `bind_external_session` 不调 `set_session_yolo`，而 `evals/coding_capability/suite.py:492` 的 `_bind_session` 调了。容器 suite（deepswe/devbench/multi_swe_bench/osworld/webarena）走 `bind_external_session` 路径，可能卡在 HITL 审批提示。
- 风险等级：**低** — 加一行调用
- 依赖：无
- 可独立执行：✅

---

## 0. 问题

两条 session 绑定路径：

| 路径 | 位置 | 调 set_session_yolo？ |
|------|------|:-:|
| `_bind_session`（coding_capability） | `evals/coding_capability/suite.py:492` | ✅ |
| `bind_external_session`（external_bench） | `evals/external_bench.py:1025-1052` | ❌ |

deepswe/devbench/multi_swe_bench/osworld/webarena 等容器 suite 走 `bind_external_session`，不设 YOLO → 如果 agent 触发 HITL 审批（高危操作），eval 卡住等用户审批，无人响应 → 超时或挂死。

## 1. 改动

| # | 文件 | 行 | 改动 | 说明 |
|---|------|----|------|------|
| 1 | `evals/external_bench.py` | 1025-1052 附近 | `bind_external_session` 末尾加 `set_session_yolo(session_id)` | 和 `_bind_session` 对齐 |

### 改后

在 `bind_external_session` 函数末尾（session 绑定完成后）加：

```python
from evals.coding_capability.suite import set_session_yolo
set_session_yolo(session_id)
```

或者把 `set_session_yolo` 提取到 `evals/shared/` 让两个路径都从 shared 导入，避免 evals/coding_capability 被 external_bench 依赖。

### 提取方案（更干净）

| # | 文件 | 改动 |
|---|------|------|
| 1 | `evals/shared/eval_session.py` | **新建**：提取 `set_session_yolo(session_id)` |
| 2 | `evals/coding_capability/suite.py` | import 改为从 shared 导入 |
| 3 | `evals/external_bench.py` | `bind_external_session` 末尾调 `set_session_yolo(session_id)` |

## 2. 验收

- 所有走 `bind_external_session` 的 suite（deepswe/devbench/multi_swe_bench/osworld/webarena）运行时 YOLO 开启
- 走 `_bind_session` 的 suite（coding_capability）行为不变
- agent 触发 HITL 审批时自动放行，不卡住
