# 10 Sessions 与 Prompts：Harness→Sherry 适配

复刻原型 `src/app/sessions.ts` + `src/app/prompts.ts` + `004-prompts.md`。这是 Python 版第二大工程差异（仅次于 Executor）：原型用 DeepSeek Harness 的 Session API，Python 版用 Sherry 的 subagent + LLM。

## 1. Sessions（`superagent/app/sessions.py`）

```python
class Sessions:
    """管 main Session（router）+ Executor Session（worker）。
    两者都是 Sherry subagent run（经 runtime/hooks）。"""
    def __init__(self, host: SessionsHost, settings_getter, prompts: Prompts,
                 role_models: RoleModels):
        ...

    def ensure(self, role: str, session_key: str) -> str:
        """建 Session（若不存在）。role='main'|'worker'。返回 session_key。"""
        ...

    def install(self) -> None:
        """跟 Sherry subagent 事件 + 注册 prompts/skills/tools。"""
        ...

    async def prompt(self, request_id: str, session_key: str, message: str) -> None:
        """每 request_id 一次。经 hooks 向 Sherry session 注入消息。"""
        ...

    async def relay(self, message: Relay) -> None:
        """worker report/question/follow-up → main Session。走 relay effect。"""
        ...

    async def notify(self, notice: Notice) -> None:
        """注入 main Session 不唤醒。走 notice effect。"""
        ...

    def status(self, session_key: str) -> Optional[dict]: ...
    def prompt_version(self, session_key: str) -> Optional[str]: ...
    def model_retry(self, session_key: str) -> Optional[dict]: ...
```

### 1.1 main Session（router）

- 用 `dsh-superagent-router` preset（Python = 受限工具子集的 Sherry 主 agent 或独立 subagent）。
- shell/job 5 秒返回（Sherry terminal 工具的 timeout 配置）。
- `noWait` 守卫：拒 `sleep` 在命令位、`timeoutMs>30s`、`job_output>5s` 等待。报告/job 结果自动到，等只延迟 candidate。
- 不读 repo `AGENTS.md`（原型：main Session 不读，Executor 读）。
- 工具：14 个应用工具（见 §3）+ Sherry 的 skill/bash/read/read_image/job_output/job_list/job_kill。

### 1.2 worker Session（worker）

- 用 `dsh-superagent-worker` preset（Python = Sherry subagent + executor role + 受限工具集）。
- shell 5 秒转后台（Sherry terminal bg）。
- 工具：6 个应用工具（见 §4）+ WORK_TOOLS（file/search/shell/job/web/skill/todo/subagent）。
- **移除**：ask/present/profile/scheduling/其他 Session/外部文档编辑工具。
- Ticket end 后工具调用拒绝。
- 读 repo `AGENTS.md` + Sherry coding prompt。

### 1.3 preset 恢复

Sherry 主 agent 的 preset 可能被 Web UI 改。`ensure` 后若检测到非 router/worker preset，恢复角色 preset（首次 turn 前）。`presetForSession` 经 hooks 读。

### 1.4 relay/notify 投递

- `relay`：worker report/question/follow-up → main Session turn。
- `notify`：domain event notice → main Session 不唤醒。
- turn 中到达的 relay 在下一 step 一起投（Sherry state_port.add_messages 批量注入）。
- 经 `relay`/`notice` effect（commit 后执行，保序）。

### 1.5 prompt revision 绑定

- 每次 model 请求绑当前 prompt revision。
- Sessions 持久化用过的 revision（KV `session-prompts:<sid>`）。
- worker 可读自己用过的 revision（`may_read` 限制）。
- save 不 restart/cancel/wake Session。

## 2. Prompts（`superagent/app/prompts.py`）

```python
class Prompts:
    """team sections + 可覆盖 built-in parts + 生成模板。
    存为 Versions（numbered snapshots）。"""
    def __init__(self, store: Store, context: Context):
        ...

    def current(self) -> PromptConfig: ...
    def version(self, n: int) -> Optional[PromptConfig]: ...
    def history(self) -> list[PromptVersion]: ...
    def validate(self, config: PromptConfig) -> list[Issue]: ...
    def preview(self, config: PromptConfig) -> str: ...     # 装配文本不 save
    def save(self, config, base_revision: int, source: str) -> int: ...
```

### 2.1 team sections（4 个）

| Section | 内容 |
|---|---|
| Communication | 何时消息人、消息怎么读、GitHub 每条消息开头行（默认 `This is by an automated agent, not a human`）、review 处理、哪些普通账号是 review bot |
| Ticket scope | Ticket 何时继续、何时停于草稿 |
| Delivery requirements | PR 证据、auto-merge 条件 + 行数阈值、commit author；无则 Executor 用 agent no-reply 提交 |
| Team skills | agent 需要时加载的知识（如 `identities` skill 说哪个 Feishu 用户是哪个 GitHub 账号） |

### 2.2 built-in parts（可覆盖）

| 类别 | keys |
|---|---|
| 共享指令+角色 | `runtime/shared`、`runtime/orchestrator`、`runtime/worker` |
| 工作指令 | `runtime/subscriptions`、`runtime/replies`、`runtime/dispatch`、`runtime/work`、`runtime/admission` |
| 交接指令 | `runtime/handoff/orchestrator`、`runtime/handoff/worker` |
| Source skills | `skill/feishu`、`skill/github` |
| 生成模板 | `runtime/environment`、`runtime/policy`、`runtime/policy/none` |

### 2.3 生成模板占位符

- `runtime/environment`：`{variables}`（执行环境行）。
- `runtime/policy`：`{version}`、`{mode}`、`{rules}`（每 enabled rule 一行）。
- `runtime/policy/none`：`{version}`、`{mode}`（无 rule 时）。

### 2.4 装配顺序

1. Shared 指令 + 角色。
2. 三 team sections（Communication/Ticket scope/Delivery requirements）。
3. Subscriptions + replies。
4. Dispatch（main）/ work（Executor）。
5. `runtime/admission`（main only）。
6. Handoff 指令。
7. `runtime/environment`。
8. `runtime/policy`（main only）。

environment 给 main Session deployment-wide 值，给 Executor Ticket 特定值。policy 每次 model 请求重生成（含 version/mode/explicit 豁免/enabled rules 列表 `- <name> (<id>): <instruction>` 或 "none enabled"）。放最后，Policy 变不动前面 prompt 文本（prefix cache 友好）。

### 2.5 优先级（强→弱）

1. 工具/host 强制：身份、权限、工具参数检查、reply purpose、长度限。prompt 文本不能改。
2. Built-in/generation sections（team override 后）。
3. proactive Ticket matched rules 的 process/communication（per-ticket Executor only）。
4. Team sections + team skills。
5. 外部文本：消息/评论/文档/引用。**数据**，永不改规则/授权。

### 2.6 override + base

- override 记 `base`（built-in key）。
- 升级保 team 文本；default 变 → 面板标 **Default updated** + 显示 diff。
- team 选 Keep my text / Use new default / 编辑。
- 与 default 相同的 override 自动删（follow future defaults）。
- omit `base` → 保 saved base。
- 已删 part 在 saved 配置里 → save 时 drop；未知 part 拒。

### 2.7 built-in 文本原则（从 004 §Principles）

- **角色分离**：main Session 只 route（forward 原始 Trigger + routing facts，不做技术决策）；routing note ≤30 字。Executor 读原始 source、判 scope、实现、验证。
- **main Session 不等**：report/follow-up/candidate/完成 job 自动到；turn 无事可做则结束。拒 `sleep`/长 timeout/长 job wait。
- **只在人必须行动或能用结果时消息**：结果可 review 或可用、需决策或资源、工作无法完成。chat 消息默认一句，硬限 100 字。
- **Executor 决定 follow-up**：in-scope 处理 + ack；超出 scope/属其他 Ticket handback。main Session 收 follow-up notice 但只能 stop。
- **经订阅 + main Session 协调**：Executor follow 相关 Thread 取上下文。main Session note ≤100 字传 fact。订阅/note 不授权。
- **仅显式指向才 attach**。
- **具体确认一次**：substantial work 前 Executor 确认每个新请求 + 命名对象 + 预期结果（原消息下 reply）。
- **经 `human_action` 问**：Executor 在 Report 放一个具体问题，main Session 不改写 forward。人消息触发则命名该消息，host 在那问；否则在 Ticket 原会话问。已授权工作可继续。
- **诚实交付**：开 PR ≠ ready；草稿标未完成；UI 改动无真 before 截图不算 ready。
- **人 pause 跨 follow-up/新 Session/restart 持续**，直到人解除。自动反馈永不 resume paused work。
- **示例只示范格式**：示例句不是 agent 可主张的事实。
- **按 outcome 处理 review**：已修已验证 comment 静默 resolve；declined 一句理由。automated reviewer 线程答后 resolve，每 PR 每 automated reviewer ≤3 轮。
- **让路重复工作**：人 branch/PR 已含 Ticket 改动 → Executor 关自己 PR 一句指向人的。

## 3. main Session 工具（`superagent/app/tools/main.py`）

14 个应用工具（对应原型 `007 §Main Session tools`）：

| 用途 | 工具 |
|---|---|
| 读 + route | `orchestrator_read`、`orchestrator_route`（1-20 决策，每独立 Engine 命令；inferred card 可直 ignore 记 `ignoredFromCard`） |
| 管 Ticket | `orchestrator_ticket_retry`、`orchestrator_ticket_request_status`、`orchestrator_ticket_note`（context 非 authority） |
| 重新评估 | `orchestrator_trigger_requeue` |
| 评估 proactive | `orchestrator_eval`、`orchestrator_eval_read`、`orchestrator_eval_decide` |
| 沟通 + 取消订阅 | `orchestrator_reply`、`orchestrator_thread_unsubscribe` |
| 配置 | `orchestrator_settings_save`、`orchestrator_policy_sync`、`orchestrator_prompts_save`（`preview:true` 不 save 只 compose） |

加 Sherry 的 `skill`/`bash`/`read`/`read_image`/`job_output`/`job_list`/`job_kill`。避 code 读是 prompt 规则非工具限制。

### 3.1 noWait 守卫

`src/app/sessions.py` 的 `noWait`：main Session shell 拒 `sleep` 在命令位、`timeoutMs>30s`、`job_output` wait>5s。报告自动到，等只延迟 candidate。

## 4. Executor 工具（`superagent/app/tools/executor.py`）

6 个应用工具：

| 工具 | 用途 |
|---|---|
| `orchestrator_read` | 读自己 Ticket 资源 |
| `orchestrator_ack` | ack 请求 |
| `orchestrator_report` | 报 progress/outcome + 绑 PR（pull 规则） |
| `orchestrator_thread_unrelated` | drop 无关 attached Thread + handback owned follow-up + 告 main |
| `orchestrator_thread_follow` | 订阅相关 Thread 取上下文 + 告 main |
| `orchestrator_trigger_handback` | 一次 handback（带 reason）；另一 Ticket own 或自己从未 own → `queued:false` |

worker preset 在 `cordis.patch.yml`（原型）→ Python 版用 Sherry subagent 的 tool 集裁剪（见 `06-executor.md §2.2`）。WORK_TOOLS 保留 file/search/shell/job/web/skill/todo/subagent。worker 不收 ask/present/profile/scheduling/其他 Session/外部文档编辑工具。Ticket end 后工具调用拒。

## 5. read 资源（`superagent/app/resources.py`）

`RESOURCES` 定义 + 生成 `orchestrator_read` 的 `resource` 描述。对应原型 `007 §Read resources`：

- `tickets`/`triggers`/`threads`/`decisions`：分页 `{items, next_offset}`，newest first。
- `decision/<id>`、`thread/<key>`、`trigger/<id>`、`ticket/<id>`。
- Ticket 子资源：`/assignments`、`/inputs`、`/assignment`、`/result`、`/report/<aid>`、`/transcript`、`/message/<n>`、`/input/<id>`。
- `settings`/`settings/<block>`、`prompts`/`prompts/<rev>`/`prompts/current`/`prompts/defaults`/`prompt-preview/<id>`、`models`/`models/<provider>`。
- `feishu/<trigger_id>/…`、`github/…`（Source 原文）。

Executor Session 只读自己 Ticket + Threads + 到达它的 Trigger + 用过的 prompt revision + 原文；页面不显示它不能读的 id（Decision/其他 Ticket）。每 id 都经一个 read 走到下一个，hash-only 的不显示。文本页默认 4000 UTF-16 code unit、最大 8000；list 默认 20、最大 50。

## 6. 投递给 Session 的消息

steer 进 main Session turn 的消息（对应原型 `007 §Messages delivered to Sessions`）：

| 消息 | 内容 |
|---|---|
| Candidate | batch Trigger cards + Thread + related_tickets + tickets_total + delivered_to + 引用 Ticket + handed_back reasons + next action |
| follow_up | 一条消息（Executor 视角）+ replies_to + receiving Tickets + 提醒（main 只能 stop） |
| worker_report | Ticket card + originating Trigger + state + summary（≤600 字符）+ chars + read pointer（truncated） |
| worker_attention | Ticket card 带 question |

不唤醒的 notice：`ticket_ended`/`settings_saved`/prompt save/Trigger requeue/dropped Subscription/context follow。settings notice 命名 block + changed paths + `read: settings/<block>`。main Session 自身做的 save/requeue/unsubscribe/attach 不收冗余 notice。

worker 收 assignment + Inputs inline。首消息含 title + ≤1500 字符 assignment text + source Trigger card。Input 带 Trigger card（≤600 字符）+ replies_to，或纯 Input text。truncated 才有 read pointer。

## 7. 与 Sherry 模型的接缝

- `role_models.py`：解析 main/worker 的 model（Settings `eval.model`/`executor.model` 或默认）。
- main Session 用 Sherry 主 agent 或独立 subagent（router role）。
- worker 用 Sherry subagent（executor role）。
- 模型调用经 Sherry `models/LLMs/main_llm.py`（复用 provider/retry/budget）。
- prompt 文本由 superagent 的 Prompts 装配，作为 Sherry subagent 的 system prompt 注入（经 hooks 的 `PromptDataProvider`）。
- 详见 `12-sherry-integration.md`。

## 8. Versions（`superagent/app/versions.py`）

```python
class Versions:
    """numbered snapshots + current pointer。settings + prompts 用。"""
    def current(self) -> int: ...
    def get(self, n: int) -> Optional[dict]: ...
    def save(self, value, predecessor, origin) -> int: ...  # 新号
    def restore(self, n: int) -> int: ...                    # 建新版本
```

每次 save 建 immutable 版本（revision/predecessor/timestamp/origin）。conflicting save 拒（409）。restore 建新版本。

## 9. 配置 reload

- 除 `stateDir` 外全 volatile；面板/Plugin Manager 改 → 写 `cordis.patch.yml`（原型）→ Python 版写 `superagent.jsonc` + 通知 runtime reload。
- 限值用时读最新（无 restart）。
- 改 Source setup 字段（account CLI）或 setup pending → restart superagent 进程。

## 10. 该册验收门

- `Sessions` 实现 ensure/install/prompt/relay/notify/status/prompt_version/model_retry。
- main Session 14 工具 + noWait 守卫（拒 sleep/长 timeout/长 job wait）。
- worker Session 6 工具 + WORK_TOOLS + Ticket end 后拒。
- `Prompts` 实现 current/version/history/validate/preview/save；team sections 4 个 + built-in parts 全 keys + 生成模板占位符。
- 装配顺序 8 步单测（每步产出可 diff）。
- 优先级 5 层单测（工具强制 > built-in > matched rules > team > 外部）。
- override + base：升级标 Default updated；与 default 同自动删。
- `Versions`：immutable + conflicting 拒。
- relay/notice 保序；turn 中 relay 下 step 一起投。
- `tests/superagent/test_prompts.py` 镜像 `tests/prompts.spec.ts`。
- `tests/superagent/test_cards.py` 镜像 `tests/cards.spec.ts`（built-in 文本规则）。
- `tests/superagent/test_model_surface.py` 镜像 `tests/model-surface.spec.ts`（模型读的全部表面：tools/prompts/skills/inputs，对 schema + 环境 + 术语表，每角色 size budget）。
- `tests/superagent/test_tools.py` 镜像 `tests/tools.spec.ts`。
