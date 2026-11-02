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
