# 02 域模型：Python 数据类映射

复刻原型 `src/core/model.ts` + `001-domain.md` 的全部概念。Python 用 `dataclass`（纯数据，不校验）+ `enum`；schema 校验用 pydantic（见 `01-architecture.md §5`）。

## 1. 模块位置

`superagent/core/model.py` —— 域类型 + 共享谓词。
`superagent/core/stage.py` —— Ticket 阶段。
`superagent/core/ids.py` —— `key()` 确定性 id。
`superagent/core/reasons.py` —— `Reason` + `REASONS`。

`superagent.core` **不 import 外部**（除 stdlib）。

## 2. Enum 全表

```python
# superagent/core/model.py
from enum import Enum

class Strength(str, Enum):
    EXPLICIT = "explicit"        # 名单里的人 @ 智能体；admission explicit cell；follow-up
    INFERRED = "inferred"        # 非名单 follow-up；admission inferred cell；bot list
    # not_admitted 不入 enum：gate 返回 None 表示不准入

class DecisionOutcome(str, Enum):
    ACCEPT = "accept"
    STOP = "stop"
    IGNORE = "ignore"
    UNAVAILABLE = "unavailable"

class AssignmentStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    ENDED = "ended"

class ReportKind(str, Enum):
    PROGRESS = "progress"
    WAITING = "waiting"
    READY = "ready"
    BLOCKED = "blocked"
    FAILED = "failed"
    DROPPED = "dropped"

class AddressKind(str, Enum):
    MENTION = "mention"          # @ 智能体
    DIRECT = "direct"            # 私信

class MandateBasis(str, Enum):
    ADDRESSED = "addressed"      # 名单里的人 @ 智能体
    FOLLOWUP = "followup"        # Thread 上有 Ticket 的 follow-up
    ADMISSION = "admission"      # admission 表 cell；observation 标 inferred
    BOT = "bot"                  # bot list 产的 inferred candidate

class AcceptMode(str, Enum):
    OFF = "off"
    OBSERVE = "observe"          # 默认
    AUTO = "auto"

class Channel(str, Enum):
    """replies 的 purpose"""
    ACKNOWLEDGEMENT = "acknowledgement"
    ANSWER = "answer"
    RESULT = "result"
    ATTENTION = "attention"

class TicketOutcome(str, Enum):
    MERGED = "merged"
    CLOSED = "closed"
    COMPLETED = "completed"
    DROPPED = "dropped"
    STOPPED = "stopped"
    # ready 无 PR → completed；nothing to do → dropped；人 stop → stopped
```

## 3. 核心数据类

下表对应原型 `src/core/model.ts` 字段。Python `@dataclass(frozen=True)`（不可变，对齐"Trigger immutable"等约束）；可变字段用 `@dataclass`。

```python
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

@dataclass(frozen=True)
class SourceRef:
    """Source 引用 + Source 自有 id"""
    source: str
    id: str

@dataclass(frozen=True)
class Actor:
    source: str
    id: str                      # 空 id 表示 CI（无 sender）

@dataclass(frozen=True)
class ChannelAddress:
    """reply 目的地。Source 自定义 shape 存 address dict"""
    source: str
    thread: str                  # channel.thread（用于排序）
    address: dict                # Source-specific: {conversation, message, url?} 等

@dataclass(frozen=True)
class Trigger:
    id: str                      # key(source, raw_id) 确定性
    source: str
    threads: list[SourceRef]     # 第一个是 primary，拥有 Eval
    actor: Actor
    human: bool
    addressed: Optional[AddressKind]  # None=未 @
    at: str                      # ISO8601，原型用 toISOString 文本比较
    text: str
    channel: Optional[ChannelAddress]
    url: Optional[str]
    meta: dict                   # Source 自有字段，不可信

@dataclass(frozen=True)
class Correlation:
    """Trigger 属于多个 Thread 的关系"""
    trigger_id: str
    threads: list[SourceRef]

@dataclass
class Subscription:
    """Thread 上的多对多。Thread 持有。"""
    ticket_id: str
    thread: SourceRef
    own: bool = False            # Ticket 开的 Thread（PR）
    context_only: bool = False   # 仅上下文订阅

@dataclass
class Eval:
    """Thread 上至多一个。无状态（决策后释放）"""
    id: str
    thread: SourceRef
    primary: str                 # 主 Trigger id
    trigger_ids: list[str]       # batch
    delivered: bool = False      # candidate 到达 main Session
    deadline: Optional[datetime] = None  # delivered 后起计

@dataclass(frozen=True)
class Reason:
    code: str
    values: dict
    trigger_id: Optional[str] = None  # batch 内单个 Trigger 时

@dataclass(frozen=True)
class Decision:
    id: str
    thread: SourceRef
    outcome: DecisionOutcome
    reason: Reason
    at: str                      # ISO8601
    trigger_ids: list[str]       # batch

@dataclass(frozen=True)
class Mandate:
    """accept 授予的。immutable。"""
    policy_version: str
    scope: str                   # Source/scope 标识
    strength: Strength
    matched_rules: list[str]     # rule id 列表
    sender: Actor
    via: MandateBasis
    basis_detail: dict           # Source/scope/row/sender/rules（revalidation 用）

@dataclass
class Follow:
    """Ticket 跟随的 Thread"""
    thread: SourceRef
    own: bool = False            # Ticket 开的（PR）
    closed: Optional[dict] = None  # {at, outcome} 一旦关闭

@dataclass
class Input:
    """送达 Ticket 的上下文。"""
    id: str
    ticket_id: str
    kind: str                    # "fanout" | "policy" | "resume" | "text"
    trigger_id: Optional[str]    # fanout/resume 时
    text: Optional[str]          # text/policy 时
    human: bool = False
    delivered: bool = False
    read: bool = False           # Executor Session 取入 history

@dataclass
class Assignment:
    id: str
    ticket_id: str
    status: AssignmentStatus = AssignmentStatus.PENDING
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    latest_report: Optional["Report"] = None

@dataclass
class Report:
    assignment_id: str
    kind: ReportKind
    summary: str                 # ≤600 字符
    question: Optional[str] = None   # Attention（人需答）
    pull_request: Optional[dict] = None  # {number, head} claim 后
    at: str

@dataclass
class Ticket:
    id: str
    origin: dict                 # {trigger_id, trigger_ids}  admissible batch
    mandate: Mandate
    execution: dict              # {name, directory, assignment}
    channels: list[ChannelAddress]
    inputs: list[Input]
    follows: list[Follow]
    assignments: list[Assignment]
    latest_report: Optional[Report]
    ended: Optional[dict] = None  # {at, by, why, outcome}
    last_said: Optional[str] = None  # 简记，实际读 latest_report

@dataclass
class Receipt:
    """Outbox 收据。单写。"""
    id: str                      # message id（idempotency key）
    ticket_id: Optional[str]
    channel: ChannelAddress
    purpose: Channel
    text: str
    status: str = "pending"      # pending|sent|dropped
    attempts: int = 0
    last_error: Optional[str] = None
    dropped: Optional[dict] = None  # {at, reason}
```

## 4. 共享谓词（对应原型 `src/core/model.ts`）

```python
# superagent/core/model.py

def waits_for_person(ticket: Ticket) -> bool:
    """latest Assignment 报 blocked/failed 且此后无人消息到达"""
    if not ticket.latest_report:
        return False
    return ticket.latest_report.kind in (ReportKind.BLOCKED, ReportKind.FAILED)

def followup_owners(trigger_id: str, tickets: list[Ticket]) -> list[str]:
    """仍拥有该人 follow-up 的 Ticket（live 或 ended 但未人 stop）"""
    return [t.id for t in tickets if _owns_followup(t, trigger_id)]

def settled(decision: Optional[Decision]) -> bool:
    """Decision 解决了请求。stop/unavailable/routed/refused-stop 不算。"""
    if decision is None: return False
    return decision.outcome in (DecisionOutcome.ACCEPT, DecisionOutcome.IGNORE)

def live_ticket(ticket: Ticket) -> bool:
    return ticket.ended is None

def running_assignment(ticket: Ticket) -> Optional[Assignment]:
    for a in reversed(ticket.assignments):
        if a.status == AssignmentStatus.RUNNING:
            return a
    return None

def latest_assignment(ticket: Ticket) -> Optional[Assignment]:
    return ticket.assignments[-1] if ticket.assignments else None

def reached(trigger_id: str, ticket: Ticket) -> bool:
    """Trigger 是否到达过 Ticket（fan-out/attach/handback/ack）"""
    # 读 disposition.fanout + ticket.inputs
    ...
```

`_owns_followup` 等实现细节见 `03-engine.md`。

## 5. Ticket 阶段（对应原型 `src/core/stage.py`）

```python
# superagent/core/stage.py
# 实现 003-execution.md 的 stage 表，从上到下第一个匹配行胜出。

class TicketStage(str, Enum):
    PERSON_DELIVERY = "person/delivery"      # Input 投递失败重试中
    PERSON_ANSWER = "person/answer"          # 有 Attention
    PERSON_REVIEW = "person/review"          # latest Report = ready
    PERSON_BLOCKED_FAILED = "person/blocked,failed"
    WORKING_QUEUED = "working/queued"
    IDLE = "idle"
    WORKING = "working"

def ticket_stage(ticket: Ticket, now: str) -> Optional[TicketStage]:
    if ticket.ended: return None  # ended 不入 stage 表
    # 按表顺序匹配，见 03-engine.md
    ...

def can_retry(ticket: Ticket) -> bool:
    """Ticket 在 person/delivery|blocked|failed 时可 retry"""
    stage = ticket_stage(ticket, now=None)
    return stage in (TicketStage.PERSON_DELIVERY,
                     TicketStage.PERSON_BLOCKED_FAILED)
```

## 6. 确定性 id（对应原型 `src/core/ids.py`）

```python
# superagent/core/ids.py
import hashlib

def key(*parts: str) -> str:
    """对相同输入返回相同 id。用于 Trigger id / Thread key / Decision id 等。"""
    raw = "\x1f".join(parts)  # unit separator，避免 "ab"+"cd" == "a"+"bcd"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

def thread_key(ref: SourceRef) -> str:
    return key(ref.source, ref.id)
```

注意：原型用 `key('webhook', message.id)`；Python 版同样 `key("webhook", msg_id)`。`sha256[:16]` 足够防碰撞（16 hex = 64 bit），且短到 prompt/card 能展示。

## 7. Reason 字典（对应原型 `src/core/reasons.py`）

```python
# superagent/core/reasons.py
REASONS: dict[str, str] = {
    "timeout": "The evaluation timed out.",
    "policyChanged": "The Policy changed during evaluation.",
    "routed": "The message was attached to a Ticket.",
    "startedTicket": "Accepted as new work.",
    "stoppedTicket": "Stopped by a person.",
    "unavailable": "The evaluation could not finish.",
    # ... 完整列表见原型 src/core/reasons.ts
}

def reason_text(code: str, values: dict) -> str:
    """读给 agent 的英文文本"""
    template = REASONS.get(code, code)
    try: return template.format(**values)
    except (KeyError, IndexError): return template
```

## 8. 字段命名映射注意

- 原型 TS 用 camelCase（`triggerIds`、`policyVersion`、`latestReport`）；Python 用 snake_case（`trigger_ids`、`policy_version`、`latest_report`）。
- **SQLite 列名保留原型 snake_case**（原型 schema 本就是 snake_case + 生成列），所以 ORM 层无命名转换。
- **HTTP API JSON 用 camelCase**（与原型 API 一致，面板不改），所以 HTTP 序列化层有 `to_camel`/`from_camel` 转换。用 `pydantic` 的 `alias_generator=to_camel` + `populate_by_name=True`。
- **prompt 文本里引用的 key**（`runtime/shared` 等）保留原型字符串。

## 9. 该册验收门

- `superagent/core/model.py` 定义 §2 全部 enum + §3 全部 dataclass。
- `superagent/core/stage.py` 实现 stage 表（含 `003-execution.md` 全部 7 行）。
- `superagent/core/ids.py` 的 `key()` 与 `thread_key()` 对相同输入确定性、对不同输入无碰撞（单测覆盖 `ab+cd` vs `a+bcd`）。
- `superagent/core/reasons.py` 的 `REASONS` 字典 key 与原型 `src/core/reasons.ts` 完全一致。
- 共享谓词（§4）每个有单测，覆盖正反例。
- `basedpyright superagent/core/` 无 error。
