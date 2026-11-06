# 08 评估：MasterAssessor 与 Eval 上下文

复刻原型 `src/app/assessment.ts` + `src/sources/admission_context.ts`。MasterAssessor 让 main Session 评估 proactive candidate；`orchestrator_eval` 返回 context（不重复 instructions，instructions 在 `prompts/admission.md`）。

## 1. MasterAssessor（`superagent/app/assessment.py`）

```python
class MasterAssessor:
    """per-Eval 缓存 + context/read/decide 三个工具后端。"""
    def __init__(self, host: AssessorHost, settings_getter, sources: SourceRegistry):
        self.host = host
        self.settings_getter = settings_getter
        self.sources = sources
        self._cache: dict[str, EvalContext] = {}  # eval_id -> context

    def request(self, eval_id: str, trigger_ids: list[str]) -> None:
        """Engine 调：起 assessment effect（steer candidate card 进 main Session turn）。"""
        enqueue("assessment", eval_id, payload={"trigger_ids": trigger_ids})

    def release(self, eval_id: str) -> None:
        """Eval end 时清缓存。"""
        self._cache.pop(eval_id, None)

    def context(self, eval_id: str) -> EvalContext:
        """orchestrator_eval 工具调：返回 bounded context（不重复 instructions）。"""
        if eval_id not in self._cache:
            self._cache[eval_id] = self._build_context(eval_id)
        return self._cache[eval_id]

    def read(self, eval_id: str, message_id: str) -> ContextMessage:
        """orchestrator_eval_read：按 id 展开单条 stub 消息。"""
        ...

    def decide(self, eval_id: str, outcome, reason) -> None:
        """orchestrator_eval_decide：调 Engine.decide。"""
        self.host.engine.decide(trigger_id=..., outcome=outcome, reason=reason)
        self.release(eval_id)
```

## 2. Eval 上下文（`_build_context`）

对应原型 `007 §Result shapes and assessment context`。

```python
@dataclass
class EvalContext:
    trigger_text: str                  # ≤4000 字符
    image_ids: list[str]
    other_triggers_in_batch: list[TriggerCard]
    related_tickets: list[TicketCard]  # ≤8，open 优先
    tickets_total: int
    policy_version: str
    thread_window: ThreadWindow        # 首 5 + 左右各 5
    other_messages: list[MessageStub]  # 50 字符 stub

@dataclass
class ThreadWindow:
    first_five: list[ContextMessage]   # Thread 首页前 5 条
    around: list[ContextMessage]       # Trigger 左右各 5（≤1200 字符）
    # 普通 message ≤400 字符；truncated 带 chars

@dataclass
class ContextMessage:
    id: str
    frm: str                           # from（避开关键字）
    at: str
    text: str
    bot: bool = False
    deleted: bool = False
    reply_to: Optional[str] = None
    url: Optional[str] = None
    path: Optional[str] = None
    line: Optional[int] = None
    images: list[str] = field(default_factory=list)
```

### 2.1 边界

- Trigger 文本 ≤4000 字符。
- `related_tickets` ≤8（open 优先），加 `tickets_total`。
- Thread 窗口：首 5 + 左右各 5。普通 ≤400 字符，左右各 ≤1200。truncated 带 `chars`。
- 其他消息 stub 50 字符，`orchestrator_eval_read` 按 id 展开。
- 图片：`images` 字段引用，`image(...)` 读为 attachment（Feishu）。

### 2.2 构造流程

```python
def _build_context(self, eval_id) -> EvalContext:
    eval_ = self.host.engine.evals.get(eval_id)
    primary = self.host.repo.triggers.get(eval_.primary)
    batch = [self.host.repo.triggers.get(tid) for tid in eval_.trigger_ids]
    # Thread 上下文：经 SourceRegistry.threadContext
    thread_ctx = self.sources.thread_context(primary.source)
    window = self._read_window(thread_ctx, primary)
    # related tickets：Thread 上的 Ticket（open 优先，≤8）
    related = self._related_tickets(eval_.thread)
    return EvalContext(
        trigger_text=primary.text[:4000],
        image_ids=self._extract_images(primary),
        other_triggers_in_batch=[card(t) for t in batch if t.id != primary.id],
        related_tickets=related[:8],
        tickets_total=len(related),
        policy_version=self.host.policy().version,
        thread_window=window,
        other_messages=self._stubs(window),
    )
```

## 3. admission 上下文（`superagent/sources/admission_context.py`）

对应原型 `src/sources/admission_context.ts`。给 assessment 起始窗口：

```python
class AdmissionContext:
    """bounded window：Trigger Thread 的前几页 + Trigger 周围消息。
    经 SourceRegistry.threadContext 读。"""
    def __init__(self, sources: SourceRegistry, max_pages: int, timeout_ms: int):
        ...

    async def build(self, trigger: Trigger) -> ThreadWindow:
        kind = self.sources.kind(trigger.source)
        if not kind or not kind.thread_context:
            return ThreadWindow.empty()
        # 读 first page + around page（带 timeout）
        ...
```

`admission_context_max_pages` + `admission_context_timeout_ms` 是 machine limit（`06-deployment.md`）。

## 4. assessment 决策路径

```
Engine.evaluate_due
  → Assessor.request(eval_id, trigger_ids)
    → enqueue("assessment", eval_id)
      → effect handler: steer candidate card 进 main Session turn
        → main Session 读 orchestrator_eval context
          → 模型研判（每 enabled rule 判一次，matching 或 not）
            → orchestrator_eval_decide(outcome, reason)
              → Engine.decide
                → accept? 创建 Ticket + Mandate
                → ignore? 记 Decision
                → assessment failure → unavailable
```

## 5. inferred accept 规则

- inferred accept **必须** assessment。
- shortcut：card 明显无 enabled rule 命中（纯聊天/无关讨论）→ main Session 可直接 ignore，记 `ignoredFromCard`。
- Direct accept 拒非 explicit 的 Trigger（防 main Session 越权接 inferred）。
- accept mode `off` → inferred 全不进 candidate。
- `observe` → inferred assessment 正向记 `ignore marked "would accept"`，不接。
- `auto` → inferred assessment 正向 accept。
- 每个 assessment 对每个 enabled rule 判**恰好一次**（match 或 not）。proactive accept 需至少一个 matching rule。

## 6. Ticket 的 Mandate 与 matched rules

accept 时 Mandate 记 matched rules。Executor 收到 rule 名 + instructions。per-ticket：

- rule 的 process/communication 要求覆盖默认。
- silence rule 可压 acknowledgement。
- review rule 可命名必要 reviewer + approval。

每 Ticket 通用的要求放 team prompt section，不放 rule。

## 7.assessment outcome 转换

```python
def assessment_outcome(assessment, settings, strength) -> EngineOutcome:
    """把模型 assessment 转 Engine outcome。
    需每 enabled rule 的 judgement + settled direction + matching rule + intent 证据。
    非 auto mode：inferred accept → 'would accept' 记录。"""
    ...
```

## 8. assessment failure

- 模型调用失败/网络错 → 记 `unavailable`。
- 超时（candidate delivered 后 eval_timeout_ms）→ 重排 Triggers；两次后 unavailable。
- 后续 Trigger 可开新 Eval。

## 9. 与 Sherry 模型的接缝

- main Session 的 model：`role_models.py` 解析 `eval.model`（Settings）或默认。
- assessment 用 main Session 的 Sherry subagent（router role）跑。
- 模型调用经 Sherry 的 `models/LLMs/main_llm.py`（不直接调 provider）—— 复用 Sherry 的 provider 配置、retry、budget。
- 详见 `10-sessions-prompts.md` 与 `12-sherry-integration.md`。

## 10. 该册验收门

- `MasterAssessor` 实现 `request/release/context/read/decide`。
- `EvalContext` 字段与边界（4000/8/5+5/400/1200/50）全覆盖单测。
- `AdmissionContext` 带 timeout + max_pages 单测。
- assessment outcome 转换：auto/observe/off 三态 × explicit/inferred。
- inferred accept 必须 assessment；card 无规则可直 ignore（`ignoredFromCard`）。
- assessment failure → unavailable。
- `tests/superagent/test_assessment.py` 镜像 `tests/assessment.spec.ts`。
- `tests/superagent/sources/test_admission_context.py` 镜像 `tests/admission-context.spec.ts`。
- `tests/superagent/sources/test_feishu_image.py` 镜像 `tests/feishu-image.spec.ts`。
