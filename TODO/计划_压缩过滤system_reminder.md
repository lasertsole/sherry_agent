# 计划：压缩时过滤过期 system-reminder

- 建档日期：2026-11-03
- 状态：**待评审**

## 问题

reminder catalog 的 8 个 source 每轮从 live state 重新派生，注入为
`<system-reminder>` HumanMessage。这些 reminder 在注入后的下一轮由
`DynamicReminderMiddleware` 重新决定是否注入（`only_if_changed` /
`transient`），但 **summarization 层不过滤它们**——压缩切片里的 reminder
原样进入摘要 prompt（`_serialize_for_summary` 截断到 500 字符标成
`[System notice]`），静态回退还会把 `is_system_reminder` 的 HumanMessage
误当 `user_requests` 收集（只排除了 `is_workspace_notice`，没排除
`is_system_reminder`）。

结果：压缩摘要里混入了过期的状态快照（旧 todo 列表、旧 TaskFlow board、
旧 boulder 等），占 token 且干扰 LLM 提取真正的对话信息。压缩后
`reset_session` 已经让下一轮从 live state 重新注入，旧快照留在摘要里
纯噪音。

## 设计思路

在 `ReminderSource` 上声明 `compress_exclude` 字段（默认 True），注入时
把该值打到 message metadata 里，压缩中间件读 metadata 标签决定是否
过滤。策略声明留在 reminder catalog（source 定义处），summarization 层
只做无差别检查，不硬编码 source id。

### 为什么默认 True

reminder 的设计前提是"每轮从 live state 重派生"。绝大多数 source 的内容
在压缩后就是过期快照，无历史价值。默认排除是安全方向：新增 source 如果
忘记设值，它的 reminder 不会漏进摘要。

### 唯一例外：continuity_context

`continuity_context` 携带跨轮对话连续性（上文做了什么、当前在做什么），
本身就是为压缩后恢复上下文而设计的。设 `compress_exclude=False`，让它
留在摘要消息列表里参与压缩。

## 各 source 的处理

| source_id | priority | 当前 transient | 设 compress_exclude | 理由 |
|---|---|---|---|---|
| `plan_mode_block` | 85 | False | **True**（默认） | 实时开关，旧状态无历史价值 |
| `todo_block` | 80 | False | **True**（默认） | 每轮重派生，旧快照是噪音 |
| `taskflow_status` | 75 | False | **True**（默认） | 实时 board，同上 |
| `boulder_status` | 70 | False | **True**（默认） | 实时指标，同上 |
| `continuity_context` | 65 | False | **False** | 连续性信息，压缩后仍有价值 |
| `project_instructions` | 62 | True | **True**（默认） | 8K AGENTS.md 节选，transient 已在多数轮剥离；漏进切片时也排除 |
| `project_dir_notice` | 60 | False | **True**（默认） | 目录通知是那一刻的状态，压缩后由 reset 重新注入 |
| `knowledge_index` | 50 | False | **True**（默认） | live 派生，旧版本无意义 |

## 改动清单

### 1. `agent/middlewares/reminder_catalog/core.py`

`ReminderSource` dataclass 新增字段：

```python
@dataclass(frozen=True)
class ReminderSource:
    ...
    transient: bool = False
    compress_exclude: bool = True  # 新增：压缩时是否从摘要消息列表中排除
```

### 2. `agent/middlewares/reminder_catalog/sources.py`

`continuity_context` 的注册处显式设 `compress_exclude=False`：

```python
register(
    ReminderSource(
        source_id="continuity_context",
        channel=ReminderChannel.CURRENT_TURN,
        builder=_as_builder(_build_continuity_block),
        budget_chars=1000,
        priority=65,
        only_if_changed=True,
        compress_exclude=False,  # 新增：连续性信息在压缩后仍有价值
    )
)
```

其余 7 个 source 不需改动（继承默认 True）。

### 3. `pub/func/message/system_reminder.py`

`system_reminder_metadata` 接受 `compress_exclude` 参数并写入 metadata：

```python
def system_reminder_metadata(
    source_id: str, *, compress_exclude: bool = True
) -> dict[str, Any]:
    return {
        "origin": source_id,
        "internal": True,
        "provenance": REMINDER_PROVENANCE,
        "compress_exclude": compress_exclude,
    }
```

默认值 True 与 `ReminderSource` 的默认一致，保证不传参时行为不变（排除）。
`is_system_reminder` 的判断逻辑不变（仍然只看 `provenance`），不受影响。

### 4. `agent/middlewares/reminder_catalog/dynamic.py`

注入时从 registry 查 source 的 `compress_exclude`，传入 metadata：

```python
from .core import clear_session, registered_sources, resolve_injections, transient_source_ids

# 在 abefore_model 中：
sources = registered_sources()
reminders = [
    HumanMessage(
        content=wrap_reminder(source_id, content),
        metadata=system_reminder_metadata(
            source_id,
            compress_exclude=sources[source_id].compress_exclude,
        ),
    )
    for source_id, content in reversed(list(injections.items()))
]
```

`registered_sources()` 返回 dict 副本，每次注入调一次，开销可忽略。

### 5. `agent/middlewares/summarization/summary_generation.py`

#### 5a. `_filter_summary_messages` 增加过滤

```python
def _filter_summary_messages(messages: list[AnyMessage]) -> list[AnyMessage]:
    """Filter out previous summary messages and compress-excluded reminders."""
    return [
        m
        for m in messages
        if getattr(m, "additional_kwargs", {}).get("lc_source") != _SUMMARY_LC_SOURCE
        and not _is_compress_excluded_reminder(m)
    ]


def _is_compress_excluded_reminder(msg: AnyMessage) -> bool:
    """True for a system-reminder whose source declared compress_exclude."""
    if not is_system_reminder(msg):
        return False
    meta = getattr(msg, "metadata", None) or {}
    return meta.get("compress_exclude") is True
```

作用域：`_create_summary` / `_acreate_summary` 都调用 `_filter_summary_messages`
→ `_serialize_for_summary` / `_build_static_fallback_summary`，两条路径都覆盖。

#### 5b. `_build_static_fallback_summary` 修 bug — 用统一 internal 检查

当前 L326 只排除 `is_workspace_notice`，**还有 7 种注入 HumanMessage 会漏进 `user_requests`**。
逐个排除（`is_workspace_notice` + `is_system_reminder` + `is_continuation` + …）是 whack-a-mole，
新增注入类型就会漏。项目级统一标记是 `metadata["internal"] is True`——所有注入类型都设它，
真实用户消息设 `{"origin": "user"}` 不含 `internal` 键。

**新建公共工具** `pub/func/message/classify.py`：

```python
def is_user_message(msg) -> bool:
    """True only for messages a human actually typed.

    Positive check: origin must be "user". Fail-closed — a message with no
    metadata or unknown origin is NOT treated as user content.

    Real user messages carry {"origin": "user"} (set by origin_for_source("user")
    in input_queue_service.py:247). Injected HumanMessages carry other origin
    values ("project_dir", "git_head", "todo_continuation", "task_intent",
    "quality_gate", "background_task", "cron", <source_id>) or provenance values
    ("reminder_catalog", "workspace_notice", "subagent_completion", "continuation",
    "side_chat_context"). None of these match "user".

    This is safer than checking `metadata.get("internal") is not True` (negative
    check): if someone creates a new injection path and forgets to set internal=True,
    the negative check would treat it as user content. The positive check doesn't.
    """
    meta = getattr(msg, "metadata", None) or {}
    return meta.get("origin") == "user"
```

**为什么 `origin == "user"` 够用**：`origin_for_source` 只对 `source == "user"` 返回
`{"origin": "user"}`，cron 返回 `{"origin": "cron", "internal": True}`。所有 9 种注入
HumanMessage 的 origin 都是其他值，没有一个冒用 `origin == "user"`。

**向后兼容**：旧 checkpoint 里的用户消息也有 `{"origin": "user"}`（这个字段从一开始就有），
不需要迁移。无 metadata 的消息 → origin 是 None ≠ "user" → 不当用户消息（fail-closed）。

**可选增强**（不在本计划内，记录备选）：如果未来觉得 `origin == "user"` 语义不够直白
（origin 字段被复用），可在 `origin_for_source("user")` 里加 `"user_authored": True`
专用标记，`is_user_message` 优先检查它、回退到 `origin == "user"` 兼容旧消息。

### 迁移目标（`is_user_message` 上线后，其他调用点逐步迁移）

当前 codebase 有 **14 个调用点**在做 ad-hoc HumanMessage 分类，其中 6 个是明确的
whack-a-mole（3 套独立重写的"是否注入"逻辑 + 4 处 `is_workspace_notice or is_system_reminder`
二选一）。`is_user_message` 上线后应逐步迁移，统一为正面检查。

**强优先级（whack-a-mole + 修 latent bug）**：

| 文件:行 | 当前检查 | 问题 | 迁移 |
|---|---|---|---|
| `pub/func/message/turn_utils.py:27-31`（`split_into_turns`） | `not is_workspace_notice and not is_system_reminder` | 只跳 2 种，其余 7 种注入消息被当 turn 边界 | → `and is_user_message(msg)` |
| `agent/middlewares/humanInTheLoop/approval_scope.py:81-103`（`_is_headless_turn`） | `is_workspace_notice or is_system_reminder` 跳过 + `origin=="cron"` + `internal` 三层 | 逻辑纠缠；先跳过注入消息再判 headless | → 跳过条件改 `not is_user_message(msg)`，cron 检查保留 |
| `agent/middlewares/summarization/nudges.py:117`（`_msg_type`） | `is_workspace_notice or is_system_reminder` | 同 1.3 的 whack-a-mole | → `isinstance(msg, HumanMessage) and not is_user_message(msg)` |
| `agent/middlewares/reminder_catalog/dynamic.py:41-47`（`_is_injected`） | `meta.internal` + content 前缀 `[SYSTEM DIRECTIVE` | 独立重写，和 TaskIntent 的 `_is_system_directive` 逻辑分叉 | → 删 `_is_injected`，用 `not is_user_message(msg)` |
| `agent/middlewares/task_intent/core.py:320-330`（`_is_system_directive`） | content 前缀 `[SYSTEM DIRECTIVE` / `<sherry-ulw-execute>` + `meta.internal` | 独立重写，和 reminder_catalog 的 `_is_injected` 逻辑分叉 | → 删 `_is_system_directive`，用 `not is_user_message(msg)` |
| `agent/middlewares/safety_filter/middlewares.py:65-77`（`_internal`） | `additional_kwargs.internal` + `additional_kwargs.provenance` | **读 additional_kwargs 而非 metadata**，和其他所有人不同 | → 用 `is_user_message(msg)`，但需确认 origin 在 metadata 不在 additional_kwargs |

**中优先级（单检查但有 drift 风险）**：

| 文件:行 | 当前检查 | 问题 | 迁移 |
|---|---|---|---|
| `agent/state_port.py:344-353`（turn cut） | `metadata.internal is True` | 和 `split_into_turns` 的跳过规则不一致 → turn 编号可能 drift | → `not is_user_message(msg)` |
| `server/service/side_chat_service.py:227-238`（turn slice） | `metadata.internal is True` | 同上 drift 风险 | → `not is_user_message(msg)` |
| `agent/middlewares/summarization/summary_generation.py:287-311`（`_latest_human_eviction_ref`） | `lc_source` + `kwargs.internal or meta.internal` 三条件 | 找最后一条真实用户消息的逻辑纠缠 | → `not is_user_message(msg)` |

**修 latent bug（无任何过滤）**：

| 文件:行 | 当前检查 | 问题 | 迁移 |
|---|---|---|---|
| `agent/middlewares/summarization/compression.py:481` | `isinstance(m, HumanMessage)` 无过滤 | 最后一条 HumanMessage 如果是注入消息 → `user_intent` 被污染 | → `and is_user_message(m)` |
| `agent/middlewares/summarization/thrash.py:68` | 同上 | 同上 | → `and is_user_message(m)` |

**不迁移（不同关注点）**：

| 文件 | 理由 |
|---|---|
| `message_persistence/prepare.py::_is_persistable` | persistence 关心"是否可见行"，notices 是 persistable 但不是 user，不同轴 |
| `subagent/events/bridge.py:91` | 非 langchain `HumanMessage`，是 `FSInboundMessage`，不同对象 |
| `media_pipeline/scrub.py:40` | 媒体块过滤，不是消息分类 |
| `context_eviction/core.py:319` | eviction 水印，不是消息分类 |
| `todo_continuation/core.py:345`（`_is_continuation_turn`） | 要的是"这条是不是我们的 continuation 指令"，和"是不是用户消息"相反 |

**迁移策略**：不在本计划内一次性迁移全部 14 个调用点。本计划只新建 `is_user_message` +
修 summarization 模块的 3 个调用点（`_build_static_fallback_summary` / `_serialize_for_summary` /
`_latest_human_eviction_ref`）。其余调用点作为后续独立 PR 逐步迁移，每个有独立的回归测试。

**修 `_build_static_fallback_summary`（L326）**：

```python
# L326 之前：
if not is_workspace_notice(msg):
    user_requests.append(content[:500])

# 之后：
from pub.func.message.classify import is_user_message
if is_user_message(msg):
    user_requests.append(content[:500])
```

一行替换，堵住全部 9 种注入 HumanMessage，未来新增注入类型只要带 `internal=True` 就自动排除。

#### 5c. `_serialize_for_summary` 同样修 bug

`_serialize_for_summary`（L217）用 `is_workspace_notice(msg) or is_system_reminder(msg)`
判断是否标 `[System notice]`——但 continuation 指令、subagent 完成通知、task-intent steering 等
7 种注入 HumanMessage 会被标成 `[User]: ...` 进入 LLM 压缩 prompt。**这不只影响 fallback 路径，
正常 LLM 压缩也受影响。**

```python
# L217 之前：
if is_workspace_notice(msg) or is_system_reminder(msg):
    content_parts.append(f"[System notice]: {text[:500]}")

# 之后：
from pub.func.message.classify import is_user_message
if isinstance(msg, HumanMessage):
    text = _extract_text(msg)
    if is_user_message(msg):
        content_parts.append(f"[User]: {text[:2000]}")
    else:
        content_parts.append(f"[System notice]: {text[:500]}")
```

这样所有注入 HumanMessage（reminder / notice / continuation / completion / steering / …）
统一标为 `[System notice]`，只有真实用户消息标为 `[User]`。LLM 压缩 prompt 不再混入
continuation 指令或 subagent 完成通知的文本。

### 6. `registered_sources()` 开销确认

`registered_sources()` 在 `agent/middlewares/reminder_catalog/core.py` 中返回
`dict(_SOURCES)`（浅拷贝 dict，~8 个 key-value）。在 `abefore_model` 中每轮调一次，
开销为一次 dict 浅拷贝（微秒级），可忽略。**如未来 source 数量增长到 50+，改为
直接遍历 `_SOURCES.items()` 避免 copy**。

### 7. `continuity_context` 序列化特殊处理

`continuity_context`（`compress_exclude=False`）留在摘要消息列表后，当前仍走
`_serialize_for_summary` 的 `is_system_reminder(msg)` 分支，被 500 字符截断标成
`[System notice]`。但它本质是连续性上下文而非系统通知，500 字符截断可能丢信息。

**改动**：`_serialize_for_summary`（`summary_generation.py` ~L217）的
`is_system_reminder(msg)` 分支内，对 `compress_exclude=False` 的 internal 消息不截断，
改用更大限额（2000 字符）并标注 `[Continuity]`：

```python
# L217 区域，HumanMessage 分支内（配合 5c 的 is_user_message 正面检查）：
if isinstance(msg, HumanMessage):
    text = _extract_text(msg)
    if is_user_message(msg):  # origin == "user"
        content_parts.append(f"[User]: {text[:2000]}")
    else:
        # 注入消息——检查是否需要保留连续性上下文
        meta = getattr(msg, "metadata", None) or {}
        if meta.get("compress_exclude") is False:
            # continuity_context：连续性上下文，不截断到 500，用 2000 限额
            content_parts.append(f"[Continuity]: {text[:2000]}")
        else:
            # 其他注入消息（旧消息无标签或被 filter 漏掉的）
            content_parts.append(f"[System notice]: {text[:500]}")
    continue
```

**fail-closed 保证**：没有 metadata 的消息 → `is_user_message` 返回 False →
标 `[System notice]` 而非 `[User]`。如果未来新增注入路径忘了设 origin，
不会被误标为 `[User]`。

**注意**：经过 `_filter_summary_messages` 后，走到 `_serialize_for_summary` 的
注入 HumanMessage 只剩 `continuity_context`（compress_exclude=False）和可能的无标签
旧消息。上述三分支（User / Continuity / System notice）对三种情况都正确处理。

- **`_serialize_for_summary`（L217）**：重构为三分支：
  - `is_user_message(msg)` → `[User]: text[:2000]`（真实用户消息）
  - `internal=True` 且 `compress_exclude=False` → `[Continuity]: text[:2000]`（continuity_context）
  - `internal=True` 且其他 → `[System notice]: text[:500]`（旧消息/漏网注入）
  这比原来的 `is_workspace_notice or is_system_reminder` whack-a-mole 更彻底——
  9 种注入 HumanMessage 全部正确归类，不只 2 种。
- **`transient` 机制**：不变。`transient` 管的是"跨轮是否留在消息列表"，
  `compress_exclude` 管的是"压缩时是否参与摘要"，两个维度正交。
- **`reset_session`**：不变。压缩后清 throttle state 让下一轮重新注入，
  与本改动无冲突。
- **workspace_notice**：不变。它是历史事件（目录切换记录），有历史价值，
  保留在摘要里，继续走 `[System notice]` 序列化。

## 边界与兼容

- **旧消息无 `compress_exclude` 标签**：`_is_compress_excluded_reminder` 检查
  `meta.get("compress_exclude") is True`，缺省为 None ≠ True，不会被过滤。
  压缩启动时消息列表里可能混有改动前注入的旧 reminder（无标签）——这些会
  继续走老行为（序列化为 `[System notice]`），不会丢。一次压缩周期后
  旧消息自然被替换。
- **subagent 的 reminder**：subagent 复用同一个 `DynamicReminderMiddleware`
  和同一个 reminder catalog，metadata 标签一致，过滤行为相同。
- **`compress_exclude=False` 但 `transient=True` 的 source**：当前不存在
  这种组合。如果未来出现，transient 会在非注入轮剥离它，压缩时如果恰好
  在切片里则不会被过滤（compress_exclude=False）——这是正确的：声明了
  要保留就保留。

**跨计划交互**：
- 如果 `计划_记忆系统优化重构.md` Route A 的 Phase 5（语义召回）落地，
  新增的 `memory_recall` reminder source 需设 `compress_exclude=True`
  （语义召回结果每轮重新派生，压缩后无价值）。本计划的过滤机制自动覆盖。

## 验收

1. **单元测试**：
   - `is_user_message`：真实用户消息（`{"origin":"user"}`）→ True；
     9 种注入消息（`internal=True`）→ False。
   - 构造一组消息（user / ai / tool / 7 个 compress_exclude=True 的
     reminder / 1 个 compress_exclude=False 的 continuity_context /
     1 个 continuation 指令 / 1 个 subagent 完成通知），
     调 `_filter_summary_messages`，断言 7 个被过滤、continuity_context 保留。
   - 调 `_build_static_fallback_summary`，断言 `user_requests` 只含真实用户消息，
     continuation 指令和 subagent 通知不被收集。
   - 调 `_serialize_for_summary`，断言：
     真实用户消息 → `[User]:`；continuity_context → `[Continuity]:`；
     continuation 指令 → `[System notice]:`（不再标 `[User]:`）。
   - 构造同组消息调 `_build_static_fallback_summary`，断言
     `user_requests` 不包含任何 `is_system_reminder` 的内容。
   - 验证无 `compress_exclude` 标签的旧 reminder 不被过滤（兼容性）。
   - 验证 > 500 字符的 continuity_context 不被 500 截断（用 2000 限额）。

2. **集成测试**：
   - 跑一个多轮对话，触发压缩，检查摘要 prompt 的 `<conversation>` 里
     不含 todo / taskflow / boulder / plan_mode / knowledge_index /
     project_dir_notice / project_instructions 的 `<system-reminder>` 文本，
     但包含 continuity_context。

3. **回归**：
   - `uv run pytest tests/agent/middlewares -q -k "not llm_e2e"` 全绿。
   - `uv run --no-sync python tests/run_tests_split.py` CI gate 全绿。
   - `uv run --with ruff ruff check . && uv run --with ruff ruff format --check .`
   - `uv run --no-sync basedpyright agent/`

## 涉及文件

| 文件 | 改动 |
|---|---|
| `pub/func/message/classify.py` | **新建** — `is_user_message(msg)` 统一检查 `metadata.internal` |
| `agent/middlewares/reminder_catalog/core.py` | `ReminderSource` 新增 `compress_exclude` 字段 |
| `agent/middlewares/reminder_catalog/sources.py` | `continuity_context` 设 `compress_exclude=False` |
| `pub/func/message/system_reminder.py` | `system_reminder_metadata` 接受并写入 `compress_exclude` |
| `agent/middlewares/reminder_catalog/dynamic.py` | 注入时从 registry 查 `compress_exclude` 传入 metadata |
| `agent/middlewares/summarization/summary_generation.py` | `_filter_summary_messages` 增加过滤 + `_build_static_fallback_summary` 用 `is_user_message` + `_serialize_for_summary` 用 `is_user_message` 三分支（User/Continuity/System notice） |
