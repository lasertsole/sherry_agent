# 计划：跨 Subagent 文件冲突检测——Staleness 传播 + 后写回归门

- 建档日期：2026-10-23
- 状态：**§2 + §3 + §4 已实施并验证（2026-10-24）**。§3 采用 `on_finish` 一次性批量重验（不是原方案的 `on_announce`），以便覆盖跨文件破坏并避免每次 announce 的重跑成本；§4 以 `file_deps` 优先重验实现（默认 prioritize，可选 filter）。基于 sherry 现有 evidence ledger + shadow-git + TaskFlow DAG 基础设施，补齐"subagent A 改文件 F（功能 X），subagent B 后面改文件 F 把功能 X 改坏，A/系统怎么知道"的检测能力。业界无框架真正解决此问题（详见调研）；sherry 的文件锁只串行化并发写入，不检测顺序写入的回归。
- **文档范围**：两个增量改造——(1) 跨 session staleness 传播：B 写文件 F 时自动标记所有 session 对 F 的 evidence 为 stale；(2) 后写回归门：对有 stale evidence 的 session 自动重跑验证命令，检测语义级破坏。两者复用现有基础设施，不引入新依赖。
- **硬约束**：evidence ledger 事件语义不变（staleness 仍派生；存储已是 SQLite）；fail-open（重验失败/命令不存在不崩）；不影响现有文件锁/TaskFlow/完成判断逻辑；跨 session 查询性能可接受。
- 关联代码：
  - sherry 现有（复用/改造）：
    - `agent/tools/pub_base/evidence_ledger.py`（SQLite evidence ledger + per-session keying + staleness 派生）
    - `agent/tools/taskflow/evidence_collector.py:15-40`（staleness 派生 + flow 聚合）
    - `agent/tools/pub_base/evidence_recorder.py:84-95,98-151`（`invalidates_evidence` 装饰器 + `mark_evidence_stale`）
    - `agent/tools/pub_base/shadow_git.py:229-252,255-373`（per-turn 快照 + read APIs）
    - `agent/tools/taskflow/tools/_shared.py:145-256`（step schema + `depends_on` + result 聚合）
    - `agent/tools/taskflow/tools/taskflow_finish.py`（Gate A–C + evidence 聚合）
    - `agent/tools/subagent/spawn/completion_judge.py:79-91,158-167`（evidence-aware 完成判断）
    - `agent/tools/subagent/announce/workspace_merge.py:57-68,132-163`（child→parent staleness + interface diff）
  - 业界参考：
    - claudecode `green_contract.rs:48-124`（`evaluate_evidence` → Satisfied/Unsatisfied + missing 列表——回归门范式）
    - hermes-agent `tool_dispatch_helpers.py:219-244`（`_paths_overlap` + `_plan_tool_batch_segments`——计划期重叠检测范式）
    - MetaGPT `qa_engineer.py`（QA 测试循环——顺序回归检测范式）

---

## 0. 背景（已核实）

### 0.1 问题陈述

```
subagent A 改文件 F → 加了功能 X → 跑了测试 → evidence: {session: A, file: F, command: "pytest tests/test_f.py", status: pass}
subagent B 改文件 F → 改了别处 → 把功能 X 改坏 → 跑了自己的测试 → evidence: {session: B, file: F, command: "pytest tests/test_b.py", status: pass}
```

**当前 sherry 的行为**：B 写文件 F 时，`evidence_recorder.py:84-95` 的 `invalidates_evidence` 装饰器调 `mark_evidence_stale(path=F, session_id=B_session)`——**只在 B 的 session scope 标记 stale**。A 的 evidence（不同 session key）不会被标记（`evidence_ledger.py:125-127` 的 staleness 比较只在同一 session 内进行）。`taskflow_finish` 聚合 evidence 时（`evidence_collector.py:30-40`），A 的 evidence 仍显示 pass——**系统不知道 A 的功能可能已被 B 破坏**。

### 0.2 sherry 已有的基础设施

| 基础设施 | 当前用途 | 可复用于本计划 |
|---|---|---|
| evidence ledger（`evidence_ledger.py`） | 记录验证命令 + stale 事件，per-session 派生 staleness | 扩展为 cross-session staleness |
| `invalidates_evidence` 装饰器（`evidence_recorder.py:84-95`） | 写文件后标记当前 session 的 evidence stale | 扩展为标记所有 session |
| shadow-git（`shadow_git.py`） | per-turn undo 快照 | 可用于 diff 对比（方案 3 辅助） |
| TaskFlow DAG（`_shared.py:145-256`） | step→step 依赖 | 可扩展 file_deps（方案 3） |
| completion judge / step judge | per-run/per-step 验证门 | 可注入 stale 检测结果 |
| workspace_merge interface diff（`workspace_merge.py:132-163`） | child→parent merge 时检测符号变更 | cross-session stale 先例 |

### 0.3 业界无解——claudecode 的规格层 vs 实现层（已核实）

调研 8 个本地框架 + 7 个业界框架。**关键发现**：claudecode 拥有最丰富的类型系统（GreenContract / branch_lock / lane_events / recovery_recipes），但**大部分未接线到生产**：

| claudecode 能力 | 代码位置 | 生产调用方 |
|---|---|---|
| GreenContract evaluate_evidence | `green_contract.rs:75-124` | **零调用方**（仅测试） |
| detect_branch_lock_collisions | `branch_lock.rs:23-49` | **零调用方**（仅 lib.rs re-export） |
| TaskPacket acceptance_tests / verification_plan | `task_packet.rs:43,66` | **只校验结构，不执行** |
| recovery_recipes attempt_recovery | `recovery_recipes.rs` | **模拟执行**（`fail_at_step` 旋钮，不跑真实命令） |
| lane_events reconcile | `lane_events.rs:505` | **无 lane 产生事件**（无 spawner） |
| LaneHeartbeat freshness | `task_registry.rs:69-78` | ⚠️ 被动分类外部推送心跳，无 watchdog |
| workspace_test_branch_preflight | `tools/src/lib.rs:2423` | ✅ **唯一真实生效的门** |
| ship.prepared 启发式 | `bash.rs:108-133` | ✅ 字符串匹配 `git push main` |

claudecode 唯一真正生效的回归门是 `workspace_test_branch_preflight`——执行 `cargo test --workspace` 前检查当前分支是否落后 main，落后则阻塞提示 rebase。它**不重跑其他 lane 的测试**，模型是"每个 lane 对最新 base 验证自己的工作"。

**结论**：本方案的 §3 回归门（B 写文件 F → 自动重跑 A 的验证命令 → fail 标 broken 通知父 agent）是 claudecode **从未实现**的能力。详见 §7 对比。

---

## 1. ⚠️ 硬约束

| 约束 | 含义 | 验收方式 |
|---|---|---|
| **F1**：evidence ledger schema 不变 | staleness 仍是 append-only `{"event":"stale","file_path":...}` 行；不改已有行；不新增表 | schema byte-identical |
| **F2**：fail-open | cross-session 查询/重验失败时不崩，退化到当前行为（per-session staleness） | 异常不阻断 turn |
| **F3**：不影响文件锁 | 文件锁逻辑不变；staleness 传播是写后通知，不影响锁的获取/释放 | 锁行为 byte-identical |
| **F4**：不影响完成判断 | completion judge / step judge 逻辑不变；stale evidence 是额外信号，不改变 judge 的 DONE/CONTINUE/PASS/RETRY 判定 | judge 行为 byte-identical |
| **F5**：性能可接受 | cross-session stale 标记是 O(S) 扫描（S = session 数），不是 O(N)（N = evidence 行数）；重验命令有超时 | 100 session / 1000 evidence 行 < 50ms |
| **F6**：可配置关闭 | 新增 feature flag，默认开启但可关 | `EVIDENCE_CROSS_SESSION_STALENESS["enabled"] = False` 退化为当前行为 |

---

## 2. 任务一：跨 session staleness 传播

### 2.1 目标

subagent B 写文件 F 时，不只标记 B 自己 session 的 evidence stale，而是扫描所有 session 的 evidence ledger，凡是 `command` 涉及文件 F 的行都标 stale。

### 2.2 改动清单

- [x] **2.2.1** `agent/tools/pub_base/evidence_ledger.py`：新增 `mark_stale_for_path_all_sessions(path: str, writer_session_id: str)`：
  - 扫描 ledger，找出所有 `session_id != writer_session_id` 且 `command` 包含 `path` 的 evidence 行
  - 为每个匹配的 session 追加一行 `{"event":"stale","file_path":path,"source_session":writer_session_id,"scope":"cross_session"}`
  - 用 `file_write_lock` 保护追加（已有模式，`:50`）
  - **fail-open**：扫描异常只 log，不阻断写入

- [x] **2.2.2** `agent/tools/taskflow/evidence_collector.py:15-21` 的 `_is_stale` 派生逻辑扩展（**注意：staleness 派生在 `evidence_collector.py` 不在 `evidence_ledger.py`**——`evidence_ledger.py:120-139` 是 `mark_stale_for_path` 追加侧，`_is_stale` 比较侧在 collector）：
  - 当前：evidence 行 stale iff 同一 session group 内有后续 `{"event":"stale","file_path":...}` 且 `file_path` 在 evidence 的 `command` 中
  - 扩展：也检查 `scope == "cross_session"` 的 stale 行（`source_session` 标识写入方）——collector 的 `collect_evidence_summary`（`:24-61`）当前按 session group 分割 stale_events（`:53`），需让 cross-session stale 行也参与每组比较
  - **向后兼容**：无 `scope` 字段的 stale 行（旧数据）仍按 per-session 处理（`scope` 缺省 = `session`）
  - **实施结果：collector 无需改动**——cross-session stale 行写进受影响 session 的 scope（`session_id=A`），现有 `_is_stale` 的同组派生直接可见；`test_evidence_cross_session.py` 覆盖。

- [x] **2.2.3** `evidence_recorder.py:98-151` 的 `mark_evidence_stale` 改造：
  - 当前只调 `mark_stale_for_path(path, session_id)`（per-session）
  - 扩展：当 `EVIDENCE_CROSS_SESSION_STALENESS["enabled"]` 为 True 时，额外调 `mark_stale_for_path_all_sessions(path, writer_session_id)`
  - per-session 标记保留不变（向后兼容）

- [x] **2.2.4** `evidence_collector.py:15-40` 的 flow 聚合逻辑：
  - 当前聚合每个 step session 的 evidence，staleness 在每个 session 内部派生
  - 扩展：`collect_flow_evidence` 时，cross-session stale 行也参与派生——A 的 evidence 因 B 的写入被标 stale，在 flow 聚合视图里 A 的 evidence 显示 `[stale]`
  - `taskflow_finish` 的 Gate B（evidence 无 FAIL/[stale]）因此能检测到 A 的 stale evidence

- [x] **2.2.5** 新增配置 `config/features/agent_side/evidence_cross_session.py`：
  ```python
  EVIDENCE_CROSS_SESSION_STALENESS: EvidenceCrossSessionConfig = {
      "enabled": True,              # 总开关
      "max_sessions_scan": 200,     # 扫描 session 数上限（防爆炸）
      "exclude_self": True,         # 不标记写入者自己的 session（已由 per-session 处理）
  }
  ```

### 2.3 风险与约束

- **F1 schema**：stale 行增 `scope` 和 `source_session` 字段，但旧行无这些字段——派生逻辑对无 `scope` 的行按 per-session 处理（向后兼容）
- **F2 fail-open**：`mark_stale_for_path_all_sessions` 全程 try-catch，异常只 log
- **F5 性能**：扫描 ledger 是 O(S × E_avg)（S = session 数，E_avg = 每 session evidence 行数），`max_sessions_scan` 上限保护。ledger 已是 SQLite（`read_for_keys` 走 `session_id` 索引）；跨 session 扫描可再按需加索引
- **误报风险**：B 写文件 F 不一定破坏 A 的功能——staleness 是"可能过期"信号，不是"已破坏"判定。这由 §3 回归门做语义确认

**验收**：F1 + F2 + F5 + F6。新建 `tests/agent/tools/pub_base/test_evidence_cross_session.py`。

---

## 3. 任务二：后写回归门（自动重验）

### 3.1 目标

当 A 的 evidence 因 B 的写入被标记 cross-session stale 后，自动重跑 A 的验证命令，检测 B 是否真的破坏了 A 的功能。

### 3.2 改动清单

- [x] **3.2.1** 新建 `agent/tools/pub_base/regression_gate.py`：
  ```python
  async def reverify_stale_evidence(file_path: str, writer_session_id: str) -> list[ReverifyResult]:
      """B 写完文件 F 后，找出所有因 F 被 cross-session stale 的 evidence，
      重跑其验证命令，返回重验结果。"""
      # 1. 从 evidence ledger 找出所有 cross-session stale 的 evidence 行
      #    （scope="cross_session", source_session=writer_session_id, file_path 匹配）
      # 2. 对每条 stale evidence，提取其 command（测试/typecheck/lint 命令）
      # 3. 重跑命令（复用 terminal 工具的执行能力，带超时）
      # 4. 对比原结果 vs 重跑结果：
  #    - 原 pass + 重跑 pass → stale 但未破坏 → 清除 stale（追加 `unmark` 事件——**新事件类型**，现有 ledger 只有 `stale` 一种失效标记，无 unmark；`_is_stale` 派生需同时识别 unmark 行做抵消）
  #    - 原 pass + 重跑 fail → 确认破坏 → 追加 {"event":"broken","file_path":...,"source_session":...}（**新事件类型**）
  #    - 原 fail + 重跑 fail → 无变化
  # 5. 返回结果列表
  ```

  **实施结果：** 重验范围改为 **flow 的全部 passed evidence 命令**（不只 stale），在 `taskflow_finish` 的 Gate C 前执行一次；这样跨文件依赖破坏（A 的命令不含 B 写的源文件）也能通过重跑 A 的测试发现。命令去重、`max_reverify_commands` 截断。

- [x] **3.2.2** `evidence_ledger.py` 增 `broken` 事件类型：
  - 追加 `{"event":"broken","file_path":path,"source_session":writer_session_id,"original_session":A_session,"command":...,"reverify_status":"fail"}`
  - `is_stale` 派生：`broken` 行的 evidence 显示 `[broken]`（比 `[stale]` 更严重）
  - `is_broken` 新派生函数：evidence 行有对应 `broken` 行 → 返回 True

- [x] **3.2.3** `evidence_collector.py:30-40` 聚合逻辑 + `taskflow_finish.py:132-161` Gate B/C 扩展：
  - 当前 Gate B/C（`taskflow_finish.py:156-160`）对 `[stale]`（**可能**过期）是**硬阻断** finish——"stale evidence (code changed since last verification)… Re-run verification commands before finishing."
  - 扩展：也检查 `[broken]`——**`[broken]`（确认破坏）必须 ≥ `[stale]`（可能过期）的阻断级别**，不能更软
  - **两档策略**（修正严重性倒挂）：
    - `[stale]` → 硬阻断 finish（现有行为不变）——可能过期，要求重验
    - `[broken]` → 硬阻断 finish + 在错误信息中标注"confirmed breakage by session {source_session}"——确认破坏，要求修复
    - `[broken]` 阻断的 finish 走现有 `_reject`（`taskflow_finish.py:365`）→ `consecutive_failures` 递增 → 达到 `finish_failure_limit` 自动 fail
  - **反误报兜底**：如果 broken 误报（重验命令环境差异导致假 fail），下一次重验通过会自动追加 `unmark` 清除；本版没有新增手动 unmark 工具（保持工具面不变）。

- [x] **3.2.4** 触发时机：
  - **方案 A（原推荐，未采用）**：在 subagent B 的 announce pipeline（`announce/core.py:132-151`）中，deliver announcement 前调 `reverify_stale_evidence`——B 完成时自动检查是否破坏了其他 session
  - **方案 B（本版实现）**：在 `taskflow_finish` 的 Gate C 前，对 flow 的全部 passed evidence 批量重验——不只 stale，因此能覆盖跨文件破坏；flow 完成前统一检查
  - **方案 C**：手动触发——父 agent 通过工具主动调 `reverify`（最保守）
  - **实施选择：方案 B（`on_finish`）**。§10.4 已论证 per-announce 的 N 倍成本与误报；finish 前一次性批量重验能看到合并后的工作区，也能覆盖跨文件破坏。`on_announce` 不接线。

- [x] **3.2.5** 重验命令执行：
  - **安全策略：** 不复用 shell；`shlex.split` + `shell=False`，且只允许 verifier allowlist（pytest/ruff/npm/cargo/...；`python -m <module>` 与 `uv run` 有额外校验）。含 `; && | > < $ \`` 等 shell 操作符的命令直接跳过。
  - 超时：`EVIDENCE_REGRESSION_GATE["reverify_timeout_s"]`（默认 60s），POSIX 用 `SIGKILL` 杀进程组
  - fail-open：命令执行失败/超时/不安全 → 不标 broken，保留 stale（保守不冤枉）

- [x] **3.2.6** 配置 `config/features/agent_side/evidence_cross_session.py` 扩展：
  ```python
  EVIDENCE_REGRESSION_GATE: EvidenceRegressionGateConfig = {
      "enabled": True,                # 回归门总开关
      "reverify_timeout_s": 60,       # 单命令超时
      "max_reverify_commands": 10,    # 单次重验命令数上限
      "trigger": "on_announce",       # on_announce | on_finish | manual
  }
  ```

### 3.3 风险与约束

- **F2 fail-open**：重验全链路 try-catch，任何异常不阻断 announce/finish
- **F4 不影响 judge**：broken 是额外信号，completion judge / step judge 不改逻辑——但 judge 的 evidence summary 可选展示 broken（`:79-91` 的 evidence summary builder 可增 broken 标记）
- **性能**：重验跑测试命令有成本（秒级）；`max_reverify_commands` 上限保护；只在 announce 时触发（非每次写）
- **误报处理**：B 写文件 F 但未破坏 A 的功能 → 重验 pass → 清除 stale（追加 unmark）→ 无负面影响。B 真破坏 → 重验 fail → 标 broken → 通知父 agent。**不自动回退**——由父 agent 决定处理方式（重跑 A / 修正 B / 人工介入）
- **命令提取**：evidence 行的 `command` 字段是 terminal 命令字符串——重验时在当前工作目录执行，环境可能与 A 执行时不同（依赖可能变化）。接受这个限制——重验是"尽力检测"

**验收**：F2 + F4。新建 `tests/agent/tools/pub_base/test_regression_gate.py`。

---

## 4. 任务三：（可选）文件级依赖声明

### 4.1 目标

让 TaskFlow step 显式声明"我依赖文件 F 稳定"——当 B 修改 F 时，引擎检测到 A 声明了 `file_deps: [F]` → 标记 A 的 step 为 `needs_reverification` → 触发 §3 回归门。

### 4.2 改动清单

- [x] **4.2.1** `_shared.py:127-142` 的 `_OPTIONAL_STEP_FIELDS` 增 `file_deps: list[str]`（文件路径列表，相对 project root）；`validate_steps_list` 校验 list[str]。
- [x] **4.2.2** `taskflow_finish` 增文件依赖检查：
  - 当 step B 修改了文件 F → 扫描所有 step 的 `file_deps` → 匹配 F 的 step A 进入 `prioritized_sessions`（即运行时的 `needs_reverification`）
  - `needs_reverification` 只在 finish 时派生，不写入持久化 step schema；B 的“修改文件”来自其 session 的写 stale 事件（`scope != cross_session`），A 自己的写入不算
- [x] **4.2.3** LLM prompt 引导：taskflow_plan 的 prompt 增"声明 file_deps"指引——让 LLM 在规划时声明文件依赖；`taskflow_summary` 也会显示 `file_deps`。
- [x] **4.2.4** 与 §2/§3 的关系：
  - §2 的 cross-session staleness 是**自动的**（不需声明，B 写 F → 所有涉及 F 的 evidence 标 stale）
  - §4 的 file_deps 是**显式的**（step 声明"我依赖 F" → 更精确的触发）
  - 两者互补：§2 覆盖所有 evidence（广但可能误报），§4 覆盖声明了依赖的 step（精但依赖 LLM 声明质量）

### 4.3 风险与约束

- **LLM 声明质量**：LLM 可能漏声明 file_deps——但 §2 的自动 staleness 是兜底
- **向后兼容**：`file_deps` 是 optional field，不声明 = 当前行为（靠 §2 自动 staleness）

**验收**：可选任务，不阻断 §2/§3 的独立交付。

---

## 5. 执行顺序

```
§2 跨 session staleness 传播（2-3 天）
  ├─ mark_stale_for_path_all_sessions
  ├─ is_stale 派生扩展（scope 字段）
  ├─ mark_evidence_stale 改造
  ├─ evidence_collector 聚合扩展
  └─ 配置 + feature flag
      │
      ↓ （§3 依赖 §2 的 cross-session stale 行）
§3 后写回归门（3-4 天）
  ├─ regression_gate.py（reverify_stale_evidence）
  ├─ broken 事件类型 + is_broken 派生
  ├─ announce pipeline 触发
  ├─ 重验命令执行（超时 + 上限）
  └─ 配置 + feature flag
      │
      ↓ （§4 是 §2/§3 的精度增强，可选）
§4 文件级依赖声明（1-2 天，可选）
  ├─ step schema 增 file_deps
  ├─ taskflow 检查 file_deps
  └─ prompt 引导

总工程量：5-7 天（§2+§3）/ 6-9 天（含 §4）
```

**关键路径**：§2 是基础（cross-session stale 行），§3 依赖 §2（重验 stale evidence），§4 是精度增强（可选）。

---

## 6. 验收标准

### 6.1 硬约束验收

- [ ] **F1**：evidence ledger schema 不变（stale 行增 `scope`/`source_session` 字段，旧行向后兼容）。
- [ ] **F2**：cross-session 查询/重验失败时不崩。
- [ ] **F3**：文件锁逻辑不变。
- [ ] **F4**：completion judge / step judge 逻辑不变。
- [ ] **F5**：100 session / 1000 evidence 行 < 50ms（staleness 扫描）。
- [ ] **F6**：feature flag 关闭时退化为当前行为。

### 6.2 功能验收

- [ ] subagent B 写文件 F → A 的 evidence（command 含 F）被标记 cross-session stale。
- [ ] `taskflow_finish` 聚合时 A 的 evidence 显示 `[stale]`。
- [ ] subagent B announce 时自动重跑 A 的验证命令。
- [ ] 重验 pass → 清除 stale（unmark 事件）。
- [ ] 重验 fail → 标记 `[broken]` + 通知父 agent。
- [ ] 父 agent 在 announce 报告中看到 broken evidence。
- [ ] （可选）step 声明 `file_deps: [F]` → B 写 F → A 标记 `needs_reverification`。

### 6.3 回归验收

- [ ] 现有 `tests/agent/tools/pub_base/test_evidence_*.py` 全套通过。
- [ ] 现有 `tests/agent/tools/taskflow/` 全套通过。
- [ ] feature flag 关闭时 evidence 行为 byte-identical 当前。

---

## 7. 与 claudecode 对齐情况（已核实）

本方案执行完后，sherry 在跨 agent 回归检测维度上**超过 claudecode 的已实现能力**。

### 7.1 逐项对比

| 维度 | claudecode 已实现 | sherry 方案交付 | 对齐情况 |
|---|---|---|---|
| **并发写串行化** | 无（分支/worktree 隔离概念未接线） | 已有：per-path 排他锁 + flock + CAS | **sherry 超出** |
| **跨 agent evidence 失效** | 无 | §2 cross-session staleness 传播 | **sherry 超出** |
| **回归重验（重跑测试）** | ❌ 不存在——只有"你的分支落后 main，先 rebase" | §3 regression_gate 自动重跑 stale evidence 的验证命令 | **sherry 超出** |
| 计划期重叠检测 | `detect_branch_lock_collisions` 有逻辑但零调用方 | 不做（§8 明确不做） | claudecode 有定义未接线 |
| base 分支新鲜度检测 | `stale_branch.rs` 有真实逻辑，接到 pre-flight | 不做 | claudecode 超出（但只用于自己的测试前置检查） |
| per-task worktree 隔离 | TaskPacket 有 `worktree` 字段（未接线） | 不做（sherry 共享工作区模型） | 都没真正实现 |
| known-flake 阻断 | GreenContract 有 `block_known_flakes`（未接线） | 不做 | 都没真正实现 |
| 心跳/活跃度检测 | `LaneHeartbeat` 被动分类（无 watchdog） | lane slot 有 `started_at` 时间戳 | 相当 |
| 恢复策略 | `recovery_recipes.rs` 模拟框架（不执行真实命令） | 不做（broken 阻断 finish，由父 agent 决定处理） | 都没真正实现 |
| 死锁检测 | 无 | 无 | 都没有 |

### 7.2 claudecode 的"规格层"本质

claudecode 的 GreenContract / branch_lock / lane_events / recovery_recipes 是精心设计的**类型系统 + 纯函数 + 单元测试**，但：
- 无并行 lane spawner（repo 全量 grep `spawn.*lane` / `parallel.*lane` / `for lane in` 零命中）
- `detect_branch_lock_collisions` 返回 `Vec<BranchLockCollision>` 后无人消费
- `evaluate_evidence` 返回 `Satisfied`/`Unsatisfied` 后无人消费
- `TaskPacket` 的 `commit_policy`/`branch_policy`/`recovery_policy`/`escalation_policy` 是自由文本字符串，未连接到 `StaleBranchPolicy`/`EscalationPolicy` 类型枚举

### 7.3 两边都缺的（独立正交增强，不影响本方案结论）

- **计划期重叠检测**：hermes-agent 的 `_paths_overlap`（`tool_dispatch_helpers.py:236`）才是真正接线的实现——本方案不做，另见 §9 参考
- **per-task worktree 隔离**：oh-my-openagent 的 per-member `git worktree add`（`team-worktree.mjs:76`）才是真正接线的实现
- **known-flake 阻断**、**死锁检测**：两边都没有

---

## 8. 不做的事（边界）

| 项 | 不做的原因 |
|---|---|
| 自动回退 B 的修改 | broken 是信号不是自动操作——由父 agent 决定处理（重跑 A / 修正 B / 人工） |
| 文件级 merge 冲突检测 | 那是 git merge 的职责（worktree 隔离方案，另见 §9 参考 oh-my-openagent） |
| per-write 快照对比 | shadow-git 是 per-turn 快照（不是 per-write），粒度不够；文本重叠 ≠ 语义破坏 |
| subagent peer-to-peer 通信 | sherry 是 hub-and-spoke 模型（父→子/子→父），兄弟通信走父节点中转——不改架构 |
| 计划期重叠检测（hermes-agent `_paths_overlap`） | 那是预防（不派发重叠任务），本计划是检测（事后发现破坏）——互补但正交 |
| LLM 判断"B 是否破坏了 A" | 用命令重验（确定性），不用 LLM 判断（不确定性）——成本低且可重复 |

---

## 9. 关键参考

### sherry 内部（复用/改造）
- `agent/tools/pub_base/evidence_ledger.py:10-13,50,104-139`（ledger + staleness 派生）
- `agent/tools/taskflow/evidence_collector.py:15-40`（staleness + flow 聚合）
- `agent/tools/pub_base/evidence_recorder.py:84-95,98-151`（`invalidates_evidence` + `mark_evidence_stale`）
- `agent/tools/pub_base/shadow_git.py:229-252`（per-turn 快照——辅助 diff 对比）
- `agent/tools/taskflow/tools/_shared.py:127-142,145-256`（step schema + 依赖）
- `agent/tools/taskflow/tools/taskflow_finish.py`（Gate B evidence 检查）
- `agent/tools/subagent/spawn/completion_judge.py:79-91,158-167`（evidence-aware judge）
- `agent/tools/subagent/announce/core.py:132-151`（announce pipeline——回归门触发点）
- `agent/tools/subagent/announce/workspace_merge.py:57-68,132-163`（child→parent staleness + interface diff 先例）

### 业界参考
- claudecode `rust/crates/runtime/src/green_contract.rs:48-124`——`evaluate_evidence` → Satisfied/Unsatisfied + missing 列表（回归门范式）
- claudecode `rust/crates/runtime/src/task_packet.rs:43,66`——per-task `acceptance_tests`/`verification_plan`（声明式验证）
- hermes-agent `agent/tool_dispatch_helpers.py:219-244`——`_paths_overlap` + `_plan_tool_batch_segments`（计划期重叠检测——互补但正交）
- MetaGPT `metagpt/roles/qa_engineer.py`——QA 测试循环（顺序回归检测范式）
- oh-my-openagent `team-worktree.mjs:76,102`——per-member git worktree + merge 冲突（隔离方案——正交）
- LangGraph `langgraph/channels/last_value.py`——`InvalidUpdateError`（state 级并发写检测——理念参考）

### 关联 TODO
- `计划_前端_消息编辑与重生成.md`——消息编辑的 `superseded` 标记与本计划的 stale/broken 标记理念一致（逻辑标记不物理删除）
- `计划_前端_轨迹调试视图.md`——轨迹面板可展示 stale/broken evidence 标记

---

## 10. 性价比分析

### 10.1 问题实际发生频率

| 场景 | 发生条件 | 频率 | 当前能否检测 |
|---|---|---|---|
| 同文件并发写 | 两个 subagent 同时写 file F | 罕见——文件锁已串行化 | 文件锁防止交错，但 B 后写可覆盖 A |
| 同文件顺序写 | B 在 A 之后写同一 file F | 中等——主 agent 分配任务时可能重叠 | ❌ 无法检测 |
| 跨文件依赖破坏 | B 改 `utils.py`，A 的 `feature_x.py` import 它 | **最高风险**——LLM 难以追踪全部 import 依赖 | ❌ 完全无感知 |
| 接口变更 | B 改了函数签名/类结构，A 调用旧签名 | 中等——`workspace_merge.py:132-163` 已有静态 interface diff | ⚠️ 只在 merge 时检测符号变更，不做语义验证 |

跨文件依赖破坏是最常见且最危险的场景——LLM 编排时很难追踪所有 import 关系。但同文件顺序写在编排良好的 TaskFlow 里应靠主 agent 分配不重叠任务避免，不一定要靠检测兜底。

### 10.2 逐层性价比

#### §2 跨 session staleness 传播——高性价比

| 维度 | 评估 |
|---|---|
| 工程量 | 2-3 天 |
| 复用已有 | ✅ evidence ledger + `workspace_merge.py:57-68` 的 child→parent 先例 + `evidence_collector.py` 现有派生逻辑 |
| 新增复杂度 | 低——只多 `scope` 字段 + 一个广播函数，staleness 派生微调 |
| 交付价值 | **系统第一次有了跨 subagent 感知**——B 写 file F → A 的 evidence 标 stale → `taskflow_finish` Gate B **硬阻断**（现有行为 `:156-160`）→ 主 agent 被迫重验或修正 |
| 误报风险 | 中——B 写 F 不一定破坏 A，但 stale 是"可能过期"不是"确认破坏"，硬阻断要求重验是安全的 |
| 性能 | O(S) 扫描（S=session 数），`max_sessions_scan=200` 上限，<50ms |

**结论：80/20 的那一层。** 2-3 天让系统从"完全无感知"变成"知道可能有问题并阻断"。父 agent 看到 stale evidence 后自己决定重验（手动跑测试 / 让 LLM 判断 / 修正 B）——不需要自动重跑命令就能获得大部分价值。

#### §3 后写回归门（自动重验）——中低性价比

| 维度 | 评估 |
|---|---|
| 工程量 | 3-4 天 |
| 复用已有 | 部分——复用 terminal 执行能力，但 `regression_gate.py` 是全新模块 |
| 新增复杂度 | **高**——2 个新事件类型（`broken`/`unmark`）、新派生逻辑、命令执行 + 超时 + 上限、触发时机选择 |
| 交付价值 | **增量价值有限**：§2 已让 Gate B 阻断 finish 并提示"stale evidence, re-run verification"（`taskflow_finish.py:159` 现有文案）。§3 只是把"手动重验"变成"自动重验"——省的是父 agent 的一个决策步骤 |
| 误报风险 | **高**——重验命令在 B 写完后的环境跑，可能与 A 当初环境不同（依赖版本/状态差异），假 fail → 假 broken → 硬阻断 finish → 流程卡住 |
| 性能成本 | 每次 announce 重跑最多 10 条命令 × 60s = **10 分钟延迟**。5 个 subagent 顺序完成 = 最多 50 分钟额外测试时间 |
| 维护负担 | 高——命令提取逻辑、执行环境一致性、超时处理、unmark 抵消逻辑，都是新维护面 |

**关键问题**：§3 的增量价值 = §2 已经阻断 finish + 提示重验，§3 只是把"提示"变成"自动执行"。而自动执行恰恰是误报风险最高的部分——命令在 B 写完后的环境跑，可能因环境差异而非 B 的破坏导致 fail。

#### §4 文件级依赖声明——低性价比

| 维度 | 评估 |
|---|---|
| 工程量 | 1-2 天 |
| 交付价值 | 精度增强——只有声明了 `file_deps` 的 step 才触发。但 §2 已是自动兜底（广播到所有 session），§4 只是减少误报 |
| 依赖 | LLM 声明质量——漏声明 = §2 兜底，多声明 = 不影响 |
| 结论 | 可选锦上添花，不阻断 §2 独立交付 |

### 10.3 更便宜的替代方案对比

| 方案 | 工程量 | 交付能力 | 与本方案关系 |
|---|---|---|---|
| **只做 §2** | 2-3 天 | B 写 F → A 标 stale → Gate B 阻断 → 父 agent 决定重验/修正 | 本方案的 80% 价值 |
| §2 + 轻量 §3'：finish 时一次性批量重验 | 3-4 天 | Gate B 发现 stale → 批量重跑所有 stale evidence 的命令 → 一次性出结果 | 比 §3 的 per-announce 触发更省（不每次 announce 都跑），但延迟到 finish 才发现 |
| announce prompt 增强 | ~0 天 | 在 B 的 announce 报告中加"B 修改了文件 F，A 曾验证过 F，请检查是否受影响" | LLM 判断（非确定性），但零工程成本 |
| 全量测试门 | 1 天 | taskflow_finish 前跑 `pytest --all` | 不区分谁破坏了谁，但最简单 |
| 现有 child→parent staleness 扩展到 sibling | 1 天 | 把 `workspace_merge.py:57-68` 从只标 parent 改为标所有 session | §2 的极简版，但只在 merge 时触发（非每次写） |

### 10.4 推荐：只做 §2，§3/§4 暂缓

理由：

1. **§2 是性价比拐点**：2-3 天，复用已有 evidence ledger + Gate B 硬阻断（现有行为），让系统从"完全无感知"变成"知道可能有问题并阻断"。父 agent 看到 stale evidence 后自己决定处理方式——比自动重跑命令更安全（父 agent 有完整上下文，可以判断是否真的需要重验）。

2. **§3 的增量不抵成本**：§2 已让 Gate B 阻断 finish 并提示"re-run verification commands before finishing"（`taskflow_finish.py:159` 现有文案）。§3 只是把"提示"变成"自动执行"，但自动执行恰恰引入最高风险（环境差异假 fail → 假 broken → 硬阻断）。如果要做自动重验，**至少应改为 `trigger: "on_finish"`（一次性批量重验）而非 `on_announce`（每次 announce 都跑）**，减少 N 倍测试开销。

3. **误报后果不对称**：§2 的 stale 误报（B 写 F 但没破坏 A）→ Gate B 阻断 → 父 agent 重验 → pass → 继续。成本是一个重验周期。§3 的 broken 误报（环境差异假 fail）→ 硬阻断 finish → 流程卡住直到人工清除。成本是流程停滞。**§3 的误报代价远高于 §2**。

4. **claudecode 也没做**：claudecode 的 GreenContract 是未接线的规格层，唯一真实门是"分支落后 main 就 rebase"。连 claudecode 都没做自动跨 agent 重验——说明这个功能的 ROI 在业界也未被验证。

**如果后续发现 §2 不够**（父 agent 经常忽略 stale 提示，导致破坏未被发现），再上 §3 的 `on_finish` 批量版本——那时有了 §2 的实际运行数据，能更好地评估误报率和重验成本。

### 10.5 修正后的工程量

| 层 | 状态 | 工程量 |
|---|---|---|
| §2 跨 session staleness 传播 | **推荐立即做** | 2-3 天 |
| §3 后写回归门 | **暂缓，待 §2 运行数据** | 3-4 天（如后续做，改 `on_finish` 触发） |
| §4 文件级依赖声明 | **暂缓，可选锦上添花** | 1-2 天 |
| **合计（推荐）** | | **2-3 天** |
| 合计（全量） | | 6-9 天 |

---

## 11. §2 实施结果（2026-10-23）

### 11.1 已落地

- `EvidenceLedger.mark_stale_for_path_all_sessions(path, writer_session_id)`：SQL 扫描其他 session 中 `command` 包含 `path` 的 evidence，按 session 去重，向每个受影响 session 追加一行 `{"event":"stale","file_path":...,"scope":"cross_session","source_session":writer}`。
- `mark_evidence_stale()`：先写 writer 自己的 per-session stale 行，再按 `EVIDENCE_CROSS_SESSION_STALENESS["enabled"]` 广播。
- 新配置 `config/features/agent_side/evidence_cross_session.py`：`enabled`（默认 true）、`max_sessions_scan`（200）、`exclude_self`（true）。
- collector / finish gate 无需改代码：cross-session stale 行写入受影响 session 的 scope，现有同组 `_is_stale` 直接看到，Gate C 保持原有硬阻断。

### 11.2 验证结果

- 新增 `tests/agent/tools/pub_base/test_evidence_cross_session.py`：6 个用例覆盖传播、无关路径、开关关闭、扫描上限、命令匹配边界、fail-open。
- `test_finish_gate.py` 新增集成用例：A 的 `pytest tests/test_feature.py` 通过后，兄弟 B 写同一路径 → A 的 evidence 在 Gate C 显示 `[stale]` → `taskflow_finish` 被拒绝。
- 聚焦套件（cross-session + ledger + stale + finish gate）43 passed。
- 性能（本机、磁盘临时目录、SQLite）：
  - 100 session / 1000 evidence 行，单 session 命中：21.33 ms（含 1 次插入）。
  - 100 session / 1000 evidence 行，100 session 全命中：12.27 ms（含 100 次插入）。
  - 均低于 F5 的 50 ms。

### 11.3 真实改善与边界

**改善是真实的，但范围窄于“跨文件回归检测”：**

- ✅ 同文件顺序写：B 写 F，A 的验证命令**点名了 F**（如 `pytest tests/test_feature.py` / `ruff check feature.py`）→ A 的 evidence 立即变 `[stale]` → Gate C 阻断 finish。
- ❌ 跨文件 import 依赖：A 的验证命令是 `pytest tests/test_beta.py`，B 改的是它 import 的 `src/alpha.py`，命令字符串不含 `src/alpha.py` → **不标 stale**。`test_command_matching_does_not_infer_source_dependencies` 把这个边界钉成了测试。
- ⚠️ 路径匹配是子串匹配：B 写 `feature.py` 会命中命令里的 `tests/test_feature.py`；偏保守的误报，只会多一次重验提示，不会漏报。

**结论**：§2 能解决“同一个被验证文件被后写”的问题，适合作为低成本兜底；计划里列为最高风险的“跨文件依赖破坏”仍需 §3 的重验（重跑 A 的测试命令）才能检出。§3 是否值得做，建议用 §2 上线后的实际 stale 命中率评估；若命中主要来自同文件覆盖，§2 足够；若跨文件破坏真实发生，则上 `trigger="on_finish"` 的批量重验版本，而不是 `on_announce` 的每次重跑。

---

## 12. §3 实施结果（2026-10-24）

### 12.1 已落地

- 新增 `agent/tools/pub_base/regression_gate.py::reverify_flow_evidence()`：`taskflow_finish` 在 Gate C 前一次性重跑 flow 全部 passed evidence 命令（去重、`max_reverify_commands=10`、单命令 60s）。
- 新事件：`broken`（重验失败）与 `unmark`（重验通过，清除该命令的 stale/broken）。
- `derive_evidence_state()` 统一读取侧派生：later event 才生效，`unmark` 抵消同命令的 stale/broken，`[broken]` 优先于 `[stale]`。
- Gate C 顺序：`FAIL` → `[broken]`（confirmed regression，硬阻断）→ `[stale]`。CompletionJudge 的 first-turn shortcut 也拒绝 `[broken]`。
- 安全执行：`shell=False` + verifier allowlist；含 shell 操作符、不可解析、非 allowlist 命令一律跳过，不冤枉。
- 配置 `EVIDENCE_REGRESSION_GATE`：`enabled`（默认 true）、`reverify_timeout_s=60`、`max_reverify_commands=10`、`max_output_chars=500`。

### 12.2 对照结果（真实 e2e）

`tests/agent/tools/taskflow/test_regression_gate_e2e.py` 用真实临时项目和真实 `python -m pytest`：

| 场景 | §2 only | §2 + §3 |
|---|---|---|
| A 验证 `test_beta.py`；B 改它 import 的 `alpha.py`，测试被破坏 | **finish 通过（漏报）** | **拒绝：`[broken]`（检出）** |
| B 写 `test_beta.py` 但测试仍通过（stale 误报） | finish 被 `[stale]` 阻断 | **重验通过 → `unmark` → finish 通过（消除误报）** |
| B 写 `test_beta.py` 且测试真的失败 | finish 被 `[stale]` 阻断 | **`[broken]`，拒绝并明确是确认回归** |

结论：**§3 的改善比 §2 大一个维度**——§2 只能发现“命令点名了被写文件”的 stale；§3 通过重跑命令，能发现跨文件 import 破坏，并能消除 §2 的误报。

### 12.3 成本与边界

- 成本：每次 `taskflow_finish` 最多 10 条命令 × 60s，且只在 finish 时执行一次（不是每次 announce）。本次 e2e 三条命令的总测试时间 < 2s。
- 只重放 passed evidence；失败的 evidence 仍由 Gate C 的 `FAIL` 处理。
- 命令必须通过 allowlist 且不含 shell 操作符；`bash -c`、`cmd1 && cmd2`、`python -c` 等会被跳过（保留原 stale，不误报 broken）。
- 在 flow 的项目根执行（creator 绑定 → step 绑定 → 默认根）；隔离 worktree 的命令在合并后的主根重跑，这是 finish 时的正确视图。
- 没有新增手动 unmark 工具：误报会由下一次通过的重验自动清除；如果命令因环境原因持续假失败，可以关闭 `EVIDENCE_REGRESSION_GATE["enabled"]`。

---

## 13. §4 实施与 A/B 结果（2026-10-24）

### 13.1 已落地

- Step schema 新增可选 `file_deps: list[str]`（project-relative）；`validate_steps_list` 校验形状，`taskflow_plan` 解析 + prompt 引导，`taskflow_summary` 显示。
- `regression_gate.prioritized_sessions()`：在 finish 时从各 session 的**写 stale 事件**推导“谁改了什么”，与其它 step 的 `file_deps` 求交；命中的 step 进入 `needs_reverification` 运行时集合（不落 step schema）。
- `reverify_flow_evidence(priority_session_keys=...)`：受影响 step 的命令稳定排序到最前，再受 `max_reverify_commands` 截断——保证 cap 内优先覆盖声明过的依赖。
- `EVIDENCE_REGRESSION_GATE["file_deps_mode"]`：`prioritize`（默认，保留 §3 全量覆盖）或 `filter`（只重放声明命中的命令，成本更低但依赖声明完整）。

### 13.2 A/B（真实 e2e，cap 可调）

| 场景 | 重放命令数 | 结果 |
|---|---:|---|
| 无 `file_deps`，cap=1，A 的破坏命令排第 4 | 1 / 4 | 只跑无关命令（pass）→ **漏报，finish 通过** |
| 有 `file_deps=["alpha.py"]`，cap=1 | 1 / 4 | A 被排到第 1 → pytest fail → `[broken]` → **拒绝** |
| `prioritize`，cap=10，A 声明 alpha，C 未声明且 delta 被破坏 | 5 / 5 | A pass、C fail → `[broken]` → **拒绝**（覆盖未声明 step） |
| `filter`，cap=10，同场景 | 1 / 5 | 只跑 A（pass）→ C 的破坏**漏报**，finish 通过 |

### 13.3 结论

**§4 没有比 §3 更大的覆盖面，它优化的是“cap 内先跑谁”和成本。**

- 默认 `prioritize` 不减少覆盖（cap 宽松时仍重放全部），只是保证声明过的受影响 step 一定在 cap 内——这对 `max_reverify_commands` 限制下的检测有真实价值。
- `filter` 把 5 条降到 1 条，但会漏掉未声明 step 的跨文件破坏；只有在所有 step 的 `file_deps` 都可信时才应启用。
- 因此不建议把 `filter` 设为默认；`prioritize` 是安全默认，`filter` 是显式的成本换覆盖选项。
- 若目标是“减少 §3 的测试开销”而非“提高检出率”，filter 有价值；若目标是“不漏报”，prioritize + §3 全量重放仍是当前最强组合。
