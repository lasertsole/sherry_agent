# 防护能力全面对比报告：Sherry Agent vs DeepAgents

> 生成日期：2026-09-16
> 对比范围：`D:\selfProj\sherry_agent` vs `D:\selfProj\deepagents-main`

---

## 目录

1. [总览对比矩阵](#1-总览对比矩阵)
2. [逐维度详细对比](#2-逐维度详细对比)
   - 2.1 [工具调用护栏](#21-工具调用护栏)
   - 2.2 [输入验证与消毒](#22-输入验证与消毒)
   - 2.3 [输出重复防护](#23-输出重复防护)
   - 2.4 [上下文/Token 限制](#24-上下文token-限制)
   - 2.5 [迭代/步骤限制](#25-迭代步骤限制)
   - 2.6 [HITL 人工审批](#26-hitl-人工审批)
   - 2.7 [子代理安全](#27-子代理安全)
   - 2.8 [资源限制与并发控制](#28-资源限制与并发控制)
   - 2.9 [错误处理与重试](#29-错误处理与重试)
   - 2.10 [记忆/压缩安全](#210-记忆压缩安全)
   - 2.11 [输出安全](#211-输出安全)
   - 2.12 [会话安全](#212-会话安全)
   - 2.13 [超时处理](#213-超时处理)
   - 2.14 [其他防护机制](#214-其他防护机制)
3. [能力差异总结](#3-能力差异总结)
   - 3.1 [Sherry 独有但 DeepAgents 缺失](#31-sherry-独有但-deepagents-缺失)
   - 3.2 [DeepAgents 独有但 Sherry 缺失](#32-deepagents-独有但-sherry-缺失)
4. [架构理念对比](#4-架构理念对比)

---

## 1. 总览对比矩阵

| 防护维度         |                    Sherry Agent                     |                        DeepAgents                         |     优势方     |
| ---------------- | :-------------------------------------------------: | :-------------------------------------------------------: | :------------: |
| 工具调用病理检测 |                ✅ 5种病理+4级升级链                 |                     ❌ 仅悬空调用修补                     |   **Sherry**   |
| 输入路径验证     |                    ✅ 会话ID+URL                    |              ✅ 路径遍历+O_NOFOLLOW+符号链接              | **DeepAgents** |
| 输出重复防护     |              ✅ 3层(中间件+包装器+流)               |                       ❌ 无显式机制                       |   **Sherry**   |
| 上下文压缩       |             ✅ T1-T5触发+防抖动+压缩锁              |               ✅ 自动摘要+溢出裁剪+消息驱逐               |  **各有千秋**  |
| 迭代限制         |                   ✅ 角色50/90/60                   |                   ✅ 图递归9999+Rubric3                   |  **各有千秋**  |
| HITL审批         |               ✅ 14层门控+47+危险模式               |               ✅ 权限→HITL桥接+路径感知谓词               |  **各有千秋**  |
| 子代理安全       |            ✅ 深度+CWD+工具继承+最小权限            |               ✅ 类型验证+递归拒绝+权限继承               |  **各有千秋**  |
| 并发控制         |               ✅ 4车道Semaphore+排空                |                           ❌ 无                           |   **Sherry**   |
| LLM重试          |              ✅ 8步分类+回退链+断路器               |                    ❌ 仅上下文溢出重试                    |   **Sherry**   |
| 消息驱逐         |                      ❌ 仅截断                      |            ✅ 工具结果→文件系统+head/tail预览             | **DeepAgents** |
| 符号链接防护     |                        ❌ 无                        |                  ✅ O_NOFOLLOW+循环检测                   | **DeepAgents** |
| Shell注入防护    |                ✅ 危险命令正则黑名单                |               ✅ base64编码+花括号展开限制                | **DeepAgents** |
| 崩溃回路断路     |                   ✅ 5min窗口3次                    |                           ❌ 无                           |   **Sherry**   |
| 心跳过期检测     |               ✅ 空闲7min/工具内20min               |                           ❌ 无                           |   **Sherry**   |
| 威胁模型文档     |                        ❌ 无                        |                    ✅ THREAT_MODEL.md                     | **DeepAgents** |
| TODO停滞追踪     |                ✅ 指数退避+恢复模式                 |                           ❌ 无                           |   **Sherry**   |
| 压缩有效性追踪   |                 ✅ 连续2次无效→标记                 |                           ❌ 无                           |   **Sherry**   |
| 环境隔离         |                   ✅ scrub_env()                    |                 ✅ inherit_env=False默认                  |  **各有千秋**  |
| 沙箱执行         | ✅ OS级沙箱(bubblewrap/Seatbelt)+env清洗+HITL审批门 | ✅ QuickJS本地JS沙箱+云沙箱(Vercel/Modal/Daytona/Runloop) |  **各有千秋**  |
| 中间件脚手架保护 |                        ❌ 无                        |                   ✅ 必需中间件不可排除                   | **DeepAgents** |

---

## 2. 逐维度详细对比

### 2.1 工具调用护栏

| 对比项       | Sherry Agent                                                                   | DeepAgents                                        |
| ------------ | ------------------------------------------------------------------------------ | ------------------------------------------------- |
| 病理检测     | 5种：精确失败重复、同工具失败累积、幂等无进展、乒乓循环、参数搅拌              | 无                                                |
| 升级链       | ALLOW→WARN→BLOCK→HALT 4级                                                      | 无                                                |
| 恢复模式     | 第一个BLOCK进入恢复模式，允许1次重试                                           | 无                                                |
| 悬空调用修补 | `ToolCallNormalize` 修复消息配对                                               | `PatchToolCallsMiddleware` 注入错误ToolMessage    |
| 工具排除     | `inherited_tool_policy` 子代理工具继承策略                                     | `_ToolExclusionMiddleware` 从请求和执行边界过滤   |
| 危险命令拦截 | 正则黑名单(`rm -rf`/`mkfs`/`shutdown`等)                                       | `_wildcard_delete_overlap()` 阻止递归删除deny路径 |
| **结论**     | **Sherry 在工具调用病理检测方面远超 DeepAgents**，DeepAgents仅做了悬空调用修补 |

**关键文件**：

- Sherry: `agent/middlewares/tool_guardrails.py` + `config/features/agent_side/tool_guardrails.py`
- DeepAgents: `middleware/patch_tool_calls.py` + `middleware/_tool_exclusion.py`

---

### 2.2 输入验证与消毒

| 对比项         | Sherry Agent                                                                       | DeepAgents                                                             |
| -------------- | ---------------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| 会话ID防护     | ✅ `is_safe_session_id()` 拒绝路径分隔符+解析后路径验证                            | ❌ 无                                                                  |
| 路径遍历防护   | ⚠️ 仅会话ID层面                                                                    | ✅ `validate_path()` + `_resolve_path(virtual_mode=True)` 阻止`..`/`~` |
| 符号链接防护   | ❌ 无                                                                              | ✅ `O_NOFOLLOW` 全覆盖 + `_raise_if_symlink_loop()`                    |
| 命名空间验证   | ❌ 无                                                                              | ✅ `_validate_namespace()` 正则拒绝通配符注入                          |
| YAML安全加载   | ❌ 不适用                                                                          | ✅ `yaml.safe_load()` 防YAML代码执行                                   |
| Base64参数编码 | ❌ 无                                                                              | ✅ 所有sandbox shell命令使用base64编码参数                             |
| URL验证        | ✅ `is_url()` 12种scheme白名单                                                     | ❌ 无独立URL验证                                                       |
| 内容消毒       | ✅ `sanitize_content()` 去括号/CoT标签剥离                                         | ✅ HTML注释剥离(AGENTS.md)                                             |
| 整数强制转换   | ❌ 无                                                                              | ✅ `normalize_read_bounds()` 确保offset/limit安全                      |
| **结论**       | **DeepAgents 在文件系统安全防护方面显著优于 Sherry**，特别是路径遍历和符号链接防护 |

**关键文件**：

- Sherry: `pub/func/validator/session_id.py` + `pub/func/validator/is_url.py` + `pub/func/format/sanitize_content.py`
- DeepAgents: `backends/utils.py` + `backends/filesystem.py` + `backends/store.py` + `middleware/skills.py`

---

### 2.3 输出重复防护

| 对比项       | Sherry Agent                                                   | DeepAgents |
| ------------ | -------------------------------------------------------------- | ---------- |
| 中间件层检测 | ✅ `OutputRepetitionGuard` 跨调用+事后检测                     | ❌ 无      |
| 包装器层检测 | ✅ `RepetitionGuardWrapper` 流级别实时检测                     | ❌ 无      |
| 检测原语     | ✅ 句子/字符流/短语三子检测器                                  | ❌ 无      |
| 跨调用重复   | ✅ WARN(2次)→HALT(3次)，双头尾哈希                             | ❌ 无      |
| 内部重复     | ✅ 句子重复比>0.6、字符连续>=8次、短语>=5次                    | ❌ 无      |
| 推理文本重复 | ✅ 独立检查`additional_kwargs`中reasoning                      | ❌ 无      |
| 流状态机     | ✅ Fresh→UpdatesSeen→ModelText→Cut                             | ❌ 无      |
| 幻影流保护   | ✅ 第一个"updates"前的模型文本被丢弃                           | ❌ 无      |
| **结论**     | **Sherry 在输出重复防护方面全面领先，DeepAgents 无任何此能力** |

**关键文件**：

- Sherry: `agent/middlewares/output_repetition_guard.py` + `agent/wrapper/repetition_guard.py` + `agent/middlewares/repetition_detectors.py`
- DeepAgents: 无

---

### 2.4 上下文/Token 限制

| 对比项         | Sherry Agent                                                                   | DeepAgents                                              |
| -------------- | ------------------------------------------------------------------------------ | ------------------------------------------------------- |
| 触发机制       | T1-T5 5层(before_agent→model→post-response→413→overflow)                       | 自动摘要(85%触发阈值)                                   |
| 路由决策       | 4路由: FITS→TRUNCATE_TOOL→COMPACT_TRUNCATE→COMPACT_ONLY                        | 2路由: 摘要→截断                                        |
| 防抖动         | ✅ 压缩冷却3轮 + 最大3次/轮 + 总最大5次 + SQLite持久化                         | ❌ 仅恢复后重试1次                                      |
| 压缩有效性追踪 | ✅ 连续2次无效→标记不可用                                                      | ❌ 无                                                   |
| 压缩锁         | ✅ SQLite原子锁(TTL 300s)                                                      | ❌ 无(单进程无需)                                       |
| 强制压缩       | ✅ 80%窗口→`_FORCE_RECOVERY_KEY`                                               | ❌ 无显式强制                                           |
| 输出预算截断   | ✅ 累积输出>20%窗口→截断客户端视图                                             | ❌ 无                                                   |
| Token启动门控  | ✅ 强制≥128K                                                                   | ❌ 无                                                   |
| 截断恢复       | ✅ `MaxTokensBoost` 指数级max_tokens提升                                       | ❌ 无                                                   |
| 溢出尾部裁剪   | ❌ 仅截断                                                                      | ✅ `_clip_overflow_tail()` 尾部ToolMessage批量卸载      |
| 消息驱逐       | ❌ 仅截断工具结果                                                              | ✅ 工具结果→文件系统 + head/tail预览 + 内联媒体卸载     |
| 参数截断       | ❌ 无                                                                          | ✅ `TruncateArgsSettings` 摘要前截断旧工具参数          |
| 模型感知默认值 | ❌ 手动配置                                                                    | ✅ `compute_summarization_defaults()` 从模型profile计算 |
| 增量检查点优化 | ❌ 无                                                                          | ✅ `DeltaChannel(snapshot_frequency=50)` O(N²)→O(N)     |
| 人类消息驱逐   | ❌ 无                                                                          | ✅ `human_message_token_limit_before_evict`             |
| **结论**       | **Sherry 在触发精度和防抖动方面领先；DeepAgents 在消息驱逐和增量优化方面领先** |

**关键文件**：

- Sherry: `agent/middlewares/summarization.py` + `agent/wrapper/context_limit.py` + `config/features/agent_side/summarization.py`
- DeepAgents: `middleware/summarization.py` + `middleware/_overflow_clip.py` + `middleware/_message_eviction.py` + `_messages_reducer.py`

---

### 2.5 迭代/步骤限制

| 对比项         | Sherry Agent                                                            | DeepAgents                |
| -------------- | ----------------------------------------------------------------------- | ------------------------- |
| 默认限制       | 50                                                                      | 9,999                     |
| 主代理         | 90                                                                      | —                         |
| 工作代理       | 60                                                                      | —                         |
| 模型调用消耗   | ✅ `wrap_model_call` 消耗                                               | ❌ 图递归自动             |
| 工具调用消耗   | ✅ `wrap_tool_call` 消耗                                                | ❌ 图递归自动             |
| 内部完成豁免   | ✅ 内部完成通知轮次不消耗                                               | ❌ 不适用                 |
| 预算耗尽行为   | 返回终止AIMessage/ToolMessage                                           | 图递归限制→RecursionError |
| Rubric评分限制 | ❌ 无                                                                   | ✅ `max_iterations=3`     |
| **结论**       | **Sherry 更精细(角色分级+豁免机制)；DeepAgents 更简单(单一图递归限制)** |

**关键文件**：

- Sherry: `agent/middlewares/iteration_budget.py` + `config/features/agent_side/iteration_budget.py`
- DeepAgents: `graph.py` (recursion_limit) + `middleware/rubric.py`

---

### 2.6 HITL 人工审批

| 对比项           | Sherry Agent                                                                         | DeepAgents                                    |
| ---------------- | ------------------------------------------------------------------------------------ | --------------------------------------------- |
| 门控层数         | 14层                                                                                 | ~5层                                          |
| 硬线封锁         | ✅ 不可绕过(`rm -rf /`/`mkfs`/`shutdown`等)                                          | ❌ 无硬线封锁                                 |
| 危险模式检测     | ✅ 47+模式(递归删除/git force push/SQL DROP等)                                       | ❌ 无模式检测                                 |
| 写入审批         | ✅ `WriteApprovalGate` 记忆/技能写入暂存                                             | ✅ `FilesystemPermission(mode="interrupt")`   |
| sandbox绕过审批  | ✅ `sandbox=False` 需人工审批                                                        | ❌ 不适用                                     |
| MCP同意          | ✅ `MCPElicitationConsent` fail-closed                                               | ❌ 无                                         |
| 看板分流         | ✅ `KanbanTriage` 3次失败→分流                                                       | ❌ 无                                         |
| 配对授权         | ✅ `PairingStore` 平台级默认拒绝                                                     | ❌ 无                                         |
| 斜线确认         | ✅ `SlashConfirm` 破坏性命令                                                         | ❌ 无                                         |
| 路径感知中断谓词 | ❌ 无                                                                                | ✅ exact vs bulk scope 谓词                   |
| 批量模式绕过检测 | ❌ 无                                                                                | ✅ `_bulk_pattern_fires()` 检测glob可绕过HITL |
| 持久化审批策略   | ❌ 无                                                                                | ✅ `ToolApprovalStore` 字节修订CAS            |
| 操作员范围       | ❌ 无                                                                                | ✅ `APPROVAL_OPERATOR` ContextVar             |
| 定时任务审批     | ❌ 无                                                                                | ✅ 无操作员时自动拒绝                         |
| **结论**         | **Sherry 更全面(14层+47+模式)；DeepAgents 在路径感知谓词和持久化审批方面有独到设计** |

**关键文件**：

- Sherry: `agent/middlewares/humanInTheLoop/` (detection.py, gates.py, approval.py, core.py)
- DeepAgents: `middleware/_fs_interrupt.py` + `talon/tool_approvals.py` + `talon/host.py`

---

### 2.7 子代理安全

| 对比项         | Sherry Agent                                                                                   | DeepAgents                                  |
| -------------- | ---------------------------------------------------------------------------------------------- | ------------------------------------------- |
| 类型验证       | ❌ 隐式                                                                                        | ✅ `SubAgentMiddleware` 验证`subagent_type` |
| 嵌套深度限制   | ✅ `validate_spawn_depth()`                                                                    | ❌ 无显式深度限制                           |
| 并发子代理限制 | ✅ `validate_concurrent_children()`                                                            | ❌ 无                                       |
| CWD限制        | ✅ 子代理CWD必须在允许前缀下                                                                   | ❌ 不适用                                   |
| 跨运行时隔离   | ✅ 标记restricted并拒绝                                                                        | ❌ 不适用                                   |
| 工具继承策略   | ✅ `main_only`不可继承 + deny/allow列表                                                        | ✅ 子代理继承或定义自己的permissions        |
| 最小权限       | ✅ LEAF/ORCHESTRATOR角色                                                                       | ✅ `subagent_permissions` 继承/覆盖         |
| 默认阻止工具   | ✅ `sessions_spawn`/`sessions_yield`(防递归)                                                   | ✅ `_FORK_RECURSION_REFUSAL` 防fork递归     |
| 状态键过滤     | ❌ 无                                                                                          | ✅ `_EXCLUDED_STATE_KEYS` 阻止特定键传递    |
| 后台回收器     | ✅ `Sweeper` 孤儿回收+悬挂投递修复+过期清理                                                    | ❌ 无                                       |
| 工作准入       | ✅ `WorkAdmission` 排空模式                                                                    | ❌ 无                                       |
| 完成排空       | ✅ `SubagentCompletionDrain`                                                                   | ❌ 无                                       |
| Schema验证     | ❌ 无                                                                                          | ✅ `_SCHEMA_MAX_BYTES/DEPTH/PROPERTIES`     |
| 异步终端状态   | ❌ 无                                                                                          | ✅ `_TERMINAL_STATUSES` frozenset           |
| **结论**       | **Sherry 在运行时安全和后台回收方面全面领先；DeepAgents 在状态隔离和schema验证方面有独到设计** |

**关键文件**：

- Sherry: `agent/tools/subagent/spawn/` (depth.py, runtime_isolation.py, inherited_tool_policy.py, gateway_dispatch.py) + `agent/tools/subagent/registry/` (work_admission.py, sweeper.py)
- DeepAgents: `middleware/subagents.py` + `middleware/async_subagents.py` + `partners/quickjs/_subagent.py`

---

### 2.8 资源限制与并发控制

| 对比项          | Sherry Agent                                                                                 | DeepAgents               |
| --------------- | -------------------------------------------------------------------------------------------- | ------------------------ |
| 并发车道        | ✅ 4车道(MAIN/SUBAGENT/NUDGE/NESTED)                                                         | ❌ 无                    |
| FIFO排队        | ✅ 超限等待而非拒绝                                                                          | ❌ 不适用                |
| 启动验证        | ✅ main >= subagent + nudge                                                                  | ❌ 无                    |
| 排空模式        | ✅ `set_drain_check(fn)`                                                                     | ❌ 无                    |
| 热更新          | ✅ `set_concurrency()` 仅影响新获取                                                          | ❌ 无                    |
| 跨loop安全      | ✅ 信号量延迟创建+重绑定                                                                     | ❌ 无                    |
| PENDING→RUNNING | ✅ 超限spawn注册PENDING                                                                      | ❌ 不适用                |
| QuickJS内存限制 | ✅ python_repl子进程内存隔离                                                                 | ✅ 64MB默认(JS沙箱)      |
| QuickJS PTC上限 | ❌ 不适用                                                                                    | ✅ 256(JS沙箱)           |
| QuickJS任务上限 | ❌ 不适用                                                                                    | ✅ 每线程32(JS沙箱)      |
| grep匹配上限    | ❌ 无                                                                                        | ✅ 默认1000              |
| Glob展开上限    | ❌ 无                                                                                        | ✅ `MAX_EXPANSIONS=1000` |
| Glob匹配上限    | ❌ 无                                                                                        | ✅ `MAX_MATCHES=10000`   |
| Glob时间预算    | ❌ 无                                                                                        | ✅ `TIME_BUDGET=5.0`     |
| **结论**        | **Sherry 在进程级并发控制方面独有能力；DeepAgents 在JS沙箱资源限制和文件操作上限方面更丰富** |                          |

**关键文件**：

- Sherry: `runtime/lane/core.py` + `config/features/infra_side/lane_system.py`
- DeepAgents: `backends/sandbox.py` + `partners/quickjs/middleware.py`

---

### 2.9 错误处理与重试

| 对比项            | Sherry Agent                                                                       | DeepAgents                                    |
| ----------------- | ---------------------------------------------------------------------------------- | --------------------------------------------- |
| 错误分类引擎      | ✅ 8步管线(特殊模式→HTTP→异常→消息→SSL→断连→传输→unknown)                          | ❌ 无                                         |
| 重试策略          | ✅ 3次+指数退避+抖动(2-60s)                                                        | ❌ 仅上下文溢出重试1次                        |
| 速率限制退避      | ✅ 遵守Retry-After header                                                          | ❌ 无                                         |
| 过期序列断路器    | ✅ 连续5次timeout→放弃                                                             | ❌ 无                                         |
| 回退链            | ✅ 可配置fallback_chain+黏性回退                                                   | ❌ 无                                         |
| 内容过滤处理      | ✅ 切换回退模型或抛ContentFilterError                                              | ❌ 无                                         |
| 部分流桩          | ✅ 前次流中断→下次重新生成                                                         | ❌ 无                                         |
| 上下文溢出恢复    | ✅ T4/T5触发压缩恢复                                                               | ✅ `_call_with_budget()` 裁剪+重试1次         |
| Shell超时捕获     | ✅ terminal 30s                                                                    | ✅ `subprocess.TimeoutExpired` →exit_code=124 |
| Shell通用异常捕获 | ❌ 不适用                                                                          | ✅ 返回一致ExecuteResponse                    |
| Sandbox结构化错误 | ❌ 不适用                                                                          | ✅ JSON错误码代替traceback                    |
| ripgrep进程清理   | ❌ 不适用                                                                          | ✅ 双重超时(SIGTERM→SIGKILL→放弃)             |
| 崩溃回路断路      | ✅ 5min窗口3次→HTTP-only模式                                                       | ❌ 无                                         |
| **结论**          | **Sherry 在LLM错误处理方面全面领先；DeepAgents 在shell/sandbox错误处理方面更完善** |

**关键文件**：

- Sherry: `agent/middlewares/llm_retry.py` + `pub/func/message/llm_error_classifier.py` + `pub/func/retry_utils.py` + `runtime/process/crash_loop_breaker.py`
- DeepAgents: `middleware/summarization.py` + `backends/local_shell.py` + `backends/sandbox.py`

---

### 2.10 记忆/压缩安全

| 对比项         | Sherry Agent                                                                     | DeepAgents                   |
| -------------- | -------------------------------------------------------------------------------- | ---------------------------- |
| 压缩锁         | ✅ SQLite原子锁(TTL 300s)                                                        | ❌ 无(单进程)                |
| 压缩前记忆冲洗 | ✅ 廉价模型提取事实→MEMORY.md(永不阻塞)                                          | ❌ 无                        |
| 压缩有效性追踪 | ✅ 连续2次无效→标记                                                              | ❌ 无                        |
| 对话历史卸载   | ❌ 仅截断                                                                        | ✅ 摘要前持久化到后端        |
| 内联媒体卸载   | ❌ 无                                                                            | ✅ base64媒体→文件+路径引用  |
| 摘要消息过滤   | ❌ 无                                                                            | ✅ 避免链式摘要冗余          |
| 工具结果卸载   | ❌ 仅截断                                                                        | ✅ 大型工具结果→文件系统     |
| 内容预览       | ❌ 无                                                                            | ✅ head+tail预览(各5行)      |
| read_file切片  | ❌ 无                                                                            | ✅ read_file结果切片而非卸载 |
| 摘要提示安全   | ✅ NEVER include API keys指令                                                    | ❌ 无                        |
| FIFO限制       | ✅ Completed最多5条                                                              | ❌ 无                        |
| 文件操作棘轮   | ✅ 压缩后保留read_files/modified_files                                           | ❌ 无                        |
| **结论**       | **Sherry 在压缩安全和记忆冲洗方面领先；DeepAgents 在消息驱逐和内容预览方面领先** |

**关键文件**：

- Sherry: `agent/middlewares/compaction_lock.py` + `agent/middlewares/memory_flush.py` + `agent/middlewares/summarization_components.py`
- DeepAgents: `middleware/summarization.py` + `middleware/_message_eviction.py`

---

### 2.11 输出安全

| 对比项             | Sherry Agent                                                               | DeepAgents                                   |
| ------------------ | -------------------------------------------------------------------------- | -------------------------------------------- |
| 内容过滤           | ✅ `llm_content_filter_blocked`标志→切换回退                               | ❌ 无                                        |
| 流级重复截断       | ✅ 实时检测+切断重复尾部                                                   | ❌ 无                                        |
| 截断通知注入       | ✅ 超预算→注入截断通知                                                     | ❌ 无                                        |
| 工具结果截断       | ✅ 500行默认/2000行最大                                                    | ✅ `truncate_if_too_long()`                  |
| 分页读取截断       | ❌ 无                                                                      | ✅ `_truncate_paginated_read()`              |
| 行中截断处理       | ❌ 无                                                                      | ✅ `_midline_truncated_read()`               |
| grep输出格式化     | ❌ 无                                                                      | ✅ `format_grep_matches()` + 截断            |
| glob结果截断       | ❌ 无                                                                      | ✅ `_format_glob_result()` + 截断原因        |
| 视频帧限制         | ❌ 无                                                                      | ✅ 5个限制(解码秒/采样帧/输出字节/像素/边长) |
| ripgrep stderr限制 | ❌ 无                                                                      | ✅ `_RIPGREP_STDERR_CAPTURE_LIMIT=500`       |
| QuickJS结果限制    | ❌ 不适用                                                                  | ✅ `_DEFAULT_MAX_RESULT_CHARS=4000`          |
| Rubric转录截断     | ❌ 无                                                                      | ✅ `_MAX_TRANSCRIPT_CHARS_PER_MESSAGE`       |
| **结论**           | **Sherry 专注输出重复和内容过滤；DeepAgents 在输出截断和格式化方面更细致** |

**关键文件**：

- Sherry: `agent/middlewares/llm_retry.py` + `agent/wrapper/repetition_guard.py` + `agent/wrapper/context_limit.py`
- DeepAgents: `backends/utils.py` + `middleware/filesystem.py` + `middleware/rubric.py`

---

### 2.12 会话安全

| 对比项         | Sherry Agent                                                      | DeepAgents                                  |
| -------------- | ----------------------------------------------------------------- | ------------------------------------------- |
| 会话ID路径遍历 | ✅ `is_safe_session_id()`                                         | ❌ 无                                       |
| 会话级状态隔离 | ✅ 所有计数器按session_id键存储                                   | ❌ 不适用                                   |
| 每轮重置       | ✅ `before_agent`重置安全状态                                     | ❌ 不适用                                   |
| 写入审批       | ✅ 记忆/技能写入需人工审批                                        | ✅ `FilesystemPermission(mode="interrupt")` |
| 用户配对       | ✅ `PairingStore`平台级默认拒绝                                   | ❌ 无                                       |
| MCP同意        | ✅ `MCPElicitationConsent` fail-closed                            | ❌ 无                                       |
| 网关绑定       | ✅ 默认`127.0.0.1:8080`                                           | ❌ 不适用                                   |
| env隔离        | ✅ `scrub_env()`                                                  | ✅ `inherit_env=False`默认                  |
| 配置文件权限   | ❌ 无                                                             | ✅ `0o700`限制性权限                        |
| OAuth验证      | ❌ 无                                                             | ✅ `_validate_oauth_url()`                  |
| **结论**       | **各有千秋，Sherry 侧重会话隔离；DeepAgents 侧重配置和OAuth安全** |

---

### 2.13 超时处理

| 对比项        | Sherry Agent                                                                                         | DeepAgents                |
| ------------- | ---------------------------------------------------------------------------------------------------- | ------------------------- |
| 终端命令      | 30s                                                                                                  | 120s                      |
| Python REPL   | 30s                                                                                                  | ❌ 不适用                 |
| Web搜索       | 15s+3次重试(5-45s退避)                                                                               | ❌ 不适用                 |
| 心跳过期      | 空闲7min/工具内20min                                                                                 | ❌ 无                     |
| 压缩锁获取    | 10s                                                                                                  | ❌ 无                     |
| LLM重试       | 3次/2-60s退避                                                                                        | ❌ 无                     |
| 过期序列      | 5次连续timeout→放弃                                                                                  | ❌ 无                     |
| 车道等待      | 5000ms告警                                                                                           | ❌ 无                     |
| 车道排空      | 30s                                                                                                  | ❌ 无                     |
| 子代理yield   | 300s                                                                                                 | ❌ 无                     |
| sessions_send | 30s                                                                                                  | ❌ 无                     |
| 悬挂投递TTL   | cron=2h/subagent=6h/interactive=24h                                                                  | ❌ 无                     |
| 输入队列      | 5s忙超时/24h过期                                                                                     | ❌ 无                     |
| WS流          | 4次续接/2次纯推理                                                                                    | ❌ 不适用                 |
| HTTP上传      | 图片25MB/音频100MB/视频500MB                                                                         | ❌ 不适用                 |
| 崩溃回路      | 5min窗口3次                                                                                          | ❌ 无                     |
| TODO停滞      | 指数退避冷却                                                                                         | ❌ 无                     |
| 本地glob      | ❌ 无                                                                                                | 5s                        |
| 中间件glob    | ❌ 无                                                                                                | 10.0s                     |
| 同步grep      | ❌ 无                                                                                                | 15s                       |
| 异步grep      | ❌ 无                                                                                                | 35s                       |
| sandbox glob  | ❌ 无                                                                                                | 30s                       |
| QuickJS eval  | ❌ 无                                                                                                | 5.0s                      |
| ripgrep看门狗 | ❌ 无                                                                                                | 15s(SigTerm→SigKill→放弃) |
| **结论**      | **Sherry 超时覆盖面更广(尤其LLM和会话级)；DeepAgents 在文件操作超时更完善(glob/grep/ripgrep看门狗)** |

---

### 2.14 其他防护机制

| 对比项           | Sherry Agent                                                                                   | DeepAgents                                 |
| ---------------- | ---------------------------------------------------------------------------------------------- | ------------------------------------------ |
| 心跳过期检测     | ✅ 7min空闲/20min工具内                                                                        | ❌ 无                                      |
| TODO停滞追踪     | ✅ 指数退避+恢复模式                                                                           | ❌ 无                                      |
| 任务意图检测     | ✅ `TaskIntent` 引导注入                                                                       | ❌ 无                                      |
| 中间件脚手架保护 | ❌ 无                                                                                          | ✅ 必需中间件不可排除                      |
| 排除覆盖审计     | ❌ 无                                                                                          | ✅ 检测typo/过期排除条目                   |
| 名称冲突检测     | ❌ 无                                                                                          | ✅ 检测字符串排除匹配多个类                |
| 多模态内容清理   | ❌ 无                                                                                          | ✅ 替换模型不支持的内容块                  |
| 增量检查点优化   | ❌ 无                                                                                          | ✅ DeltaChannel O(N²)→O(N)                 |
| 消息去重         | ❌ 无                                                                                          | ✅ `_messages_delta_reducer()` ID去重+墓碑 |
| 威胁模型文档     | ❌ 无                                                                                          | ✅ THREAT_MODEL.md                         |
| 模型安全解析     | ❌ 无                                                                                          | ✅ `openai:`前缀检测+数据保留文档          |
| **结论**         | **Sherry 专注运行时行为安全(心跳/停滞/意图)；DeepAgents 专注框架级安全(脚手架/去重/威胁模型)** |

---

## 3. 能力差异总结

### 3.1 Sherry 独有但 DeepAgents 缺失

| 能力                              | 价值评估   | 实现复杂度 |
| --------------------------------- | ---------- | ---------- |
| 工具调用5种病理检测+4级升级链     | ⭐⭐⭐⭐⭐ | 高         |
| 输出重复3层防护(中间件+包装器+流) | ⭐⭐⭐⭐⭐ | 高         |
| LLM 8步错误分类+回退链+断路器     | ⭐⭐⭐⭐⭐ | 高         |
| 4车道并发控制+排空模式            | ⭐⭐⭐⭐   | 高         |
| 崩溃回路断路器                    | ⭐⭐⭐⭐   | 中         |
| 心跳过期检测                      | ⭐⭐⭐⭐   | 中         |
| TODO停滞追踪+指数退避恢复         | ⭐⭐⭐⭐   | 中         |
| 压缩有效性追踪                    | ⭐⭐⭐⭐   | 中         |
| 压缩前记忆冲洗(never-blocking)    | ⭐⭐⭐     | 中         |
| SQLite原子压缩锁(TTL 300s)        | ⭐⭐⭐     | 低         |
| HITL 14层门控+47+危险模式         | ⭐⭐⭐⭐⭐ | 高         |
| 任务意图检测与引导注入            | ⭐⭐⭐     | 中         |
| MaxTokensBoost指数级截断恢复      | ⭐⭐⭐     | 中         |
| 子代理后台回收器(Sweeper)         | ⭐⭐⭐⭐   | 高         |
| 工作准入排空模式                  | ⭐⭐⭐     | 中         |
| 5层上下文触发(T1-T5)              | ⭐⭐⭐⭐   | 高         |

### 3.2 DeepAgents 独有但 Sherry 缺失

| 能力                                   | 价值评估   | 实现复杂度 |
| -------------------------------------- | ---------- | ---------- |
| O_NOFOLLOW 符号链接防护                | ⭐⭐⭐⭐⭐ | 低         |
| 符号链接循环检测                       | ⭐⭐⭐⭐   | 低         |
| 工具结果→文件系统驱逐+head/tail预览    | ⭐⭐⭐⭐⭐ | 中         |
| 内联base64媒体卸载                     | ⭐⭐⭐⭐   | 中         |
| 参数截断(TruncateArgsSettings)         | ⭐⭐⭐⭐   | 中         |
| 模型感知摘要默认值                     | ⭐⭐⭐⭐   | 低         |
| DeltaChannel增量检查点(O(N²)→O(N))     | ⭐⭐⭐⭐⭐ | 高         |
| 消息增量缩减器(去重+墓碑+重置)         | ⭐⭐⭐⭐   | 高         |
| base64参数编码防Shell注入              | ⭐⭐⭐⭐   | 低         |
| 花括号展开限制(MAX_EXPANSIONS=1000)    | ⭐⭐⭐     | 低         |
| Glob时间预算+匹配上限                  | ⭐⭐⭐     | 低         |
| 伪文件系统修剪(/proc,/sys,/dev)        | ⭐⭐⭐     | 低         |
| 多模态内容清理(替换不支持块)           | ⭐⭐⭐     | 中         |
| 中间件脚手架保护(不可排除)             | ⭐⭐⭐⭐   | 低         |
| 排除覆盖审计(typo检测)                 | ⭐⭐⭐     | 低         |
| 路径感知HITL谓词(exact vs bulk)        | ⭐⭐⭐⭐   | 中         |
| 批量模式绕过HITL检测                   | ⭐⭐⭐⭐   | 中         |
| 持久化工具审批策略(字节修订CAS)        | ⭐⭐⭐     | 中         |
| ripgrep双重超时看门狗(SIGTERM→SIGKILL) | ⭐⭐⭐⭐   | 中         |
| 威胁模型文档(THREAT_MODEL.md)          | ⭐⭐⭐     | 低         |
| read_file结果切片(非卸载)              | ⭐⭐⭐     | 低         |
| 输出截断原因标记                       | ⭐⭐⭐     | 低         |

---

## 4. 架构理念对比

| 维度           | Sherry Agent                                       | DeepAgents                                       |
| -------------- | -------------------------------------------------- | ------------------------------------------------ |
| **设计哲学**   | 重运行时防护 — 所有安全机制围绕中间件链+包装器构建 | 重框架安全 — 安全机制围绕后端协议+中间件栈构建   |
| **防护层级**   | 5层：车道→包装器→中间件→工具→后台守护              | 3层：后端→中间件→图配置                          |
| **失败模式**   | Fail-open：所有安全中间件异常被吞掉(log+return)    | 渐进降级：超时→部分结果(truncated=True)→最终错误 |
| **配置驱动**   | TypedDict + config/features/ 38个配置模块          | 构造函数参数 + 硬编码常量                        |
| **状态管理**   | session_id键 + state_register_mem + SQLite持久化   | 图状态 + DeltaChannel + 后端持久化               |
| **并发模型**   | 4车道asyncio.Semaphore + FIFO排队                  | 无显式并发控制                                   |
| **记忆策略**   | 压缩前冲洗→截断→压缩                               | 卸载到后端→摘要→截断                             |
| **子代理模型** | 深度限制+CWD隔离+工具继承策略+后台回收             | 类型验证+递归拒绝+权限继承+状态键过滤            |
| **错误处理**   | 8步分类→回退链→断路器                              | 结构化JSON错误→裁剪重试→终止                     |
| **安全文档**   | 无                                                 | THREAT_MODEL.md完整威胁模型                      |

---

### 总评

- **Sherry Agent** 在**运行时行为安全**方面更强：工具调用病理检测、输出重复防护、LLM错误处理、并发控制、崩溃回路、心跳检测、TODO停滞追踪等，形成了深度防御体系。适合**长时运行、多子代理、复杂工具编排**的场景。

- **DeepAgents** 在**文件系统和框架安全**方面更强：路径遍历防护、符号链接防护、消息驱逐、增量检查点、Shell注入防护、中间件脚手架保护、威胁模型文档等。适合**文件操作密集、多模型供应商**的场景。

两者互补性极强，Sherry 可从 DeepAgents 借鉴文件系统安全、消息驱逐、增量检查点和威胁模型文档；DeepAgents 可从 Sherry 借鉴工具病理检测、输出重复防护、LLM错误处理、OS级沙箱和并发控制。
