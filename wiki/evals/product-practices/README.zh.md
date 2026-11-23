# 🧪 产品侧的评测实战经验

> [English](README.md) · 中文 · [한국어](README.ko.md) · [日本語](README.ja.md)

> 六项机制最初来自评测框架的加固（DeepSWE / external-bench），现在是产品的一等公民行为。每一项都是 post-task 或 fail-open：都不会中断回合，且都源自一次实测到的评测失败模式。权威来源：每节列出的文件。

## 1. 叶子超时 1800 秒（`agent/tools/subagent/config.py`）

`run_timeout_seconds` 默认改为 **1800 秒**（原 600 秒）。DeepSWE 实测：600 秒时 20 题中解决 9 题，7200 秒时 20/20——瓶颈是子进程在读取-实现-测试中途被杀，不是模型能力。深度梯子每上一层乘以 3（depth-1 = 5400 秒）。`evals/deepswe/suite.py` 设 `_EVAL_CHILD_RUN_TIMEOUT_MULTIPLIER = 1.0`（产品默认已覆盖评测预算，不再额外 ×3）。显式覆盖值仍受最小 600 / 最大 7200 钳制。

## 2. Memory 字符上限按 CJK 加权（`agent/tools/memory.py`）

Memory 的字符上限（2200 / 1375 / 1375）现在通过 `_weighted_char_count` 把一个 CJK 字符按 `chars_per_token / chars_per_token_cjk`（4/2 = 2）个加权字符计算，复用 `pub/func/cjk.py::count_cjk` 与 token 估算配置。ASCII 容量不变；CJK 内容现在占用相同的 token 预算，不再超支 2 倍。所有上限检查（add / replace / evict / `_append_to_target`）与用户可见的 `[n% — X/Y chars]` 指示器都使用加权计数。空值或失败计数保持 fail-open。

## 3. 跨回合 diff-hash 无进展检测（`agent/tools/todolist/stagnation_tracker.py`、`agent/middlewares/todo_continuation/core.py`）

计划文本停滞检测看不出模型一边编辑 todo、一边代码补丁完全不变（"closure-flow 空转"）。每个 continuation 回合结束时，enforcer 在事件循环外对已暂存 + 未暂存的二进制 git diff 做哈希（`_working_tree_diff_hash`）。若连续 `TODOLIST_INFRA["diff_stagnation_threshold"]`（默认 2）个 continuation 回合哈希相同、且**没有 step 子进程在飞**，则注入 `[SYSTEM DIRECTIVE: NO PROGRESS DETECTED]` 并锁定停止。真实用户回合或有子进程在飞会重置基线；非 git 目录返回空哈希（fail-open，绝不误判）。与计划文本停滞检测并行，任一触发都会停止门禁。

## 4. Verify 完成前的 settle 等待（`config/features/agent_side/todolist_infra.py`、`agent/middlewares/todo_continuation/core.py`）

step 子进程可能报告 `done`，但文件编辑仍在写盘。此时完成 verify 会产生假完成。当 reviewer 已完成且有 step 子进程在飞时，门禁等待最多 `verify_settle_wait_s`（默认 30 秒；评测用 180 秒）让其 settle。超时则在 verify 指令追加 `[WARNING: SUB-TASKS STILL RUNNING]`——完成绝不阻塞，但模型会被告知补丁可能不完整。流程读取失败降级为"已 settle"（fail-open）。

## 5. Reviewer 验收清单（`agent/tools/taskflow/checklist_extractor.py`、`agent/tools/taskflow/tools/_dispatch.py`）

TaskFlow step 中以 `functional_role="reviewer"` spawn 的 reviewer，现在会在 task 末尾收到编号的 PASS/FAIL **ACCEPTANCE CHECKLIST**，从规范性句子（must / should / shall / raise / reject / only / never / charset / return / error 等）提取，上限 20 条、去重、大小写不敏感。由 `TASKFLOW_INFRA["reviewer_checklist_enabled"]` 控制（默认开）。只改 task 文本——reviewer 的模型、工具与隔离上下文均不变。幂等：已含 checklist 标记的 task 不会重复追加（评测侧 `set_reviewer_checklist` 仍然优先）。提取失败 fail-open。

## 6. Session 运行报告（`pub/func/session_report/**`、`server/service/session_report_service.py`、`server/trigger/http/session_report.py`）

三个 post-task 分析器从评测侧迁移到产品共享层——不在热路径插桩：

- **token tracker**——把 `usage_metadata` 聚合为主 agent + 各角色子 agent 分解；仅当 `SHERRY_SESSION_REPORT_TOKEN_PRICES`（或旧评测变量）指向有效价格文件时才输出 USD 成本；
- **context analyzer**——按调用对工具调用做 HIT/WASTED 打分（对照任务关键词）；`hit_rate = effective / (effective + wasted)`；规划/编排工具不计分；无法判断的算 HIT（保守）；
- **code-intel tracker**——精确检索（LSP / explore / callers / semantic）与粗暴检索（search_files / read_file）的调用比率，并从 fail-open 的 ToolMessage 文本检测降级调用。

报告在首次 API 读取时生成并缓存在进程内（无回合结束后台任务、无状态寄存器写入）；`GET /sessions/:session_id/report`（或 `?session_id=`、`?refresh=1`）在缓存未命中时实时生成。客户端在上下文查看器中以折叠面板展示（`client/app/components/chat/SessionReport.vue`）。进程重启后下次读取自动重建缓存。

## 安全不变量

- 六项都是**追加式**：没有不兼容地改变任何既有提示词契约、工具 schema、状态字面量或回合生命周期钩子（报告缓存是进程内的，没有新增状态寄存器 key）。
- 所有读取路径都 **fail-open**：git 缺失、store 故障、提取失败或 transcript 不可用都会降级为"无信号"，绝不硬失败。
- 阈值统一放在 `TODOLIST_INFRA` / `TASKFLOW_INFRA`；热路径里没有魔法数字。
