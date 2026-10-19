# 📋 验证证据账本 — 审计轨迹

[English](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> 一个 append-only 的 JSONL 审计轨迹，记录 agent 跑过的每一条验证命令（测试、lint、构建），并在读取时推导每条结果是否仍然新鲜——这样完成门控就能拒绝一个"声称完成但没有通过验证、或验证已过期"的子代理或 TaskFlow。

证据账本在每个完成边界回答一个问题：**"agent 验证了它的成果吗，验证还有效吗？"** 它不存储过期标志——轨迹是 append-only 的，新鲜度在读取时从验证行和文件编辑事件的顺序计算。

**代码来源：** `agent/tools/pub_base/evidence_ledger.py`（存储层）、`agent/tools/pub_base/evidence_recorder.py`（生产者）、`agent/tools/taskflow/evidence_collector.py`（读取/消费层）、`config/features/agent_side/evidence_ledger.py`（分类法）。

---

## 工作原理

```
terminal / python_repl 跑了一条命令
  │
  ├─ 分类：匹配 "pytest" → kind=test，"ruff" → kind=lint，等
  │        （分类法在 EVIDENCE_LEDGER["verify_commands"] 里）
  │        不匹配的命令静默忽略
  │
  ├─ record_verification_evidence()
  │    └─ 追加一行 JSONL：{kind, command, exit_code, status, output_snippet, sandboxed, session_id, timestamp}
  │
  └─ 之后：write_file / patch_file 编辑了一个源文件
       └─ mark_evidence_stale(file_path, session_id)
            └─ 追加一行 JSONL：{event:"stale", file_path, session_id, timestamp}
                 │
                 └─ 读取时：collect_evidence_summary()
                      ├─ 一条 evidence 行是 [stale] 的，当且仅当后面的 stale 事件的 file_path
                      │   是该行 command 的子串
                      ├─ 方向重要：stale 在 evidence 之前 = 新鲜（已重新验证）
                      └─ 返回："  [pass] test: `pytest foo.py`\n  [FAIL] lint: `ruff .` [stale]"
```

---

## 存储

**文件：** `src/data/evidence-ledger.jsonl`（由 `config.path.resolve_evidence_ledger_path` 解析）

| 属性 | 值 |
|------|-----|
| 格式 | JSON Lines（每行一个 JSON 对象） |
| 作用域 | Repo 级（跨所有 session 共享） |
| 可变性 | Append-only — 已有行从不改写或删除 |
| Session 隔离 | 读取时按 `session_id` 字段过滤实现；行携带的是**会话**键（主会话 id，或子代理的 `agent:{agent}:subagent:{uuid}`），绝不是 plan / todo / flow id |
| 多键读取 | `read_for_keys(keys)` 一次遍历文件、按键分组，并保持每个键自己的行序（陈旧性只在同一会话的行序列内推导） |
| 坏行 | 无法解析的行被跳过并记一条警告——一行坏数据绝不能让整个账本读不出来 |
| 写锁 | 追加持共享的 per-path 写锁（`file_write_lock`：进程内锁 + `flock`） |
| 文件缺失 | `read_all()` 返回 `[]` — `data/` 目录在首次追加时创建 |

文件故意保持 repo 级而非 session 级：它是跨 session 共享的 append-only 审计轨迹，那些 session 验证的是同一个计划。

---

## 生产者（记录什么）

### 1. Terminal & Python REPL — 自动记录（always on）

每次 `terminal` 和 `python_repl` 调用在**每条**退出路径（成功、非零退出、超时、取消、异常）上都经过 `record_verification_evidence()`。记录器：

1. 通过 `classify_verification_command()` 分类命令——对 `EVIDENCE_LEDGER["verify_commands"]` 做小写子串匹配：
   ```python
   "verify_commands": {
       "test": ["pytest", "jest", "vitest", "cargo test", "go test", "npm test"],
       "lint": ["ruff", "eslint", "flake8", "pylint", "clippy"],
       "build": ["cargo build", "npm run build", "make", "cmake"],
       "typecheck": ["basedpyright", "mypy", "tsc", "pyright"],
       "format": ["ruff format", "prettier", "black"],
   }
   ```
   不匹配任何模式的命令被静默忽略（不写行）。

2. 从结果字符串解析 `exit_code`（正则匹配 "Exit code N" / 失败前缀）。
3. `status` = exit_code 为 0 时 `"passed"`，否则 `"failed"`。
4. 追加一行，含 `output_snippet`（前 500 字符）和 `sandboxed` 标志。

**Fail-open：** 每个记录器吞掉自己的异常——账本写入永远不中断工具。

### 2. 文件编辑 — stale 事件

`write_file` 和 `patch_file` 用 `@invalidates_evidence(path_arg="file_path", session_arg="session_id")` 装饰。装饰器在 `finally` 块里运行，所以**每种**结果（成功、错误字符串、抛异常）都会追加一行 `{"event": "stale", "file_path": "..."}`。

### 3. 子代理工作区合并 — stale 事件

子代理的隔离工作区合并回父 session 时，`workspace_merge._mark_merged_paths_stale()` 对所有被应用/创建/删除的路径在**父** session id 下调 `mark_evidence_stale_for_paths()`。父 session 对这些文件的先前验证现在失效了。

---

## 行结构

### 验证证据行

```json
{
  "kind": "test",
  "command": "pytest tests/test_foo.py",
  "exit_code": 0,
  "status": "passed",
  "output_snippet": "===== 3 passed in 0.42s =====",
  "sandboxed": true,
  "session_id": "agent:main:session:abc-123",
  "timestamp": "2026-10-15T08:30:00.123456+00:00"
}
```

### Stale 事件行

```json
{
  "event": "stale",
  "file_path": "src/foo.py",
  "session_id": "agent:main:session:abc-123",
  "timestamp": "2026-10-15T08:31:00.654321+00:00"
}
```

---

## 过期推导

过期**在读取侧推导**，从不存为标志。规则：

> 一条 evidence 行是 stale 的，当且仅当一个**后来的** stale 事件（在追加顺序中索引更大）的 `file_path` 是该行的 `command` 字符串的**子串**。

```python
def _is_stale(index, row, stale_events):
    command = str(row.get("command") or "")
    return any(
        stale_index > index
        and str(event.get("file_path") or "") in command
        for stale_index, event in stale_events
    )
```

关键细节：

- **方向重要：** 只有**后来的** stale 事件能使**先前的** evidence 行过期。stale 事件在 evidence 行之前意味着文件被编辑后又重新验证了——验证是新鲜的。
- **子串匹配：** `file_path in command`。故意宽松——`src/foo.py` 的 stale 事件会使 `pytest src/foo.py tests/` 过期。
- **Session 隔离：** `SessionEvidenceLedger.list_records()` 先按 `session_id` 过滤，其他 session 的 stale 事件不会串扰。

---

## 证据摘要格式

`collect_evidence_summary(session_key=None, flow_id=None)` 返回多行字符串（无 evidence 行时返回 `None`）：

```
  [pass] test: `pytest tests/test_foo.py`
  [FAIL] lint: `ruff check .`
  [pass] build: `npm run build` [stale]
  [?] test: `pytest bar.py`
```

每行格式：`  [{icon}] {kind}: \`{command}\`{stale_tag}`

| 组成 | 来源 |
|------|------|
| Icon | status 为 passed 时 `"pass"`，failed 时 `"FAIL"`，未知时 `"?"` |
| Kind | 验证类别：test / lint / build / typecheck / format |
| Command | 原始命令字符串，用反引号包裹 |
| Stale tag | `_is_stale` 返回 True 时追加 `" [stale]"` |

这个字符串就是门控和判断器解析的对象——它们搜索字面子串 `"FAIL"`、`"[stale]"` 和 `"[pass]"`。

---

## 消费者 — 三个门控层级

### 层级 1：per-step（TaskFlow step judge）

`taskflow_resume` 派发带验收标准的 step 时，调 `judge_step_result()` 并传入 `evidence_summary=collect_evidence_summary(session_key=step.child_session_key)`。step judge 的 prompt 包含 `## Verification Evidence` 段，里面有摘要（或 `"(none recorded)"`）。judge 用它决定 PASS / RETRY / BLOCK。

### 层级 2：per-subagent（completion judge + drain gate）

- **Completion judge 跳过：** goal-loop 第一 turn 时，`_first_turn_self_evident(result_text, evidence_summary)` 检查：result 非空 + 至少一个 `[pass]` + 无 `FAIL` + 无 `[stale]` → **跳过 judge 调用**，省一次辅助 LLM 调用。
- **Completion judge prompt：** 未跳过时，`judge_completion()` 在 prompt 里收到 evidence 摘要。
- **Drain gate：** `SubagentCompletionDrainMiddleware`（父 agent 侧 `before_model`）在排空子代理完成消息时检查 evidence。无 evidence 或有 `FAIL` → 注入 `[GATE] Completion blocked: verification evidence missing or failing` 阻塞消息。

### 层级 3：per-flow（taskflow_finish Gate C）

TaskFlow 关流为 DONE 前，Gate C 检查该 flow 的**聚合**证据——它每个已派发步的 `child_session_key` 加上 flow 的 `creator_session_key` 的并集（行按会话键存放，而一个 flow 横跨多个会话；从未派发过步的 flow 没有任何键，因此没有证据，放行）：

| 条件 | 结果 |
|------|------|
| 摘要包含 `"FAIL"` | 拒绝："failing verification evidence" |
| 摘要包含 `"[stale]"` | 拒绝："stale evidence (code changed since last verification), re-run verification commands before finishing" |
| 否则（或无 evidence / collector 出错） | 通过 |

门控顺序：A（DAG 完整性）→ B（无未决 step）→ C（evidence）→ E（目标 judge）。

---

## 设计原则

1. **处处 fail-open。** 每个记录器吞掉自己的异常。每个门控在 collector 出错时返回"通过/未违反"。坏掉的账本永远不会阻塞完成或困住子代理。
2. **Append-only + 读取时推导。** 已有行从不改写。过期在读取时从行的顺序计算。审计轨迹不会被编辑损坏——它只会增长，reader 在读时算新鲜度。
3. **路径引用，不是内容。** 账本记录命令字符串和输出摘要（500 字符），不记录文件内容。它记录*验证了什么*和*是否通过*，不是*文件里有什么*。
4. **Always on，无开关。** 自动记录路径没有 on/off 开关。每条匹配分类法的 terminal/python_repl 调用都写一行。每次文件编辑都写一个 stale 事件。模型无法绕过。
