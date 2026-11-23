# 讞划：PathGuard 拒绝计数 + 有 step 在飞时 wait-only 指令

- 日期：2026-11-23
- 状态：**待评审**
- 来源：两个 eval 优秀实践合并——①PathGuard 拒绝计数（`evals/external_bench.py:946-1016`）②有 step 在飞时 wait-only 指令（`evals/deepswe/suite.py:180-188`）
- 风险等级：**低** — ①加计数器 ②改 prompt 措辞
- 依赖：无
- 可独立执行：✅

---

## 0. 问题 A：PathGuard 拒绝不可见

agent 跑了 300 次工具调用啥也没产出，用户不知道是 PathGuard 在拦所有写操作。产品有 PathGuard（`agent/middlewares/path_guard/`）但不暴露拒绝计数。

## 0B. 问题 B：有 step 在飞时可能过早宣布完成

当前 PlanContinuationEnforcer 的指令在有 dispatched step 时仍然说"continue working on the next pending task"，可能让 agent 在子 agent 还在跑时宣布完成。eval 的做法是：有 step 在飞时指令**只说**"wait, do not declare completion"，去掉"continue working"框架。

---

## 1. 改动 A：PathGuard 拒绝计数

| # | 文件 | 改动 | 说明 |
|---|------|------|------|
| 1 | `agent/middlewares/path_guard/core.py` | deny 路径加 per-session 计数器 | 记录拒绝次数 + 被拒路径 |
| 2 | `server/service/turn_runner.py` | turn 结束时如果 path_denials > 0，WS 推送 `path_guard_denial` 帧 | 通知前端 |
| 3 | `client/app/composables/use-chat-stream.ts` | 处理 `path_guard_denial` 帧，toast 提示 | UI |

### 计数器

```python
# agent/middlewares/path_guard/core.py
_PATH_DENIALS: dict[str, dict] = {}  # {session_id: {count, paths: set}}

def _record_denial(session_id: str, path: str, reason: str):
    entry = _PATH_DENIALS.setdefault(session_id, {"count": 0, "paths": set()})
    entry["count"] += 1
    entry["paths"].add(path)
```

### WS 帧

```json
{"event": "path_guard_denial", "session_id": "...", "count": 15, "paths": ["src/main.py", "config.py"]}
```

### UI

toast: "PathGuard 拒绝了 15 次写操作（2 个文件）。agent 可能无法修改目标文件。"

---

## 2. 改动 B：wait-only 指令

| # | 文件 | 改动 | 说明 |
|---|------|------|------|
| 1 | `agent/middlewares/todo_continuation/core.py` | 有 dispatched/ready step 时用 wait-only 指令 | 改 prompt |

### 现有

```python
# 有 dispatched step 时，指令仍然说 "continue working on the next pending task"
# + board-actions footer 提示 wait_all
```

### 改后

```python
if any_in_flight:  # 有 dispatched/ready step
    directive = _WAIT_ONLY_PROMPT  # 只说 "call wait_all, do not declare completion"
else:
    directive = _CONTINUATION_PROMPT  # 正常 "continue working"
```

```python
_WAIT_ONLY_PROMPT = """[SYSTEM DIRECTIVE: STEPS IN FLIGHT]

TaskFlow steps are still running. Call `taskflow_step(action='wait_all')` now
and wait for their results. Do NOT declare completion, do NOT start new work
until all dispatched steps have completed and their results are processed.
"""
```

## 3. 验收

### A

- PathGuard 拒绝写操作 → 计数器递增
- turn 结束时 count > 0 → WS 推送 + toast
- 用户能看到"被拒了多少次""哪些文件"

### B

- 有 dispatched step → 指令只说 wait_all，不说 continue working
- 无 dispatched step → 正常 continuation 指令（不变）
- agent 不会在有子 agent 在飞时宣布完成
