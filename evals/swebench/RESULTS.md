# SWE-bench 运行记录（2026-11-01 起）

本文件保留 2026-11-01 起的运行记录。更早的 arm 已从失败历史中移除，完整分析见
`wiki/evals/SWE-Bench/`。原始报告（gitignored）在
`evals/results/swebench/<run_id>/`。

> **2026-11-08 切片变更：** `sympy__sympy-20639` 已从可运行 HARD 切片移除；
> 其隐藏测试语义无法从 issue 与修复前测试推导，属于模型能力分歧而非 harness
> 失败。后续 `--hard` 运行 `django__django-17051` 与 `django__django-16820`
> （两者均通过 gold check）；Sympy 历史运行仅作记录。
>
> **保留策略：** 报告、diff、`instance_results` 与 `sandbox/workspace` 保留；
> `sandbox/src/checkpoints` 和 `shadow/` 是派生会话状态（单次可达数百 MB），
> 会拖慢仓库级搜索测试的预算，跑完即删。

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
     而不是继续全局探测。
  4. DeepSWE 仍未运行（SWE 未全通过）。

---

## 20261103_203547 — 签名 BLOCK 后改用探针文件（0/1）

- 目的：验证“警告预算用尽后拦截精确签名”是否终止同一 terminal 命令的重复。
- 命令：`uv run --no-sync python evals/swebench/suite.py --hard sympy__sympy-20639 --persona coding --keep-instances --instance-cache-dir /tmp/swebench-instance-cache`
- 结果：**0/1**；196.7s；96 次工具调用；90 次模型调用耗尽。
- 观察：同一 terminal 命令在约 20 次时被精确签名 BLOCK；agent 随即改写
  `probe1.py`…`probe19.py`，每次参数不同、输出逐字节相同，签名检测无法命中。
  这些探针文件还被算进了 diff，`pretty.py` 一次都没改。
- 修改建议：增加“非幂等工具连续返回相同结果”的检测（结果重复）。

---

## 20261103_205218 — 结果重复 + 编辑后解锁（0/1）

- 目的：验证结果重复拦截，以及编辑后能否恢复验证。
- 命令同上。
- 结果：**0/1**；185.0s；91 次工具调用；90 次模型调用耗尽。
- 观察：terminal 在第 10 次调用时因连续 6 次相同结果被 BLOCK；python_repl
  在第 30 次时同样被 BLOCK。agent 在第 72 次才 `patch_file`，但
  terminal/python_repl 仍处于封锁状态，随后把剩余 18 次调用花在 `read_file`
  与已被拦截的 python_repl 之间，无法验证补丁。
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
  2. 收敛链路保留签名拦截、结果重复拦截、编辑后解除拦截。
  3. DeepSWE 仍未运行（SWE 未全通过）。

---

## 20261104_003521 — 目标测试写进 prompt（0/2）

- 目的：按计划建议，把实例的 test patch 和 FAIL_TO_PASS 名称放进任务 prompt（checkout 不预打补丁）。
- 命令：`uv run --no-sync python evals/swebench/suite.py --hard --persona coding --keep-instances --instance-cache-dir /tmp/swebench-instance-cache`
- 结果：**0/2**；396.5s；Django / Sympy 各 90 次模型调用耗尽；`target_tests_exposed=true`。
- 观察：Django 产出源码候选补丁但仍未通过；Sympy 只写了 `repro_issue.py`，没有改
  `pretty.py`。仅仅把测试放进 prompt 还不够，agent 无法真正运行目标测试。

## 20261104_004714 — 目标测试预先应用到 checkout（1/2）

- 目的：把 test patch 在 agent 运行前应用到 checkout，并按计划建议把 F2P 名称/测试内容写进 prompt；判题前重置测试文件再按官方 test patch 判题。
- 命令同上。
- 结果：**1/2**；487.5s；Django **resolved=True**（F2P/P2P 全通过）；Sympy 0/1；总成本约 $3.57。
- 观察：Django 的 90 次调用耗尽，但最终补丁正确；Sympy 在 `_print_Pow` 使用了
  `e.is_Symbol` 条件，`1/x_17` 是 `Pow` 而不是 Symbol，P2P/F2P 都失败；它还写了一个
  45 行的 `repro_issue.py` 并反复重写/运行，未再回到库源码。
- 结论：暴露并预应用目标测试对 Django 有效；Sympy 剩余问题是模型正确性/收敛能力，
  不是测试不可见。后续单独重跑 Sympy（20261104_005721）仍 0/1。

---

## 20261108_111407 — HARD Django only; target F2P+P2P in prompt（1/1）

- 目的：移除有隐藏语义争议的 Sympy 实例后，在通用 prompt 中要求编辑前阅读
  FAIL_TO_PASS 和 PASS_TO_PASS 测试体，并显式列出两组测试名；重跑 HARD 切片。
- 命令：`uv run --no-sync python evals/swebench/suite.py --hard django__django-17051 --persona coding --keep-instances --instance-cache-dir /tmp/swebench-instance-cache`
- 结果：**1/1 resolved**；254.1s；94 tool calls；tokens 4,071,258 / 14,569；
  估算 $2.057；`changed_paths` 包含 `django/db/backends/{base,postgresql,sqlite3}/features.py`
  和 `django/db/models/query.py`，另有 agent 自建的 `dbg_tmp.py`（非测试文件，未判 dirty）。
- 对照：`20261108_105816` 同实例、同 prompt 首跑 0/1；F2P 5/5 通过，但 P2P
  `test_ignore_conflicts_ignore` 报 `'NoneType' object is not iterable`。该次运行的
  `final_text` 是 “The iteration budget (90) has been exhausted”，即 92 次工具调用
  后被预算截断在修复中途——这是“回合不够”型失败，不是模型不知道该改哪里。
  第二次运行 1/1 同样在预算耗尽时结束（补丁已先完成），说明该实例的调用需求就在
  90 附近，单次结果受方差支配。
- 结论：HARD 现在只跑 Django；Sympy 历史保留但不再计分。DeepSWE 的“SWE 全通过”
  门现在满足，但 DeepSWE 本身仍未验证。

---

## 20261108_133112 — temperature=0 pinned（Django，隔离失效，结果不计入）

- 目的：把 eval 主模型温度固定为 0，消除两次 Django 运行之间最明显的采样方差；
  同时保留 prompt 中的 F2P+P2P 列表。
- **有效性：无效。** `pin_eval_temperature()` 在 `SHERRY_WORKSPACE_DIR` 覆盖与
  `config.path` 初始化之前导入了 `server.service.messages`/`config.path`，把
  `WORKSPACE_DIR` 冻结回 live workspace：报告 isolation 为
  `eval_workspace_has_sessions=false`、`live_sessions_unchanged=false`，因此本次
  1/1 不能作为能力证据。修复：pin 移到 workspace 覆盖与 config 初始化之后。
- 命令：`uv run --no-sync python evals/swebench/suite.py --hard django__django-17051 --persona coding --keep-instances --instance-cache-dir /tmp/swebench-instance-cache`
- 结果：**1/1 resolved**；168.2s；57 tool calls；tokens 2,195,175 / 8,452；
  估算 $1.110；`resumes=0`；改动 `django/db/models/query.py` 与
  `django/db/models/sql/compiler.py`，无测试文件、无 scratch 文件。
- 对照：不固定温度时同实例两次为 0/1（437s / 92 calls）和 1/1（254s / 94 calls）；
  固定温度后本次为 1/1（168s / 57 calls）。这是单次实验，不能单独证明稳定性，
  但说明 temperature 是主要方差源之一。
- 注：HARD 切片新增的 `django__django-16820` 尚未完成 host gold check（网络拉取
  超时），暂不把它的结果计入能力结论。

---

## 20261108_133743 — temperature=0，pin 顺序修正后（Django，1/1，有效）

- 目的：修正 `pin_eval_temperature()` 的导入顺序——在 `SHERRY_WORKSPACE_DIR`
  覆盖与 `config.path` 初始化之后再 pin，恢复 eval 沙箱隔离，然后重跑同实例。
- 命令：`uv run --no-sync python evals/swebench/suite.py --hard django__django-17051 --persona coding --keep-instances --instance-cache-dir /tmp/swebench-instance-cache`
- 结果：**1/1 resolved**；60.6s；17 tool calls；tokens 395,375 / 2,346；
  估算 $0.201；`resumes=0`；只改 `django/db/models/query.py`，无测试文件、无临时文件。
- 隔离：`eval_workspace_has_sessions=true`、`live_sessions_unchanged=true`；
  `model.temperature=0.0`、`thinking_enabled=false`。
- 观察：agent 在 17 次调用内完成定位→编辑→运行 `bulk_create` 全量测试（52 个，
  7 skipped），并在最终答复中明确报告 F2P 5/5 与全部 PASS_TO_PASS 通过。与不固定
  温度时的 57～94 次调用相比，确定性解码同时降低了方差和调用量。
- 结论：temperature=0 的收益在有效隔离下成立；单次样本不足以证明稳定性，下面用
  三次重复补齐。`django__django-16820` 的 gold check 另行记录。

---

## 20261108_134640 / 135057 / 135604 — temperature=0 重复三次（Django，3/3）

- 目的：单次 1/1 不能证明稳定；在 temperature=0、隔离有效的前提下重复同一实例，
  观察调用量与结果的方差。
- 命令同 133743（每次独立 run_id；checkout/venv 复用实例缓存）。
- 结果：**3/3 resolved**，且四次有效 temperature=0 运行（含 133743）全部 1/1：

  | run | calls | 时长 | resolved |
  | --- | --- | --- | --- |
  | 133743 | 17 | 60.6s | 1/1 |
  | 134640 | 69 | 235.8s | 1/1 |
  | 135057 | 82 | 286.3s | 1/1 |
  | 135604 | 16 | 63.1s | 1/1 |

  全部 `eval_workspace_has_sessions=true`、`live_sessions_unchanged=true`。
- 观察：temperature=0 下结果稳定，但**调用量并不确定**（16～82 次，约 5 倍差）。
  最长的一次用掉 90 次预算中的 82 次——如果轨迹再长一点，就会再次落入
  “预算耗尽、补丁未收敛”的失败模式。
- 结论：
  1. 该实例的稳定性来自“确定性解码 + 编辑后跑全量目标测试”，不是来自回合数；
  2. “加回合”只对**截断型**失败有效（105816 的 0/1 正是 90 次预算在修复中途耗尽）；
     它不修复错误补丁，也不保证语义分歧的实例收敛；
  3. 对长轨迹实例，90 次预算余量偏薄，可考虑按实例给出更大的上限，但应作为
     安全边际而不是收敛策略。

---

## 20261108_145011 — 新增 HARD 实例 django__django-16820，90 次预算（0/1）

- 目的：验证新增 HARD 实例在真实 agent 下的行为；temperature=0、隔离有效、
  gold check 已通过（F2P 5/5、P2P 91/91）。
- 命令：`uv run --no-sync python evals/swebench/suite.py --hard django__django-16820 --persona coding --keep-instances --instance-cache-dir /tmp/swebench-instance-cache`
  （本次 checkout 由 codeload tarball 重建：git fetch 在代理下两次超时）。
- 结果：**0/1**；263.7s；92 tool calls；tokens 5,104,262 / 10,018；估算 $2.567；
  `final_text` 为 “The iteration budget (90) has been exhausted”，即再次在修复
  中途被预算截断。F2P 5/5 通过，P2P 未通过；改动
  `django/db/migrations/{autodetector,operations/models,state}.py`。
- P2P 输出异常：Django runner 在子进程回报失败时抛
  `TypeError: cannot pickle 'traceback' object`，`judge_detail` 只保留了尾部，
  无法从报告定位具体失败测试；这属于 runner 的报告缺陷，判题仍是失败。
- 有效性：`eval_workspace_has_sessions=true`、`live_sessions_unchanged=true`；
  `temperature=0.0`、`thinking_enabled=false`。
- 结论：该实例把“回合预算”从理论问题变成了实测约束——90 次调用不够它走完
  修复→全量 P2P 验证。下一次用 `SHERRY_EVAL_MAX_ITERATIONS=180` 重跑同一实例，
  直接检验“加回合能否收敛”。

---

## 20261108_184144 — 同实例，180 次预算（1/1）

- 目的：直接检验“加回合能否收敛”。环境与上一条相同（temperature=0、隔离有效、
  同一 checkout / venv / gold check），只把主模型调用上限从 90 提到 180。
- 命令：`SHERRY_EVAL_MAX_ITERATIONS=180 uv run --no-sync python evals/swebench/suite.py --hard django__django-16820 --persona coding --keep-instances --instance-cache-dir /tmp/swebench-instance-cache`
- 结果：**1/1 resolved**；96.5s；**24 tool calls**；tokens 715,973 / 3,007；
  估算 $0.362；F2P 5/5、P2P 91/91；只改
  `django/db/migrations/operations/models.py`，无测试文件改动。
- 报告新增字段：`budget.main_max_iterations=180`；`unrunnable_test_ids` 为
  `{"fail_to_pass": 2, "pass_to_pass": 112}`——数据集里混入的测试描述文本不是
  合法 Django 标签，被 harness 丢弃并如实上报（实际运行 5 + 91 个）。
- 关键观察：180 的预算并没有“让”它成功——它在 24 次调用内就完成了修复并跑完
  `migrations` 全量 722 个测试。与上一条 92 次耗尽预算的轨迹相比，同样的代码、
  同样的 temperature=0，轨迹长度相差近 4 倍。也就是说：
  1. 90 的预算确实能截断一条长轨迹（上一条的失败模式），提高上限可以消除这个
     截断风险；
  2. 但“加回合”不是收敛的充分条件，也不能归因本次成功——模型这次走了短路径；
  3. 真正可复用的部分是编辑后的全量目标测试验证（prompt 已要求），预算只是
     安全边际。
