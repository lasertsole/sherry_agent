# 计划：响应式压缩对齐 ZCode Reactive phase（显式化 + 可观测性）

- 建档日期：2026-10-19
- 状态：**计划（未开工）**
- **重要事实**：sherry **已经有响应式压缩**——`agent/middlewares/summarization/overflow.py` 的 T4(`payload_too_large`)/ T5(`context_overflow`)forced-recovery loop 就是 provider 报错后的响应式压缩。本 TODO 不是从零实现，是**对齐 ZCode `CompactPhase.Reactive` 的显式化设计**——加 phase 标记、可观测性、reason 分类。
- **硬约束**：**不破坏 T1-T5 现有行为**——T4/T5 forced-recovery loop 的核心逻辑（`classify_provider_error` → forced compression → retry handler）一字不改，详见 §1
- 关联代码：
  - **sherry 响应式压缩现状**（核心改造对象）：
    - `agent/middlewares/summarization/overflow.py:625-720`（`_forced_recovery_request` / `_aforced_recovery_request` / `_execute_with_recovery` / `_aexecute_with_recovery`）
    - `agent/middlewares/summarization/overflow.py:105-112`（`PAYLOAD_TOO_LARGE: "T4"` / `CONTEXT_OVERFLOW: "T5"` 映射 + retry key）
    - `agent/middlewares/summarization/core.py:545, 551, 581, 606, 612, 623`（`_execute_with_recovery` / `_aexecute_with_recovery` 在 wrap_model_call 内的调用点）
    - `pub/func/message/llm_error_classifier.py:96-97, 389-435`（`classify_provider_error` + `_CONTEXT_OVERFLOW_PATTERNS`）
  - **ZCode 参考**（对齐目标）：
    - `ZCode-main/.../packages/contracts/src/compact/index.ts:32-39, 114-117`（`CompactPhase` 四值：StandaloneTurn / PreRequest / MidTurn / **Reactive**）
    - `ZCode-main/.../packages/core/src/runtime/helpers/compact.ts:43`（`CompactPhase.Reactive` 触发点）
    - `ZCode-main/.../packages/core/src/runtime/methods/compact.ts:429-431`（`compactReason: ProviderOverflow, phase: Reactive`）
  - **关联 TODO**：`TODO/计划_TaskFlow决策LLM独立配置.md`（任务 B：压缩 LLM 独立配置；同属压缩路径改造，但独立）

---

## 0. 背景：sherry 现状 vs ZCode Reactive phase（已核实）

### 0.1 sherry 已经有响应式压缩（T4/T5）

`overflow.py:1` 的 docstring 开头就写："Overflow routing and provider-error recovery (T1-T5) for summarization."

T1-T5 的角色分工：

| Phase | 名字 | 触发时机 | 数据源 |
|---|---|---|---|
| **T1** | `before_agent` preflight | turn 开始前 | 估算 token |
| **T2** | `wrap_model_call` pre-call | 主 LLM 调用前 | 估算 token |
| **T3** | post-response re-check | 主 LLM 调用返回后 | provider 报告的真实 token |
| **T4** | `payload_too_large` recovery | provider 报 `payload_too_large` 错后 | 报错本身 |
| **T5** | `context_overflow` recovery | provider 报 `context_overflow` 错后 | 报错本身 |

**T4/T5 就是响应式压缩**——provider 报错后，`_execute_with_recovery`(`overflow.py:674-720`)捕获异常，`classify_provider_error(exc)` 分类，命中 T4/T5 就跑 `_forced_recovery_request` 强制压缩，然后 retry handler。

关键代码（`overflow.py:700-714`）：

```python
while True:
    try:
        return handler(request)
    except Exception as exc:
        error_class = classify_provider_error(exc)
        if error_class is None:
            raise  # 非 T4/T5 错误，原样抛出
        trigger = _TRIGGER_BY_ERROR_CLASS.get(error_class)
        retry_key = _RETRY_KEY_BY_ERROR_CLASS.get(error_class)
        if trigger is None or retry_key is None:
            raise
        retries = state_register_mem.get_state(session_id, retry_key, 0) or 0
        # ... forced compression + retry handler，最多 MAX_OVERFLOW_RETRIES 次
```

**所以"让 sherry 也能响应式压缩"这个说法不准确——sherry 早就有了**。本 TODO 的真正目标是**对齐 ZCode 的显式化设计**。

### 0.2 sherry 跟 ZCode 的差距

| 维度 | sherry（现状） | ZCode（对齐目标） |
|---|---|---|
| 响应式压缩能力 | ✅ 有（T4/T5） | ✅ 有（`CompactPhase.Reactive`） |
| **显式 phase 标记** | ❌ T4/T5 是 `_execute_with_recovery` 内部的隐式 recovery loop | ✅ `CompactPhase.Reactive` 跟 PreRequest/MidTurn/StandaloneTurn 显式并列 |
| **reason 分类** | ❌ 隐式（只分 `payload_too_large` / `context_overflow` 两个 error_class） | ✅ 显式 `CompactReason`（`ContextLimit` / `ProviderOverflow` / `UserRequested`） |
| **trajectory event** | 不带 phase 字段（只记 `compact` + `messages_before/after`） | 显式记 `phase` + `reason` |
| **路径独立** | 混在 `wrap_model_call` 内部 | 独立 runtime method（`compact.ts` 显式分类） |
| **可调用性** | 只能从 `wrap_model_call` 触发，不能外部主动调 | 可通过 runtime API 主动触发（用户手动 `/compact` 走 StandaloneTurn） |
| **可观测性** | 日志只说 "trigger=T4/T5"，不区分 phase 概念 | 日志显式 `phase=Reactive reason=ProviderOverflow` |

### 0.3 改造目标

借鉴 ZCode，把 sherry 的响应式压缩（T4/T5）从**隐式 recovery loop** 升级为**显式 Reactive phase**：

1. **加 phase 标记**：把 T4/T5 显式标为 `ReactivePhase`，跟 T1/T2/T3 的 pre-call/post-call 隐式分类对齐
2. **加 reason 分类**：引入 `CompactReason` 枚举（`ContextLimit` / `ProviderOverflow` / `UserRequested`）
3. **加 trajectory event 字段**：`compact` event 加 `phase` + `reason` 字段
4. **（可选）抽独立 method**：把 `_forced_recovery_request` 抽出来，能从外部主动调（用于未来"用户手动触发压缩"场景）

---

## 1. ⚠️ 硬约束：不破坏 T1-T5 现有行为

| 约束 | 含义 | 验收方式 |
|---|---|---|
| **R1**：T4/T5 forced-recovery 核心逻辑不变 | `classify_provider_error` → forced compression → retry handler 的循环结构不动；`MAX_OVERFLOW_RETRIES` 上限不动；耗尽 retry 后原异常抛出的语义不动 | 触发 T4/T5 的 e2e 测试 byte-identical |
| **R2**：`_execute_with_recovery` / `_aexecute_with_recovery` 签名不变 | 外部调用点（`core.py:545, 551, 581, 606, 612, 623`）的调用方式不变 | grep 调用点不变 |
| **R3**：非 T4/T5 错误传播不变 | `classify_provider_error` 返回 None 的异常仍原样抛出，不吞错 | 非 context overflow 类异常测试 byte-identical |
| **R4**：T1/T2/T3 行为不动 | preflight / pre-call / post-response 的触发条件、route 决策、dispatch 路径不变 | T1/T2/T3 的现有测试 byte-identical |
| **R5**：`_FORCE_RECOVERY_KEY` 语义不变 | forced-recovery 仍 bypass anti-thrash gate（不计冷却、不计 turn attempt），但不实际设置 `_FORCE_RECOVERY_KEY`（这是现有设计） | grep `_FORCE_RECOVERY_KEY` 的使用点不变 |
| **R6**：trajectory event 向后兼容 | 现有 `compact` event 的 `messages_before` / `messages_after` 字段保留；新字段（`phase` / `reason`）追加，不替换 | 老 trajectory 读取代码不崩 |
| **R7**：新枚举是 derived | `CompactPhase` / `CompactReason` 是新加的 derived 标记，从 trigger/error_class 推导，不修改 trigger/error_class 本身 | 触发 T4 的代码路径不变，只是 event 里多记一个 phase="Reactive" 字段 |
| **R8**：不破坏压缩 LLM 配置 | 跟 `TODO/计划_TaskFlow决策LLM独立配置.md` 任务 B（压缩 LLM 独立配置）正交——本 TODO 不动 LLM 选择，只动 phase/reason 标记 | 两个 TODO 可并行 |

---

## 2. 任务一：引入 `CompactPhase` 和 `CompactReason` 枚举（基础设施）

### 2.1 新建 `agent/middlewares/summarization/phase.py`

对齐 ZCode 的 `CompactPhase`（4 值）+ `CompactReason`（3 值）：

```python
"""Compact phase + reason classification (对齐 ZCode CompactPhase/CompactReason).

Derived from the trigger/error_class — never modifies them. Used in trajectory
events and logs to make the compression path observable, mirroring ZCode's
explicit phase/reason taxonomy.
"""

from enum import StrEnum


class CompactPhase(StrEnum):
    """When in the turn lifecycle a compression runs. 对齐 ZCode CompactPhase."""

    STANDALONE_TURN = "standalone_turn"   # 用户手动 /compact（sherry 暂不支持，但保留枚举位）
    PRE_REQUEST = "pre_request"          # T1 before_agent + T2 wrap_model_call pre-call
    MID_TURN = "mid_turn"                 # 工具调用后的下一次 pre-call（sherry 不显式区分，但保留）
    REACTIVE = "reactive"                # T4 payload_too_large + T5 context_overflow forced-recovery
    POST_RESPONSE = "post_response"      # T3 post-response re-check（sherry 独有，ZCode 没有显式对应）


class CompactReason(StrEnum):
    """Why a compression runs. 对齐 ZCode CompactReason."""

    CONTEXT_LIMIT = "context_limit"        # T1/T2/T3：估算或报告的 token 超 ctx × ratio
    PROVIDER_OVERFLOW = "provider_overflow"  # T4/T5：provider 报 payload_too_large / context_overflow
    USER_REQUESTED = "user_requested"      # 用户主动 /compact（sherry 暂不支持，保留枚举位）


_TRIGGER_TO_PHASE_REASON = {
    "T1": (CompactPhase.PRE_REQUEST, CompactReason.CONTEXT_LIMIT),
    "T2": (CompactPhase.PRE_REQUEST, CompactReason.CONTEXT_LIMIT),
    "T3": (CompactPhase.POST_RESPONSE, CompactReason.CONTEXT_LIMIT),
    "T4": (CompactPhase.REACTIVE, CompactReason.PROVIDER_OVERFLOW),
    "T5": (CompactPhase.REACTIVE, CompactReason.PROVIDER_OVERFLOW),
}


def derive_phase_reason(trigger: str) -> tuple[CompactPhase, CompactReason]:
    """Derive (phase, reason) from an existing T1-T5 trigger string (R7)."""
    return _TRIGGER_TO_PHASE.get(trigger, (CompactPhase.PRE_REQUEST, CompactReason.CONTEXT_LIMIT))
```

### 2.2 改动清单

- [ ] **2.2.1** 新建 `agent/middlewares/summarization/phase.py`（如上）
- [ ] **2.2.2** `agent/middlewares/summarization/__init__.py` 导出 `CompactPhase` / `CompactReason` / `derive_phase_reason`

### 2.3 风险

- **STANDALONE_TURN 和 USER_REQUESTED 是预留枚举位**：sherry 当前不支持用户手动 `/compact`（ZCode 支持）。先留枚举位不实施，未来如果加 `/compact` 命令再启用。
- **POST_RESPONSE 是 sherry 独有**：ZCode 的 4 phase 没有 post-response，sherry 的 T3 是独立一类。不要硬塞进 ZCode 的分类，保留 sherry 的特色。

**验收**：R7。新建 `tests/agent/middlewares/summarization/test_phase.py`，断言所有 T1-T5 trigger 都能 derive 出正确的 (phase, reason)。

---

## 3. 任务二：trajectory event 加 `phase` + `reason` 字段（可观测性）

### 3.1 目标

sherry 当前 `record_trajectory_event(session_id, "compact", {"messages_before": ..., "messages_after": ...})`（`core.py:575-579`）——不带 phase/reason。借鉴 ZCode，加这两个字段。

### 3.2 改动清单

- [ ] **3.2.1** 找到所有 `record_trajectory_event(session_id, "compact", ...)` 调用点：
  - `agent/middlewares/summarization/core.py:575-579`（T2 路径）
  - 可能还有 T1/T3/T4/T5 路径的调用点（要 grep 确认）
- [ ] **3.2.2** 给每个调用点的 payload 追加 `phase` + `reason` 字段：
  ```python
  from .phase import derive_phase_reason

  phase, reason = derive_phase_reason("T2")  # 当前 trigger
  record_trajectory_event(
      session_id,
      "compact",
      {
          "messages_before": messages_before,
          "messages_after": messages_after,
          "phase": phase.value,         # 新增
          "reason": reason.value,       # 新增
          "trigger": "T2",              # 保留（向后兼容，R6）
      },
  )
  ```
- [ ] **3.2.3** T4/T5 forced-recovery 路径也要加——`_forced_recovery_request` / `_aforced_recovery_request`（`overflow.py:625-672`）成功压缩后也发 trajectory event，标 `phase=reactive` + `reason=provider_overflow`
- [ ] **3.2.4** `server/service/trajectory_store.py` 的 schema 检查：确认 `event_data` JSON 字段能容纳新字段（应该是 JSON 列，自动兼容）

### 3.3 风险

- **trajectory event 是 fail-open**：写入失败不阻塞压缩（AGENTS.md 提过 trajectory ledger 是 fail-open）——所以加字段不会引入新失败模式
- **R6 向后兼容**：老 trajectory 读取代码读不到 `phase`/`reason` 字段不崩（JSON 字段缺失返回 None）

**验收**：R6。新建 `tests/agent/middlewares/summarization/test_trajectory_phase.py`，断言 T4/T5 路径触发后 trajectory event 含 `phase=reactive`。

---

## 4. 任务三：日志显式化（debug 可读性）

### 4.1 目标

当前日志（`core.py:569-574`）只说 `Summarization: compacted (path=T2, messages 50 -> 20, session=...)`——没说 phase/reason。加 phase/reason 让日志更可读。

### 4.2 改动清单

- [ ] **4.2.1** `core.py:569-574` 的 logger.info 改造，加 phase/reason：
  ```python
  logger.info(
      "Summarization: compacted (path=T2, phase={}, reason={}, messages {} -> {}, session={})",
      phase.value, reason.value, messages_before, messages_after, session_id,
  )
  ```
- [ ] **4.2.2** T4/T5 forced-recovery 路径的 logger 也加 phase/reason——`overflow.py:630-660` 的 `_forced_recovery_request` / `_aforced_recovery_request` 加日志，标 `phase=reactive reason=provider_overflow`
- [ ] **4.2.3** T3 post-response 路径的 logger（`overflow.py:501, 537, 560`）加 `phase=post_response`

### 4.3 风险

- **日志格式变化**：现有日志解析（如果有 monitor 系统 grep 日志）可能受影响。但 phase/reason 是追加字段，不删现有 `path=T2` 等字段——R4/R6 向后兼容

**验收**：日志格式追加，不删字段。

---

## 5. 任务四（可选）：把 `_forced_recovery_request` 抽成独立 method（可调用性）

### 5.1 目标

ZCode 的 `CompactPhase.Reactive` 是独立 runtime method，可从外部主动调（用户手动触发响应式压缩）。sherry 的 T4/T5 隐藏在 `_execute_with_recovery` 内部，不能外部主动调。

### 5.2 改动清单（可选）

- [ ] **5.2.1** 把 `_forced_recovery_request` / `_aforced_recovery_request`（`overflow.py:625-672`）抽出来，作为 `SummarizationMiddleware` 的公开方法 `force_reactive_compact(request, session_id, error_class)`
- [ ] **5.2.2** 加 `_execute_with_recovery` 调用这个公开方法（内部走同一个，不动逻辑）
- [ ] **5.2.3** 未来如果加 `/compact` 命令（对应 ZCode 的 StandaloneTurn），可以从外部调这个方法

### 5.3 风险

- **优先级低**：sherry 当前不支持用户手动 `/compact`，这个改造是为未来扩展铺路。如果短期内不打算加 `/compact`，可以跳过这个任务。
- **接口稳定性**：抽公开方法后，签名要稳定（避免未来 break）。

**验收**：可选。如果做，新建 `tests/agent/middlewares/summarization/test_force_reactive_compact.py`。

---

## 6. 任务五（可选）：对齐 ZCode 的 `CompactReason.ProviderOverflow` 区分

### 6.1 目标

ZCode 把 T4（payload_too_large）和 T5（context_overflow）都归到 `CompactReason.ProviderOverflow`——不区分是 payload 还是 context。sherry 当前区分 T4/T5，更细。要不要对齐 ZCode 的"统一为 ProviderOverflow"是个设计选择。

### 6.2 改动清单（可选）

- [ ] **6.2.1** 决策点：保留 T4/T5 细分（sherry 现状）vs 统一为 ProviderOverflow（对齐 ZCode）
- [ ] **6.2.2** 如果统一：`derive_phase_reason` 对 T4/T5 都返回 `reason=provider_overflow`，但保留 `trigger="T4"` / `trigger="T5"` 字段向后兼容

### 6.3 推荐

**保留 T4/T5 细分**——sherry 的细分更有用（payload too large 跟 context overflow 是不同问题：前者是单次请求体过大，后者是累计上下文超窗口），统一了反而丢信息。`reason=provider_overflow` 作为粗分类，`trigger=T4/T5` 作为细分类，两层并存。

**验收**：不做改动，保留现状。本任务标"可选"。

---

## 7. 执行顺序与依赖

```
§2 引入 CompactPhase/CompactReason 枚举  — 0.5 天（基础设施，必做）
  │
  ↓
§3 trajectory event 加 phase/reason 字段  — 0.5 天（可观测性，必做）
  │
  ↓
§4 日志显式化                              — 0.25 天（debug 可读性，必做）
  │
  ↓
§5 抽独立 method（可选，为未来 /compact 铺路）— 0.5 天
§6 reason 细分决策（推荐保留现状，0 工作量）

总工程量：1.25-1.75 天（必做部分）/ 1.75-2.25 天（含可选）
```

**关键路径**：§2 枚举是基础，§3/§4 都依赖它。做完 §2 后 §3/§4 可以并行。

**跟其他 TODO 的关系**：
- 跟 `TODO/计划_TaskFlow决策LLM独立配置.md` 任务 B（压缩 LLM 独立配置）**正交**——本 TODO 不动 LLM 选择，只动 phase/reason 标记。两个可并行做。
- 跟 `TODO/计划_GraphRAG_*` 系列独立。

---

## 8. 验收标准

### 8.1 硬约束验收（每项必须过）

- [ ] **R1**：T4/T5 forced-recovery 核心循环逻辑不变（`classify_provider_error` → forced compression → retry handler 顺序 + `MAX_OVERFLOW_RETRIES` 上限 + 耗尽 retry 原异常抛出语义）。
- [ ] **R2**：`_execute_with_recovery` / `_aexecute_with_recovery` 函数签名不变。
- [ ] **R3**：非 T4/T5 错误（`classify_provider_error` 返回 None）仍原样抛出，不吞错。
- [ ] **R4**：T1/T2/T3 行为 byte-identical（preflight / pre-call / post-response 触发条件、route 决策、dispatch 路径）。
- [ ] **R5**：`_FORCE_RECOVERY_KEY` 语义不变（forced-recovery 仍 bypass anti-thrash，但不实际设置 key）。
- [ ] **R6**：trajectory event 新字段 `phase` / `reason` 是追加，老字段 `messages_before` / `messages_after` / `trigger` 保留。
- [ ] **R7**：`CompactPhase` / `CompactReason` 是 derived，从 trigger/error_class 推导，不修改 trigger/error_class 本身。
- [ ] **R8**：跟压缩 LLM 配置 TODO 正交，不冲突。

### 8.2 功能验收

- [ ] 触发 T4（payload_too_large）后，trajectory event 含 `phase=reactive` + `reason=provider_overflow` + `trigger=T4`。
- [ ] 触发 T5（context_overflow）后，trajectory event 含 `phase=reactive` + `reason=provider_overflow` + `trigger=T5`。
- [ ] 触发 T2（pre-call）后，trajectory event 含 `phase=pre_request` + `reason=context_limit` + `trigger=T2`。
- [ ] 触发 T3（post-response）后，trajectory event 含 `phase=post_response` + `reason=context_limit` + `trigger=T3`。
- [ ] 日志格式追加 `phase=` + `reason=`，不删 `path=` 字段。

### 8.3 回归验收

- [ ] 现有 `tests/agent/middlewares/summarization/` 全套测试通过（特别是 T4/T5 forced-recovery 的现有测试）。
- [ ] 现有 trajectory event 读取代码（`server/service/trajectory_store.py` + 客户端 trajectory 渲染）不崩。
- [ ] 现有日志解析（如果有 monitor）不崩。

---

## 9. 不做的事（边界）

| 项 | 不做的原因 |
|---|---|
| 从零实现响应式压缩 | sherry 已经有 T4/T5 forced-recovery（§0.1），本 TODO 是改进不是从零实现 |
| 用户手动 `/compact` 命令 | 对应 ZCode 的 StandaloneTurn + UserRequested，sherry 当前不支持；本 TODO 只预留枚举位，实施另起 TODO |
| 区分流式过程中触发压缩 | sherry 和 ZCode 都不支持，本 TODO 也不做（流式中触发压缩需要 stream cancel + 重发，体验代价大，另起 TODO 评估） |
| 把 T1/T2 合并成 PreRequest | sherry 的 T1（before_agent preflight）和 T2（wrap_model_call pre-call）虽都映射到 `pre_request`，但内部逻辑不同（T1 是 turn 级、T2 是 model call 级）。保留 T1/T2 细分，phase 用 PreRequest 作粗分类 |
| 把 T4/T5 合并成一个 trigger | 保留 T4/T5 细分（§6.3 推荐），统一为 ProviderOverflow 是粗分类，两层并存 |
| 修改 `classify_provider_error` 的 pattern 列表 | 那是 `pub/func/message/llm_error_classifier.py` 的独立模块，不在本 TODO 范围 |

---

## 10. 关键参考

### sherry 内部（改造对象）

- `agent/middlewares/summarization/overflow.py:1, 105-112, 625-720`（T4/T5 forced-recovery loop）
- `agent/middlewares/summarization/overflow.py:674-720`（`_execute_with_recovery` 主循环）
- `agent/middlewares/summarization/core.py:533-632`（`wrap_model_call` / `awrap_model_call` 调用 `_execute_with_recovery`）
- `agent/middlewares/summarization/core.py:575-579`（trajectory event `compact` 当前调用点）
- `pub/func/message/llm_error_classifier.py:96-97, 389-435`（`classify_provider_error` + `_CONTEXT_OVERFLOW_PATTERNS`）
- `agent/trajectory_events.py`（trajectory event 写入 seam）

### ZCode 参考（对齐目标）

- `ZCode-main/.../packages/contracts/src/compact/index.ts:32-39, 114-117`（`CompactPhase` 四值）
- `ZCode-main/.../packages/core/src/runtime/helpers/compact.ts:43`（`CompactPhase.Reactive` 触发）
- `ZCode-main/.../packages/core/src/runtime/methods/compact.ts:429-431`（`compactReason: ProviderOverflow, phase: Reactive`）
- `ZCode-main/.../packages/core/src/runtime/methods/turn-loop.ts:83-88`（microcompact + autoCompact 在每个 model step 前触发，带 phase 标记）

### 关联 TODO

- `TODO/计划_TaskFlow决策LLM独立配置.md`（任务 B：压缩 LLM 独立配置；正交，可并行）
- `TODO/计划_GraphRAG_*` 系列（独立，互不影响）
