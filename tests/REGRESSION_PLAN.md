# 全套回归方案（Regression Test Plan）

> 覆盖范围：sherry_agent 全部后端功能域 + 用户操作模拟 + 边界情况。
> 生成日期：2026-09-06。运行器：`tests/run_tests_split.py`（组 C = 本目录）。

---

## 1. 运行方式

```bash
# 只跑回归组
uv run pytest tests/regression -q

# 全套分进程回归（A: unit → B: integration/system/module → C: regression）
uv run python tests/run_tests_split.py

# 按场景过滤
uv run pytest tests/regression -k "UC-03 or cron" -q
```

标记约定：`pytestmark = [pytest.mark.module, pytest.mark.timeout(60)]`（组 B/C 同进程约束一致）。

---

## 2. 功能域 → 测试映射矩阵

| # | 功能域 | 关键行为 | 已有测试（引用） | 本目录回归流 |
|---|--------|----------|------------------|--------------|
| F1 | 消息持久化 | add_messages / ts_ms 严格递增 / 14 位显示戳 | `tests/module/test_store_timestamp_ordering.py` | UC-01, EC-11 |
| F2 | 历史检索 | turn 分页 / 越界 / 边界钳制 | `tests/module/test_fts5_recall.py`（部分） | UC-04, EC-03 |
| F3 | 会话列表 | MAX(ts_ms) 排序 / 子代理排除 / 标题派生 | `tests/module/test_get_session_ids.py` | UC-02 |
| F4 | 会话清空 | delete_messages_by_session + FTS 触发器联动 | — | UC-03 |
| F5 | FTS5 搜索 | 三路路由（unicode61/trigram/LIKE）+ 注入上限 | `tests/module/test_fts5_recall.py`, `test_fts5_query_caps.py` | UC-05, EC-04 |
| F6 | 时间戳碰撞 | 同秒/同毫秒单调递增 | `test_store_timestamp_ordering.py` | UC-15, EC-11 |
| F7 | cron 调度 | at/every/cron 三型 / 固定相位网格 / 漂移修复 | `tests/unit/cron/test_cron_interval_drift.py`, `test_cron_failure_breaker.py` | UC-07, UC-08, EC-06, EC-07 |
| F8 | HITL 审批 | 6 层管道（hardline/deny/YOLO/allowlist/session/dangerous） | `tests/unit/test_hitl_integration.py`, `test_hitl_sandbox_bypass.py` | UC-06, EC-15 |
| F9 | 多模态输入 | base64 落盘 / 提示词注入 / 历史剥离 / 过期清理 | — | UC-09, EC-16 |
| F10 | 迭代预算 | wrap_model_call 计数 / 耗尽终态消息 / 回合重置 | — | UC-11, EC-08 |
| F11 | 工具护栏 | 5 病理 × ALLOW/WARN/BLOCK/HALT + 恢复模式 | `tests/unit/middlewares/test_tool_guardrails.py`（17 个） | UC-12, EC-10 |
| F12 | 重复输出守卫 | 跨调用 / 字符连跑 / 短语重复 / 流级检查 | `tests/unit/middlewares/test_tool_guardrails.py`（部分）、`stream_repetition_guard_wrapper` | UC-13 |
| F13 | 心跳看门狗 | 停滞计数 / 击杀 / 超时异常 | `tests/unit/heartbeat/test_heartbeat_backoff.py` | UC-14 |
| F14 | 消息总线 | 有界双队列 / 背压 / 顺序 | — | UC-16, EC-09 |
| F15 | 状态注册表 | mem/db 双寄存器 / clear_all 联动 | `tests/unit/state/` | EC-05 |
| F16 | 技能加载 | 扫描 / 第三方默认停用 / 快照缓存 / 循环导入消除 | `tests/unit/skills/test_loader.py`, `tests/module/test_skill_scope.py` | EC-12 |
| F17 | 模型共享件 | read_env_file_value / resolve_gguf_path / 三件套基类 | `tests/module/test_models_utils.py`, `test_base_local_llama.py` | EC-13 |
| F18 | robyn 服务 | 单进程固定 / 帧投递 / 持久化 | 人工 e2e（见 §5） | — |

---

## 3. 用户操作模拟场景（UC）

每个 UC 模拟一条真实用户操作路径，**全程封闭**（不联网、不下载、不依赖真实 LLM/GGUF）。

| UC | 用户操作 | 模拟方式（测试文件见 §5） |
|----|----------|--------------------------|
| UC-01 | 发消息 → 收回复 → 刷新历史 | 直接驱动 `add_messages`（human+ai 一回合）→ `get_history_by_turn_page` → 断言 turn 递增、内容往返 |
| UC-02 | 打开会话列表 | 两个会话不同活动时间 → `get_session_ids` 顺序/标题/子代理排除 |
| UC-03 | 清空当前会话 | `delete_messages_by_session` → 历史/列表双确认 |
| UC-04 | 上滑翻页历史 | 5 回合 × 每页 2 → 3 页全量覆盖、页序 newest-first、min_turn_num 钳制 |
| UC-05 | 全文搜索历史 | 造语料 → 正常词 / 64-token 截断 / 特殊字符注入 → 有界返回 |
| UC-06 | HITL 命令审批 | `ApprovalPipeline.check_command`：hardline 拒 / 危险升级 / 普通放行 / YOLO 语义 / deny_rules |
| UC-07 | 新建定时任务 | `add_job(every)` → `run_job(force)` 执行回调 + next 槽位推进网格 → `remove_job` |
| UC-08 | 定时任务到期批量触发 | 两个到期任务 → `_on_timer` 全执行 + 槽位重排 |
| UC-09 | 上传图片/语音 | `_before_agent_impl`：base64 → 落盘 + 提示词注入 + kwargs 持久化 + 旧消息剥离 |
| UC-10 | 渠道收发消息 | `MessageBus` 双队列 publish/consume 顺序 |
| UC-11 | 预算耗尽被终止 | `IterationBudget.wrap_model_call`：N 次后终态 AIMessage、handler 不再调用、新回合重置 |
| UC-12 | 工具连续失败被拦截 | `ToolGuardrails.wrap_tool_call`：失败 ×2 警告 → ×5 预检拦截（legacy 模式） |
| UC-13 | 模型复读被切断 | `check_stream_repetition`：字符连跑 → 一次性警告 → 去重门 |
| UC-14 | 卡死回合被心跳击杀 | `_check_progress` 停滞累计 → killed → `wrap_model_call` 抛 HeartbeatTimeoutError |
| UC-15 | 同秒多会话排序 | 同秒两个会话 → `MAX(ts_ms)` 稳定排序（#21 回归） |
| UC-16 | 渠道满载背压 | 队列填满 → publish 挂起不丢失 → 消费后恢复 |

---

## 4. 边界情况清单（EC）

| EC | 边界 | 期望 |
|----|------|------|
| EC-01 | `add_messages([])` / `None` | 静默 no-op，不写库 |
| EC-02 | 未知 role 的消息 | 跳过不崩溃 |
| EC-03 | 历史页码越界 / `turn_page_num=0` | 空列表 / pydantic ValidationError（ge=1） |
| EC-04 | FTS5：空查询 / 纯特殊字符 / 未闭合引号 / 全通配 | `[]` 或有界结果，绝不抛出 |
| EC-05 | 状态注册：空白 session_id、缺键删除、跨寄存器 clear_all | 防御性返回，不崩溃 |
| EC-06 | cron：`every_ms=0`、过期 `at` | `next_run_at_ms=None`（永不触发） |
| EC-07 | breaker：5 连败降级跳过、10 连败自动停用落盘、成功重置 | 断路器状态机完整 |
| EC-08 | 预算恰好用尽 vs 超出 1 次 | 第 N 次放行、第 N+1 次终态 |
| EC-09 | 总线 `maxsize=0` | ValueError（拒绝无界队列） |
| EC-10 | 护栏：幂等无进展 / ping-pong 打断重置 / 参数变体抖动 | 各病理独立计数与重置 |
| EC-11 | 同毫秒时间戳 / 14 位垃圾回填 | +1ms 单调 / 回填 0 不崩溃 |
| EC-12 | 技能：lookalike 路径、损坏状态文件 JSON | 段级判定 / 防御性空 dict |
| EC-13 | .env：空值行不吞下一行、export 前缀、引号剥离 | `read_env_file_value` 语义 |
| EC-14 | 快照文件缺失 → None；损坏 JSON | 与历史行为一致 |
| EC-15 | HITL deny_rules glob 命中 | 层 2 拒绝优先于 YOLO |
| EC-16 | 多模态：非数字文件名 / 7 天过期清理 | 直接删除 + 过期删除 |

---

## 5. 文件结构

```
tests/regression/
├── __init__.py
├── test_user_flows.py      # UC-01 ~ UC-16（用户操作模拟，封闭）
└── test_edge_cases.py      # EC-01 ~ EC-16（边界情况）
```

## 6. 失败处理约定

1. 回归失败 → 先对照 §2 矩阵定位功能域 → 打开对应单元测试文件缩小范围。
2. 修复后必须让**整组 C** 通过（回归测试之间共享行为约定）。
3. 新功能合入时：先在 §2 矩阵登记引用测试，再补 UC/EC 用例。
4. 预先存在的环境性失败（不纳入本方案，已 stash 验证在 HEAD 上同样存在）：
   - `tests/unit/test_rag_anything_integration.py` / `tests/unit/test_snkv_storage.py` — 导入链需要 embed 权重文件 + HF 网络；权重缺失的机器上收集期报 `cannot import name 'build_embed_model'`，导致 `pytest tests/unit` 整组收集 INTERNALERROR（这也是 split runner GROUP A rc=2 的原因）。修复方向：将二者的 graph_rag 导入改为延迟/权重就绪探测。
   - `tests/module/test_python_repl_tool.py::test_popen_gets_scrubbed_env` — 本机 bwrap 可用时沙箱 wrap 产生 2 次 Popen（外层 bwrap + 内层命令），测试写死 1 次。
   - `tests/module/test_main_agent_e2e.py` — 真 LLM 网络测试，偶发。
   - 运行 GROUP A 时临时绕过：`pytest tests/unit -q --ignore=tests/unit/test_rag_anything_integration.py --ignore=tests/unit/test_snkv_storage.py`
