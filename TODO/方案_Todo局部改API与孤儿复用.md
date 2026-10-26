# 方案:Todo 局部改 API + 孤儿 Flow 复用(预防层)

## 定位

**预防层** —— 给模型局部改 todo 的能力(改名/加减步骤),根本不触发全量替换,从源头避免误删 flow_id todo。

与 `方案_Todo与TaskFlow引用完整性.md`(补救层:P1 补偿/P2 去重/P3 即时 sweep)叠加:预防层堵源头,补救层兜底漏网(局部 API 误用 / bug / 老代码仍走全量替换)。两层独立可部署。

## 现状(已核对代码)

| 能力 | 现状 | 证据 |
|------|------|------|
| 模型 mutation 路径 | **只有全量替换**(`todowrite` → `update_todos` → `replace_all` DELETE+INSERT) | `todowrite.py:74`、`service.py:207`、`store_sqlite.py:196` |
| `add_todo`(单条插) | 存在但**只 TaskFlow 内部用**(`bind_flow_todo` 调),不暴露模型 | `service.py:238`、`_shared.py:60` |
| `patch_todo`(局部改单条) | **不存在** | store 层无此方法 |
| `remove_todo`(删单条) | **不存在** | store 层无此方法 |
| `get_todos_by_flow` | 存在(按 flow_id 查 todo) | `store_sqlite.py:297` |
| 孤儿 flow 发现 | **无 API**(sweep 内部用,不暴露) | sweep 在 `taskflow/registry/store_sqlite.py:127-613` |
| 重绑 todo 到已有 flow | **不存在**(`bind_flow_todo` 只 creation-only) | `_shared.py:44` |

## 设计:四类局部操作 + 孤儿复用

### M1. `todowrite` 加 `action` 参数(向后兼容)

**文件**:`agent/tools/todolist/tools/todowrite.py`

把 `todowrite` 从"只全量替换"改成 action-based(同 TaskFlow 的 `taskflow_flow(action=...)` 模式),**默认 action 保持现有全量替换行为**(向后兼容,老 prompt 不破):

```python
class TodoWriteInput(BaseModel):
    action: Literal["replace", "patch", "add", "remove", "reattach", "list_orphans"] = "replace"
    # replace(默认,向后兼容):todos=[全列表]
    # patch:position + 可选 content/status/priority/category(局部改单条,保 flow_id)
    # add:content + 可选 category/plan_ref(尾部追加单条)
    # remove:position(删单条,若带 flow_id 触发补偿)
    # reattach:position + flow_id(给孤儿 flow 重绑 GC root)
    # list_orphans:返回本 session 的孤儿 flow 列表
    todos: list[dict] | None = None       # action="replace" 用
    position: int | None = None           # patch/remove/reattach 用
    content: str | None = None            # patch/add 用
    status: str | None = None             # patch 用
    priority: str | None = None           # patch 用
    category: str | None = None           # patch/add 用
    plan_ref: str | None = None           # add 用
    flow_id: str | None = None            # reattach 用
```

### M2. `patch_todo` —— 局部改单条(保 flow_id)

**文件**:`agent/tools/todolist/service.py` + `registry/store_sqlite.py`

只改调用方传的字段,**flow_id/step_id/plan_ref 不在可改字段里**(保引用):

```python
# store_sqlite.py
async def patch_todo(session_id: str, position: int, *, content=None, status=None, priority=None, category=None) -> dict | None:
    """局部改单条 todo。只 UPDATE 调用方传的字段;flow_id/step_id/plan_ref 不动。
    返回更新后的 todo,或 None(position 不存在)。"""
    sets = []
    params = []
    if content is not None: sets.append("content = ?"); params.append(content)
    if status is not None: sets.append("status = ?"); params.append(status)
    if priority is not None: sets.append("priority = ?"); params.append(priority)
    if category is not None: sets.append("category = ?"); params.append(category)
    if not sets: return await get_todo(session_id, position)
    params.extend([session_id, position])
    async with connect() as db:
        await db.execute(f"UPDATE todos SET {', '.join(sets)} WHERE session_id = ? AND position = ?", params)
        await db.commit()
    return await get_todo(session_id, position)
```

```python
# service.py
@staticmethod
async def patch_todo(session_id, position, *, content=None, status=None, priority=None, category=None):
    if status == "completed":
        todo = await store.get_todo(session_id, position)
        if todo:
            await _assert_transition_allowed(todo, session_id)  # 复用现有完成 guard
    updated = await store.patch_todo(session_id, position, content=content, status=status, priority=priority, category=category)
    if updated:
        latest = await store.get_todos(session_id)
        await _push_todo_update(session_id, latest)
    return updated
```

**用例**:模型想改"做 X"为"做 X(含单测)"→ `todowrite(action="patch", position=3, content="做 X(含单测)")` → 只改 content,flow_id 保留 → **flow 不孤儿**。

### M3. `add_todo` 暴露给模型 —— 局部加(不 clobber)

**文件**:`todowrite.py`(action="add")+ `service.py`(复用现有 `add_todo`)

现有 `add_todo`(single INSERT,`service.py:238`)已经是安全的(不 clobber)。只是没暴露给模型。M3 只在 todowrite 加 action="add" 入口,转调现有 `TodoService.add_todo`,**不动 store**:

```python
# action="add" 分支
result = await service.TodoService.add_todo(
    session_id, content=content, category=category or DEFAULT_CATEGORY, plan_ref=plan_ref
)
# 注意:不传 flow_id —— 模型加的是普通 todo;flow_id 只由 bind_flow_todo 内部设
```

**用例**:模型想加"做 Z"→ `todowrite(action="add", content="做 Z")` → 尾部追加单条,其它 todo(含 flow_id 的)不动。

### M4. `remove_todo` —— 局部删(带 flow 补偿)

**文件**:`store_sqlite.py`(新 `remove_todo`)+ `service.py`(集成补偿)

删单条,如果该 todo 带 flow_id,**触发即时补偿**(复用 `方案_Todo与TaskFlow引用完整性.md` 的 P1 `compensate_orphaned_flows`):

```python
# store_sqlite.py
async def remove_todo(session_id: str, position: int) -> dict | None:
    """删单条 todo,返回被删的 todo(供 service 判断是否要补偿 flow)。"""
    async with connect() as db:
        row = await db.execute("SELECT * FROM todos WHERE session_id = ? AND position = ?", (session_id, position)).fetchone()
        if not row: return None
        await db.execute("DELETE FROM todos WHERE session_id = ? AND position = ?", (session_id, position))
        await db.commit()
    return _row_to_todo(row)
```

```python
# service.py
@staticmethod
async def remove_todo(session_id, position):
    removed = await store.remove_todo(session_id, position)
    if removed and removed.get("flow_id"):
        # 删的是 GC root → 即时补偿(同 P1,不等 sweep)
        from agent.tools.taskflow.tools._shared import compensate_orphaned_flows
        await compensate_orphaned_flows(
            session_id, [removed["flow_id"]], reason="todo_removed_explicitly"
        )
    latest = await store.get_todos(session_id)
    await _push_todo_update(session_id, latest)
    return removed
```

**用例**:模型想删"做 Y"→ `todowrite(action="remove", position=5)` → 删单条,若 Y 绑了 flow 则即时 cancel 那个 flow(显式意图,不是误删)。

### M5. `list_orphans` + `reattach` —— 孤儿 flow 复用

**文件**:`service.py`(新 `list_orphan_flows`)+ `_shared.py`(新 `bind_todo_to_existing_flow`)+ `todowrite.py`(action="list_orphans"/"reattach")

#### `list_orphan_flows` —— 发现孤儿

```python
# service.py
@staticmethod
async def list_orphan_flows(session_id: str) -> list[dict]:
    """本 session 活跃(flow 非 terminal)但无 GC root todo 引用的 flow。
    让模型能发现被误删 todo 的孤儿 flow,选择复用还是让其被 sweep 回收。"""
    from agent.tools.taskflow.registry import store_sqlite as tf_store
    active_flow_ids = await store.get_active_flow_ids(session_id)  # 有 todo 引用的
    all_active_flows = await tf_store.get_active_flows_for_session(session_id)  # 全部非 terminal
    orphans = [f for f in all_active_flows if f["flow_id"] not in active_flow_ids]
    return [{"flow_id": f["flow_id"], "status": f["status"], "goal": f.get("goal", ""),
             "description": f.get("description", ""), "child_session_key": f.get("child_session_key", "")}
            for f in orphans]
```

#### `bind_todo_to_existing_flow` —— 重绑 GC root

```python
# _shared.py
async def bind_todo_to_existing_flow(session_id: str, position: int, flow_id: str) -> dict | None:
    """把一个已存在的 todo(position)重绑为孤儿 flow 的 GC root。
    镜像 bind_flow_todo,但 todo 已存在,只 UPDATE flow_id 字段。"""
    flow = await store_sqlite.get_flow(flow_id, session_id)
    if not flow or _is_terminal(flow):
        return None  # flow 不存在或已 terminal,不能重绑
    from agent.tools.todolist.registry import store_sqlite as todo_store
    return await todo_store.set_todo_flow_id(session_id, position, flow_id)
```

```python
# store_sqlite.py
async def set_todo_flow_id(session_id: str, position: int, flow_id: str) -> dict | None:
    async with connect() as db:
        await db.execute(
            "UPDATE todos SET flow_id = ? WHERE session_id = ? AND position = ?",
            (flow_id, session_id, position),
        )
        await db.commit()
    return await get_todo(session_id, position)
```

#### `todowrite` 集成

```python
# action="list_orphans"
return await service.TodoService.list_orphan_flows(session_id)

# action="reattach"
return await _shared.bind_todo_to_existing_flow(session_id, position, flow_id)
```

**用例**:模型全量替换误删了 flow_id todo → `todowrite(action="list_orphans")` 发现孤儿 flow F → `todowrite(action="reattach", position=3, flow_id=F)` 把新 todo(或某个 todo)重绑为 F 的 GC root → F 复用,不重跑。

## 关键设计决策

| # | 决策 | 理由 |
|---|------|------|
| D1 | M1 默认 action="replace"(向后兼容) | 老 prompt 仍走全量替换,不破;新 action 增量采用 |
| D2 | M2 `patch_todo` 不允许改 flow_id/step_id/plan_ref | 引用完整性是后台关注,不让模型手动改 flow_id(防误操作孤儿);flow_id 只由 bind/reattach 内部设 |
| D3 | M3 `add_todo` 暴露后,模型加的 todo flow_id 恒 NULL | flow_id 只由 `bind_flow_todo`(创建 flow)或 `bind_todo_to_existing_flow`(复用孤儿)设;模型直接加的是普通 todo |
| D4 | M4 `remove_todo` 删带 flow_id 的 → 即时补偿(复用 P1) | 显式删除是模型意图,补偿合理;不等 sweep |
| D5 | M5 孤儿复用需两步(list_orphans + reattach) | 模型丢了 flow_id 后不知道孤儿 id;先 list 再 reattach |
| D6 | `bind_todo_to_existing_flow` 只 UPDATE flow_id 字段,todo 已存在 | 区别于 `bind_flow_todo`(创建 todo);复用是给已有 todo 加引用 |
| D7 | 预防层(M1-M4)与补救层(P1/P3)叠加 | 预防堵源头(局部改不触发全量替换),补救兜漏网(老代码/bug/局部 API 误用) |

## 风险与开放问题

| # | 风险 | 严重度 | 缓解 |
|---|------|--------|------|
| R1 | 模型仍偏好全量替换(prompt 习惯),局部 API 闲置 | 中 | prompt 引导(工具描述强调局部改优先);默认 action="replace" 不破但 prompt 鼓励 patch/add/remove |
| R2 | M2 patch 改 status="completed" 绕过完成 guard? | 中 | patch_todo 内复用 `_assert_transition_allowed`(同 update_todos 的 guard) |
| R3 | M5 list_orphans 返回的孤儿可能正在被 sweep 回收(竞态) | 中 | reattach 前重查 flow 是否 terminal;已 terminal 的不能重绑;reattach 后 sweep 看到有引用不再回收 |
| R4 | M5 reattach 到一个还在跑子 agent 的孤儿 flow,子 agent 状态? | 中 | reattach 只恢复 GC root(防 sweep 回收);子 agent 是否还在跑由 flow 状态决定,reattach 不干预子 agent |
| R5 | M4 remove 删 position 后,后续 position 重排? | 中 | position 是 PK 一部分;删后不重排(留空位)或重排(需 sweep 全表)。建议留空位(简单),get_todos 返回时跳过空位 |
| R6 | 局部 API 的权限:patch 能改任意 todo 吗(包括别人的)? | 低 | todo 是 session 级,session 内 position 唯一;同 session 内任意改是预期行为 |
| R7 | M1 action-based 接口跟 TaskFlow 的 action 模式一致,模型学习成本 | 低 | 一致是优点;工具描述给清四类 action 的用途 |

## 落地顺序

1. **阶段 1(M1+M2 patch)**:加 action="patch" —— 最常用(改名/改状态),保 flow_id,直接堵孤儿源头。改动小(store 加 UPDATE 单条)。
2. **阶段 2(M3 add)**:加 action="add" —— 暴露已有 `add_todo`,几乎无 store 改动。
3. **阶段 3(M4 remove)**:加 action="remove" —— 删单条 + 补偿(依赖 `方案_Todo与TaskFlow引用完整性.md` 的 P1 `compensate_orphaned_flows`)。
4. **阶段 4(M5 list_orphans + reattach)**:加孤儿发现 + 重绑 —— 最复杂,需跨 todolist/taskflow 两 store。最后做,依赖前面稳定。

阶段 1-2 是预防层核心(堵源头),可单独发布;阶段 3-4 是完整体验。

## 测试清单

### 阶段 1(M2 patch)
- [ ] `todowrite(action="patch", position=3, content="新名")` 只改 content,flow_id 保留
- [ ] patch 不传的字段不动(只改 content,status/priority/flow_id 都不变)
- [ ] patch status="completed" 走 `_assert_transition_allowed`(flow 未 terminal 拒)
- [ ] patch 不存在 position → 返回 None/错误
- [ ] patch 后 todo_updated push 带新 content + 保留的 flow_id

### 阶段 2(M3 add)
- [ ] `todowrite(action="add", content="做 Z")` 尾部追加单条
- [ ] add 的 todo flow_id NULL(普通 todo,非 TaskFlow 绑定)
- [ ] add 不影响其它 todo(不 clobber flow_id todo)
- [ ] position 递增(追加在末尾)

### 阶段 3(M4 remove)
- [ ] `todowrite(action="remove", position=5)` 删单条
- [ ] 删带 flow_id 的 todo → `compensate_orphaned_flows` cancel 该 flow
- [ ] 删不带 flow_id 的 todo → 无补偿,只删
- [ ] 删不存在 position → 返回 None

### 阶段 4(M5 list_orphans + reattach)
- [ ] 全量替换误删 flow_id todo → `list_orphans` 返回该孤儿 flow
- [ ] `reattach(position=3, flow_id=F)` 把 todo 重绑为 F 的 GC root
- [ ] reattach 后 sweep 看到 F 有引用,不回收
- [ ] reattach 已 terminal 的 flow → 返回 None(不能重绑)
- [ ] reattach 后 todo_updated push 带 flow_id
- [ ] list_orphans 不返回已 terminal 的 flow

### 端到端(预防 + 补救叠加)
- [ ] 模型 `patch` 改名 → flow_id 保留 → flow 不孤儿 → 不触发 P1 补偿(预防层生效)
- [ ] 模型仍走全量替换误删 → P1 补偿 cancel 孤儿 → `list_orphans` 返回空(已 cancel)→ 模型重建 flow(补救层生效)
- [ ] 模型全量替换误删 → P1 补偿失败 → `list_orphans` 返回孤儿 → `reattach` 重绑 → flow 复用不重跑(预防+补救兜底)

## 与其他方案的关系

| 方案 | 关系 |
|------|------|
| `方案_Todo与TaskFlow引用完整性.md`(补救层) | **叠加** —— 本方案是预防层(局部改不触发全量替换),那方案是补救层(全量替换误删后补偿)。M4 remove 复用那方案的 `compensate_orphaned_flows`。两层独立部署,叠加成完整防线 |
| `方案_最终校验波_FinalVerificationWave.md`(校验波) | **协同** —— 校验波给 todo 加 `kind`/`accept_criteria`,M2 patch 要保这两个字段(同 flow_id 一样不在可改字段里)。两方案不冲突,patch 默认保留所有引用字段 |
| `方案_证据台账_加flow_id列.md` | 无关 —— 那是证据表加列,本方案是 todo mutation API |

本方案是 todo mutation 的**预防层补丁**,跟补救层方案叠加,从"堵源头 + 兜漏网"两面解孤儿问题。
