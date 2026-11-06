# 05 Sources：SourceKind 协议、GitHub/Feishu、轮询

复刻原型 `src/sources/` + `001-domain.md §Source` + `008-cookbook.md`。Source 是事件流：一个 kind（可复用实现）+ 一个 instance（一份配置）。

## 1. SourceKind 协议（`superagent/sources/kind.py`）

```python
# superagent/sources/kind.py
from typing import Protocol, Optional, Any
from pydantic import BaseModel

class SourceKind(Protocol):
    kind: str                              # 稳定 id（"github"/"feishu"/"webhook"）
    title: dict[str, str]                  # {zh, en}
    description: dict[str, str]
    schema: type[BaseModel]                # 配置 schema（pydantic）
    fields: list["FieldSpec"]              # 面板表单（rows/text/select/number/advanced）
    health: list[str]                      # claim 的健康项名
    facts: Optional["FactsFn"] = None      # setup 派生值

    # 可选行为
    scope: Optional["ScopeFn"] = None
    channel: Optional["ChannelFn"] = None
    thread_context: Optional["ThreadContextFn"] = None
    find: Optional["FindFn"] = None
    policy: Optional["PolicyPartFn"] = None
    environment: Optional["EnvironmentFn"] = None
    refresh_labels: Optional["RefreshLabelsFn"] = None
```

每个可选行为的签名：

| 行为 | 签名 | 用途 |
|---|---|---|
| `scope(meta, config)` | `→ {listened, observed, row}` 或 `{listened: False, reason}` | Engine 收 Trigger 前调；out-of-scope 存为 filtered |
| `channel.allowed(addr, config)` | `→ bool` | 目的地是否仍被听 |
| `channel.send(host, message)` | `→ "sent"\|"gone"` 或 throw | Outbox 调；`gone`=永久丢，throw=重试 |
| `channel.link(addr)` | `→ {url, label}` | 面板链接 |
| `thread_context.read(commands, trigger, part, cursor)` | `→ {messages, next?, body, status?}` | assessment Thread 窗口 |
| `thread_context.message_id(trigger)` | `→ str` | 在 messages 里找 Trigger 自己 |
| `thread_context.images(text)` / `image(...)` | | Feishu 图片用 |
| `replies_to(trigger)` | `→ Optional[str]` | follow-up 通知/worker Input 显示 replied-to |
| `find(query)` | `→ {threads, terms}` | Source 特定引用搜索（`#12`/URL/msg id） |
| `policy.schema` | `type[BaseModel]` | Source Policy（allowed/bots 等） |
| `policy.rows(config)` | `→ list[AdmissionRow]` | admission 行 |
| `policy.sync` | `{label, ready, run}` | 成员同步（startup + Sync now） |
| `environment.variables` | `list[VarSpec]` | 执行环境变量声明 |
| `environment.values(deployment, config, trigger)` | `→ dict[str, str]` | Ticket 特定值 |

## 2. SourceRegistry（`superagent/sources/registry.py`）

```python
class SourceRegistry:
    """注册 kind、管 instance、claim 健康项。"""
    def __init__(self, settings_getter, board: HealthBoard): ...
    def register(self, kind: SourceKind) -> None: ...   # claim 健康项；拒碰撞
    def unregister(self, kind_name: str) -> None: ...
    def kind(self, name: str) -> Optional[SourceKind]: ...
    def instances(self) -> list[SourceInstance]: ...
    def instance(self, id: str) -> Optional[SourceInstance]: ...
    def validate(self, kind_name, config) -> list[Issue]: ...
    def validate_policy(self, kind_name, part) -> list[Issue]: ...
    def policy_part(self, kind_name, settings) -> dict: ...
    def channel_allowed(self, addr, config) -> bool: ...
    def send(self, message) -> "sent|gone|waiting": ...
    def link(self, addr) -> dict: ...
    def thread_context(self, kind_name) -> Optional[ThreadContextFn]: ...
    def find(self, query) -> dict: ...
    def scope(self, kind_name) -> ScopeFn: ...
    # SourceHost 接口（plugin 注入的）
    def context(self, kind_name) -> SourceContext: ...
```

注册 claim 健康项到 `HealthBoard`，拒碰撞（`runtime`/`reply`/`worker`/effect kind 名保留）。

## 3. SourceContext / SourceHost（`superagent/sources/host.py`）

Source 通过 `SourceContext` 拿能力（窄接口，不直接拿 Runtime）：

```python
class SourceContext:
    ready: asyncio.Future              # setup 完成
    configured: callable[[], bool]     # kind 是否已配
    config: callable[[str], Optional[dict]]  # 读当前配置
    receive: callable[[Trigger], str]  # → Engine.receive
    atomic: Atomic                     # 事务（写 cursor 等）
    state: StateAccessor               # KV 读写（typed）
    threads: ThreadAccessor            # describe/patchMeta
    tickets: TicketAccessor            # bind_thread/thread_closed（调 Engine）
    notify: callable[[OutboundMessage], None]  # Source 自发通知走 Outbox
    report: callable[[str, HealthState], None]
    commands: Commands                 # CLI 传输
```

`SourceHost` 是 plugin 注入的更窄接口：`registry + context(kind)`。

## 4. GitHub 源（拆 5 文件，对应原型 `github*.ts`）

```
superagent/sources/
├── github.py            # kind + plugin entry（apply/watch）
├── github_trigger.py    # 评论/review/issue body/CI 变化 → Trigger
├── github_pull.py       # PR 跟随规则（claim_pull/observe_pull/pull_end_notices）
├── github_channel.py    # 评论 + review thread reply
└── github_read.py       # CI 快照、repo 读、githubThreadContext
```

### 4.1 触发翻译（`github_trigger.py`）

- Trigger id = `key("github", "<type>:<id>:<updated_at>")` —— `updated_at` 让编辑算新事件。
- Thread = issue/PR/comment thread；root 是第一条。
- actor = `{source: "github", id: login}`；bot 判定：`[bot]` 后缀 + GitHub Apps。
- `addressed`: 评论里 `@agent_login` → `mention`；无 → None。
- agent 自己的消息不产 Trigger。
- CI 结果无 sender，actor.id 空，走 admission unlisted-unaddressed cell。

### 4.2 PR 跟随（`github_pull.py`）

- `claim_pull(ticket, number, head)`：Ticket 报告它开了 PR → `bind_thread` own=True。
- `observe_pull(ticket, number)`：起源 PR → `bind_thread` own=False。
- `pull_end_notices`：Ticket end merged/closed → 发一条 chat notice（稳定 id 幂等）。
- Ticket 跟随它开的所有 PR；reopened 重跟；所有跟随 PR 关闭 → Ticket end。

### 4.3 Channel（`github_channel.py`）

- `send`：评论（`gh pr comment`/`gh issue comment`）；review thread reply（`gh api .../replies`）。
- idempotency：GitHub 用隐藏 comment marker（`<!-- dsh-superagent: <id> -->`），重试不重复发。
- `gone`：404/410 → 对话消失；其余 throw 重试。

### 4.4 读（`github_read.py`）

- `github_thread_context`：issue/PR 会话按页读，`ContextMessage` shape。
- CI 快照：`gh pr checks --json`，指纹存 KV 防重复 Trigger。
- repo 读：`readRepositoryPath`。

### 4.5 轮询（`github.py::watch`）

- `gh api notifications` 一轮 + 订阅 Issue/PR/评论/CI 轮询 + 默认分支 CI（开关）。
- cursor 在 KV（`github-cursor:<n>`），`cursor_at(started)` 减 5 秒容晚到。
- 失败计数：一次网络错下轮重试；连续 3 次 → `health("error", "pollFailures")`。
- abort signal 贯穿。

### 4.6 配置（pydantic）

```python
class GithubConfig(BaseModel):
    repository: str                    # owner/name
    cli: str                           # agent gh wrapper 绝对路径
    observe_issues: bool = False
    observe_pulls: bool = False
    observe_comments: bool = False
    observe_ci: bool = False
    model_config = {"extra": "forbid"}
```

## 5. Feishu 源（拆 6 文件，对应原型 `feishu*.ts`）

```
superagent/sources/
├── feishu.py            # kind + plugin entry
├── feishu_trigger.py    # 消息 → Trigger（一条规则覆盖 live/history/topic）
├── feishu_loops.py      # 事件消费 + 历史恢复 + chat 名 + 订阅话题轮询
├── feishu_channel.py    # 话题回复
├── feishu_read.py       # Trigger 消息的 root/parent/topic + feishuThreadContext
└── feishu_image.py      # 消息图片读为 attachment
```

### 5.1 配置

```python
class FeishuChat(BaseModel):
    chat_id: str
    name: str = ""
    listen: Literal["mentions", "all"] = "mentions"
    model_config = {"extra": "forbid"}

class FeishuConfig(BaseModel):
    cli: str                          # lark-bot wrapper
    chats: list[FeishuChat] = []
    direct_messages: bool = False
    other_chats: Literal["none", "mentions"] = "none"
    model_config = {"extra": "forbid"}
```

### 5.2 触发翻译

- Trigger id = `key("feishu", message_id)`。
- Thread = `<chat>:<root message id>`。
- actor.id = `open_id`；bot = `sender.bot`。
- `addressed`: `mentions_me` → mention；DM → direct。
- bot 自己的消息不产 Trigger。

### 5.3 chat 词限

Outbox 应用 Feishu chat 词限（`infra/text.py` 的 `chat_limit`）。

## 6. 共享轮询（`superagent/sources/poll.py`）

```python
POLL_HORIZON_MS = 14 * 24 * 3600 * 1000  # 两周

def polled(subscribed: callable, last_trigger_at, ticket_end_at) -> bool:
    """保留 live 订阅 + 最后 Trigger/Ticket end 后两周窗口"""
    ...

def cursor_at(started: str) -> str:
    """减 5 秒容晚到"""
    ...

async def poll_threads(ctx, threads, read_one, signal): ...
```

每个失败记 `pollError` 在 Thread 上。

## 7. webhook 模板（Cookbook 复刻）

`superagent/sources/webhook.py` 完整复刻 `008-cookbook.md` 的示例 Source：`hookq` CLI 契约、4 步实现（配置/翻译/Channel/Thread context/Policy/环境/loop/plugin/tests）。作为"加新 Source"的模板与测试参照。

## 8. Source 配置 reload

- 配置（instance 的 enabled + config）存 Settings，volatile。
- `SourceContext.config(kind)` 每轮读最新值——off 时返回 None，loop 报 `paused`。
- 不需要 restart 即可开关 Source。

## 9. Source failure 处理

- 一次失败：留 `pollError` 在 Thread，下轮重试。
- 连续 3 次：`health("error", ...)`，status 退出码 2。
- GitHub 404：可能丢私库访问，仍重试；一天后丢该 Issue/PR 通知，read position 不动。
- Feishu：每失败都报；订阅话题轮询留 `pollError`。
- malformed item：跳过 + Source state 计数（`validItems`）。

## 10. 该册验收门

- `SourceKind` Protocol + `SourceRegistry` + `SourceContext`/`SourceHost` 定义齐备。
- GitHub 源 5 文件 + Feishu 源 6 文件齐备，各文件 docstring 标对应原型 `.ts`。
- `webhook.py` 完整实现 cookbook 示例。
- `tests/superagent/sources/test_github.py` 镜像 `tests/sources.spec.ts` + `tests/github-read.spec.ts`。
- `tests/superagent/sources/test_feishu.py` 镜像 Feishu 部分 + `feishu-read.spec.ts` + `feishu-image.spec.ts`。
- `tests/superagent/sources/test_webhook.py` 镜像 cookbook §11 测试清单。
- `tests/superagent/sources/test_registry.py` 镜像 `tests/registry.spec.ts`。
- `tests/superagent/test_poll.py` 镜像 `pollThreads` 行为（live + 两周窗口）。
- CLI mock：`Commands.run` spy，覆盖 `sent`/`gone`(410)/throw 三态。
