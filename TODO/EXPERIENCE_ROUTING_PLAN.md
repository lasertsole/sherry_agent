# 经验分流方案（计划注意事项保留 / 模块技能 / FACTS.md）

> 来源：用户对经验提炼的最终设计决策（2026-09-19）。三部分互相独立、可分批执行。
> 背景：`skills/auto/` 同时承载**泛化能力**与**流程能力**；本方案解决"各类经验分别去哪、怎么注入"的分流问题。
> 本文件只描述当前规划，不含历史叙述。

## 目标

| 经验类型 | 去向 | 生命周期 |
| --- | --- | --- |
| **计划绑定的注意事项**（执行本计划学到的、只对本计划有意义的坑/约定） | 压缩摘要的专用节（Part 1） | 计划活跃期全程保留，计划完成即弃 |
| **领域绑定的坑**（绑定具体文件/模块/测试，未来碰同一模块会再遇到） | `skills/auto/` 的 `<module>-notes` 技能（Part 2） | 技能库长期，curator 管理 |
| **广泛存在的坑**（不绑定领域，任何工作都可能遇到） | `FACTS.md` 第三记忆文件（Part 3） | 常驻上下文，字数上限内滚动淘汰 |

与既有记忆的分工：`MEMORY.md`（用户偏好/行为期望/环境-项目事实）、`USER.md`（用户画像）、计划文件 + todos（进度真源）不变。

---

## Part 0：压缩摘要 structured_output 化（Part 1 的实现前置）

### 问题（free-form Markdown 的脆弱点）

1. 结构靠 prompt 指令约束（"Keep every section, even when empty"）——LLM 偶发漏节/改节名，下游解析漂移
2. `_enforce_fifo_limits` 在**事后解析 Markdown** 执行 Completed/Key Decisions 的 FIFO——对自由文本做节解析，脆
3. Part 1 要加的 Active Plan Notes"逐字延续"也只能靠提示词承诺，LLM 违约无兜底
4. 总长超限的 head/tail 截断可能把中段节切烂

### 方案：aux 压缩改走 LangChain structured_output，代码拼接 Markdown

1. **Pydantic `SummaryDoc`**（字段与现模板一一对应）：`latest_user_request / goal / constraints[] / completed[] / in_progress[] / blocked[] / key_decisions[] / next_steps[] / critical_context[] / relevant_files[]` + **`active_plan_notes[]`**（Part 1 的载体，代码层 cap 与逐字延续）+ **`evicted_refs[]`**（本次压缩覆盖范围内出现过的驱逐文件路径——预览中的 `[evicted to: <path>]` 收进字段，随 Doc 链逐字延续、不受 FIFO 限制；保证被驱逐的超长内容**在摘要链上永远带指针**，模型任意后续轮次可顺指针 `read_file`/`message_search` 找回全文；计划完成后该数组清空）
2. **aux 模型**经 `with_structured_output(SummaryDoc)`（失败退化 `json_mode` + `json_repair`——仓库既有 `instructor`/`json_repair`）；链式更新时把**上一份 SummaryDoc** 作为结构化输入与 `<conversation>` 一起喂入，输出**合并后的新 Doc**（"conversation wins" 语义由 prompt 表达，载体类型化）
3. **代码渲染 Markdown**（节与顺序同现模板）→ 包 `<summary>` 标签进摘要对——模型可见形态不变；**同 Doc → 同渲染字节**，前缀缓存不受影响
4. **FIFO/上限变数组切片**：`completed[-5:]`、`key_decisions[-5:]`、`active_plan_notes[-20:]`——`_enforce_fifo_limits` 的 Markdown 解析**整段删除**；`SUMMARY_TOTAL_MAX_CHARS` 的 head/tail 截断对 Doc 渲染后仍作最终保险
5. **`_extract_previous_summary` 升级**：优先读摘要对 `additional_kwargs` 里的 Doc（结构化），回退正则（旧会话 MD 形态兼容）
6. **旧摘要自然过渡**：链上第一轮 JSON 压缩把旧 MD 当 `<prior-summary>` 文本照常喂入，输出即新 Doc——存量无需迁移

### 文件清单（Part 0）

| 文件 | 修改类型 | 说明 |
| --- | --- | --- |
| `agent/middlewares/summarization/summary_doc.py` | 新建 | `SummaryDoc` Pydantic 模型 + 渲染器（JSON → 现模板 Markdown） |
| `agent/middlewares/summarization/core.py` | 修改 | `_create_summary`/`_acreate_summary` 改 structured_output；`_enforce_fifo_limits` 删除（数组切片替代）；`_build_new_messages` 渲染接 `additional_kwargs["summary_doc"]` |
| `tests/agent/middlewares/` | 新增 | Schema 往返、cap 执行、畸形 JSON 兜底、链式渲染字节稳定、旧 MD 过渡 |

### 风险与对策

| 风险 | 对策 |
| --- | --- |
| provider structured-output 兼容性（`glm-5.3-flash` openai 兼容口） | 实现前小验证；退化 `json_mode`+`json_repair`；再不行保留 free-form 旧路径作 fallback |
| JSON 输出 token 略增 | 内容不变，增量小；实测确认 |
| 摘要对不落 MesMemory（lc_source 过滤） | 无下游形态依赖 |

## Part 1：压缩保留计划注意事项（计划活跃期）

### 现状（已核实，`agent/middlewares/summarization/core.py`）

1. **链式延续指令已存在**：`_SUMMARY_UPDATE_INSTRUCTIONS`（`:250-262`）明确 "anything you do not carry into the new summary is lost" 与 "Carry forward objectives, constraints, decisions from `<prior-summary>` even when the `<conversation>` does not mention them"。上一批的链式摘要过滤（`_filter_summary_messages` 把旧摘要对剔出 `<conversation>`）**不丢内容**——旧摘要文本经 `_extract_previous_summary`（`:1611`）注入 `<prior-summary>`，由延续指令负责搬运。
2. **真正的缺口——模板级 FIFO 淘汰**：`_SUMMARY_TEMPLATE` 的指令要求 "keep only the most recent N items in Completed / Key Decisions（较早的加 omitted）" 且 "Remove items that are finished and no longer needed"。⇒ 计划注意事项若被写进这些节，**跨 N 次压缩后会被 FIFO 逐出**；"看起来已完成"的注意事项会被 "Remove finished items" 指令删除。
3. **压缩器缺少计划权威上下文**：TaskFlow 已有先例——`_get_taskflow_context_sync(session_id)`（`:283`）把会话的 TaskFlow 状态作为 "authoritative" 注入摘要 prompt；**当前计划（plan_ref）没有同等待遇**，摘要器不知道"有一个活跃计划及其注意事项"。

### 方案（实现载体 = Part 0 的 `SummaryDoc`）

1. **`Active Plan Notes` 成为 `SummaryDoc.active_plan_notes[]` 字段**（不再向 `_SUMMARY_TEMPLATE` 加节）：
   - **不受 FIFO 限制**：cap 在代码层（`active_plan_notes[-20:]`，淘汰最旧并注记）——同时解决总长 head/tail 截断可能切烂该节的问题
   - **逐字延续在代码层强制**：合并时该数组**原样继承**上一份 Doc 的条目（LLM 只做"新增条目"的追加，不重写既有条目）
   - **新增吸收**：对话（或上一份 Doc）中出现的新计划教训 → 追加进数组（每条：现象 → 规避动作，一行一条）
   - **计划完成即弃**：todos 全部 completed/cancelled 时，渲染与下一份 Doc 都不再携带该数组（避免死计划残留）
2. **摘要 prompt 注入计划上下文**（仿 TaskFlow 注入）：新增 `_get_plan_context_sync(session_id)`——
   - 计划活跃判定：`state_register_db.get_state(session_id, "plan_ref")` 或本会话 todos 的 `plan_ref`（复用 `knowledge/ownership.py` 的来源 ①② 模式）
   - 注入内容：计划文件相对路径 + 计划名 + todos 未完成项概要（**计划全文不注入**，避免与模型可自行 `read_file` 重复）
   - 放在 TaskFlow 注入块旁，同为 "authoritative" 标注
3. **计划上下文的注入同样作用于首次摘要**（`_SUMMARY_PROMPT_FIRST` 路径）：计划注意事项在**第一次压缩**就该被收进 `active_plan_notes`，不等链式

### 文件清单

| 文件 | 修改类型 | 说明 |
| --- | --- | --- |
| `agent/middlewares/summarization/core.py` | 修改 | 模板加节、更新指令加延续/吸收/弃用规则、新增 `_get_plan_context_sync` 与 prompt 注入 |
| `tests/agent/middlewares/`（既有 summarization 测试文件内追加） | 新增 | 见测试计划 |
| `docs/summarization/README{,.zh,.ja,.ko}.md` | 修改 | 摘要模板节与指令说明 |

### 测试计划（Part 1）

- 跨 5 次链式压缩后，Active Plan Notes 节的条目**逐字保留**（FIFO 不影响该节）
- 对话中出现的新计划教训在下一次压缩后被并入该节
- 计划完成（todos 全 done）后压缩 → 该节被移除
- 无活跃计划 → 不出现该节、指令不提计划
- 既有摘要测试全绿（模板加节不得破坏既有断言的结构性检查）

### 执行顺序（Part 1）

模板加节 → 更新指令 → 计划上下文注入 → 测试 → 文档

---

## Part 2：领域坑 → `<module>-notes` 技能

### 问题

绑定具体文件/模块/测试的教训（如"改 `auth/token.py` 必须先跑 `test_auth_17`"）在 plan-extraction 的 Part 2 里容易被判为"不够通用"而不沉淀——但它对**未来碰同一模块的计划**价值很高。

### 方案（plan-extraction Part 2 指引增强）

1. `_nudge_plan_extraction` 的 prompt（`nudges.py`）Part 2 增加 **carry-forward 指引**：
   - 判定标准：教训**绑定具体文件/模块/测试**，且该模块**可预见会被再次改动**
   - 产出：`skill_manage(action="create"/"update", name="<module>-notes")` ——命名约定 **`<module>-notes`**（如 `auth-module-notes`）；已存在同名技能则 **update/append**
   - 内容格式：按模块归类的"现象 → 原因 → 规避动作"清单，简短可执行
   - **宁缺毋滥**：一次性任务细节留给 Part 1 计划知识；LLM 判定不可复用的不写
2. 与 Part 1 的分工：Part 1 = 这份计划的上下文（不进技能库）；carry-forward = 模块级的、跨计划可复用的注意事项
3. `<module>-notes` 是普通 agent 自建技能 ⇒ **自然进入 curator 生命周期**（合并/归档/使用统计），无需改 curator

### 文件清单

| 文件 | 修改类型 | 说明 |
| --- | --- | --- |
| `agent/middlewares/summarization/nudges.py` | 修改 | Part 2 prompt 增加 carry-forward 指引与命名约定 |
| `tests/agent/middlewares/` | 新增 | 指引锁定 + 行为测试（create/update 分支） |
| `docs/experience/README{,.zh,.ja,.ko}.md` | 修改 | plan extraction 产出补充第三类：模块注意事项技能 |

### 测试计划（Part 2）

- prompt 内容断言：carry-forward 指引与 `<module>-notes` 约定存在
- 行为测试：假 LLM 按新指引产出 `skill_manage(create, name="auth-module-notes")` → 技能落在 `skills/auto/`；同名已存在 → update 不新建
- 既有 nudge/plan-extraction 测试全绿

---

## Part 3：广泛坑 → `FACTS.md`（第三记忆文件）

### 问题

不绑定领域的、广泛存在的坑（如"本机 `test_auth_17` 依赖网络"、"Windows 下路径分隔符注意"）目前**没有任何去处**：不绑定模块（不适合技能）、又需要**常驻上下文**（任何工作都可能遇到）。`MEMORY.md`/`USER.md` 的关注面是用户与行为方式，不宜混入。

### 方案：`FACTS.md` 作为第三记忆文件，经 nudge 机制维护

1. **文件与模板**：`workspace/memory/FACTS.md`（初始内容一句用途注释）；加入 `workspace/__init__.py` 文件清单与 `workspace/template/<lang>/`（四语模板）；`file_sync.py` 懒同步覆盖；存量工作区首次启动自动创建（照 USER.md 缺失处理模式）
2. **memory 工具/存储扩 `facts` target**（`agent/tools/memory.py`）：
   - `_path_for("facts") → mem_dir / "FACTS.md"`；`facts_char_limit`（建议默认 1375，与 USER 同级；配置键照既有风格进 memory 配置 TypedDict）
   - `add/replace/remove` action 支持 `facts`；上限执行/原子写/滚动淘汰**照既有 MEMORY/USER 机制**（淘汰最旧）
   - 归入规则（prompt 与注释定义）：绑定具体模块 → 技能（Part 2）；用户偏好 → USER；**跨计划普适的坑/约定** → FACTS；可泛化做法 → 技能
3. **注入**：`format_memory_for_system_prompt`（memory_store）把 FACTS.md 并入系统提示词 memory 块——**每次都在上下文里**（这类坑不绑定领域，常驻是合理的）
4. **nudge 维护**（"用 nudge 机制抽"）：
   - **记忆回顾 nudge**（每次压缩，`_nudge_memory`）关注面扩展：除用户/行为外，新增"广泛存在的坑/约定"→ 写 `facts` target（整理式 read-modify-write：prompt 给当前 FACTS.md 内容与上限，要求合并同类、淘汰过时）
   - **plan-extraction nudge**（Part 3）：计划执行中发现的**不绑定模块**的广泛坑 → 同样写 `facts` target
   - 两条 nudge 都可维护，工具层的上限/滚动/锁保证一致性
5. **注入格式**：FACTS.md 内容作为 memory 块内的独立小节（标题 `# FACTS`），与 MEMORY/USER 并列

### 文件清单

| 文件 | 修改类型 | 说明 |
| --- | --- | --- |
| `agent/tools/memory.py` | 修改 | `facts` target（路径/上限/entries/action/淘汰） |
| `config/features/agent_side/memory*.py` | 修改 | `facts_char_limit` 配置键 |
| `workspace/memory/FACTS.md` + `workspace/template/<lang>/FACTS.md` | 新建 | 初始文件与四语模板 |
| `workspace/__init__.py`、`workspace/file_sync.py` | 修改 | 文件清单与懒同步 |
| `agent/middlewares/summarization/nudges.py` | 修改 | 记忆回顾与 plan-extraction 的 FACTS 提炼指引 |
| `workspace/prompt_builder.py`（经 memory_store） | 修改/核实 | FACTS 注入 |
| `tests/agent/tools/`、`tests/workspace/`、`tests/config/` | 新增 | 见测试计划 |

### 测试计划（Part 3）

- `facts` target：add/replace/remove、**上限执行**（超限滚动淘汰最旧）、原子写
- 注入：FACTS.md 内容出现在系统提示词 memory 块（空文件不产生空块）
- nudge 记忆回顾/plan-extraction 的 FACTS 提炼：整理式更新（并入同类、淘汰过时各一条断言）
- 模板：新工作区懒同步产出空 FACTS.md；存量工作区首启自动创建
- 配置契约：`facts_char_limit` 键

---

## 交互与边界（三部分之间）

| 边界 | 规则 |
| --- | --- |
| Part 1 vs Part 3 | 计划绑定（随计划完成弃）vs 跨计划普适（常驻滚动）；一条教训同时满足两者时 → Part 1 收进摘要节，**且**若不绑定模块可进 FACTS（由 plan-extraction 判定） |
| Part 2 vs Part 3 | 绑定模块 → 技能（按需加载）；广泛存在 → FACTS（常驻） |
| Part 2 vs curator | `<module>-notes` 是普通自建技能，curator 可合并/归档——合并后名可能变，provenance 保留 |
| 与 MEMORY/USER | MEMORY/USER 关注面不变（用户与行为方式）；FACTS 是新文件新 target |

## 执行顺序（建议）

0. **Part 0**（压缩摘要 structured_output 化）——**前置**：Part 1 的 Active Plan Notes 依赖 `SummaryDoc` 的类型化载体；本身也是摘要链鲁棒性的独立提升
1. **Part 3**（FACTS.md）——独立、影响面最小、先建立"广泛坑"的去处
2. **Part 2**（`<module>-notes` 技能指引）——独立、纯 prompt 增强
3. **Part 1**（计划上下文注入 + `active_plan_notes` 字段）——触及 summarization 核心，最后做；做完后三部分形成完整分流

> 每部分独立可交付；顺序颠倒不产生正确性问题，只影响经验落位的完备度。

## 不做的事

- 不恢复 `context_engine/facts/`（TieredMemoryStore 分类库，已删除的设计）
- 不把计划文件全文注入摘要 prompt（模型可自行 `read_file`）
- 不给 `MEMORY.md`/`USER.md` 扩新关注面（用户与行为方式仍是它们的内容）
- 不做历史叙述（各文档只写现状）
