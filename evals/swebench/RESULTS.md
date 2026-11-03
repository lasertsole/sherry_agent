# SWE-bench 运行记录（2026-11-01）

本文件只保留 2026-11-01 的运行记录。更早的 arm 已从失败历史中移除，完整分析见
`wiki/evals/SWE-Bench/`。原始报告（gitignored）在
`evals/results/swebench/<run_id>/`。

---

## 20261101_000025 — prompt + no-edit 门（1/2，脏）

- 目的：验证 eval-only no-edit 门能否强制主代理从研究转入编辑。
- 命令：`uv run --no-sync python evals/swebench/suite.py --hard --persona coding --keep-instances --instance-cache-dir /tmp/swebench-instance-cache`
- 结果：**1/2**。Django F2P/P2P 通过，但新增
  `tests/bulk_create/test_upsert_ids_tmp.py`；Sympy 726B patch 破坏 P2P。
- 失败链：门在 15 次工具调用后 WARN，20 次附近阻断；Django 用 `terminal cp` 创建
  测试文件（门只认 `patch_file`/`write_file`），清理命令被 terminal 安全屏/旧预算
  阻断；Sympy 首次 patch 后门永久关闭，后续探针无人约束。
- 结论：no-edit 门只能防“完全不写”，不能防“写错/写测试/不验证”；已删除。

---

## 20261101_002305 — delegate-only（2/2，干净）

- 目的：把主代理的直接工具全部移除，只允许 `sessions_spawn` 编排。
- 结果：**2/2 resolved**，diff 无测试文件；主代理 0 次 read/search/terminal/
  patch/write，13 次 spawn，总 936.7s。
- 代价：协调开销大；主代理无法独立验证；完成的子代理 checkpoint 可能已被清理，
  主代理为确认状态反复 spawn；报告只统计主 session token。

---

## 20261101_143952 — 首版软门 10/20（1/2）

- 目的：单回合累计读调用；10 次警告、20 次阻断；写或 accepted spawn 复位。
- 结果：**1/2**。Sympy resolved；Django 97 次工具调用、521s，P2P 失败。
- 失败链：Django 在 20 次读后被阻断，spawn 了一个 researcher；产品写入使门复位，
  随后主代理在 `compiler.py` 上约 70 次 `patch_file` 重试。门没有把 `patch_file`
  纳入目标，write 复位又让压力消失；`write_file` 的 read-before-write 许可证不覆盖
  `patch_file`，所以没有拦住。
- 结论：软门的阈值/复位策略需要改成“单回合累计、阻断读写、仅 after_agent 清零”。

---

## 20261101_145717 — delegate-only 对照（2/2）

- 同一代码、同一 HARD 切片；主代理只编排，子代理全量工具。
- 结果：**2/2 resolved**；总 3469.0s；主 session tokens 2,561,580 / 20,925；
  估算 $1.312。
- 说明：作为软门 8/16 的同代码对照；比 002305 慢，说明单轮方差和协调成本都很大。

---

## 20261101_174750 — 软门 8/16（2/2）

- 目的：单回合累计读调用；8 次警告、16 次后阻断所有读写；浏览器调试工具豁免；
  spawn 不解除、写不清零，`after_agent` 才复位。
- 命令：`SHERRY_EVAL_DELEGATION_PRESSURE=1 uv run --no-sync python evals/swebench/suite.py --hard --persona coding --keep-instances --instance-cache-dir /tmp/swebench-instance-cache`
- 结果：**2/2 resolved**，diff 干净（Django 无测试文件改动）；总 1693.4s；
  主 session tokens 1,962,778 / 12,673；估算 $1.000。
- Django 在 16 次读后被阻断，转交子代理完成，未出现上一轮的 patch 重试循环。

---

## 20261103_010944 — 7/14 建议式软门（0/2）

- 目的：在当前 7/14 建议式 gate（不阻断工具）下重跑 HARD 切片，确认是否能恢复 2/2。
- 命令：`uv run --no-sync python evals/swebench/suite.py --hard --persona coding --keep-instances --instance-cache-dir /tmp/swebench-instance-cache`
- 结果：**0/2**；总 669.7s；tokens 8,370,656 / 31,119；估算 $4.232。
  环境探测 `terminal_egress=false`（GitHub API 403），本次没有可用的上游检索。
- Django：只改了 `django/db/models/query.py` 两处 `on_conflict is None` →
  `on_conflict in (None, OnConflict.UPDATE)`；F2P 仍以
  `UNIQUE constraint failed: bulk_create_country.iso_two_letter, bulk_create_country.name`
  报错（3 个 F2P error），P2P 也失败。表面上允许了返回行，实际没有生成
  SQLite `ON CONFLICT ... DO UPDATE` 的写入路径。
- Sympy：diff 为空、`changed_paths=[]`；主代理在 90 次模型调用后触发
  `IterationBudget exhausted`，没有产出补丁；P2P 通过、F2P 失败。
- 失败建议：
  1. **Django**：不要只放宽 `can_return_rows_from_bulk_insert` 条件。需要让
     `QuerySet.bulk_create(on_conflict=OnConflict.UPDATE)` 在 SQLite 上真正生成
     `ON CONFLICT (...) DO UPDATE SET ...`，或在 `_batched_insert` / compiler 层
     把 `update_fields` 交给冲突分支；补一个只覆盖目标 F2P 的回归测试。
  2. **Sympy**：为“90% 预算仍无 diff”增加收敛信号（eval harness 可先做只读检查，
     产品侧可复用建议式 gate 的 escalation）；主代理不能把整个预算花在 terminal
     探测上。先定位目标函数/测试，再编辑，最后用目标测试收口。
  3. **环境**：本次 `terminal_egress=false`，`web_search` 无后端。harness 应把
     网络失败显式标为 degraded，并提示模型直接走本地代码/测试，而不是反复探测
     GitHub。
  4. **DeepSWE**：按约定“SWE 全通过才跑 DeepSWE”，本次 **未运行**。

---

## 20261103_194320 — 网络状态桥接 + 签名 streak（Sympy 单实例，0/1）

- 目的：验证 P0a（网络命令 status 桥接）+ P1（签名 streak）+ P2（网络感知
  directive）是否让 Sympy 在预算内产出 diff，并修复上一轮的空 diff。
- 命令：`uv run --no-sync python evals/swebench/suite.py --hard sympy__sympy-20639 --persona coding --keep-instances --instance-cache-dir /tmp/swebench-instance-cache`
- 结果：**0/1**；281.2s；tokens 3,472,950 / 16,953；估算 $1.762。
- 改善：diff 非空（`sympy/printing/pretty/pretty.py`，897 B），不再像上一轮那样
  0 diff；主代理仍耗尽 90 次模型调用。
- 新失败：F2P/P2P 都失败。P2P 的 `test_pretty_sqrt` 期望 `2**(1/x)` 仍使用根号
  表示，代理新增的 `e.is_Rational` 条件把这种非有理指数的符号形式也排除了，输出
  变成普通分数指数格式。
- 日志：ToolGuardrails 在 terminal 上触发 3 次 WARN（签名 streak），但它是
  WARN-only，达到 `streak_max_warnings_per_turn=3` 后不再有动作；主代理继续探测到
  budget exhausted。
- 修改建议：
  1. **Sympy 正确性**：不要用 `e.is_Rational` 作为根号路径的总开关；需要保留
     `2**(1/x)` 这类 `d` 为符号 Atom 的根号打印，只排除 `exp(-1)` 等非根号表达式。
     先跑目标 P2P（`test_pretty_sqrt`）再收口。
  2. **收敛强度**：签名 streak 连续命中并耗尽 3 次警告后，应升级为 BLOCK（或至少
     阻断当前 terminal），否则 agent 仍可用到 90 次；这与当前计划的 WARN-only
     设计冲突，需要单独评审。
  3. **预算**：在 diff 已存在但目标测试失败时，harness 应优先要求运行目标测试，
     而不是继续全局探测；考虑在 70-80% 预算时注入一次强制的 `patch_file` /
     target-test checkpoint。
  4. DeepSWE 仍未运行（SWE 未全通过）。

---

## 20261103_203547 — 签名 BLOCK 后改用探针文件（0/1）

- 目的：验证“警告预算用尽后拦截精确签名”是否终止同一 terminal 命令的重复。
- 命令：`uv run --no-sync python evals/swebench/suite.py --hard sympy__sympy-20639 --persona coding --keep-instances --instance-cache-dir /tmp/swebench-instance-cache`
- 结果：**0/1**；196.7s；96 次工具调用；90 次模型调用耗尽。
- 观察：同一 terminal 命令在约 20 次时被精确签名 BLOCK；agent 随即改写
  `probe1.py`…`probe19.py`，每次参数不同、输出逐字节相同，签名检测无法命中。
  这些探针文件还被算进了 diff，`pretty.py` 一次都没改。
- 修改建议：增加“非幂等工具连续返回相同结果”的检测（结果重复），并在
  80% 预算注入一次收敛指令。

---

## 20261103_205218 — 结果重复 + 80% 收敛指令（0/1）

- 目的：验证结果重复拦截和预算收敛指令。
- 命令同上。
- 结果：**0/1**；185.0s；91 次工具调用；90 次模型调用耗尽。
- 观察：terminal 在第 10 次调用时因连续 6 次相同结果被 BLOCK；python_repl
  在第 30 次时同样被 BLOCK；80% 指令在 72/90 时注入。agent 在第 72 次才
  `patch_file`，但 terminal/python_repl 仍处于封锁状态，随后把剩余 18 次调用
  花在 `read_file` 与已被拦截的 python_repl 之间，无法验证补丁。
- 修改建议：成功的源码编辑（`patch_file` 的 `clears_guardrail_blocks` metadata）
  应清空旧探针留下的所有 block/streak，让 agent 能验证新补丁；新建探针文件的
  `write_file` 不携带该标记，不能重新打开循环。

---

## 20261103_210023 — 源码编辑解除拦截（0/1，收敛正常）

- 目的：验证“patch_file 成功后解除拦截”能否让 agent 在预算内完成编辑→验证循环。
- 命令同上。
- 结果：**0/1**；92.4s；33 次工具调用；仅 28 次模型调用，未触发任何 ToolGuardrails
  事件，也未耗尽预算。
- 观察：agent 正常定位 `_print_Pow`，一次 patch 后主动运行 `test_pretty.py`；
  P2P 通过，F2P 失败。补丁把根号路径限定为 `e.is_Rational` 或
  `e.is_Pow and e.exp is S.NegativeOne and e.base is d`，修好了报告中的
  `pi**(1/E)`（`exp(-1)` 不是 Pow），但没有处理 `pi**(1/pi)`、`pi**(1/EulerGamma)`。
- F2P 分析：`test_issue_17616` 要求 NumberSymbol 分母（pi/E/EulerGamma）退回
  指数记法，而 Symbol 分母（`7**(1/x_17)`）保留根号；test patch 同时把
  `test_issue_6285` 中 `Pow(x, 1/pi)` 的旧期望（根号）改成指数记法。仓库里
  现有的 `test_issue_6285` 断言的恰是**旧行为**，因此这个语义选择无法仅凭
  issue + 现有测试推导出来；本轮失败是信息缺口/模型泛化不足，不是循环或预算。
- 修改建议：
  1. 若要让该实例可解，harness 需要在运行前暴露目标测试（或至少给出
     NumberSymbol vs Symbol 的期望矩阵），否则模型只能猜维护者的语义选择。
  2. 当前收敛链路已正常：签名拦截、结果重复拦截、80% 指令、编辑后解除拦截
     都在对应场景生效；继续保留。
  3. DeepSWE 仍未运行（SWE 未全通过）。
