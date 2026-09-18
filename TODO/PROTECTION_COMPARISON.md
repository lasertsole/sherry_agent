# 防护能力全面对比报告：Sherry Agent vs DeepAgents

> 生成日期：2026-09-16 ｜ **全量复核：2026-09-17** ｜ **第三批复核：2026-09-18（基准 `main` @ `2b9569b`）**
> 复核基准：Sherry = 本仓 `main` @ `2b9569b`（第三批；第一批 `2768ec0`、第二批 `4db42e0`）；DeepAgents = 本地检出 `/home/honor/Desktop/project/deepagents` 的 `libs/deepagents`（v0.7.14 @ `7f9e8ed3a`）+ `libs/partners/` + `libs/talon/` + `libs/code/`。原报告对比的是 Windows 上的 `D:\selfProj\deepagents-main`，与本次检出**未必同版本**；DeepAgents 的正向声明均已在本地检出定位到 `文件:行`，无法核实的会显式标注。DeepAgents 标 ❌ 的负向条目是本地检出**检索未见**对应实现，不能排除上游其他版本存在。
>
> **复核记录（2026-09-17，本轮改了哪些结论）：**
> 1. **矩阵结论翻转（Sherry 侧）**：「输入路径验证」「符号链接防护」由"Sherry 缺失 / DeepAgents 优势"改为**各有千秋**——Sherry 现已具备三道结构门禁（分量判定 `..`/`~`、resolve + `relative_to`、符号链接**环**检测）+ `_open_no_follow`（`O_NOFOLLOW` 闭合 TOCTOU，read/write/patch 全走）+ 虚拟路径回显 + 搜索 containment + `PathGuard` 中间件 + 外部路径六检查审批网关（`approve` / `approve_dir` / `yolo` / `reject`）。
> 2. **Shell 注入行**：Sherry 新增 terminal 敏感文件闸门（`_SENSITIVE_FILE_PATTERNS`，**缓解而非屏障**，局限见 §2.2 / §2.14）；当时判 DeepAgents 的结构化参数编码（base64）更稳，并补注其 `LocalShellBackend` 直传 `shell=True` 且零校验（其 THREAT_MODEL 自述）。**该判断已被第二批复核否决（见第 6 条）。**
> 3. **威胁模型文档行**：Sherry 由 ❌ 改为 ⚠️——无独立 `THREAT_MODEL.md`，但 `docs/sandbox/README*.md` 含威胁模型章节与局限清单（§2.14 / §4 同步）。
> 4. **其余更正**：URL scheme 白名单 12 → **13**（§2.2）；Sherry 整数参数约束由"无"改为"Pydantic 约束 + clamp"（§2.2）；DeepAgents `O_NOFOLLOW` 覆盖澄清为 read/write/edit/download（非无差别全覆盖，§2.2）；Sherry 子代理类型校验补注 `agent_id` 正则 + allow-list（§2.7）；Sherry `python_repl` 更正为**无内存上限**（§2.8）；视频帧限制 5 → **6 项**（§2.11）；ripgrep 看门狗更正为 `SIGKILL` 双重有界等待（§2.13）；§2.14 新增 `PathGuard` 与前缀缓存稳定两行。
> 5. **§3 同步**：§3.2 移出「O_NOFOLLOW 符号链接防护」「符号链接循环检测」（Sherry 已补齐）；§3.1 新增 PathGuard、外部路径审批网关、OS 沙箱读遮蔽、128K 四闸门四项。
> 6. **第二批变更（2026-09-17，随 P0 项处置；基准 `main` @ `4db42e0`）**：P0-1 符号链接防护复核确认已落地（`agent/tools/pub_base/path_utils.py` 的 `_open_no_follow`/`_raise_if_symlink_loop`，read/write/patch 全走）；**P0-3（base64 参数编码）经评估后否决**——`terminal` 本就以 shell 语义执行（`["/bin/sh","-c",cmd]` / `shell=True`），base64+`eval` 不减少风险，且置于 `_check_dangerous`/`_check_sensitive_file_access` 之前会让明文绕过现有防线，真实读屏障是 OS 沙箱读遮蔽；P0-4 搜索资源限制已落地（`file_tools_search_max_matches`=10000 / `file_tools_search_time_budget_s`=5.0 / `file_tools_search_prune_dirs`=`proc`/`sys`/`dev` + `agent/tools/file_tools/search_scan.py` 的 `bounded_walk`，提交 `6073f7c`/`c77846e`/`4db42e0`）。详见 `DEEPAGENTS_BORROWING_PLAN.md` 头部 P0 处置记录。
> 7. **第三批变更（2026-09-18，基准 `main` @ `2b9569b`，覆盖 `4db42e0` 之后的 77 个提交）：**
>    - **矩阵结论翻转（Sherry 侧）**：「消息驱逐」由 **DeepAgents** 改为**各有千秋**——P0-2 工具结果驱逐落地：超 `evict_threshold_chars`(20 000) 的结果写入 `SESSIONS_DIR/<session_id>/evicted/`，state 中只保留 head/tail 各 5 行预览（`agent/middlewares/context_eviction/core.py` / `pub/func/message/eviction.py`；提交 `4a260c7`/`565fc13`/`466c7d3`/`06f8d77`；中间件后于 P1-9 更名 `context_eviction`，见第四批记录）；P2-4 `read_file` 执行期切片同批落地（文件已在盘上，只切片不写新文件）。工具结果是三态：state=预览 / MesMemory=全文（工具返回即落库）/ 磁盘可取回。DeepAgents 仍在内联媒体卸载与人类消息驱逐两项上有独到设计。**新增矩阵行「消息持久化」→ 各有千秋**：Sherry 逐模型边界 + 工具返回即时落库（`persisted_message_ids` 水位、写一次、跨重启去重；摘要对 `lc_source="summarization"` 不落库；提交 `1ab6800`/`127af14`/`ffe438b`/`54e5844`），DeepAgents 为摘要前把丢弃历史卸载到后端。
>    - **「上下文压缩」行补充**：Sherry 新增 **P1-2 非 LLM 溢出尾部裁剪**（`pub/func/message/overflow_clip.py`，在溢出路由执行与 forced recovery 压缩 **之前**执行：够用即直接返回、不调 LLM；不够才降级到既有 T1-T5 路由；`model_copy` 保消息身份，配对/水位不破；三键 `overflow_clip_enabled`=True / `overflow_clip_max_remove`=10 / `overflow_clip_min_keep`=5；提交 `aa327fa`/`dcf8ece`/`f83fe20`）。§2.4「溢出尾部裁剪」由 ❌ 改为 ✅。
>    - **§2.10 五项更新**：「对话历史卸载」「工具结果卸载」「内容预览」「read_file 切片」「摘要消息过滤」由 ❌ 改为 ✅/改写。摘要链过滤：`_filter_summary_messages` 在重摘要输入前去掉上一对 `lc_source="summarization"` 消息（Human+AI 两半都带该标记），旧摘要文本改经 `<prior-summary>` 单独注入（提交 `3cfafc8`/`1e45ef5`/`f85e9e2`）。
>    - **其余更正**：§2.4「参数截断」由 ❌ 改为 ⚠️（预算截断路径本就有 `truncate_tool_args`，`pub/func/message/tool_args_truncate.py`；缺的是摘要期模型感知等价物）；§2.13 悬挂投递 TTL 已定位（`agent/tools/subagent/registry/sweeper.py:239` 的 `_REQUESTER_TYPE_EXPIRY_MS`：cron 2h / subagent 6h / interactive 24h）。
>    - **§3 同步**：§3.2 移出「工具结果→文件系统驱逐+head/tail预览」「read_file结果切片(非卸载)」两项，「参数截断」改写为「摘要期模型感知参数截断」；§3.1 新增「逐消息持久化+水位」「工具结果驱逐+read_file切片」「非LLM溢出尾部裁剪」「摘要消息过滤（链式摘要去重）」四项。
>    - **结构与命名核对**：文档引用的模块路径已按现行包结构核对——`agent/middlewares/system_prompt/core.py`（`system_prompt_injection`，原 `context_engine/`）、`agent/middlewares/summarization/nudges.py`（原 `nudge.py`）、配置 `NUDGE`/`MEDIA_PIPELINE`（原 `CONTEXT_ENGINE_HOOK` 拆分）、计划路径 `workspace/sessions/<session_id>/plans/`（`config/path.py::session_plans_dir`/`resolve_plan_path`，legacy `.omo/plans/` 仅作回退）。
>
> 8. **第四批变更（2026-09-18，基准 `main` @ `a19525e`）：**
>    - **P1-9 人类消息驱逐落地**（来源：第三批复核 §2.4「人类消息驱逐」❌；计划 `DEEPAGENTS_BORROWING_PLAN.md` §P1-9）：`tool_result_eviction/` 更名 `context_eviction/`（`ContextEvictionMiddleware`，提交 `033aabb`），配置新增 `human_evict_enabled` / `human_evict_threshold_chars`(200 000) / `human_preview_head_lines` / `human_preview_tail_lines` 四键（`10e1d22`），纯函数 `evict_human_message` / `build_human_preview` / `human_eviction_notice`（`4868d37`），中间件 `before_model` 打标落盘 + `wrap_model_call` 仅截模型视图 + 文件缺失自愈（`9778898`）。**人类消息三态与工具侧刻意相反：state = 全文 + `lc_evicted_to` 标记 / MesMemory = 全文 / 磁盘 = 逐字节原文**（工具侧 state = 预览）——原因与逐项交互矩阵见计划 §P1-9「设计决策」。
>    - **矩阵更新**：§2.4「人类消息驱逐」由 ❌ 改为 ✅；§1「消息驱逐」行 Sherry 侧补"人类消息全文留存 + 仅模型视图截断(P1-9)"；§2.4 / §2.10 结论中"DeepAgents 在人类消息驱逐领先"改为"已对齐"。
>
> 模块路径已按**包结构**核对：`agent/middlewares/<name>/core.py`（如 `path_guard/core.py`、`tool_guardrails/core.py`、`summarization/core.py`）；DeepAgents 路径按检出实际前缀 `libs/...` 标注。

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

| 防护维度         |                          Sherry Agent                           |                              DeepAgents                              |     优势方     |
| ---------------- | :-------------------------------------------------------------: | :------------------------------------------------------------------: | :------------: |
| 工具调用病理检测 |                     ✅ 5种病理+4级升级链                        |                        ❌ 仅悬空调用修补                             |   **Sherry**   |
| 输入路径验证     | ✅ 会话ID+URL+三闸+O_NOFOLLOW+PathGuard+外部路径六检查审批      | ✅ virtual_mode+validate_path+O_NOFOLLOW(读/写/改/下载)+符号链接环   | **各有千秋**   |
| 输出重复防护     |                    ✅ 3层(中间件+包装器+流)                     |                        ❌ 无显式机制                                 |   **Sherry**   |
| 上下文压缩       |        ✅ T1-T5触发+防抖动+压缩锁+非LLM溢出尾部裁剪             |                  ✅ 自动摘要+溢出裁剪+消息驱逐                       |  **各有千秋**  |
| 迭代限制         |                        ✅ 角色50/90/60                          |                      ✅ 图递归9999+Rubric3                           |  **各有千秋**  |
| HITL审批         |                 ✅ 14层门控+47+危险模式+外部路径审批            |                  ✅ 权限→HITL桥接+路径感知谓词                       |  **各有千秋**  |
| 子代理安全       |                 ✅ 深度+CWD+工具继承+最小权限                  |                  ✅ 类型验证+递归拒绝+权限继承                       |  **各有千秋**  |
| 并发控制         |                    ✅ 4车道Semaphore+排空                       |                              ❌ 无                                   |   **Sherry**   |
| LLM重试          |                   ✅ 8步分类+回退链+断路器                      |                       ❌ 仅上下文溢出重试                            |   **Sherry**   |
| 消息驱逐         |      ✅ 工具结果→SESSIONS_DIR/evicted+head/tail预览+read_file切片 + 人类消息全文留存+仅模型视图截断(P1-9)        |           ✅ 工具结果→文件系统+head/tail预览+内联媒体/人类消息驱逐            | **各有千秋**   |
| 消息持久化       |       ✅ 逐模型边界+工具返回即时落库+水位去重(摘要对不落库)       |                ✅ 摘要前持久化到后端(丢弃历史)                      | **各有千秋**   |
| 符号链接防护     |           ✅ 三闸 + O_NOFOLLOW + 环检测 + 搜索containment       |                  ✅ O_NOFOLLOW+符号链接环检测                        | **各有千秋**   |
| Shell注入防护    |            ✅ 危险命令正则黑名单 + 终端敏感文件闸门(缓解)；base64 参数编码方案已评估并否决(对 terminal 无增益，见 DEEPAGENTS_BORROWING_PLAN 处置记录)       | ✅ sandbox参数base64编码+花括号展开限制(LocalShell直传shell=True无校验) | **DeepAgents** |
| 崩溃回路断路     |                        ✅ 5min窗口3次                           |                              ❌ 无                                   |   **Sherry**   |
| 心跳过期检测     |                    ✅ 空闲7min/工具内20min                      |                              ❌ 无                                   |   **Sherry**   |
| 威胁模型文档     |      ⚠️ 无独立文档；docs/sandbox/README* 含威胁模型章节         |                  ✅ libs/deepagents/THREAT_MODEL.md                  | **DeepAgents** |
| TODO停滞追踪     |                     ✅ 指数退避+恢复模式                        |                              ❌ 无                                   |   **Sherry**   |
| 压缩有效性追踪   |                      ✅ 连续2次无效→标记                        |                              ❌ 无                                   |   **Sherry**   |
| 环境隔离         |                        ✅ scrub_env()                           |                      ✅ inherit_env=False默认                        |  **各有千秋**  |
| 沙箱执行         | ✅ OS级沙箱(bwrap/Seatbelt)+env清洗+读遮蔽+HITL门(Windows无读保护) | ✅ QuickJS+云沙箱(Vercel/Modal/Daytona/Runloop，libs/partners独立包) |  **各有千秋**  |
| 中间件脚手架保护 |           ❌ 无用户排除入口(链在代码中固定组装)                 |                      ✅ 必需中间件不可排除                           | **DeepAgents** |

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

- Sherry: `agent/middlewares/tool_guardrails/core.py` + `config/features/agent_side/tool_guardrails.py`
- DeepAgents: `libs/deepagents/deepagents/middleware/patch_tool_calls.py:11` + `libs/deepagents/deepagents/middleware/_tool_exclusion.py:34`

---

### 2.2 输入验证与消毒

| 对比项             | Sherry Agent                                                                                      | DeepAgents                                                                     |
| ------------------ | ------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| 会话ID防护         | ✅ `is_safe_session_id()` 拒绝路径分隔符+解析后路径验证                                           | ❌ 无                                                                          |
| 路径遍历防护       | ✅ 三道结构门禁：`..`/`~` 分量判定 → resolve + `relative_to` 包含性 → 符号链接环检测              | ✅ `validate_path()` + `_resolve_path(virtual_mode=True)` 阻止`..`/`~`         |
| 符号链接防护       | ✅ `_open_no_follow()`（`O_NOFOLLOW`，Windows 回退 `is_symlink()`）read/write/patch 全覆盖 + 环检测 | ✅ read/write/edit/download 用 `O_NOFOLLOW` + `_raise_if_symlink_loop()`（delete/ls/grep 走 resolve+环检测，非无差别全覆盖） |
| 虚拟路径回显       | ✅ `to_virtual_path()` / `display_path()` / `safe_error_detail()` —— 不回显 `ROOT_DIR`            | ✅ virtual_mode 下 `_to_virtual_path()` / `_display_path()` 由设计保证         |
| 搜索 containment   | ✅ `_stays_within_root()` 过滤解析后逃逸出根的符号链接                                            | ✅ 搜索在虚拟命名空间内解析                                                    |
| 命名空间验证       | ❌ 无                                                                                             | ✅ `_validate_namespace()` 正则拒绝通配符注入                                  |
| YAML安全加载       | ❌ 不适用                                                                                         | ✅ `yaml.safe_load()` 防YAML代码执行                                           |
| Base64参数编码     | ❌ 无（该方案已评估并**否决**：对以 shell 语义执行的 `terminal` 无增益；见 `DEEPAGENTS_BORROWING_PLAN.md` 处置记录） | ✅ 所有sandbox shell命令使用base64编码参数                                     |
| URL验证            | ✅ `is_url()` 13种scheme白名单                                                                    | ❌ 无独立URL验证                                                               |
| 内容消毒           | ✅ `sanitize_content()` 去括号/CoT标签剥离                                                        | ✅ HTML注释剥离(memory 源，如 AGENTS.md)                                       |
| 整数强制转换       | ⚠️ `read_file` 用 Pydantic 字段约束 + `limit` clamp(1–2000)，其余工具参数未统一 normalize         | ✅ `normalize_read_bounds()` 确保offset/limit安全                              |
| **结论**           | **两侧现在都具备完整路径防护，但路线不同：DeepAgents 用 `virtual_mode` 虚拟命名空间让穿越"结构性不可行"；Sherry 用三道结构门禁 + `O_NOFOLLOW`（闭合 TOCTOU）+ 环检测 + 虚拟路径回显 + 搜索 containment + `PathGuard` + 外部路径六检查审批做纵深防御（外部路径交人审而非直接拦截）** |

**关键文件**：

- Sherry: `agent/tools/pub_base/path_utils.py`（`_reject_traversal_input`:25 / `_raise_if_symlink_loop`:55 / `_open_no_follow`:71 / `to_virtual_path`:128 / `display_path`:138 / `safe_error_detail`:151 / `resolve_external_path`:310）+ `agent/tools/file_tools/search_files.py`（`_stays_within_root`:38）+ `agent/middlewares/path_guard/core.py` + `pub/func/validator/session_id.py` + `pub/func/validator/is_url.py` + `pub/func/format/sanitize_content.py`
- DeepAgents: `libs/deepagents/deepagents/backends/utils.py`（`validate_path`:692 / `normalize_read_bounds`:431）+ `libs/deepagents/deepagents/backends/filesystem.py`（`_resolve_path`:182 / `_raise_if_symlink_loop`:1556 / `O_NOFOLLOW`:449,509,573,1462）+ `libs/deepagents/deepagents/backends/store.py`（`_validate_namespace`:49）+ `libs/deepagents/deepagents/middleware/skills.py`（`yaml.safe_load`:408）+ `libs/deepagents/deepagents/middleware/memory.py`（HTML 注释剥离:173）

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
| **结论**     | **Sherry 在输出重复防护方面全面领先；DeepAgents 本地检出未见任何对应机制** |

**关键文件**：

- Sherry: `agent/middlewares/output_repetition_guard/core.py` + `agent/wrapper/repetition_guard.py` + `agent/middlewares/output_repetition_guard/repetition_detectors.py`
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
| Token启动门控  | ✅ 强制≥128K（四道闸门：boot/build/spawn/env 写入）                            | ❌ 无                                                   |
| 截断恢复       | ✅ `MaxTokensBoost` 指数级max_tokens提升                                       | ❌ 无                                                   |
| 溢出尾部裁剪   | ✅ P1-2 非LLM尾部裁剪（够用即直接返回、不调LLM；不够才降级路由；`pub/func/message/overflow_clip.py`） | ✅ `_clip_overflow_tail()` 尾部ToolMessage批量卸载      |
| 消息驱逐       | ✅ 工具结果→`SESSIONS_DIR/<sid>/evicted/` + head/tail预览(各5行) + `read_file`执行期切片 | ✅ 工具结果→文件系统 + head/tail预览 + 内联媒体卸载     |
| 参数截断       | ⚠️ 预算截断路径会截断超大工具调用参数（`pub/func/message/tool_args_truncate.py`，head+tail+中段省略）；无摘要期模型感知等价物 | ✅ `TruncateArgsSettings` 摘要前截断旧工具参数          |
| 模型感知默认值 | ❌ 手动配置                                                                    | ✅ `compute_summarization_defaults()` 从模型profile计算 |
| 增量检查点优化 | ❌ 无                                                                          | ✅ `DeltaChannel(snapshot_frequency=50)` O(N²)→O(N)     |
| 人类消息驱逐   | ✅ P1-9：超大 HumanMessage 全文落 `SESSIONS_DIR/<sid>/evicted/human-<id>.md`，state 与 MesMemory 保留全文，`before_model` 只打 `lc_evicted_to` 标记、`wrap_model_call` 仅把模型视图换成 路径+head/tail+`read_file` 提示（提交 `033aabb`/`10e1d22`/`4868d37`/`9778898`） | ✅ `human_message_token_limit_before_evict`             |
| **结论**       | **Sherry 在触发精度、防抖动与非LLM溢出裁剪方面领先；DeepAgents 在增量检查点优化与模型感知默认值方面领先；工具结果驱逐/预览/read_file 切片与人类消息驱逐（P1-9）已对齐** |

**关键文件**：

- Sherry: `agent/middlewares/summarization/core.py`（`_fast_tail_clip`:880；调用点 1033/1076/1289/1334）+ `agent/wrapper/context_limit.py` + `config/features/agent_side/summarization.py`（`overflow_clip_*` 三键:55-57,110-112）+ `config/features/agent_side/token_guard.py` + `pub/func/message/overflow_clip.py` + `pub/func/message/eviction.py`（`evict_tool_result`/`slice_read_file_result`/`evict_human_message`/`build_human_preview`）+ `agent/middlewares/context_eviction/core.py` + `config/features/agent_side/tool_result_eviction.py`
- DeepAgents: `libs/deepagents/deepagents/middleware/summarization.py`（0.85 触发:34 / `TruncateArgsSettings`:168 / `compute_summarization_defaults`:262）+ `libs/deepagents/deepagents/middleware/_overflow_clip.py` + `libs/deepagents/deepagents/middleware/_message_eviction.py` + `libs/deepagents/deepagents/_messages_reducer.py` + `libs/deepagents/deepagents/middleware/filesystem.py`（人类消息驱逐:1755）

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

- Sherry: `agent/middlewares/iteration_budget/core.py` + `config/features/agent_side/iteration_budget.py`
- DeepAgents: `libs/deepagents/deepagents/graph.py:971` (`recursion_limit=9_999`) + `libs/deepagents/deepagents/middleware/rubric.py:567` (`max_iterations=3`)

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
| 斜线确认         | ✅ `SlashConfirm` (Layer 14) 破坏性命令                                              | ❌ 无                                         |
| 外部路径审批     | ✅ 六检查网关（`approve`/`approve_dir`/`yolo`/`reject`）                             | ❌ 无外部路径概念（virtual_mode；路径规则走 permission 谓词） |
| 路径感知中断谓词 | ❌ 无                                                                                | ✅ exact vs bulk scope 谓词                   |
| 批量模式绕过检测 | ❌ 无                                                                                | ✅ `_bulk_pattern_fires()` 检测glob可绕过HITL |
| 持久化审批策略   | ❌ 无                                                                                | ✅ `ToolApprovalStore` 字节修订CAS            |
| 操作员范围       | ❌ 无                                                                                | ✅ `APPROVAL_OPERATOR` ContextVar             |
| 定时任务审批     | ❌ 无                                                                                | ✅ 无操作员时自动拒绝                         |
| **结论**         | **Sherry 更全面(14层+47+模式+外部路径六检查网关)；DeepAgents 在路径感知谓词和持久化审批方面有独到设计** |

**关键文件**：

- Sherry: `agent/middlewares/humanInTheLoop/`（`detection.py` Layer 1–2 hardline/dangerous + `gates.py` Layer 8/9/11/12/13/14 + `approval.py` + `core.py`）+ `agent/tools/pub_base/path_utils.py::resolve_external_path`（外部路径网关，`allowed_decisions`:391）
- DeepAgents: `libs/deepagents/deepagents/middleware/_fs_interrupt.py`（exact/bulk 谓词:49 / `_bulk_pattern_fires`:140）+ `libs/talon/deepagents_talon/tool_approvals.py`（`APPROVAL_OPERATOR`:87 / `ToolApprovalStore`:90 / 字节修订 CAS:142）+ `libs/talon/deepagents_talon/runtime.py:526` + `libs/talon/deepagents_talon/host.py`

---

### 2.7 子代理安全

| 对比项         | Sherry Agent                                                                                   | DeepAgents                                  |
| -------------- | ---------------------------------------------------------------------------------------------- | ------------------------------------------- |
| 类型验证       | ⚠️ `agent_id` 正则(`^[a-zA-Z0-9_-]+$`) + allow-list 目标策略（默认 `*`，非注册类型枚举）       | ✅ `SubAgentMiddleware` 验证`subagent_type` |
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

- Sherry: `agent/tools/subagent/spawn/` (depth.py, runtime_isolation.py, inherited_tool_policy.py, gateway_dispatch.py, target_policy.py) + `agent/tools/subagent/registry/` (work_admission.py, sweeper.py) + `agent/tools/subagent/spawn/core.py:220` (agent_id 正则)
- DeepAgents: `libs/deepagents/deepagents/middleware/subagents.py`（类型校验:773 / `_FORK_RECURSION_REFUSAL`:61 / `_EXCLUDED_STATE_KEYS`:392）+ `libs/deepagents/deepagents/middleware/async_subagents.py:659`（`_TERMINAL_STATUSES`）+ `libs/partners/quickjs/langchain_quickjs/_subagent.py:33`（schema 上限）

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
| JS执行内存限制  | ⚠️ python_repl 仅受限 builtins + 30s 超时，无内存上限                                        | ✅ 64MB默认(QuickJS 堆)  |
| QuickJS PTC上限 | ❌ 不适用                                                                                    | ✅ 256(JS沙箱)           |
| QuickJS任务上限 | ❌ 不适用                                                                                    | ✅ 每线程32(JS沙箱)      |
| grep匹配上限    | ✅ 分页 + 扫描硬上限(10000 匹配) + 5s 时间预算 + 伪文件系统剪枝(proc/sys/dev)                                               | ✅ 默认1000              |
| Glob展开上限    | ❌ 不适用（搜索走 `fnmatch`，无花括号展开）                                                                       | ✅ `MAX_EXPANSIONS=1000` |
| Glob匹配上限    | ✅ 扫描硬上限 10000 匹配（`file_tools_search_max_matches`）                                                                       | ✅ `MAX_MATCHES=10000`   |
| Glob时间预算    | ✅ 5s 时间预算（`file_tools_search_time_budget_s`）                                                                                        | ✅ `TIME_BUDGET=5.0`     |
| 伪文件系统剪枝  | ✅ `proc`/`sys`/`dev`（`file_tools_search_prune_dirs`） | ✅ `PRUNE_AT_ROOT=('proc','sys','dev')` |
| **结论**        | **Sherry 在进程级并发控制与文件搜索资源上限方面均有能力；DeepAgents 在JS沙箱资源限制与花括号展开上限方面领先** |                          |

**关键文件**：

- Sherry: `runtime/lane/core.py` + `config/features/infra_side/lane_system.py` + `agent/tools/file_tools/search_scan.py`（`ScanState`/`bounded_walk`） + `config/features/agent_side/tools_timeouts.py`（`file_tools_search_max_matches`/`file_tools_search_time_budget_s`/`file_tools_search_prune_dirs`） + `tests/agent/tools/file_tools/test_search_bounds.py`（10 例）
- DeepAgents: `libs/deepagents/deepagents/backends/sandbox.py`（`MAX_EXPANSIONS`:67 / `MAX_MATCHES`:68 / `TIME_BUDGET`:69）+ `libs/partners/quickjs/langchain_quickjs/middleware.py`（64MB:56 / PTC 256:58 / eval 5.0s:57）+ `libs/partners/quickjs/langchain_quickjs/_repl.py:63`（每线程 32 任务）

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
| ripgrep进程清理   | ❌ 不适用                                                                          | ✅ kill→5s→再 kill→5s→放弃(SIGKILL 双重有界等待) |
| 崩溃回路断路      | ✅ 5min窗口3次→HTTP-only模式                                                       | ❌ 无                                         |
| **结论**          | **Sherry 在LLM错误处理方面全面领先；DeepAgents 在shell/sandbox错误处理方面更完善** |

**关键文件**：

- Sherry: `agent/middlewares/llm_retry/core.py` + `pub/func/message/llm_error_classifier.py` + `pub/func/retry_utils.py` + `runtime/process/crash_loop_breaker.py`
- DeepAgents: `libs/deepagents/deepagents/middleware/summarization.py`（`_call_with_budget`:1410）+ `libs/deepagents/deepagents/backends/local_shell.py`（120s 默认:23 / `exit_code=124`:353 / 通用异常→ExecuteResponse:363）+ `libs/deepagents/deepagents/backends/sandbox.py`（结构化 error 码:369）+ `libs/deepagents/deepagents/backends/filesystem.py`（ripgrep 看门狗 `_kill_on_timeout`:993 / `_reap_ripgrep`:1129）

---

### 2.10 记忆/压缩安全

| 对比项         | Sherry Agent                                                                     | DeepAgents                   |
| -------------- | -------------------------------------------------------------------------------- | ---------------------------- |
| 压缩锁         | ✅ SQLite原子锁(TTL 300s)                                                        | ❌ 无(单进程)                |
| 压缩前记忆冲洗 | ✅ 廉价模型提取事实→MEMORY.md(永不阻塞)                                          | ❌ 无                        |
| 压缩有效性追踪 | ✅ 连续2次无效→标记                                                              | ❌ 无                        |
| 对话历史卸载   | ✅ 逐消息持久化：每个模型边界 + 工具返回即时写入 MesMemory（`persisted_message_ids` 水位写一次，跨重启按 id/指纹去重；摘要对不落库） | ✅ 摘要前持久化到后端        |
| 内联媒体卸载   | ❌ 无（多模态中间件把上传媒体落盘并从历史消息剥离旧 image_url 块，但不做 base64→文件+路径引用的摘要期卸载） | ✅ base64媒体→文件+路径引用  |
| 摘要消息过滤   | ✅ `_filter_summary_messages` 在重摘要输入前过滤上一对 `lc_source="summarization"` 消息（Human+AI 两半都带标记）；旧摘要经 `<prior-summary>` 注入 | ✅ 避免链式摘要冗余          |
| 工具结果卸载   | ✅ 超 20 000 字符→`SESSIONS_DIR/<sid>/evicted/`（全文落盘，state 只留预览；8 项 `excluded_tools`） | ✅ 大型工具结果→文件系统     |
| 内容预览       | ✅ head/tail 各 5 行 + `read_file` 取回提示（`pub/func/message/eviction.py::build_preview`） | ✅ head+tail预览(各5行)      |
| read_file切片  | ✅ 执行期切片（`pub/func/message/eviction.py::slice_read_file_result`，≤4000字符+提示、不写新文件）与压缩期可找回切片（`pub/func/message/target_truncation.py::_truncate_read_file_content`）互补 | ✅ read_file结果切片而非卸载 |
| 摘要提示安全   | ✅ NEVER include API keys指令                                                    | ❌ 无                        |
| FIFO限制       | ✅ Completed最多5条                                                              | ❌ 无                        |
| 文件操作棘轮   | ✅ 压缩后保留read_files/modified_files                                           | ❌ 无                        |
| **结论**       | **Sherry 在压缩安全、记忆冲洗、逐消息持久化与摘要消息过滤方面领先；工具结果驱逐/预览/read_file 切片与人类消息驱逐已与 DeepAgents 对齐（DeepAgents 另有内联媒体卸载）** |

**关键文件**：

- Sherry: `agent/middlewares/summarization/compaction_lock.py`（TTL 300s:29 / 获取超时 10s:31）+ `agent/middlewares/summarization/memory_flush.py` + `agent/middlewares/summarization/summarization_components.py` + `agent/middlewares/message_persistence/core.py`（`after_model`:194 / `awrap_tool_call`:223）+ `context_engine/store/core.py`（`is_message_persisted`:455 / `filter_persisted_message_ids`:501 / `mark_message_ids_persisted`:519）+ `pub/func/message/eviction.py` + `pub/func/message/overflow_clip.py`
- DeepAgents: `libs/deepagents/deepagents/middleware/summarization.py`（内联媒体卸载 / 摘要消息过滤:742）+ `libs/deepagents/deepagents/middleware/_message_eviction.py`（head+tail 预览:38 / 卸载:120）+ `libs/deepagents/deepagents/middleware/_overflow_clip.py`（read_file 切片:76）

---

### 2.11 输出安全

| 对比项             | Sherry Agent                                                               | DeepAgents                                   |
| ------------------ | -------------------------------------------------------------------------- | -------------------------------------------- |
| 内容过滤           | ✅ `llm_content_filter_blocked`标志→切换回退                               | ❌ 无                                        |
| 流级重复截断       | ✅ 实时检测+切断重复尾部                                                   | ❌ 无                                        |
| 截断通知注入       | ✅ 超预算→注入截断通知                                                     | ❌ 无                                        |
| 工具结果截断       | ✅ 500行默认/2000行最大                                                    | ✅ `truncate_if_too_long()`                  |
| 分页读取截断       | ⚠️ read_file 原生分页(500行默认/2000上限 + truncated + hint)，无后置截断   | ✅ `_truncate_paginated_read()`              |
| 行中截断处理       | ❌ 无                                                                      | ✅ `_midline_truncated_read()`               |
| grep输出格式化     | ❌ 无                                                                      | ✅ `format_grep_matches()` + 截断            |
| glob结果截断       | ❌ 无                                                                      | ✅ `_format_glob_result()` + 截断原因        |
| 视频帧限制         | ❌ 无                                                                      | ✅ 6项限制(解码秒/采样帧/输出字节/像素上限/边长上限/输出宽高) |
| ripgrep stderr限制 | ❌ 无                                                                      | ✅ `_RIPGREP_STDERR_CAPTURE_LIMIT=500`       |
| QuickJS结果限制    | ❌ 不适用                                                                  | ✅ `_DEFAULT_MAX_RESULT_CHARS=4000`          |
| Rubric转录截断     | ❌ 无                                                                      | ✅ `_MAX_TRANSCRIPT_CHARS_PER_MESSAGE`       |
| **结论**           | **Sherry 专注输出重复和内容过滤；DeepAgents 在输出截断和格式化方面更细致** |

**关键文件**：

- Sherry: `agent/middlewares/llm_retry/core.py` + `agent/wrapper/repetition_guard.py` + `agent/wrapper/context_limit.py`
- DeepAgents: `libs/deepagents/deepagents/backends/utils.py`（`truncate_if_too_long`:611 / `format_grep_matches`:994）+ `libs/deepagents/deepagents/backends/filesystem.py`（`_RIPGREP_STDERR_CAPTURE_LIMIT`:64）+ `libs/deepagents/deepagents/middleware/filesystem.py`（`_midline_truncated_read`:990 / `_truncate_paginated_read`:1027）+ `libs/deepagents/deepagents/middleware/_video.py`（帧限制:61-80）+ `libs/deepagents/deepagents/middleware/rubric.py`（`_MAX_TRANSCRIPT_CHARS_PER_MESSAGE`:110）+ `libs/partners/quickjs/langchain_quickjs/middleware.py`（`_DEFAULT_MAX_RESULT_CHARS`:59）

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
| 配置文件权限   | ❌ 无                                                             | ✅ `0o700`限制性权限（CLI 状态目录）        |
| OAuth验证      | ❌ 无                                                             | ✅ `_validate_oauth_url()`（talon MCP）     |
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
| 悬挂投递TTL   | cron=2h/subagent=6h/interactive=24h（`agent/tools/subagent/registry/sweeper.py:239` 的 `_REQUESTER_TYPE_EXPIRY_MS`） | ❌ 无                     |
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
| QuickJS eval  | ❌ 不适用(python_repl 30s)                                                                           | 5.0s                      |
| ripgrep看门狗 | ❌ 无                                                                                                | 15s 看门狗→kill→5s→再 kill→5s→放弃(SIGKILL) |
| **结论**      | **Sherry 超时覆盖面更广(尤其LLM和会话级)；DeepAgents 在文件操作超时更完善(glob/grep/ripgrep看门狗)** |

---

### 2.14 其他防护机制

| 对比项           | Sherry Agent                                                                                   | DeepAgents                                 |
| ---------------- | ---------------------------------------------------------------------------------------------- | ------------------------------------------ |
| 心跳过期检测     | ✅ 7min空闲/20min工具内                                                                        | ❌ 无                                      |
| TODO停滞追踪     | ✅ 指数退避+恢复模式                                                                           | ❌ 无                                      |
| 任务意图检测     | ✅ `TaskIntent` 引导注入                                                                       | ❌ 无                                      |
| 工具参数级路径筛查 | ✅ `PathGuard` 中间件（wrap_tool_call，一次调用一次审批）                                    | ❌ 无（路径规则在 backend/_fs_interrupt 层）|
| 中间件脚手架保护 | ❌ 无用户排除入口(链在代码中固定组装)                                                           | ✅ 必需中间件不可排除                      |
| 排除覆盖审计     | ❌ 无                                                                                          | ✅ 检测typo/过期排除条目                   |
| 名称冲突检测     | ❌ 无                                                                                          | ✅ 检测字符串排除匹配多个类                |
| 多模态内容清理   | ❌ 无                                                                                          | ✅ 替换模型不支持的内容块                  |
| 增量检查点优化   | ❌ 无                                                                                          | ✅ DeltaChannel O(N²)→O(N)                 |
| 消息去重         | ❌ 无                                                                                          | ✅ `_messages_delta_reducer()` ID去重+墓碑 |
| 前缀缓存稳定     | ✅ 记忆快照冻结(会话内不变) + ToolCallNormalize 无变化不写状态                                  | ✅ `_prompt_caching.py` 中间件             |
| 威胁模型文档     | ⚠️ 无独立 THREAT_MODEL.md；docs/sandbox/README* 含威胁模型章节与局限清单                       | ✅ THREAT_MODEL.md                         |
| 模型安全解析     | ❌ 无                                                                                          | ✅ `openai:`前缀检测+数据保留文档          |
| **结论**         | **Sherry 专注运行时行为安全(心跳/停滞/意图)与工具调用参数级路径筛查；DeepAgents 专注框架级安全(脚手架/去重/威胁模型)** |

**关键文件**：

- Sherry: `agent/middlewares/path_guard/core.py` + `agent/tools/memory.py:13`（记忆快照冻结以稳定前缀缓存）+ `agent/middlewares/tool_call_normalize/core.py:20`（无变化返回 None）
- DeepAgents: `libs/deepagents/deepagents/_excluded_middleware.py`（脚手架保护:23 / 名称冲突:67 / 覆盖审计:168）+ `libs/deepagents/deepagents/middleware/_prompt_caching.py` + `libs/deepagents/deepagents/middleware/filesystem.py:272`（多模态清理）

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
| 逐消息持久化+SQLite水位(工具返回即写、摘要对不落库) | ⭐⭐⭐⭐ | 中       |
| 工具结果驱逐(P0-2)+read_file切片(P2-4)三态存储 | ⭐⭐⭐⭐ | 中     |
| 非LLM溢出尾部裁剪(够用不调LLM)    | ⭐⭐⭐⭐   | 中         |
| 摘要消息过滤(链式摘要去重)        | ⭐⭐⭐     | 低         |
| HITL 14层门控+47+危险模式         | ⭐⭐⭐⭐⭐ | 高         |
| 任务意图检测与引导注入            | ⭐⭐⭐     | 中         |
| MaxTokensBoost指数级截断恢复      | ⭐⭐⭐     | 中         |
| 子代理后台回收器(Sweeper)         | ⭐⭐⭐⭐   | 高         |
| 工作准入排空模式                  | ⭐⭐⭐     | 中         |
| 5层上下文触发(T1-T5)              | ⭐⭐⭐⭐   | 高         |
| 工具参数级路径筛查(PathGuard)     | ⭐⭐⭐⭐   | 中         |
| 外部路径六检查审批网关(approve_dir) | ⭐⭐⭐⭐ | 中         |
| OS沙箱敏感目录读遮蔽(bwrap/Seatbelt) | ⭐⭐⭐⭐ | 中         |
| 128K Token四道闸门(boot/build/spawn/env) | ⭐⭐⭐ | 低        |

### 3.2 DeepAgents 独有但 Sherry 缺失

> 复核注（2026-09-17）：原表「O_NOFOLLOW 符号链接防护」「符号链接循环检测」两项经复核确认 Sherry 已补齐（`agent/tools/pub_base/path_utils.py:55,71`），移出本表。
>
> 第二批（2026-09-17）：P0-4 落地后，「Glob时间预算+匹配上限」「伪文件系统修剪」两项移出本表（`agent/tools/file_tools/search_scan.py`，提交 `6073f7c`/`c77846e`/`4db42e0`）；「base64参数编码防Shell注入」经评估后**否决**，不再列为待借鉴项（见头部第 6 条）；「花括号展开限制」对 Sherry 不适用（搜索走 `fnmatch`，无花括号展开）。
>
> 第三批（2026-09-18，基准 `main` @ `2b9569b`）：P0-2 工具结果驱逐（提交 `4a260c7`/`565fc13`/`466c7d3`/`06f8d77`）与 P2-4 `read_file` 执行期切片落地后，「工具结果→文件系统驱逐+head/tail预览」「read_file结果切片(非卸载)」两项移出本表；「参数截断」改写为「摘要期模型感知参数截断」（Sherry 的 `truncate_tool_args` 仅在预算截断路径生效，摘要期无模型感知设置）。下列为复核后仍为 DeepAgents 独有的能力。

| 能力                                   | 价值评估   | 实现复杂度 |
| -------------------------------------- | ---------- | ---------- |
| 内联base64媒体卸载                     | ⭐⭐⭐⭐   | 中         |
| 摘要期模型感知参数截断(TruncateArgsSettings) | ⭐⭐⭐⭐ | 中       |
| 模型感知摘要默认值                     | ⭐⭐⭐⭐   | 低         |
| DeltaChannel增量检查点(O(N²)→O(N))     | ⭐⭐⭐⭐⭐ | 高         |
| 消息增量缩减器(去重+墓碑+重置)         | ⭐⭐⭐⭐   | 高         |
| 多模态内容清理(替换不支持块)           | ⭐⭐⭐     | 中         |
| 中间件脚手架保护(不可排除)             | ⭐⭐⭐⭐   | 低         |
| 排除覆盖审计(typo检测)                 | ⭐⭐⭐     | 低         |
| 路径感知HITL谓词(exact vs bulk)        | ⭐⭐⭐⭐   | 中         |
| 批量模式绕过HITL检测                   | ⭐⭐⭐⭐   | 中         |
| 持久化工具审批策略(字节修订CAS)        | ⭐⭐⭐     | 中         |
| ripgrep双重超时看门狗(SIGKILL双重有界等待) | ⭐⭐⭐⭐ | 中        |
| 威胁模型文档(THREAT_MODEL.md)          | ⭐⭐⭐     | 低         |
| 输出截断原因标记                       | ⭐⭐⭐     | 低         |

---

## 4. 架构理念对比

| 维度           | Sherry Agent                                       | DeepAgents                                       |
| -------------- | -------------------------------------------------- | ------------------------------------------------ |
| **设计哲学**   | 重运行时防护 — 所有安全机制围绕中间件链+包装器构建 | 重框架安全 — 安全机制围绕后端协议+中间件栈构建   |
| **防护层级**   | 5层+路径闸门：车道→包装器→中间件(含PathGuard)→工具闸门→后台守护 | 3层：后端→中间件→图配置                          |
| **失败模式**   | Fail-open：所有安全中间件异常被吞掉(log+return)    | 渐进降级：超时→部分结果(truncated=True)→最终错误 |
| **配置驱动**   | TypedDict + config/features/ 38个配置模块          | 构造函数参数 + 硬编码常量                        |
| **状态管理**   | session_id键 + state_register_mem + SQLite持久化 + persisted_message_ids水位 | 图状态 + DeltaChannel + 后端持久化               |
| **并发模型**   | 4车道asyncio.Semaphore + FIFO排队                  | 无显式并发控制                                   |
| **记忆策略**   | 逐边界/工具返回持久化→溢出裁剪→记忆冲洗→截断→压缩   | 卸载到后端→摘要→截断                             |
| **子代理模型** | 深度限制+CWD隔离+工具继承策略+后台回收             | 类型验证+递归拒绝+权限继承+状态键过滤            |
| **错误处理**   | 8步分类→回退链→断路器                              | 结构化JSON错误→裁剪重试→终止                     |
| **安全文档**   | docs/sandbox/README*(威胁模型章节+局限清单) + docs/token-guard/ | THREAT_MODEL.md完整威胁模型(2026-03-28生成，可能滞后于代码) |

---

### 总评

- **Sherry Agent** 在**运行时行为安全**与**文件工具路径纵深防御**方面更强：工具调用病理检测、输出重复防护、LLM错误处理、并发控制、崩溃回路、心跳检测、TODO停滞追踪、`PathGuard` 参数级筛查、外部路径六检查审批、OS 沙箱读遮蔽等，形成了深度防御体系。适合**长时运行、多子代理、复杂工具编排**的场景。

- **DeepAgents** 在**记忆/上下文工程与框架安全**方面更强：`virtual_mode` 虚拟命名空间路径模型、增量检查点、模型感知摘要默认值、多模态内容清理、中间件脚手架保护、威胁模型文档等（base64 参数化 Shell 经评估后否决；伪文件系统修剪与文件搜索上限已补齐，见 §2.8 / §3.2；消息驱逐/溢出裁剪/read_file 切片已对齐，见 §2.4 / §2.10 / §3.1）。适合**文件操作密集、多模型供应商**的场景。

两者互补性极强：Sherry 可从 DeepAgents 借鉴增量检查点优化、摘要期模型感知参数截断、模型感知摘要默认值、多模态内容清理和威胁模型文档（消息驱逐/溢出裁剪/read_file 切片已于第三批落地，见 §2.4 / §2.10 / §3.1）；DeepAgents 可从 Sherry 借鉴工具病理检测、输出重复防护、LLM错误处理、并发控制、OS级沙箱和外部路径人审网关。
