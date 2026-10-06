# 计划: 预设新增三栏 —— 工具 / 中间件 / 子代理模型

> **状态**: 待实施
> **创建日期**: 2026-10-06
> **交付形态**: 「预设」面板（PersonaPanel）在「用户信息」之后新增三个标签页——**工具**（main agent 用哪些工具）、**中间件**（main agent 开哪些中间件）、**子代理模型**（general/researcher/executor/reviewer/librarian 各自用哪个模型）。三项配置随预设保存，应用后写入当前会话（下一轮生效），新建会话流程同样写入。
> **已定取舍**（用户未逐项选择，按最稳妥默认；任一可改）:
> 1. **生效时机** = 应用到当前会话、下一轮生效（与「模型切换 / 思考开关」同一规格：忙时挂起到轮次边界）；
> 2. **中间件范围** = 只放开非安全必需项（5 个），其余 13 个（12 个 `_MAIN_REQUIRED` + 提示词注入 + 思考控制）锁定只读；
> 3. **子代理模型取值** = 从「环境配置」的模型档案里选（含 provider / 密钥 / base_url，能真正换模型），不选则沿用角色 `model_tier`。

---

## 〇、现状：为什么必须这样做（三条结构事实）

| 事实 | 证据 | 后果 |
| --- | --- | --- |
| **工具集是编译期固定的进程级单例** | `agent/core.py:96` `_tools = build_main_tools()`、`:102-103 get_agent_tools()`，`create_agent(tools=...)` 在 `:257-263`；图缓存只按事件循环键控（`:123-124`、`:284-325`），且 `builtin_agent()` 有 6 处无参调用点 | 不能"按会话重建图"（改动面大、每次重建昂贵）；只能在**调用期**裁剪模型可见工具 |
| **中间件的成员与顺序是构建期契约** | `agent/core.py:143-223`（19 项硬编码链）；`tests/agent/core/test_middleware_order.py:18-38` 逐名钉死；`agent/middlewares/scaffolding.py:63-88` 12 项 `_MAIN_REQUIRED` 在 `create_agent` 之前断言 | 不能按会话增删成员；可行做法是"**常驻实例 + 钩子内按会话 no-op**"（HITL / 访问模式已有同型先例） |
| **子代理"按名换模型"是死通道** | `agent/tools/subagent/spawn/core.py:1110-1116` `from models import build_llm_by_name`，而 `models/__init__.py` 无该符号 → 该分支永远落回 tier/深度选择；子代理**不继承**父会话的 `LLM_MAIN_MODEL`（只有 `build_main_llm()`/`build_auxiliary_llm()` 两个 env 模型） | 需要新的"角色 → 模型档案"注入点，且必须用 `build_main_llm_for_profile()`（`models/LLMs/main_llm.py:306`）才能换 provider/密钥 |

另一条现状：客户端**没有任何**工具名 / 中间件名 / 角色名清单（全仓 grep 仅命中 i18n 文案与测试夹具），所以三栏 UI 必须由后端提供只读目录接口，前端不得硬编码后端名字。

---

## 一、数据来源：`GET /agent/catalog`（新，只读）

新增 `server/trigger/http/agent_catalog.py`，一次返回三份清单：

```jsonc
{
  "tools": [ { "name": "read_file", "group": "files" }, ... ],
  "middlewares": [ { "name": "TaskIntentMiddleware", "required": false }, ... ],
  "subagent_roles": [ { "role": "researcher", "model_tier": "auxiliary", "description": "..." }, ... ]
}
```

- **tools**：名称取实际构建出的工具（`build_main_tools()` 的 `tool.name`），分组表写在 `agent/tools/catalog.py`——按 `agent/tools/__init__.py:41-60` 的 builder 来源映射到组：`files`（read/write/patch/search_files）、`terminal`（terminal/python_repl）、`tasks`（taskflow/todolist/knowledge）、`memory`（memory/message_search）、`skills`（skill_*）、`subagents`（subagent runtime）、`interaction`（question）、`web`（web_search）、`mcp`（mcp 工具）。
- **middlewares**：名字取 `_build_middlewares()` 链上的类名（`agent/core.py:143-223`），`required` 由 `scaffolding._MAIN_REQUIRED` 判定（再叠加 `system_prompt_injection` / `ThinkingControlMiddleware` 两个逻辑必需项，见 §四）。
- **subagent_roles**：`roles/loader.py::load_all_role_definitions()` 的产物（`RoleDefinition.role / model_tier / description`）；GENERAL 没有定义文件，单独合成一行（`model_tier: null` = 深度默认）。

失败开放：目录接口只读、无副作用；某项读取失败按空列表返回并记日志，不 500。

---

## 二、按会话的 agent 配置

### 2.1 状态键

`runtime/session/state_keys.py` 新增一对孪生键（沿用现有命名风格）：

```
AGENT_CONFIG = "agent_config"                  # 生效值
AGENT_CONFIG_PENDING = "agent_config_pending"  # 忙时挂起（与 LLM_MAIN_MODEL 同型）
```

载荷（单一 JSON，缺项 = 默认）：

```jsonc
{
  "tools": ["read_file", "terminal", ...] | null,   // null/缺 = 全开
  "middlewares_disabled": ["TaskIntentMiddleware"], // 缺/空 = 全开
  "subagent_models": { "researcher": { "id": "...", "label": "...", "provider": "...",
                                        "model": "...", "base_url": "...", "api_key": "..." } | null }
}
```

### 2.2 服务

新增 `server/service/agent_config_service.py`：

- 读取：`get_agent_config_state(session_id)`（mem → durable，泊车值优先，与 `session_settings_service` 同构）。
- 写入：`set_agent_config(session_id, payload)` —— **白名单校验**：工具名 ∈ 目录、中间件名 ∈ 目录且非必需（必需项出现即 400，错误信息点名）、角色名 ∈ `FunctionalRole`；子代理模型描述符复用 `sanitize_main_model_override`（`session_settings_service.py:278-313`）的字段规则（`model` 必填字符串、其余可选、未知字段拒绝）。
- 泊车/提升：把 `(AGENT_CONFIG, AGENT_CONFIG_PENDING)` 加进 `_SETTINGS_KEYS`（`session_settings_service.py:56-59`），从而自动获得 `turn_runner.on_turn_finished` 的提升（`turn_runner.py:253`）与 HITL 延后，不需要新钩子。

### 2.3 端点

`server/trigger/http/session_settings.py` 旁新增 `GET/PUT /sessions/agent_config`（同文件的请求体读取 / `ok()` / `bad_request()` / `pending` 语义）。

---

## 三、工具按会话生效：`ToolSelectionMiddleware`

新增 `agent/middlewares/tool_selection/`（`core.py` + `__init__.py`），注册在 `ProjectDirNoticeMiddleware` 之后（`agent/core.py`）。

- `wrap_model_call` / `awrap_model_call`：从 `request.state["session_id"]` 读配置（mem 寄存器，只读、无阻塞 IO——与 `thinking_control/core.py:140-171` 同规格）。未配置或 `tools == null` → 原样放行；否则 `request.override(tools=[t for t in request.tools if t.name in enabled])`。
  - **可行性已核实**：安装版 langchain 1.3.9 的 `ModelRequest.override(tools=...)` 存在（`site-packages/langchain/agents/middleware/types.py:72-83, 201-267`），且 `factory.py:1328, 1394-1400` 按 `request.tools` 重新 `bind_tools`。
- `wrap_tool_call` / `awrap_tool_call`：补"ToolNode 仍持有全部工具"的洞——被关掉的工具若仍被调用（旧 checkpoint 状态、模型幻觉），返回 `status="error"` 的 ToolMessage 拒绝执行，措辞说明"该工具已被会话配置禁用"。
- 失败开放：任何读取/裁剪异常 → 记录并放行原请求（与 ThinkingControl 同策略）。
- 需要同步：`agent/middlewares/__init__.py` 导出；`tests/agent/core/test_middleware_order.py::EXPECTED_ORDER` 加名；四语 `agent/middlewares/README` 增章节与链位置。

---

## 四、中间件按会话生效（只放开非必需项）

新增助手 `agent/middlewares/agent_switch.py`：

```python
def middleware_enabled(session_id: str | None, name: str) -> bool:
    """会话配置里是否启用该中间件（mem-only；缺省 True；异常 → True）。"""
```

在**可关项**的钩子入口各加一行 early-return（关掉即 no-op，链成员与顺序不变）：

| 可关（5） | 入口 | 关掉的语义 |
| --- | --- | --- |
| `TodoContinuationEnforcer` | `aafter_agent` | 计划未完成不再自动续跑 |
| `TaskIntentMiddleware` | `abefore_model` | 不再注入任务意图引导 |
| `ProjectDirNoticeMiddleware` | `abefore_agent` | 目录切换不再通知 agent |
| `MultimodalProcessor` | `abefore_agent` | 上传的图片/音频/视频不再预处理 |
| `SubagentCompletionDrainMiddleware` | `abefore_model` | 子代理完成载体不再注入 |

**锁定（前端只读 + 说明）**：12 个 `_MAIN_REQUIRED`（HITL 审批、摘要压缩、上下文驱逐、PathGuard、LLM 重试、迭代预算、ToolGuardrails、ToolCallNormalize、OutputRepetitionGuard、MaxTokensBoost、MessagePersistence、HeartbeatStaleness）+ `system_prompt_injection`（关掉即无系统提示词）+ `ThinkingControlMiddleware`（它本身就是模型/思考开关）。

---

## 五、子代理按类型选模型

- 注入点：`agent/tools/subagent/spawn/core.py:349-364`（`role_def` 与 `requester_session_key` 都在作用域内）。读会话配置 `subagent_models[role]` → 作为新形参 `model_profile` 沿 `spawn_subagent_direct → _execute_subagent_with_lane → _execute_subagent → _build_child_agent` 传下去。
- 解析：`_build_child_agent`（`core.py:1099-1118`）在现有 tier 分支**之前**判断——配了 profile 就 `build_main_llm_for_profile(provider=…, model=…, api_key=…, base_url=…)`；没配走原有 `_select_child_llm()` 不变。
- 顺带修 `control/steer.py:139-145`：steer 重建子代理时带上同一 profile（现在 steer 会丢角色模型）。
- **不做**（明确排除）：复活 `build_llm_by_name` 死通道、给 `sessions_spawn` 工具加 `model` 参数、把模型选择写进 `SubagentRunRecord`（重启恢复）——需要时另开计划。

---

## 六、前端：三栏 + 预设存取

### 6.1 桥接与 store

- `client/app/composables/bridge/agent-config.ts`：`fetchAgentCatalog()`（GET `/agent/catalog`）、`fetchAgentConfig(sid)`、`setAgentConfig(sid, payload)`；类型与现有 `bridge/session.ts` 同风格（显式模块说明符，便于测试 `vi.mock`）。
- `client/app/stores/agent-config.ts`：per-session `hydrate(sid)` / `update(sid, patch)`，乐观写入 + 失败回滚 + `pending` 标记（照抄 `stores/session-model.ts` 的形状）。

### 6.2 三个标签页（`PersonaPanel.vue`，「用户信息」之后）

1. **工具**：按 §一 的分组渲染，每组一行组头（组名 + 全选/清空 + `n/m`）与逐项勾选框；顶部一个"全部启用/全部禁用"；默认全选（= 不配置）。
2. **中间件**：5 个可关项用开关；13 个锁定项灰显 + 锁图标 + 「系统必需」说明；默认全开。
3. **子代理模型**：每个角色一行（角色名 + 描述 + `model_tier`）；选择器列出「跟随角色默认（tier）」+「环境配置档案」的档案（复用 `stores/llm-profiles.ts` 与 `SessionModelPicker.vue` 的描述符构造 `toDescriptor`）。

数据流：三栏编辑的是**本地草稿**（与文件 tab 的 `editContent` 同规格）→ 「保存预设」写入预设、「应用」写入当前会话（`PUT /sessions/agent_config`，忙时后端自动泊车）。

### 6.3 预设模型

- `db.ts`：`PersonaPreset` / `PresetCharacter` 旁新增 `AgentProfile = { tools?: string[] | null; middlewares_disabled?: string[]; subagent_models?: Record<string, SessionModelProfile | null> }`；`PersonaPreset.agent?: AgentProfile`（**非索引字段，不需要 Dexie 版本升级**）；`usePersonaPresets.create/update` 透传。
- `persona-catalog.ts`：`PersonaPresetPayload` 增 `agent?: AgentProfile`；两个内置预设 = 空（全默认，不改变现网行为）；`loadPresetPayload` 带上；`applyPresetPayload(payload, sessionId?)` 在写文件/角色后追加一次 `setAgentConfig`（新建会话弹窗因此自动带上）。
- 只读查看器 `SessionPresetPanel.vue` 补三段摘要（工具计数、被关中间件、按角色模型）。

### 6.4 国际化

`PersonaPanel.vue` 内联 `<i18n>` 块新增 `config.agent.*`：三个栏名、工具分组名（9 组）、5 个可关中间件名 + 锁定说明、5 个角色名、若干提示语（"跟随角色默认"、"系统必需"、"已选 n/m"），四语齐全；`app/composables/__tests__/locales.test.ts` 的键结构断言同步。

---

## 七、测试清单

| 层 | 用例 |
| --- | --- |
| 后端 · 目录 | `/agent/catalog` 返回三份清单；分组覆盖全部主工具；必需中间件 `required=true` |
| 后端 · 服务 | 白名单拒绝未知工具/中间件/角色（400 语义）；必需中间件被拒；子代理描述符校验；忙时泊车、轮次边界提升、空闲直接生效 |
| 后端 · 工具门控 | 未配置 → 请求原样；配置子集 → `request.tools` 只剩子集；被关工具调用被拒（error ToolMessage）；读配置异常 → 放行 |
| 后端 · 中间件开关 | 关掉 5 项各自 no-op（含 `TodoContinuation` 不再续跑、`TaskIntent` 不再注入）；未配置全开 |
| 后端 · 子代理 | 配了 profile → `build_main_llm_for_profile` 被调用；未配 → 原 tier 选择不变；steer 保留 profile |
| 前端 · 三栏 | 三栏渲染（TabPanel 表头顺序更新）；工具分组勾选与计数；必需中间件不可点；角色选择器写入描述符 |
| 前端 · 预设 | 保存→选中→应用 round-trip 带上 `agent`；内置预设为空；`applyPresetPayload` 追加一次 agent_config 写入（含新建会话流程） |
| 前端 · 只读查看器 | 三段摘要渲染 |

---

## 八、文档同步

- `agent/middlewares/README.{md,zh,ja,ko}`：新中间件章节、链位置、5 个可关项的开关说明。
- `client/README.{md,zh,ja,ko}`：预设面板三栏、新文件（bridge/store）、`PersonaPreset.agent`。
- `AGENTS.md`：配置语义（生效时机、锁定项、目录接口）与中间件链条目的更新。
- 门禁：`tests/run_tests_split.py`、`ruff`、`basedpyright agent/`、`lint-imports`、客户端四件套、`check_docs_parity.py` + `check_doc_links.py`。

---

## 九、交付顺序

| 步骤 | 内容 | 依赖 |
| --- | --- | --- |
| P1 | 状态键 + `agent_config_service` + `GET/PUT /sessions/agent_config` + `GET /agent/catalog`（含测试） | 无 |
| P2 | `ToolSelectionMiddleware` + 链登记 + 顺序表 + 中间件门控（含测试） | P1 |
| P3 | 子代理按角色模型（含 steer） | P1 |
| P4 | 前端桥接 / store → 三栏 UI → 预设 round-trip → 只读查看器 → i18n | P1–P3 |
| P5 | 四语文档 + 全量门禁 + 浏览器实测 + 提交推送 CI | P4 |

估：P1+P2+P3 ≈ 1.5d，P4 ≈ 1.5d，P5 ≈ 0.5d。

---

## 十、风险与已知代价

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 工具只裁"模型可见"层 | 旧 checkpoint 里已发出的调用仍会打到 ToolNode | 同中间件的 `wrap_tool_call` 拒绝执行（§三），并有测试 |
| 中间件开关被误当成"性能开关" | 关掉多模态/意图引导会让行为明显变化 | 前端每项带一句"关掉后……"的说明；锁定项标注"系统必需" |
| 子代理模型选错 provider/密钥 | 子代理起不来 | 复用主模型切换的同一描述符校验；失败时现有 tier 兜底（构造失败按 `_select_child_llm()` 回退并记警告） |
| 预设新增字段 | 旧预设无 `agent` 块 | 缺省即全默认（`undefined` 容错，与角色块的先例一致） |
| 顺序契约测试 | 新增中间件会红灯 | P2 同批更新 `EXPECTED_ORDER`（既有做法） |

---

## 附: 证据来源（本计划撰写时核对）

| 事实 | 来源 |
| --- | --- |
| `request.override(tools=...)` 可用、无 `override_tools`，`_get_bound_model` 按 `request.tools` 重绑 | 安装版 `langchain 1.3.9`：`agents/middleware/types.py:72-83, 99, 201-267`；`agents/factory.py:1328, 1394-1400`（源码确认） |
| 工具集编译期固定、图缓存仅按事件循环键控、6 处无参 `built_agent()` | `agent/core.py:96, 102-103, 123-124, 257-263, 284-325`（源码确认） |
| 中间件顺序为契约、`_MAIN_REQUIRED` 12 项、构建前断言 | `agent/core.py:143-223`；`tests/agent/core/test_middleware_order.py:18-38`；`agent/middlewares/scaffolding.py:63-88`（源码确认） |
| 泊车/提升机制与 `_SETTINGS_KEYS` | `server/service/session_settings_service.py:56-59, 94-104, 373-441`；`server/service/turn_runner.py:253`（源码确认） |
| 按会话读寄存器、提前缓存 LLM 变体的先例 | `agent/middlewares/thinking_control/core.py:120-215`（源码确认） |
| `build_llm_by_name` 不存在 → 按名换模型是死通道；`build_main_llm_for_profile` 是唯一可换 provider 的构造器 | `agent/tools/subagent/spawn/core.py:1110-1118`；`models/__init__.py:4-22`；`models/LLMs/main_llm.py:306-346`（源码确认） |
| 子代理不继承父会话主模型覆盖 | `agent/tools/subagent/spawn/core.py:752-759`（只写 `REQUESTER_SESSION_KEY` / `CALLER_SCOPE`）（源码确认） |
| 角色定义与 tier 解析（5 角色、未知 tier → inherit、每次 spawn 重读文件） | `agent/tools/subagent/roles/loader.py:31-39, 60-117`；`roles/definitions/*/AGENTS.md`（源码确认） |
| 客户端无工具/中间件/角色清单，目录接口必需 | 全仓 grep（`read_file`/`ToolGuardrails`/`executor` 仅命中 i18n 与测试夹具）（源码确认） |
| 预设模型与 Dexie 版本规则（非索引字段无需升版） | `client/app/composables/db.ts:230-256, 307-343, 712-790`；`client/app/composables/usePersonaPresets.ts`（源码确认） |
| 会话模型选择器的描述符与桥接形状 | `client/app/stores/session-model.ts`；`client/app/composables/bridge/session.ts:456-526`；`components/SessionModelPicker.vue:80-177`（源码确认） |
| 新建会话流程的 apply → bind → snapshot → navigate | `client/app/pages/home/components/NewSessionPresetDialog.vue:107-139`（源码确认） |
