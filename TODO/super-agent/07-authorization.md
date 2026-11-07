# 07 授权与 Policy：gate、admission、Mandate

复刻原型 `src/policy.py` + `src/admission.py` + `002-authorization.md`。host 强制 allowed list/rules/Mandate；消息/评论/引用是决策输入，**不能**改权限。

## 1. Policy（`superagent/policy.py`）

```python
class Policy:
    """stateless，每次用从 settings 重建。"""
    version: str                # hash(rules + accept_mode + Source parts)
    paused: bool
    eval_timeout_ms: int

    def gate(self, trigger, thread, tickets) -> GateResult: ...
    def revalidate(self, mandate) -> RevalidateResult: ...
    def admits_inferred(self, rules) -> bool: ...
```

`settings_policy(settings, scope) -> Policy` 每次调用返回新实例。

### 1.1 Policy version

`policy_version = hash(rules + accept_mode + 各 Source Policy part（含 bot list）)`。改规则 → version 变 → 丢弃运行 Eval + 通知 live Ticket。

### 1.2 Policy part（每 Source）

```python
{
  "github": {"allowed": [{"id": "<login>"}], "bots": [{"id": "dependabot[bot]"}]},
  "feishu": {"allowed": [{"id": "ou_…", "name": "…"}], "sync_chat": "oc_…",
             "admission": {"oc_…": {"unlisted_addressed": "explicit"}}}
}
```

- `allowed`：可发起工作的 sender id（case-insensitive）。
- `bots`：可产 inferred candidate 的 bot。
- `admission`：admission 表（见 §3）。
- `sync_chat`（Feishu）：从该 chat 同步成员名单。

## 2. 准入 gate（`Policy.gate`）

Trigger 两条路径：

### 2.1 Eval 路径（决定是否开新工作）

Source 记 `addressed`（mention/direct）。gate 检查 sender + Source Policy：

| 情况 | 强度 |
|---|---|
| 名单里的人 @ 智能体 | explicit |
| 名单里的人 follow-up（Thread 有 Ticket） | explicit |
| 非名单的人 follow-up | inferred（除非其 admission cell 是 explicit） |
| 非名单的人在其他消息 | admission 表决定 |
| bot list 上的 bot @ 或全收 scope | inferred candidate |
| 其他 bot | not admitted |

### 2.2 订阅路径（继续已有工作）

订阅 Thread 上的**所有**消息（含 bot/CI）扇出到订阅 Ticket，bypass gate，不授权。人消息到 Ticket → 归该 Ticket Executor 决定。

## 3. admission 表（`superagent/admission.py`）

每 Source Policy 有一张 admission 表。行 = receiving scope：

- **Feishu**：每个配置的 chat + 其他收到 mention 的 chat + DM。
- **GitHub**：new Issues / new pull requests / comments / default-branch CI。

每行三 cell，每 cell `explicit|inferred|ignore`：

| Cell | 默认 | 适用 |
|---|---|---|
| listed-unaddressed | inferred | 名单里的人，未 @ |
| unlisted-addressed | ignore | 非名单，@ 了 |
| unlisted-unaddressed | ignore | 非名单，未 @ |

- 未 @ 的 cell 仅在 Source 收所有消息时可用（Feishu `listen=all` / GitHub receive-all 开关）。
- CI 无 sender，只第 3 cell。
- unlisted-addressed 设 explicit → 任何人 @ 智能体即可请求（适合支持群）。
- admission 表是 Policy version 一部分；weaken cell 不立即停 open Ticket，但 revalidation 时可撤 Mandate。
- 名单里的人的 follow-up 通常 bypass 表，但 explicit cell 仍给 explicit 强度。

## 4. 强度（Strength）

| Strength | Cases |
|---|---|
| explicit | 名单 @ 智能体；名单 follow-up；admission explicit cell |
| inferred | 非名单 follow-up（除非 explicit cell）；admission inferred cell；bot list @ 或全收 scope |
| not admitted | 其他 bot；admission ignore；inferred case 在 off mode |

## 5. accept mode

| Mode | Effect |
|---|---|
| `off` | inferred 全不进 candidate |
| `observe`（默认） | inferred 走 assessment；正向记 `ignore marked "would accept"`，不接 |
| `auto` | inferred assessment 正向 → accept |

explicit 请求三模式都工作。建议从 observe 起，review Decision 后切 auto。

## 6. Mandate（`superagent/core/model.py::Mandate`）

accept 授予的，immutable。字段（见 02-domain-model.md）：

- `policy_version`、`scope`、`strength`、`matched_rules`、`sender`、`via`、`basis_detail`。

只有三个操作改 Mandate：

1. accept 创建。
2. 人 stop 撤。
3. revalidation 失败撤。

### 6.1 revalidation（resume 前调）

Source 必须**仍听**原 Thread。其余按 basis：

| Basis | revalidation |
|---|---|
| explicit follow-up | 通过 |
| inferred | 需 auto mode + matched rules 仍 enabled |
| admission cell | 当今表 + sender 成员仍给 ≥ 原强度（删 sender 可撤） |
| bot | 该 bot 仍在 bot list；inferred 还需 auto + rules |
| 其他 | sender 仍在 allowed list |

revalidation 失败 → Ticket 保持 ended，waiting 消息不取；每条消息当未到 Ticket，若无其他 owner 进 Eval。

## 7. 订阅与 follow-up

### 7.1 扇出

订阅 Thread 上的消息扇出到所有 live 订阅 Ticket（含 ended 未人 stop）。人消息到 Ticket → 归该 Ticket Executor，无 Eval。bot/CI 不到 ended Ticket（resume 时 Executor 重读 Thread）。

### 7.2 ownership

Executor own 期间（含 Subscription 删后、含 ended 未人 stop 且有 waiting 消息）：

- main Session 只能 stop（accept/attach/ignore/requeue 拒绝）。
- Executor 处理 in-scope；超出 scope 或属其他 Ticket → handback。

### 7.3 handback

Executor 一次 handback（Ticket 保 Subscription）。attached Thread 无关 → handback owned follow-up。Ticket end 释放未读/未 ack/未 handback 的 follow-up。每释放消息（无其他 owner）进 Eval，candidate 带 Executors 的 reasons。

### 7.4 context-only 订阅

Executor 可 follow 相关 Thread 仅取上下文：

- 消息到 Ticket，但不授权、不增强 follow-up strength。
- 人消息**不**唤醒 ended Ticket（除非 main Session attach）。
- main Session 的 note 也是 context-only。

### 7.5 attach

main Session 仅在消息**显式指向** Ticket（PR/Issue/source message/ack/最近改动唯一匹配）时 attach。topic 相似不够。attach 订阅整个 Thread；Executor 觉无关 → `mark_unrelated` + 不答。

## 8. resume 与 stop

### 8.1 resume

人消息在 ended Ticket 的 Thread 上 → 不新 Eval/Decision，原 Mandate revalidate（§6.1）→ 通过则 reopen + 新 Input + 唤醒 Executor。bot/CI 不唤醒。Executor 决定请求是否 in-scope，不 in-scope 则结束本轮。

### 8.2 stop

- 人 `@agent stop` 是普通 Trigger → Eval 决定 stop 强度。
- 面板 stop 是命令，直接作用 Ticket，记 manual stop。
- Executor **不能**宣布人要 stop（防社工）。
- stop 撤 Mandate，Ticket 永久 ended，不再收消息。stop 消息若被 handback 可再 Eval（main Session 可接其余为新工作）。

## 9. Eval 超时

- deadline 从 candidate 到达 main Session 起计（未 delivered 不超时）。
- 超时重排 Triggers。两次连续重排后第三次记 `unavailable`。
- assessment failure 也记 `unavailable`。
- Policy 变更或对过时 Policy 的 decision → `unavailable` + 立即重排当前版本。
- `evals` 健康项报最近 1h 无后续 judgement/dismissal 的 unavailable Decision。

面板提示人：unavailable Decision <24h + 仍是 Thread 最新 + 未 dismiss + Thread 非 queued/evaluating。

## 10. Policy 变更

`policy_changed(old, now, effects)`：

1. 通知受影响 live Ticket（`policyEffects`）：
   - matched rule 变（含新文本）或不再适用。
   - requester 可能不再发起工作。
   - proactive 不再被接（mode auto→其他）。
2. 不受影响 Ticket 不通知。
3. 不立即撤 live Ticket Mandate。
4. 丢弃运行 Eval + 重排队列（新 version 下）。

`policyEffects` 识别哪些 open Ticket 受影响 + 如何。

## 11. Source 配置 vs Policy

| 维度 | Source 配置 | Policy |
|---|---|---|
| 控什么 | 进什么系统（哪些 chat、receive-all/mentions） | 谁能发起工作、规则 |
| 改 Mandate? | 否（Mandate 记 Source/scope） | 是（revalidation） |
| 改后 | 仍听 Thread 才能 resume | 撤 Mandate 阻止 resume |

replies 等 Source off 时 wait；Source 不再听目的地 → drop。

## 12. 该册验收门

- `Policy` + `gate` + `revalidate` + `admits_inferred` 实现。
- admission 表三 cell × 三值全覆盖（`tests/superagent/test_policy.py`）。
- 强度表全 case 单测（explicit/inferred/not_admitted 各路径）。
- accept mode：off/observe/auto 三态 + inferred 在各态行为。
- Mandate revalidation：按 basis 分支全测（explicit-followup 通过、inferred 需 auto+rules、bot 需 list、cell 需表+成员）。
- resume：人消息唤醒、bot/CI 不唤醒、revalidation 失败保持 ended。
- stop：人 stop 撤 Mandate + 永久 ended；Eval 决定 stop 强度。
- handback：一次、保 Subscription、release 未读 follow-up。
- attach：显式指向才 attach；topic 相似拒绝。
- Policy 变更：通知受影响 Ticket + 丢运行 Eval + 重排；不受影响不通知。
- Eval 超时：未 delivered 不超时；两次重排后 unavailable。
- `tests/superagent/test_policy.py` 镜像 `tests/policy.spec.ts`。
- `tests/superagent/core/test_engine.py` 覆盖 follow-up ownership/handback（`tests/followups.spec.ts`）。
