# 方案:证据台账 finish 门改 flow_id-only(去 hybrid)

## 定位

**细化** `方案_证据台账_加flow_id列.md` —— 把该方案的 M6(`taskflow_finish` hybrid:`by_flow` 主 + `by_session` 兜底)收窄成 **flow_id-only 单查**(`WHERE flow_id = ?`),前提是加 **M2b**(creator session 在 finish 时设/清 flow_id),让 creator 跑的 regression replay 证据也带 flow_id。

**不是独立方案**,是 flow_id 方案的收尾细化。flow_id 方案的 hybrid 是过渡态(老行 NULL + creator 无 flow_id),本方案把两个 NULL 源头都堵掉,让 hybrid 的 `by_session` 兜底可删。

## 前置方案

| 前置 MD | 为什么是前置 |
|---------|--------------|
| **`方案_证据台账_加flow_id列.md`** | 本方案细化它的 M6 + 扩展它的 M2。前置方案必须先落地 M1-M5(`StateKey.FLOW_ID` + 子 session spawn 注入 + `evidence_events.flow_id` 列 + recorder 盖章 + `read_for_flow`)。本方案在其之上加 M2b(creator 盖章)+ 改 M6(删 hybrid) |

**无其它前置**。todo_id 方案 / 校验波方案 / 局部改 API 方案都无关(那些是 todolist 路径,本方案是 TaskFlow evidence 路径)。

## 为什么要做(creator replay 证据丢失)

flow_id 方案的 M6 hybrid:`by_flow`(有 flow_id 的行)+ `by_session`(creator + 老 child session 兜底)。`by_session` 兜底主要兜两类 creator-side 证据:

1. **creator 直接跑的命令**:creator(主会话)跑 terminal/python_repl —— flow_id NULL(M2 只在 spawn 子 session 时设,creator 没设)
2. **regression gate replay 的证据**:`taskflow_flow(action="finish")` 时重放已通过的验证命令(AGENTS.md:260)—— replay 在 **creator 上下文**跑(finish action 在 creator 执行)→ 证据在 creator session,flow_id NULL

第 2 类是关键:**regression replay 实际重跑验证命令**,这些证据属于这个 flow,但 flow_id NULL。若直接砍 hybrid 用 flow_id-only(不加 M2b)→ replay 证据漏 → finish 门可能误判"无 FAIL 证据"(实际 replay 跑出了 FAIL 但没被 flow_id 查到)。

M2b 堵这个洞:finish 期间给 creator 设 flow_id → replay + creator 直接命令都盖章 → flow_id-only 完整。

## 设计

### M2b. creator session 在 finish 时设/清 flow_id

**文件**:`agent/tools/taskflow/tools/taskflow_finish.py`(finish action 入口)+ `runtime/session/state_register.py`(set/clear)

扩展 flow_id 方案的 M2(原 M2 是 child-scoped,只在 spawn 子 session 时设 `StateKey.FLOW_ID`)。M2b 加 **creator-scoped**:在 `taskflow_flow(action="finish")` 执行期间,给 creator session 设 `StateKey.FLOW_ID = flow_id`,finish 完成后清除:

```python
# taskflow_finish.py 入口
from runtime.session.state_register import set_state

async def taskflow_flow_finish(session_id, flow_id, ...):
    creator_session_id = session_id  # finish 在 creator 上下文跑
    prev_flow_id = get_state(creator_session_id, StateKey.FLOW_ID, "")
    set_state(creator_session_id, StateKey.FLOW_ID, flow_id)  # finish 期间 creator 带这个 flow
    try:
        # ... regression replay + 最终验证命令 ...
        # 这些命令走 terminal/python_repl → recorder 读 StateKey.FLOW_ID 盖 flow_id
        # ... finish 门判(read_for_flow 单查,M6-refined)...
    finally:
        set_state(creator_session_id, StateKey.FLOW_ID, prev_flow_id)  # 恢复(creator 可能编排多 flow)
```

**为什么 try/finally 恢复而非清空**:creator 可能编排多个 flow(并行多个 finish?),用 `prev_flow_id` 恢复进入前的值更安全(嵌套 finish 场景不会误清外层的 flow_id)。实际上 finish 不会嵌套(一个 flow finish 是终态),但 try/finally + 恢复是防御性写法。

**set/clear 时机**:finish action 入口设,finish 出口(无论成功/失败/异常)清。regression replay 在 finish 内部跑,期间 creator 带 flow_id → replay 证据盖章。

### M6-refined. 删 hybrid `by_session`,改 `read_for_flow` 单查

**文件**:`agent/tools/taskflow/evidence_collector.py`(聚合入口)+ `taskflow_finish.py`

flow_id 方案 M6 原是 hybrid:
```python
by_flow = read_for_flow(flow_id)
flow_session_ids = {r.session_id for r in by_flow}
legacy_keys = [k for k in session_keys if k not in flow_session_ids]
by_session = read_for_keys(session_keys=legacy_keys) if legacy_keys else []
return by_flow + by_session
```

M6-refined 改成 flow_id-only:
```python
async def aggregate_flow_evidence(flow_id, session_keys) -> list[EvidenceRecord]:
    """flow_id-only 单查。creator 的 finish 期证据(M2b 盖章)+ 各 child session 证据(M2 盖章)都带 flow_id。"""
    return read_for_flow(flow_id)
```

`session_keys` 参数保留(向后兼容签名,但不用于查询)—— 过渡期老行未清完时,可选 fallback(见迁移节)。最终删参数。

### 迁移:老行 + retention

**前提**:flow_id 方案已落地(M1-M5),新行都带 flow_id(child via M2,creator-finish via M2b)。老行(flow_id 方案落地前的证据)flow_id = NULL。

**过渡期策略**(M2b 落地后,retention 清完老行前):

```python
async def aggregate_flow_evidence(flow_id, session_keys):
    by_flow = read_for_flow(flow_id)
    # 过渡兜底:若该 flow 有老行(NULL flow_id),session_keys 兜底
    # 判据:by_flow 是否覆盖了所有 step child session(老行的 child session 证据可能 NULL)
    covered_sessions = {r.session_id for r in by_flow}
    legacy_keys = [k for k in session_keys if k not in covered_sessions]
    if legacy_keys:
        by_session = read_for_keys(session_keys=legacy_keys)
        return by_flow + by_session
    return by_flow  # 全覆盖,flow_id-only
```

**何时彻底删 hybrid 分支**:
- retention purge 清完所有老行(flow_id 方案落地前的证据,通常 retention 周期几天到几周)
- 确认 `legacy_keys` 恒空(连续 N 天/ N 次 finish 都走 flow_id-only 分支)
- 单独 PR 删 hybrid 分支 + `session_keys` 参数

**加速迁移**:可在 flow_id 方案落地时,对存量活跃 flow 的 child session 补盖章(读 step child_session_key → set_state → 重新跑一次证据收集?不现实)。更实际:接受过渡期 hybrid 兜底老行,等 retention 清。

## 关键设计决策

| # | 决策 | 理由 |
|---|------|------|
| D1 | M2b 用 try/finally 恢复 `prev_flow_id` 而非清空 | 防御嵌套/异常;creator 可能编排多 flow;恢复进入前值最安全 |
| D2 | M6-refined 保留 `session_keys` 参数(过渡期兜底) | 老行未清完前仍需兜底;签名向后兼容;retention 清完后单独 PR 删 |
| D3 | 过渡兜底的判据:`by_flow` 是否覆盖所有 step child session | 不盲目 fallback,只在确实有未覆盖 session 时才走 by_session;减少误计 |
| D4 | 不对存量老行补盖章 | 不现实(要重跑证据收集);接受 retention 自然清;过渡期 hybrid 兜底 |
| D5 | M2b 只在 finish 期设 creator flow_id,不在其它时间 | creator 在 finish 之外跑的命令(编排/探索)不是 flow 验证工作,漏了不影响门判;finish 是 replay + 最终验证的集中点,盖章精准 |

## 风险与开放问题

| # | 风险 | 严重度 | 缓解 |
|---|------|--------|------|
| R1 | M2b 设/清 flow_id 的竞态:finish 期间 creator 跑了非本 flow 的命令(如手动 grep)→ 被误盖 flow_id | 中 | finish 期短(门判 + replay);creator 在 finish 期跑的命令大概率是本 flow 的验证;漏盖比误盖危险(漏→门误判,误→证据多算但不影响"无 FAIL"判定方向);可接受 |
| R2 | regression replay 失败时 flow_id 已清(try/finally)→ replay 证据漏 | 低 | replay 在 try 块内跑,finally 在 replay 完成后才清;replay 异常也会在清之前完成记录(recorder 在命令返回时即记) |
| R3 | 过渡期 hybrid 兜底老行:`legacy_keys` 判据错(漏判未覆盖 session)→ 漏证据 | 中 | 判据用"by_flow 覆盖的 session 集合 vs 期望 session_keys",保守(宁可多 fallback 也不漏);过渡期短(retention 清完即删 hybrid) |
| R4 | creator 编排多 flow 并行 finish 时,M2b 互相覆盖 flow_id | 低 | finish 不并行(一个 flow finish 是终态,creator 串行处理);try/finally + prev 恢复防嵌套;若将来支持并行 finish 需改 per-flow context |
| R5 | `read_for_flow` 单查遗漏:某 child session 的 flow_id 没设(M2 漏盖)→ 该 child 证据漏 | 中 | M2 在 spawn 时设(结构性,不依赖运行时);漏盖是 bug 不是常态;过渡期 hybrid 兜底会查到该 child 的 session 证据 |
| R6 | 老 session 的 evidence 老行 retention 未清完,hybrid 长期保留 | 低 | retention 周期固定;迁移完成后(legacy_keys 恒空)单独 PR 删;不阻塞功能 |

## 落地顺序

**前置**:flow_id 方案 M1-M5 已落地(`StateKey.FLOW_ID` + child spawn 注入 + 列 + recorder 盖章 + `read_for_flow`)。

1. **阶段 A(M2b)**:creator session 在 finish 时设/清 flow_id。新证据行:child(via M2)+ creator-finish(via M2b)都带 flow_id。
2. **阶段 B(M6-refined 过渡)**:`aggregate_flow_evidence` 改"flow_id-only 主 + 老行 session 兜底"(判据:by_flow 覆盖 vs 期望 session_keys)。
3. **阶段 C(观察)**:连续观察 `legacy_keys` 是否恒空(retention 清完老行的标志)。
4. **阶段 D(删 hybrid)**:`legacy_keys` 恒空确认后,删 hybrid 分支 + `session_keys` 参数,`aggregate_flow_evidence(flow_id)` 单查。

阶段 A-B 是核心(让新 flow 走 flow_id-only);阶段 C-D 是收尾(等老行清完删死代码)。

## 测试清单

### 阶段 A(M2b)
- [ ] `taskflow_flow(action="finish")` 入口:creator session 的 `StateKey.FLOW_ID` 被设为目标 flow_id
- [ ] finish 期间跑的 terminal/python_repl 命令 → 证据行 flow_id 有值(creator 盖章)
- [ ] finish 完成(成功/失败/异常)→ creator 的 `StateKey.FLOW_ID` 恢复进入前值(try/finally)
- [ ] regression replay 跑的命令 → 证据行 flow_id 有值(replay 在 finish 期,creator 带 flow_id)
- [ ] creator 在 finish 之外跑的命令 → 证据行 flow_id NULL(不误盖)

### 阶段 B(M6-refined 过渡)
- [ ] 新 flow(全证据带 flow_id):`aggregate_flow_evidence` 走 flow_id-only(`legacy_keys` 空)
- [ ] 老 flow(有 NULL flow_id 老行):`legacy_keys` 非空 → fallback `read_for_keys` 兜底
- [ ] by_flow + by_session 去重(同一行不双计)
- [ ] flow_id-only 查到 creator-finish 证据(regression replay)—— M2b 生效验证

### 阶段 C(观察)
- [ ] 连续 N 次 finish 的 `legacy_keys` 恒空(retention 清完老行)
- [ ] 无 fallback 触发

### 阶段 D(删 hybrid)
- [ ] 删 hybrid 分支后 `aggregate_flow_evidence(flow_id)` 单查,行为不变
- [ ] `session_keys` 参数删除,签名简化
- [ ] finish 门判结果与删前一致(回归测试)

### 端到端
- [ ] flow 的所有 step 派子 agent(child 带 flow_id)+ finish 期 creator 跑 replay(creator 带 flow_id)→ `read_for_flow(flow_id)` 覆盖全部证据 → 门判准
- [ ] 一个 session 编排两个 flow(A 先 finish,B 后 finish)→ A 的 evidence 不被 B 的 finish 查到(flow_id 隔离)—— 精度验证(对比 hybrid 的过度包含)

## 与其他方案的关系

| 方案 | 关系 |
|------|------|
| **`方案_证据台账_加flow_id列.md`** | **前置 + 细化** —— 本方案在其 M2 上加 M2b(creator 盖章),在其 M6 上删 hybrid。必须先落地该方案的 M1-M5 |
| `方案_Todo加todo_id列.md` / 局部改 API / 引用完整性 | 无关 —— todolist 路径,不碰 evidence 表 |
| `方案_最终校验波_FinalVerificationWave.md` | 无关 —— 校验波用 descendant 枚举 + read_for_keys(todolist 路径),不依赖 flow_id |

本方案是 TaskFlow evidence 路径的**收尾细化**,让 finish 门从"枚举 session 链 hybrid"收敛成"flow_id 单查",前提是 creator-side 也盖章(M2b)。
