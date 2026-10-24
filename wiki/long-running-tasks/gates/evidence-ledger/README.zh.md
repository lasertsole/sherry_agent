# 📋 验证证据账本 — 审计轨迹

[English](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> 一个 SQLite 审计轨迹，记录 agent 跑过的每一条验证命令（测试、lint、构建），并在读取时推导每条结果是否仍然新鲜——这样完成门控就能拒绝一个"声称完成但没有通过验证、或验证已过期"的子代理或 TaskFlow。

证据账本在每个完成边界回答一个问题：**"agent 验证了它的成果吗，验证还有效吗？"** 它不存储过期标志——事件行不可变，新鲜度在读取时从验证行和文件编辑事件的顺序计算（保留期只删除旧行，不改写历史）。

**代码来源：** `agent/tools/pub_base/evidence_ledger.py`（存储层 + 派生）、`agent/tools/pub_base/evidence_recorder.py`（生产者）、`agent/tools/pub_base/regression_gate.py`（finish 时重验）、`agent/tools/taskflow/evidence_collector.py`（读取/消费层）、`config/features/agent_side/evidence_ledger.py`（分类法）、`config/features/agent_side/evidence_cross_session.py`（跨 session + 回归门开关）。

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
  │    └─ 插入一行 SQLite：{kind, command, exit_code, status, output_snippet, sandboxed, modified_files, session_id, timestamp}
  │
  └─ 之后：write_file / patch_file 编辑了一个源文件
       └─ mark_evidence_stale(file_path, session_id) + record_file_edit()
            └─ 插入一行 SQLite：{event:"stale", file_path, session_id, timestamp}
                 │
                 └─ 读取时：collect_evidence_summary()
                      ├─ 一条 evidence 行是 [stale] 的，当且仅当后面的 stale 事件的 file_path
                      │   是该行 command 的子串
                      ├─ 方向重要：stale 在 evidence 之前 = 新鲜（已重新验证）
                      └─ 返回："  [pass] test: `pytest foo.py`\n  [FAIL] lint: `ruff .` [stale]"
```

**Changed-path 快照（B'）。** `write_file` / `patch_file` 还会调用 `EvidenceLedger.record_file_edit()`，累积该 session 编辑过的路径（去重，上限 `EVIDENCE_LEDGER["changed_paths_limit"]` = 200；**验证后不重置**，因此同一批编辑后的 lint 与 test 共用一份快照）。记录验证命令时，当前列表被复制进 evidence 行的 `modified_files`。之后任何对其中文件的写入都会使该 evidence 过期，即使命令从未点名该文件。

**跨 session 传播。** 文件写入先标记 writer 自己的 session。当 `EVIDENCE_CROSS_SESSION_STALENESS["enabled"]` 为 true 时，`EvidenceLedger.mark_stale_for_path_all_sessions()` 扫描其他 session 中 `modified_files` 包含被编辑路径、command 以真实路径 token 点名该路径，或旧行 command 包含它的 evidence 行，并在每个受影响 session 下追加一行 `scope="cross_session"` 的 stale 事件。`source_session` 记录写入方；`max_sessions_scan` 限制扫描数量，`exclude_self` 跳过已单独标记的 writer。下方的读取侧推导无需改动——广播行就落在受影响 session 自己的序列里。

**Finish 时重验。** `taskflow_finish` 的 Gate C 会先调用 `regression_gate.reverify_flow_evidence()`：把 flow 各 session 里每条 passed 命令去重后重跑一次（allowlist 可执行文件、不走 shell、受 `max_reverify_commands` 与单命令超时约束），工作目录是 flow 的项目根。通过时对曾被 stale/broken 的命令追加 `unmark`；失败时追加 `broken`。这是唯一能捕获跨文件 import 破坏的路径——即使没有任何 stale 行点名被改的源文件，它也会重跑 A 的测试命令。step 可以声明 `file_deps`；当其他 step 的写入命中其中之一时，`prioritized_sessions()` 会把该 step 的命令排到 `max_reverify_commands` 上限之前（`file_deps_mode="prioritize"`，默认）。可选的 `file_deps_mode="filter"` 只重放声明命中的 session——更便宜，但可能漏掉未声明 step 的破坏。

---

## 存储

**文件：** `src/data/evidence-ledger.db`（由 `config.path.resolve_evidence_ledger_path` 解析）

| 属性 | 值 |
|------|----|
| 格式 | SQLite 表 `evidence_events`；WAL 模式 + `busy_timeout=5000` |
| 范围 | 仓库级（所有会话共享） |
| 可变性 | 行只 INSERT、不 UPDATE；保留期会删除旧行（age TTL / 每会话上限 / 全局上限） |
| 会话隔离 | 在带索引的 `session_id` 上 `WHERE session_id = ?`；行携带 SESSION key（主会话 id，或子代理的 `agent:{agent}:subagent:{uuid}`）——绝不是 plan / todo / flow id |
| 文件编辑记忆 | 同库的 `verification_state(session_id, changed_paths_json, last_edit_at)`；每会话上限，随 session/孤儿清理删除 |
| 多 key 读取 | `read_for_keys(keys)` 走一次带索引的 `IN` 查询，并保留每个 key 的插入顺序（陈旧性只在单个会话的行序列内推导） |
| 并发 | WAL 允许读写重叠；写入通过 `busy_timeout` 重试 |
| 旧 JSONL | 首次打开时导入一次（按行哈希去重），随后重命名为 `.migrated` |
| 文件不存在 | `read_all()` 返回 `[]`；目录在首次访问时创建 |

数据库保持仓库级、不是会话级：它是跨会话共享的验证审计轨迹。

### 保留期与清理

`EVIDENCE_LEDGER` 持有策略：`pruning_enabled`（默认 true）、`max_age_days`（30）、`max_events_per_session`（100）、`max_total_events`（10 000）。清理是惰性的——每次 `append` 在 INSERT 后执行三层 DELETE，清理失败绝不丢失刚写入的行。删除会话时调用 `purge_session`（evidence 与 `verification_state` 各一条 `DELETE WHERE session_id = ?`）；boot 孤儿 sweep 调用 `purge_orphans(known_sessions)`，因此即使会话绕过了正常清理路径，其 evidence 与 changed-path 状态也会被删除。`changed_paths_limit`（200）限制每会话文件列表。`purge_on_session_delete` / `purge_on_boot_sweep` 控制这两处显式删除；`pruning_enabled=false` 只关闭惰性保留期清理。

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

`write_file` 和 `patch_file` 用 `@invalidates_evidence(path_arg="file_path", session_arg="session_id")` 装饰。装饰器在 `finally` 块里运行，所以**每种**结果（成功、错误字符串、抛异常）都会追加一行 `{"event": "stale", "file_path": "..."}` 并调用 `EvidenceLedger.record_file_edit()`。跨 session 开关开启时，同一次调用还会向每个 evidence 覆盖该路径的其他 session（`modified_files` 命中，或旧行 command 子串）各追加一行 stale 事件。

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
  "modified_files": ["src/alpha.py"],
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

跨 session 行额外携带两个可选字段：`"scope": "cross_session"` 与 `"source_session": "<写入方 session id>"`。没有这两个字段的行就是原来的 per-session 标记。

`broken` 与 `unmark` 沿用同一套追加式结构：`broken` 携带 `command`、`exit_code`、`output_snippet` 与 `scope="regression_gate"`；`unmark` 携带 `command` 与 `scope`。两者都不会改写历史行。

---

## 过期推导

过期**在读取侧推导**，从不存为标志。规则：

> 一条 evidence 行是 stale 的，当且仅当一个**后来的** stale 事件（在追加顺序中索引更大）点名的 `file_path` 在该行的 `modified_files` 集合中，或被 command 作为真实路径 token 点名。没有 `modified_files` 的旧行降级到原来的 command 子串规则。

```python
def derive_evidence_state(records, index):
    row = records[index]
    command = str(row.get("command") or "")
    modified = row.get("modified_files") or []
    broken = stale = False
    for later in records[index + 1:]:
        event = later.get("event")
        if event == "unmark" and later.get("command") == command:
            broken = stale = False
        elif event == "broken" and later.get("command") == command:
            broken = True
        elif event == "stale":
            path = str(later.get("file_path") or "")
            if modified:
                stale = stale or (path in modified) or _command_references_path(command, path)
            else:
                stale = stale or (path in command)
    return broken, stale
```

关键细节：

- **方向重要：** 只有**后来的** stale 事件能使**先前的** evidence 行过期。stale 事件在 evidence 行之前意味着文件被编辑后又重新验证了——验证是新鲜的。
- **新行精确集合：** `file_path in modified_files`，或 command 中的真实路径 token。能捕获命令从未点名的源文件（`pytest tests/test_beta.py` + 写入 `alpha.py`）以及 A 没写过的测试文件，同时避免旧的子串误报（`feature.py` vs `test_feature.py`）。
- **旧行降级：** `file_path in command`。B' 之前的行保持原行为，故意宽松且不变。
- **按 session 分组：** `SessionEvidenceLedger.list_records()` 先按 `session_id` 过滤，派生只在一个 session 的序列内比较。因此跨 session 失效会**写进受影响 session 自己的组**（每个 session 一行），而不是留在写入方的组里。
- **Broken 与 unmark：** 同 `command` 的更晚 `broken` 事件渲染为 `[broken]`（重验后命令失败）；同 `command` 的更晚 `unmark` 同时清除 `[stale]` 与 `[broken]`。`evidence_ledger.py` 的 `derive_evidence_state()` 是唯一实现，`[broken]` 优先于 `[stale]`。

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
| 状态标记 | `derive_evidence_state()` 返回 broken 时追加 `" [broken]"`；否则 stale 时追加 `" [stale]"` |

这个字符串就是门控和判断器解析的对象——它们搜索字面子串 `"FAIL"`、`"[broken]"`、`"[stale]"` 和 `"[pass]"`。

---

## 消费者 — 三个门控层级

### 层级 1：per-step（TaskFlow step judge）

`taskflow_resume` 派发带验收标准的 step 时，调 `judge_step_result()` 并传入 `evidence_summary=collect_evidence_summary(session_key=step.child_session_key)`。step judge 的 prompt 包含 `## Verification Evidence` 段，里面有摘要（或 `"(none recorded)"`）。judge 用它决定 PASS / RETRY / BLOCK。

### 层级 2：per-subagent（completion judge + drain gate）

- **Completion judge 跳过：** goal-loop 第一 turn 时，`_first_turn_self_evident(result_text, evidence_summary)` 检查：result 非空 + 至少一个 `[pass]` + 无 `FAIL` + 无 `[stale]` + 无 `[broken]` → **跳过 judge 调用**，省一次辅助 LLM 调用。
- **Completion judge prompt：** 未跳过时，`judge_completion()` 在 prompt 里收到 evidence 摘要。
- **Drain gate：** `SubagentCompletionDrainMiddleware`（父 agent 侧 `before_model`）在排空子代理完成消息时检查 evidence。无 evidence 或有 `FAIL` → 注入 `[GATE] Completion blocked: verification evidence missing or failing` 阻塞消息。

### 层级 3：per-flow（taskflow_finish Gate C）

TaskFlow 关流为 DONE 前，Gate C 检查该 flow 的**聚合**证据——它每个已派发步的 `child_session_key` 加上 flow 的 `creator_session_key` 的并集（行按会话键存放，而一个 flow 横跨多个会话；从未派发过步的 flow 没有任何键，因此没有证据，放行）：

| 条件 | 结果 |
|------|------|
| 摘要包含 `"FAIL"` | 拒绝："failing verification evidence" |
| 摘要包含 `"[broken]"` | 拒绝："confirmed regression (a re-verified command now fails)" |
| 摘要包含 `"[stale]"` | 拒绝："stale evidence (code changed since last verification), re-run verification commands before finishing" |
| 否则（或无 evidence / collector 出错） | 通过 |

门控顺序：A（DAG 完整性）→ B（无未决 step）→ C（evidence）→ E（目标 judge）。

---

## 设计原则

1. **处处 fail-open。** 每个记录器吞掉自己的异常。每个门控在 collector 出错时返回"通过/未违反"。坏掉的账本永远不会阻塞完成或困住子代理。
2. **不可变事件行 + 读取时推导。** 已记录的行从不 UPDATE。过期在读取时从行的顺序计算。保留期可能删除旧行（age/会话/全局上限），但从不改写历史；reader 始终从幸存的行序列计算新鲜度。
3. **路径引用，不是内容。** 账本记录命令字符串和输出摘要（500 字符），不记录文件内容。它记录*验证了什么*和*是否通过*，不是*文件里有什么*。
4. **自动记录始终开启，两个派生层可开关。** 每条匹配分类法的 terminal/python_repl 调用都写一行，每次文件编辑都标记写入方自己的 session，没有 on/off 开关。跨 session 广播（`EVIDENCE_CROSS_SESSION_STALENESS["enabled"]`）与 finish 时重验（`EVIDENCE_REGRESSION_GATE["enabled"]`）是受门控的层，默认都开启；关闭它们即恢复原行为，且不改变账本 schema。
