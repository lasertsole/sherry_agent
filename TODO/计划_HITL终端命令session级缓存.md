# 计划：HITL 终端命令 session 级缓存（普通命令可选记忆，危险命令每次询问）

- 建档日期：2026-10-22
- 状态：**计划（未开工）**
- **文档范围**：修正 `TerminalApprovalHandler` 在用户 approve 后不写回 session allowlist 的断层——同一危险命令在本 session 内被反复审批。新设计：自动编辑模式下普通命令用户可选"本会话允许"（写入 session 缓存）或"允许"（不写入）；危险命令不写入缓存、每次询问；变更前确认模式无视缓存、每次拦截。
- **硬约束**：不破坏现有 allowlist 读路径、不破坏 confirm_all 模式已有的"无视缓存"行为、不破坏 YOLO 全量绕过、不破坏 `InterruptOnApprovalHandler` 的 `approve_tool_for_session` 机制，详见 §1
- 关联代码：
  - 后端管线：
    - `agent/middlewares/humanInTheLoop/approval.py:186-287`（`check_command` 七层管线）
    - `agent/middlewares/humanInTheLoop/approval.py:318-377`（`_apply_decision` + `_add_to_session` + `_extract_pattern`——**已存在，terminal 路径未调用**）
    - `agent/middlewares/humanInTheLoop/approval.py:289-316`（`check_command_with_approval`——完整闭环但用 `prompt_fn`，非 interrupt 路径）
    - `agent/middlewares/humanInTheLoop/strategies.py:107-227`（`TerminalApprovalHandler`——**断层所在**）
    - `agent/middlewares/humanInTheLoop/detection.py:203-220`（`detect_dangerous_command`——47+ 危险模式）
    - `agent/middlewares/humanInTheLoop/detection.py:166-183`（`detect_clawhub_command`——clawhub 远程 npm 检测）
    - `agent/middlewares/humanInTheLoop/types.py:53-65`（`ApprovalDecision` 枚举：ONCE / SESSION / ALWAYS / DENY）
  - 前端卡片：
    - `client/app/composables/use-hitl-approval.ts:101`（`handleHitlDecision` decision 类型联合）
    - `client/app/pages/home/index/[sid].vue:343-370`（HITL 卡片按钮区）
    - `client/app/pages/home/index/[sid].vue:1056-1110`（四语言 i18n 标签）
    - `client/app/composables/bridge/agent-socket.ts`（`sendHitlResponse`）

---

## 0. 背景：现有断层

### 0.1 管线里有基础设施，terminal 路径没接通

`ApprovalPipeline.check_command()` 的七层管线中，Layer 6 是 session 级命令缓存：

| 层 | 缓存 | 存储 key | 作用 |
|---|---|---|---|
| Layer 5 | 永久 allowlist | `HITL_PERMANENT_KEY`（state_register_db，跨 session） | `ApprovalDecision.ALWAYS` 写入 |
| Layer 6 | session allowlist | `HITL_SESSION_APPROVED_KEY`（state_register_mem，本 session） | `ApprovalDecision.SESSION` 写入 |

写回方法也齐全：

```python
# approval.py:345-352
elif decision == ApprovalDecision.SESSION:
    self._add_to_session(session_id, command)

# approval.py:371-377
def _add_to_session(self, session_id, command):
    pattern = _extract_pattern(command)   # "git push --force origin main" → "git push*"
    ...
    _set_state(session_id, HITL_SESSION_APPROVED_KEY, current)
```

`_extract_pattern`（`approval.py:118-126`）取命令前两个词 + 通配符：`"npm install --save-dev"` → `"npm install*"`。

### 0.2 断点：TerminalApprovalHandler 的 approve 分支

```python
# strategies.py:207 (当前代码)
elif decision_type == "approve":
    ctx.outcome.approve(tool_call)    # ← 只批准本次，不调 _apply_decision(SESSION)
```

对比 `InterruptOnApprovalHandler`（`strategies.py:462-466`）——它**会**写回：

```python
elif decision["type"] == "approve" and "approve" in allowed:
    ctx.outcome.approve(tool_call)
    mw.approval.approve_tool_for_session(...)  # ← 写回！
```

结果：同一危险命令（如 `rm -rf build/`）在同一 session 里每次执行都被重新审批。

### 0.3 HITL 卡片当前选项

ReviewConfig 只给 `["approve", "reject"]`：

```python
# strategies.py:191-194
review_config = ReviewConfig(
    action_name=tool_name,
    allowed_decisions=["approve", "reject"],
)
```

前端卡片（`[sid].vue:343-370`）渲染四个按钮：

| 按钮 | decision | 触发条件 | 作用 |
|---|---|---|---|
| 拒绝 | `reject` | 始终显示 | 拒绝本次调用 |
| 同意所有操作 | `yolo` | 始终显示 | 绕过本 session 所有后续审批（全量，非命令级） |
| 批准整个目录 | `approve_dir` | `allowed_decisions.includes('approve_dir')` | 外部路径目录级缓存 |
| 批准 | `approve` | 始终显示 | 批准本次调用 |

没有"本会话允许此命令"选项。`yolo` 是全量绕过——跳过本 session 所有审批（不只是同一条命令），粒度过粗。

---

## 1. 硬约束

| 约束 | 含义 | 验收方式 |
|---|---|---|
| **C1**：confirm_all 无视缓存 | `check_command` Layer 4 在 Layer 5/6 之前返回——confirm_all 模式下每个命令都弹卡片，session allowlist 不参与 | 现有行为，不改 Layer 4 |
| **C2**：YOLO 全量绕过不变 | `yolo` decision 仍设 `SESSION_YOLO_KEY`，Layer 3 绕过全部 | 现有行为，不改 Layer 3 |
| **C3**：危险命令不写入 session 缓存 | `detect_dangerous_command` 返回非空时：(a) 卡片不显示"本会话允许"按钮；(b) 即使 session allowlist 有匹配模式，Layer 6 也跳过 | 新增 Layer 6 前置检查 |
| **C4**：普通命令"本会话允许"才写入 | 只有用户显式选 `session` decision 才调 `_apply_decision(SESSION)`；选 `approve` 只放行本次 | handler 分支区分 |
| **C5**：永久 allowlist 不受影响 | Layer 5（`HITL_PERMANENT_KEY`）行为不变——危险命令如果在永久 allowlist 里仍自动放行（用户显式选了"ALWAYS"） | 不改 Layer 5 |
| **C6**：`InterruptOnApprovalHandler` 不受影响 | `interrupt_on` 工具的 `approve_tool_for_session`（按 args_hash 存）机制不变 | 不改该 handler |
| **C7**：`check_command_with_approval` 不受影响 | 用 `prompt_fn` 的旧闭环路径不变 | 不改该方法 |
| **C8**：sandbox-bypass 不受影响 | `_sandbox_bypass_interrupt` 有自己的 interrupt，不在本次改动范围 | 不改该方法 |

---

## 2. 任务一：后端——check_command 危险命令跳过 session 缓存

### 2.1 目标

`check_command` 的 Layer 6（session allowlist）在命令被 `detect_dangerous_command` 判定为危险时跳过——即使 session allowlist 里有匹配模式，危险命令也继续走到 Layer 7 弹卡片。

### 2.2 改动清单

- [ ] **2.2.1** `approval.py:259-269`——Layer 6 前置危险检测

**改动前**：

```python
# Layer 6: Session allowlist
session_list: list[str] = _get_state(session_id, HITL_SESSION_APPROVED_KEY, [])
for pattern_str in session_list:
    if fnmatch.fnmatch(command, pattern_str):
        result = ApprovalResult(
            approved=True,
            decision=ApprovalDecision.SESSION,
            reason="Session allowlist match",
        )
        self._fire_hooks(session_id, result)
        return result

# Layer 7: Dangerous pattern detection
dangerous = detect_dangerous_command(command)
if not dangerous:
    ...
```

**改动后**：

```python
# Layer 7 (moved up): Dangerous pattern detection — needed BEFORE Layer 6
# so a dangerous command skips the session allowlist even if its pattern
# was somehow stored (defence-in-depth: the write path in §3 also refuses
# to cache dangerous commands).
dangerous = detect_dangerous_command(command)

# Layer 6: Session allowlist (SKIPPED for dangerous commands — they always
# ask, regardless of what was previously approved for this session).
if not dangerous:
    session_list: list[str] = _get_state(session_id, HITL_SESSION_APPROVED_KEY, [])
    for pattern_str in session_list:
        if fnmatch.fnmatch(command, pattern_str):
            result = ApprovalResult(
                approved=True,
                decision=ApprovalDecision.SESSION,
                reason="Session allowlist match",
            )
            self._fire_hooks(session_id, result)
            return result

# Layer 7 (continued): non-dangerous commands are auto-approved here.
if not dangerous:
    result = ApprovalResult(
        approved=True,
        decision=ApprovalDecision.ONCE,
        reason="No dangerous patterns detected",
    )
    self._fire_hooks(session_id, result)
    return result
```

**要点**：
- `detect_dangerous_command` 调用从 Layer 7 提到 Layer 6 之前——Layer 4（confirm_all）和 Layer 5（permanent）不依赖它，不受影响
- 危险命令跳过 Layer 6 → 继续到 Layer 7 的危险分支 → 返回 `approved=False, decision=None` → 弹卡片
- 非危险命令走 Layer 6 → 如果有 session allowlist 匹配则放行；否则走 Layer 7 → 自动放行
- Layer 5（permanent allowlist）仍对危险命令生效（C5）——永久 allowlist 是用户的显式跨 session 决定，不受 session 缓存策略影响

**验收**：C1 + C3 + C5。新建 `tests/agent/middlewares/humanInTheLoop/test_check_command_cache.py`。

---

## 3. 任务二：后端——TerminalApprovalHandler 写回 session 缓存

### 3.1 目标

`TerminalApprovalHandler` 在用户选 `session` decision 时调 `_apply_decision(SESSION)` 写入 session allowlist；危险命令只给 `approve`（不缓存），不给 `session` 选项。

### 3.2 改动清单

- [ ] **3.2.1** `strategies.py:170-227`——ReviewConfig 和 approve 分支

**改动前**（关键部分）：

```python
# strategies.py:173
if (clawhub_tag or not result.approved) and not is_yolo_mode(mw.config, ctx.session_id):
    if mw._turn_operator(ctx.state) is None:
        ctx.outcome.deny(tool_call, tool_name, _auto_deny_content())
        return True
    description = ...
    action_request = ActionRequest(...)
    review_config = ReviewConfig(
        action_name=tool_name,
        allowed_decisions=["approve", "reject"],
    )
    hitl_response = interrupt(HITLRequest(...))
    decisions = hitl_response.get("decisions", [])
    decision_type = decisions[0]["type"] if decisions else ""
    if decision_type == "yolo":
        set_session_yolo(ctx.session_id)
        ctx.outcome.approve(tool_call)
    elif decision_type == "approve":
        ctx.outcome.approve(tool_call)
    else:
        ctx.outcome.deny(...)
    return True
```

**改动后**：

```python
# Determine whether this escalation is for a dangerous command (never
# cached) or an ordinary command that reached the card (clawhub, etc.)
# — only the latter may offer a "session" decision.
from .detection import detect_dangerous_command
is_dangerous = bool(detect_dangerous_command(command))
# confirm_all mode also never caches: Layer 4 already returns before 5/6,
# so a "session" decision there would be a misleading no-op write.
is_confirm_all = is_confirm_all_mode(mw.config, ctx.session_id)
can_cache = not is_dangerous and not is_confirm_all and not clawhub_tag

# Build the allowed-decisions list the card offers:
#   can_cache  → ["approve", "session", "reject"]
#   otherwise  → ["approve", "reject"]
allowed = ["approve", "session", "reject"] if can_cache else ["approve", "reject"]

description = ...
action_request = ActionRequest(...)
review_config = ReviewConfig(
    action_name=tool_name,
    allowed_decisions=allowed,
)
hitl_response = interrupt(HITLRequest(
    action_requests=[action_request],
    review_configs=[review_config],
))
decisions = hitl_response.get("decisions", [])
decision_type = decisions[0]["type"] if decisions else ""
if decision_type == "yolo":
    set_session_yolo(ctx.session_id)
    ctx.outcome.approve(tool_call)
elif decision_type == "session":
    # Write the command pattern to the session allowlist so subsequent
    # calls with the same two-word prefix (e.g. "git push*") are
    # auto-approved by Layer 6.  Only reached when can_cache is True.
    mw.approval._apply_decision(
        result.pattern_key or "escalated",
        ApprovalDecision.SESSION,
        ctx.session_id,
        command,
    )
    ctx.outcome.approve(tool_call)
elif decision_type == "approve":
    ctx.outcome.approve(tool_call)
else:
    ctx.outcome.deny(...)
return True
```

**要点**：
- `can_cache` 三个排除条件：
  - `is_dangerous`——`detect_dangerous_command` 非空（危险命令每次问）
  - `is_confirm_all`——变更前确认模式（Layer 4 已在 Layer 6 前返回，写入是无意义的）
  - `clawhub_tag`——clawhub 远程 npm 执行每次都需要显式确认（不缓存，与当前行为一致）
- `_apply_decision` 是 `ApprovalPipeline` 的现有方法（`approval.py:318`），内部调 `_add_to_session` → `_extract_pattern(command)` → 存入 `HITL_SESSION_APPROVED_KEY`
- `_apply_decision` 还会 `_fire_hooks`——审批事件追踪正常触发
- `result.pattern_key` 来自 `check_command` 的返回值（危险模式标签或 `"confirm_all"`），传给 `_apply_decision` 做事件追踪（实际存储用的是 `_extract_pattern(command)`，不用 `pattern_key`）

**验收**：C3 + C4 + C6。在现有 `tests/agent/middlewares/test_hitl_strategies.py` 中新增用例。

---

## 4. 任务三：前端——HITL 卡片新增"本会话允许"按钮

### 4.1 目标

HITL 审批卡片在 `allowed_decisions` 包含 `"session"` 时显示"本会话允许"按钮，发送 `decision: "session"` 给后端。

### 4.2 改动清单

- [ ] **4.2.1** `use-hitl-approval.ts:101`——decision 类型联合加 `"session"`

**改动前**：

```typescript
const handleHitlDecision = (decision: 'approve' | 'approve_dir' | 'reject' | 'yolo', message: string = '') => {
```

**改动后**：

```typescript
const handleHitlDecision = (decision: 'approve' | 'approve_dir' | 'reject' | 'session' | 'yolo', message: string = '') => {
```

- [ ] **4.2.2** `[sid].vue:357-369`——按钮区加"本会话允许"按钮

在 `approve_dir` 按钮和 `approve` 按钮之间插入：

```vue
<Button
    v-if="(hitlRequest?.allowed_decisions ?? []).includes('session')"
    :label="t('hitl.approveSession')"
    icon="pi pi-bookmark"
    severity="secondary"
    size="small"
    :title="t('hitl.approveSessionTooltip')"
    @click="handleHitlDecision('session')" />
```

按钮显示条件与 `approve_dir` 相同的模式：`allowed_decisions.includes('session')`——后端不发 `"session"` 就不显示。

- [ ] **4.2.3** `[sid].vue:1056-1110`——四语言 i18n 标签

| 语言 | key | 值 |
|---|---|---|
| zh | `approveSession` | `本会话允许` |
| zh | `approveSessionTooltip` | `允许本会话内执行相同命令，不再弹出审批（前两个词匹配，如 "git push" 覆盖所有 git push 子命令）` |
| en | `approveSession` | `Allow for Session` |
| en | `approveSessionTooltip` | `Allow the same command for this session — no more prompts (matches the first two words, e.g. "git push" covers all git push subcommands)` |
| ja | `approveSession` | `セッション内で許可` |
| ja | `approveSessionTooltip` | `このセッション内で同じコマンドを許可し、確認を省略（先頭2語でマッチ、"git push" はすべての git push サブコマンドをカバー）` |
| ko | `approveSession` | `세션 내 허용` |
| ko | `approveSessionTooltip` | `이 세션 내에서 동일한 명령을 허용하고 확인을 생략 (처음 두 단어로 매칭, "git push"는 모든 git push 하위 명령을 포함)` |

**验收**：C8。在 `client/app/composables/__tests__/hitl-bridge.test.ts` 中新增用例。

---

## 5. 完整审批流程（改动后）

### 5.1 自动编辑模式（auto_edit，默认）

```
terminal 命令
  ↓ check_command()
Layer 1: hardline blocklist → 拒绝（不可绕过）
Layer 2: deny rules → 拒绝
Layer 3: YOLO → 放行（全量绕过）
Layer 4: confirm_all → N/A（auto_edit 模式）
Layer 5: permanent allowlist → 放行（含危险命令——用户显式 ALWAYS）
Layer 6: session allowlist（危险命令跳过）
  ├─ 普通命令 + pattern 匹配 → 放行（SESSION）
  └─ 危险命令 → 跳过，继续
Layer 7: dangerous detection
  ├─ 非危险 → 放行（ONCE，不弹卡片）
  └─ 危险 → approved=False → 弹卡片
       ↓ 卡片按钮
       ├─ 拒绝 (reject) → 拒绝
       ├─ 同意所有操作 (yolo) → 全量绕过
       ├─ 本会话允许 (session) → 【不显示——危险命令】
       └─ 批准 (approve) → 放行本次，不缓存 → 下次再问

clawhub / 非危险但到达卡片
  ↓ 卡片按钮
  ├─ 拒绝 (reject) → 拒绝
  ├─ 同意所有操作 (yolo) → 全量绕过
  ├─ 本会话允许 (session) → 写入 session allowlist → 下次同类命令自动放行
  └─ 批准 (approve) → 放行本次，不缓存 → 下次再问
```

### 5.2 变更前确认模式（confirm_all）

```
terminal 命令
  ↓ check_command()
Layer 4: confirm_all → approved=False（每个命令都弹卡片，Layer 5/6 不参与）
  ↓ 卡片按钮（无 "session" 选项——can_cache=False）
  ├─ 拒绝 (reject) → 拒绝
  ├─ 同意所有操作 (yolo) → 全量绕过
  └─ 批准 (approve) → 放行本次 → 下次再问（即使之前批准过相同命令）
```

---

## 6. `_extract_pattern` 的匹配粒度

`_extract_pattern`（`approval.py:118-126`）取前两个词 + `*`：

| 命令 | 提取的模式 | 匹配范围 |
|---|---|---|
| `npm install --save-dev lodash` | `npm install*` | `npm install`、`npm install -g`、`npm install --save-dev xxx` |
| `git push origin main` | `git push*` | `git push`、`git push --force`、`git push origin main` |
| `docker build -t app .` | `docker build*` | `docker build`、`docker build --no-cache` |
| `pytest` | `pytest*` | `pytest`、`pytest -v`、`pytest tests/` |
| `ls` | `ls*` | `ls`、`ls -la`、`ls /tmp` |

**注意**：`git push*` 会覆盖 `git push --force`（危险命令），但 `git push --force` 会被 `detect_dangerous_command` 判定为 `git_force_push` → Layer 6 跳过 → Layer 7 弹卡片。所以即使 session allowlist 里有 `git push*`，`git push --force` 仍会每次询问。这是 §2 危险命令跳过 Layer 6 的防御效果。

---

## 7. 不做的事（边界）

| 项 | 不做的原因 |
|---|---|
| 改 `_extract_pattern` 的粒度 | 前两词通配是合理默认——太窄（全命令匹配）缓存没意义，太宽（一词匹配）误放行风险大 |
| 给 sandbox-bypass 加 session 缓存 | sandbox-bypass 有自己的 interrupt（`_sandbox_bypass_interrupt`），语义不同（是"允许不沙盒"不是"允许此命令"），另起 TODO |
| 给 clawhub 加 session 缓存 | clawhub 是远程代码执行，每次都应显式确认——与当前行为一致 |
| 改永久 allowlist（Layer 5）行为 | 永久 allowlist 是用户的显式跨 session 决定（ALWAYS），不受 session 缓存策略影响 |
| 给 `InterruptOnApprovalHandler` 加 session 选项 | 该 handler 已有 `approve_tool_for_session`（按 args_hash 存），机制不同，不混用 |
| 在 confirm_all 模式下写 session allowlist | Layer 4 在 Layer 6 前返回，写入是无意义的——confirm_all 的定义就是"每次都问" |

---

## 8. 验收标准

### 8.1 硬约束验收

- [ ] **C1**：confirm_all 模式下同一命令连续执行两次都弹卡片
- [ ] **C2**：YOLO 后同 session 不再弹任何卡片
- [ ] **C3**：危险命令（如 `rm -rf build/`）在 session allowlist 有 `rm -rf*` 时仍弹卡片
- [ ] **C4**：普通命令用户选 `session` 后，同类命令（前两词匹配）在本 session 不再弹卡片；选 `approve` 后同类命令仍弹卡片
- [ ] **C5**：永久 allowlist 中的危险命令自动放行（不弹卡片）
- [ ] **C6**：`InterruptOnApprovalHandler` 的 `approve_tool_for_session` 行为不变
- [ ] **C7**：`check_command_with_approval` 行为不变
- [ ] **C8**：sandbox-bypass interrupt 行为不变

### 8.2 功能验收

- [ ] 自动编辑模式 + 普通命令（如 `npm install`）选"本会话允许"后，`npm install --save-dev` 不弹卡片
- [ ] 自动编辑模式 + 危险命令（如 `git push --force`）卡片不显示"本会话允许"按钮
- [ ] 自动编辑模式 + 危险命令选"批准"后，再次执行同一危险命令仍弹卡片
- [ ] 变更前确认模式卡片不显示"本会话允许"按钮
- [ ] 前端四语言标签正确显示

### 8.3 回归验收

- [ ] `tests/agent/middlewares/test_hitl_strategies.py` 全套通过
- [ ] `tests/agent/middlewares/test_humanInTheLoop.py`（如存在）通过
- [ ] `client/app/composables/__tests__/hitl-bridge.test.ts` 通过

---

## 9. 执行顺序与依赖

```
§2 check_command 危险命令跳过 Layer 6（后端，1-2h）
  ├─ 改 approval.py Layer 6/7 顺序
  ├─ 新增 test_check_command_cache.py
  │
  ↓ （§3 的 can_cache 依赖 detect_dangerous_command 判定，与 §2 一致）
§3 TerminalApprovalHandler 写回 session 缓存（后端，1-2h）
  ├─ 改 strategies.py ReviewConfig + approve 分支
  ├─ 扩展 test_hitl_strategies.py
  │
  ↓ （§4 的按钮显示依赖后端发的 allowed_decisions）
§4 前端卡片新增"本会话允许"按钮（前端，1-2h）
  ├─ 改 use-hitl-approval.ts decision 类型
  ├─ 改 [sid].vue 按钮区 + i18n
  └─ 扩展 hitl-bridge.test.ts

总工程量：3-6h
```

**关键路径**：§2 → §3 → §4（后端先改管线和 handler，前端再适配新 decision 类型）。

---

## 10. 关键参考

### sherry 内部

- `agent/middlewares/humanInTheLoop/approval.py:118-126`（`_extract_pattern`——前两词 + `*`）
- `agent/middlewares/humanInTheLoop/approval.py:246-269`（Layer 5 permanent + Layer 6 session allowlist 读路径）
- `agent/middlewares/humanInTheLoop/approval.py:318-377`（`_apply_decision` → `_add_to_session` 写路径——已存在，待接通）
- `agent/middlewares/humanInTheLoop/approval.py:289-316`（`check_command_with_approval`——完整闭环，用 `prompt_fn` 不用 interrupt）
- `agent/middlewares/humanInTheLoop/strategies.py:107-227`（`TerminalApprovalHandler`——断层所在）
- `agent/middlewares/humanInTheLoop/strategies.py:405-493`（`InterruptOnApprovalHandler`——参照：它调了 `approve_tool_for_session`）
- `agent/middlewares/humanInTheLoop/detection.py:203-220`（`detect_dangerous_command`——47+ 模式）
- `agent/middlewares/humanInTheLoop/types.py:53-65`（`ApprovalDecision` 枚举）
- `client/app/pages/home/index/[sid].vue:357-364`（`approve_dir` 按钮的条件渲染——参照模式）
- `client/app/composables/use-hitl-approval.ts:101`（`handleHitlDecision` decision 联合类型）

### 关联 TODO

- 跟 `计划_reminder高优先级置于消息尾部.md` 正交
- 跟 `计划_中断系统多项目借鉴.md` 相关——后者调研了六项目的 HITL 机制，本文档是其落地之一
