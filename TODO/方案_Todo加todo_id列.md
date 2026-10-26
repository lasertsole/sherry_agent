# 方案:Todo 加 todo_id 列(解耦身份与排序)

## 设计目标

给 `todos` 表加 `todo_id` 列作**稳定身份标识**,把 `position` 降级为**纯排序键**(可重排)。这是局部改 API(`patch`/`remove`/`reattach`)能稳定定位的前提 —— 否则中间插入重排 position,定位漂移,局部 API 退化。

**核心矛盾**(现状):schema 无 `todo_id`,position 既是排序键又是 PK 一部分。它同时管"在哪显示"和"是哪个 todo"。中间插入要么重排(改 PK,UPDATE 要避冲突)要么用小数(INTEGER 不允许);且 `patch`/`remove`/`reattach` 用 position 定位,重排后定位漂移。

**解法**:加 `todo_id`,局部 API 改用它定位,position 只管显示顺序(可重排不影响定位)。

## 现状(已核对代码)

| 事实 | 证据 |
|------|------|
| schema 无 `todo_id` 列 | `store_sqlite.py:4-9` `todos(..., position INTEGER NOT NULL, ..., PRIMARY KEY (session_id, position))` |
| `append_todo` 用 `MAX(position)+1` 末尾追加 | `store_sqlite.py:257-273` `SELECT COALESCE(MAX(position), -1) + 1` |
| `replace_all` 全量替换(DELETE+INSERT) | `store_sqlite.py:196-211` |
| position 只在 store 内部用(排序/PK),未被外部当稳定 ID 引用 | grep 显示 position 只在 `_row_to_todo`/`_normalize_rows`/`get_todos` ORDER BY 用 |
| `方案_Todo局部改API与孤儿复用.md` 的 M2/M4/M5 用 position 定位 | 该方案 `patch(position=3, ...)`/`remove(position=...)`/`reattach(position=..., flow_id=...)` |
| 中间插入不支持 | `append_todo` 只 MAX+1;`replace_all` 模型自己控制 position 但全量替换(孤儿风险) |

## 任务分解

### M1. schema 加 `todo_id` 列 + 索引

**文件**:`agent/tools/todolist/registry/store_sqlite.py`(`_SCHEMA` + `_migration_ddls`)

```sql
-- _migration_ddls(老库加列,IF NOT EXISTS 容错)
ALTER TABLE todos ADD COLUMN todo_id INTEGER NOT NULL DEFAULT 0;

-- _index_ddls
CREATE INDEX IF NOT EXISTS idx_todolist_todo_id ON todos(session_id, todo_id);
```

**PK 不变**:`(session_id, position)` 保持(向后兼容,`replace_all` 仍按 position 工作)。`todo_id` 作**稳定身份** + UNIQUE 索引(定位用),position 作**排序**(可重排)。

**类型选择**:per-session INTEGER(非 UUID)。`todo_id` 由 store 层 assign 为 `MAX(todo_id)+1` per session(同 position 的 assign 模式),简单、短、易调试。全局唯一性不需要(定位是 session 级)。

### M2. 老行回填迁移

**文件**:`store_sqlite.py`(迁移逻辑)

`ALTER TABLE ADD COLUMN todo_id ... DEFAULT 0` 让老行 todo_id=0。迁移时回填:每行 `todo_id = position`(position 在老行里是唯一的,作稳定 ID 合法):

```python
async def _backfill_todo_ids(session_id: str) -> None:
    """老行(todo_id=0)回填:todo_id = position。一次性,幂等。"""
    async with _connect() as db:
        await db.execute(
            f"UPDATE todos SET todo_id = position WHERE session_id = ? AND todo_id = 0",
            (session_id,),
        )
        await db.commit()
```

回填在 `ensure_db` 后首次 `get_todos`/`get_active_flow_ids` 时触发(惰性,per-session)。回填后老行 todo_id = 原 position(稳定);后续重排 position 不影响 todo_id。

### M3. `_next_todo_id` 分配器

**文件**:`store_sqlite.py`

```python
async def _next_todo_id(db, session_id: str) -> int:
    """per-session 自增 todo_id。"""
    async with db.execute(
        f"SELECT COALESCE(MAX(todo_id), 0) + 1 FROM {TABLE_NAME} WHERE session_id = ?",
        (session_id,),
    ) as cursor:
        result = await cursor.fetchone()
    return int(result[0]) if result else 1
```

`append_todo`、`replace_all`、新 `add_todo` 都用它分配 todo_id。

### M4. 现有写路径补 todo_id

**文件**:`store_sqlite.py`(`_INSERT_SQL` + `_INSERT_COLUMNS` + `append_todo` + `replace_all`)

`_INSERT_COLUMNS` 加 `todo_id`,`_INSERT_SQL` 占位符 +1:

```python
_INSERT_COLUMNS = (
    "session_id, content, status, priority, position, category, delegation, "
    "subagent_id, flow_id, step_id, plan_ref, created_at, todo_id"  # 加末尾
)
_INSERT_SQL = f"INSERT INTO {TABLE_NAME} ({_INSERT_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
```

`append_todo` / `replace_all` 在 INSERT 前调 `_next_todo_id` 填 todo_id 字段。`replace_all` 全量替换时,新行全部是新 todo_id(模型不传 todo_id,store assign)—— **语义变化**:全量替换后 todo_id 全新(老 todo_id 不保留)。这是现有行为(全量替换本来就连 todo 身份都换了),todo_id 不例外。

### M4b. 读路径返回 todo_id(`_row_to_todo` 补字段)—— **与 M1 同步,否则读全炸**

**文件**:`agent/tools/todolist/registry/store_sqlite.py`(`_row_to_todo`)

**关键陷阱**:`_row_to_todo` 用**位置解包** `(session_id, ..., created_at) = row`。`ALTER TABLE ADD COLUMN todo_id` 把列加到表**末尾**,`SELECT *` 多返回一列 → 位置解包**直接报错**(too many values to unpack)→ 所有读路径(`get_todos`/`get_todos_by_flow`/`update_todos` 的 read-back 返回/`todoread` 工具)全炸。**M1 加列必须与 M4b 同 PR 同事务**,否则迁移后第一次读就崩。

`_row_to_todo` 补 todo_id(末尾,匹配 `SELECT *` 列序):

```python
def _row_to_todo(row: tuple) -> dict:
    (
        session_id, content, status, priority, position, category,
        delegation, subagent_id, flow_id, step_id, plan_ref, created_at,
        todo_id,  # 末尾,匹配 ALTER TABLE ADD COLUMN 的位置
    ) = row
    return {
        "session_id": session_id, "content": content, "status": status,
        "priority": priority, "position": int(position), "category": category,
        "delegation": delegation, "subagent_id": subagent_id, "flow_id": flow_id,
        "step_id": step_id, "plan_ref": plan_ref, "created_at": created_at,
        "todo_id": int(todo_id),  # 返回给模型,供 patch/remove/reattach 定位
    }
```

**影响的所有读路径**(都过 `_row_to_todo`):
- `get_todos` / `get_todos_sync`(session 全量读,`todoread` 工具 + `todo_updated` push 源)
- `get_todos_by_flow`(按 flow 查)
- `update_todos` 的 read-back 返回(`service.py:228-230` `return latest`)→ **`todowrite` 工具 `json.dumps(result)` 返回模型,模型靠这个拿 todo_id 做后续 patch/remove/reattach**
- `append_todo` / `add_todo` 的返回(`get_todo_by_id` 也过 `_row_to_todo`)

**模型可见性链路**:`update_todos`(全量替换)返回 read-back list → `todowrite` 工具 `json.dumps` 给模型 → 模型从返回里读 todo_id → 后续 `patch(todo_id=...)` 定位。**todo_id 必须在返回里**,否则模型全量替换后不知道新 todo 的 todo_id,局部 API 无法接续。

`_INSERT_COLUMNS`/`_INSERT_SQL` 列序也要含 todo_id(M4 已加),`SELECT *` 列序 = schema 序 = `(..., created_at, todo_id)` —— `_row_to_todo` 解包序匹配。

**`todo_updated` WS push payload**:`service.py` 各处 `await _push_todo_update(session_id, latest)` 推的 `latest` 是 `get_todos` 结果(过 `_row_to_todo`),自动带 todo_id —— 前端无需额外改,推送 payload 已含。

### M4c. `_build_todo_block` 格式化加 todo_id(system-reminder 文本格式化点)

**文件**:`workspace/prompt_builder.py`(`_build_todo_block:75-108`)

`_build_todo_block` 是**唯一**从 todo dict 构建模型可见文本的格式化点(system-reminder 每轮注入,reminder_catalog source `todo_block`)。M4b 让 dict 带 todo_id,但**格式化丢掉了它** —— 当前格式:

```
- [icon] (category, delegation, flow:flow_id, step_id) content (priority)
```

模型从 system-reminder 读 todo(每轮都看这个块),若 todo_id 不在里面,模型不知道哪个 todo 是哪个 id,`patch(todo_id=...)`/`remove(todo_id=...)`/`reattach(todo_id=...)` 无法接续。**这是模型拿 todo_id 的主路径**(每轮常驻,比 todowrite/todoread 返回更稳定 —— 那俩是调用时才有)。必须显式加 todo_id 到格式化输出:

```python
# _build_todo_block 内,每行加 todo_id 前缀(像 GitHub issue 编号,显眼易提取)
todo_id = todo.get("todo_id")
id_prefix = f"#{todo_id} " if todo_id else ""
lines.append(f"- [{icon}] {id_prefix}{tag} {content} ({priority})")
```

格式变:
```
- [▶] #42 (quick, flow:F1) 跑全量测试 (high)
- [○] #43 (quick) 写文档 (medium)
```

模型从 system-reminder 直接读 `#42` → `patch(todo_id=42, content="...")`。

### 完整性审计:所有读 todo 路径

| 路径 | 类型 | 过 `_row_to_todo`? | 模型可见? | 需单独补? |
|------|------|---------------------|-----------|------------|
| `update_todos` read-back → `todowrite` json.dumps | 数据(dict) | ✓ | ✓ | 否(M4b 覆盖,dict 带 todo_id) |
| `todoread` 工具返回 | 数据(dict) | ✓ | ✓ | 否(M4b 覆盖) |
| `todo_updated` WS push | 数据(dict) | ✓ | 前端 | 否(M4b 覆盖) |
| **`_build_todo_block`**(system-reminder,reminder_catalog `todo_block` source) | **文本格式化** | ✓(via `_read_todos_sync`→`AgentPromptDataProvider.get_todos`→`get_todos_sync`) | ✓(每轮注入) | **是 —— M4c**(格式化丢 todo_id) |
| `nudges.py` 读(get_todos_sync,4 处) | 内部决策(返回 bool) | ✓ | ✗ | 否(不展示给模型) |
| `plan_context.py` 读 | 内部/上下文 | ✓ | 取决于消费方 | 否(返回 dict,M4b 覆盖;若消费方格式化再查) |
| `todo_continuation/core.py` 读 | 内部决策 | ✓ | ✗ | 否(不展示) |
| `AgentPromptDataProvider.get_todos()` | 数据(dict) | ✓ | 取决于消费方 | 否(唯一消费方是 `_read_todos_sync`→`_build_todo_block`,M4c 覆盖) |

**结论**:数据层全过 `_row_to_todo`(M4b 一处覆盖);**唯一文本格式化点 `_build_todo_block` 需 M4c 单独补**(dict→text 时丢字段)。其它"内部决策"读路径不展示给模型,无需补。

**审计方法**(防漏):grep `get_todos`/`get_todos_sync`/`_read_todos_sync`/`AgentPromptDataProvider.get_todos` 全调用点,对每个判断"返回是否模型可见 + 是否过格式化"。模型可见 + 过格式化的,都要显式加 todo_id。本审计只发现 `_build_todo_block` 一处;若将来新增格式化点,按此方法复查。

### M5. `add_todo` —— 追加+插队合二为一(按 todo_id 定插入点)

**文件**:`store_sqlite.py`(新方法,替代 `append_todo`)

合并"末尾追加"和"中间插入"为**一个机制**,插入点用 `todo_id`(稳定)而非 position(会漂移):

- `after_todo_id=None` → **追加到队尾**(MAX(position)+1,无需 shift)
- `after_todo_id=42` → **插到 #42 后面**(position = #42 的 position + 1),后续 position 全部 +1 让位

```python
async def add_todo(
    session_id: str, todo: dict, *, after_todo_id: int | None = None
) -> dict | None:
    """加一条 todo。after_todo_id=None 追加队尾;指定则插到该 todo_id 后面,后续挪位。
    返回新插入的 todo(含 todo_id)。after_todo_id 不存在 → 报错(模型引用了不存在的 id)。"""
    session_id = (session_id or "").strip()
    if not session_id:
        raise ValueError("session_id must be non-empty")
    row = list(_normalize_rows(session_id, [todo])[0])
    await ensure_db()
    async with _connect() as db:
        if after_todo_id is None:
            # 追加队尾:position = MAX(position)+1,无需 shift
            async with db.execute(
                f"SELECT COALESCE(MAX(position), -1) + 1 FROM {TABLE_NAME} WHERE session_id = ?",
                (session_id,),
            ) as cur:
                r = await cur.fetchone()
            target_position = int(r[0]) if r else 0
        else:
            # 插队:找 after_todo_id 的 position,插到它后面
            async with db.execute(
                f"SELECT position FROM {TABLE_NAME} WHERE session_id = ? AND todo_id = ?",
                (session_id, after_todo_id),
            ) as cur:
                r = await cur.fetchone()
            if r is None:
                raise ValueError(f"after_todo_id {after_todo_id} not found in session {session_id}")
            anchor_position = int(r[0])
            target_position = anchor_position + 1
            # 双步避 PK 冲突:把 >= target_position 的全部上移再回挪,腾出插入点
            OFFSET = 1_000_000
            await db.execute(
                f"UPDATE todos SET position = position + {OFFSET} "
                f"WHERE session_id = ? AND position >= ?",
                (session_id, target_position),
            )
            await db.execute(
                f"UPDATE todos SET position = position - {OFFSET} + 1 "
                f"WHERE session_id = ? AND position >= {OFFSET} + ?",
                (session_id, target_position),
            )
        # 插入新 todo,position = 插入点,todo_id 自增
        todo_id = await _next_todo_id(db, session_id)
        row[4] = target_position
        row.append(todo_id)  # _INSERT_COLUMNS 末尾是 todo_id
        await db.execute(_INSERT_SQL, tuple(row))
        await db.commit()
    return await get_todo_by_id(session_id, todo_id)
```

**边界**:
- `after_todo_id` 是队尾(MAX position)→ `target_position = MAX+1`,shift 条件 `position >= MAX+1` 命中 0 行,等价追加(无需特判,自然退化)
- `after_todo_id` 不存在 → `ValueError`(模型引用了不存在的 id,应被告知,不静默追加)
- `after_todo_id=None` → 纯追加,无 shift,等价老 `append_todo`

**`append_todo` 迁移**:老 `append_todo`(`bind_flow_todo` 用)改成调 `add_todo(after_todo_id=None)` 的薄包装,或 `bind_flow_todo` 直接调 `add_todo`。`append_todo` 可保留作向后兼容别名。

**OFFSET 选 1,000,000**:单 session 不会有百万 todo,偏移足够大避撞。双步在单事务(`async with _connect()` 隐式事务),原子。

### M6. `get_todo_by_id` + 局部 API 改 todo_id 定位

**文件**:`store_sqlite.py`(新 `get_todo_by_id`)+ `service.py`(patch/remove/reattach 改签名)

```python
# store_sqlite.py
async def get_todo_by_id(session_id: str, todo_id: int) -> dict | None:
    async with _connect() as db:
        async with db.execute(
            f"SELECT * FROM {TABLE_NAME} WHERE session_id = ? AND todo_id = ?",
            (session_id, todo_id),
        ) as cursor:
            row = await cursor.fetchone()
    return _row_to_todo(row) if row else None

async def patch_todo_by_id(session_id, todo_id, *, content=None, status=None, priority=None, category=None):
    """局部改单条,按 todo_id 定位(不受 position 重排影响)。flow_id/step_id/plan_ref 不在可改字段。"""
    sets, params = [], []
    if content is not None: sets.append("content = ?"); params.append(content)
    if status is not None: sets.append("status = ?"); params.append(status)
    if priority is not None: sets.append("priority = ?"); params.append(priority)
    if category is not None: sets.append("category = ?"); params.append(category)
    if not sets: return await get_todo_by_id(session_id, todo_id)
    params.extend([session_id, todo_id])
    async with _connect() as db:
        await db.execute(f"UPDATE todos SET {', '.join(sets)} WHERE session_id = ? AND todo_id = ?", params)
        await db.commit()
    return await get_todo_by_id(session_id, todo_id)

async def remove_todo_by_id(session_id, todo_id) -> dict | None:
    async with _connect() as db:
        async with db.execute(f"SELECT * FROM {TABLE_NAME} WHERE session_id = ? AND todo_id = ?", (session_id, todo_id)) as c:
            row = await c.fetchone()
        if not row: return None
        await db.execute(f"DELETE FROM {TABLE_NAME} WHERE session_id = ? AND todo_id = ?", (session_id, todo_id))
        await db.commit()
    return _row_to_todo(row)

async def set_todo_flow_id_by_id(session_id, todo_id, flow_id) -> dict | None:
    async with _connect() as db:
        await db.execute(f"UPDATE todos SET flow_id = ? WHERE session_id = ? AND todo_id = ?", (flow_id, session_id, todo_id))
        await db.commit()
    return await get_todo_by_id(session_id, todo_id)
```

`service.py` 的 `patch_todo`/`remove_todo`/`bind_todo_to_existing_flow` 签名从 `position` 改 `todo_id`:

```python
@staticmethod
async def patch_todo(session_id, todo_id, *, content=None, status=None, priority=None, category=None):
    if status == "completed":
        todo = await store.get_todo_by_id(session_id, todo_id)
        if todo:
            await _assert_transition_allowed(todo, session_id)
    return await store.patch_todo_by_id(session_id, todo_id, content=content, status=status, priority=priority, category=category)
```

### M7. `todowrite` 工具:action 入参改 todo_id + add 合并 insert

**文件**:`agent/tools/todolist/tools/todowrite.py`

`add` 和 `insert` **合二为一**为 `add` action —— 用 `after_todo_id` 控制插入点(None=队尾,指定=插到该 id 后面)。`remove` 必须指定 `todo_id`(不再支持按 position 删):

```python
class TodoWriteInput(BaseModel):
    action: Literal["replace", "patch", "add", "remove", "reattach", "list_orphans"] = "replace"
    todos: list[dict] | None = None       # replace
    todo_id: int | None = None             # patch/remove/reattach(remove 必填)
    after_todo_id: int | None = None       # add:None=追加队尾,指定=插到该 id 后面
    content: str | None = None            # patch/add
    status: str | None = None             # patch
    priority: str | None = None
    category: str | None = None
    plan_ref: str | None = None
    flow_id: str | None = None            # reattach
```

`add` action 转 `store.add_todo`(M5 的统一方法):

```python
# action="add" —— after_todo_id=None 追加队尾,指定则插队
result = await store.add_todo(
    session_id,
    {"content": content, "status": "pending", "priority": "medium",
     "category": category or DEFAULT_CATEGORY, "plan_ref": plan_ref},
    after_todo_id=after_todo_id,  # None=队尾,#N=插到 N 后面
)
```

`remove` action 必须带 `todo_id`(不再支持按 position):

```python
# action="remove" —— todo_id 必填
if todo_id is None:
    return "error: remove requires todo_id"
result = await service.TodoService.remove_todo(session_id, todo_id)  # 内部触发 compensate_orphaned_flows 若带 flow_id
```

`todo_updated` push 的 payload 带 `todo_id`(前端可用它定位,不靠 position)。

**工具描述(prompt 给模型看的)**:`add` 的 `after_todo_id` 描述要写清"None 加到队尾,指定 todo_id 则插到那条后面" —— 模型从 system-reminder 的 `#<id>` 读到 id 后,能直接 `add(after_todo_id=42, content="新步骤")` 插队。

### M8. todo_id 不存在 → 提醒 + 返回完整 todolist(含 todo_id)

**文件**:`agent/tools/todolist/service.py`(typed error)+ `agent/tools/todolist/tools/todowrite.py`(catch + 格式化)

当前 M6 的 `patch_todo_by_id`/`remove_todo_by_id`/`set_todo_flow_id_by_id` 找不到 todo_id 返回 `None`(哑,模型不知道哪里错)。M5 的 `add_todo(after_todo_id=...)` 抛 `ValueError`。**改成统一:找不到 → 提醒模型 todo_id 错误 + 返回完整 todolist(含 todo_id),让模型立即看到合法 id 重试**。

#### service 层:typed error 带合法列表

```python
# service.py
class TodoIdNotFoundError(Exception):
    """todo_id 不存在。携带合法 todo 列表,供工具层格式化返回模型。"""
    def __init__(self, todo_id, valid_todos: list[dict]):
        self.todo_id = todo_id
        self.valid_todos = valid_todos
        super().__init__(f"todo_id {todo_id} not found")

@staticmethod
async def patch_todo(session_id, todo_id, *, content=None, status=None, priority=None, category=None):
    if status == "completed":
        todo = await store.get_todo_by_id(session_id, todo_id)
        if not todo:
            raise TodoIdNotFoundError(todo_id, await store.get_todos(session_id))
        await _assert_transition_allowed(todo, session_id)
    result = await store.patch_todo_by_id(session_id, todo_id, content=content, status=status, priority=priority, category=category)
    if result is None:
        raise TodoIdNotFoundError(todo_id, await store.get_todos(session_id))
    return result

# remove_todo / set_todo_flow_id / bind_todo_to_existing_flow 同模式:
# store 返回 None → raise TodoIdNotFoundError(todo_id, get_todos(session_id))
```

`add_todo(after_todo_id=...)` 的 `ValueError` 也改成 `TodoIdNotFoundError`(after_todo_id 不存在时,带合法列表)。

#### 工具层:catch + 格式化返回模型

```python
# todowrite.py 各 action 分支
from agent.tools.todolist.service import TodoIdNotFoundError

try:
    if action == "patch":
        result = await service.TodoService.patch_todo(session_id, todo_id, content=content, status=status, ...)
    elif action == "remove":
        result = await service.TodoService.remove_todo(session_id, todo_id)
    elif action == "reattach":
        result = await _shared.bind_todo_to_existing_flow(session_id, todo_id, flow_id)
    elif action == "add":
        result = await store.add_todo(session_id, {...}, after_todo_id=after_todo_id)
    # ... 正常返回 result
except TodoIdNotFoundError as e:
    # 提醒 + 返回完整 todolist(含 todo_id),让模型看到合法 id 重试
    todos_lines = []
    for t in e.valid_todos:
        icon = _TODO_ICONS.get(t.get("status", ""), "○")
        todos_lines.append(f"- [{icon}] #{t['todo_id']} {t.get('content', '')} ({t.get('priority', 'medium')})")
    return (
        f"todo_id {e.todo_id} not found in this session's todolist. "
        f"It may have been removed or never existed. "
        f"Current todos (use one of these todo_id values):\n"
        + "\n".join(todos_lines)
    )
```

**返回示例**:
```
todo_id 99 not found in this session's todolist. It may have been removed or never existed.
Current todos (use one of these todo_id values):
- [▶] #42 跑全量测试 (high)
- [○] #43 写文档 (medium)
- [○] #44 类型检查 (medium)
```

模型读完立即知道:99 不对,合法的是 42/43/44,重试 `patch(todo_id=42, ...)`。

**适用范围**:所有带 todo_id / after_todo_id 的局部操作(patch / remove / reattach / add-with-after_todo_id)。

## 关键设计决策

| # | 决策 | 理由 |
|---|------|------|
| D1 | `todo_id` 用 per-session INTEGER(MAX+1),**非 UUID** | **模型可读性是主理由**:模型要在工具调用里读写 todo_id,INTEGER `#42` 一眼读到、`patch(todo_id=42)` 易传不易错;UUID 36 字符在 system-reminder 里冗长、模型易截断/抄错、token 消耗大。UUID 的全局唯一性用不上 —— todo 是 session 级(patch/remove/reattach 隐式带 session_id),从不跨 session 引用。MAX+1 竞态(R6)被 SQLite 单事务隔离 + busy_timeout 解决 |
| D2 | PK 保持 `(session_id, position)` 不变 | 向后兼容(`replace_all` 仍按 position);todo_id 作 UNIQUE 索引定位;position 仍管排序 |
| D3 | 老行回填 `todo_id = position` | position 在老行唯一,作稳定 ID 合法;回填后老行 todo_id 稳定,后续重排 position 不影响 |
| D4 | `add_todo` 合并追加+插队,插入点用 `after_todo_id`(稳定)非 position | position 会漂移,用 todo_id 定插入点与"todo_id 作身份"原则一致;None=队尾,指定=插到该 id 后;双步 UPDATE 避 PK 冲突 |
| D5 | 局部 API(patch/remove/reattach)用 todo_id 定位,不用 position;**remove 必填 todo_id** | position 可重排(add 插队会 shift),用 position 定位会漂移;todo_id 稳定;remove 不带 todo_id 直接报错(不再支持按 position 删) |
| D6 | `replace_all` 新行 todo_id 全新(不保留老 todo_id) | 全量替换本就是"换列表",身份也换;todo_id 不例外。模型若想保身份应用 patch |
| D7 | OFFSET=1,000,000 | 单 session 不会有百万 todo;偏移足够避撞;可配置 |
| D8 | **模型可见性链路**:`update_todos` 返回 read-back list → `todowrite` `json.dumps` 给模型 → 模型从返回读 todo_id → 后续 `patch(todo_id=...)` 接续 | todo_id 必须在所有返回模型的 payload 里(全量替换 read-back / todoread / todo_updated push);否则模型不知道新 todo 的 todo_id,局部 API 无法定位 |
| D9 | `_row_to_todo` 是单一映射,DB row → dict 全过它 | 加 todo_id 一处改,所有读路径同步;也意味着 M1 加列不与 M4b 同步会一处炸全局(R8) |
| D10 | **todo_id 不存在不静默失败**:service 层抛 `TodoIdNotFoundError` 带合法列表,工具层 catch 后返回"提醒 + 完整 todolist(含 #id)" | 模型可能引用过期/错误 id(全量替换后 todo_id 变 / reminder 滞后 / 抄错);静默返回 None 模型不知道哪里错;返回合法列表让模型立即重试。store 层仍返回 None(纯数据),错误处理在 service+tool 层 |

## 风险与开放问题

| # | 风险 | 严重度 | 缓解 |
|---|------|--------|------|
| R1 | 老行回填:`todo_id = position` 后,若 position 之前有空洞(删过留空),todo_id 也空洞 | 低 | 空洞无害(todo_id 不连续是正常的,只要唯一);回填后新行 MAX(todo_id)+1 接上 |
| R2 | `replace_all` 不保留老 todo_id,模型若依赖 todo_id 跨替换引用会断 | 中 | 全量替换本就换列表;文档强调"保身份用 patch 不用 replace";前端 todo_id 在 replace 后刷新 |
| R3 | 双步 UPDATE 的 OFFSET 撞上极端大 position(>1M) | 极低 | 单 session 不可能有百万 todo;防御性加 assert position < OFFSET |
| R4 | 中间插入触发 position 重排,`todo_updated` push 的高频更新 | 低 | 单事务原子,一次 push;前端按 position 排序渲染,todo_id 定位操作不受影响 |
| R5 | `get_todo_by_id` 索引性能 | 低 | `idx_todolist_todo_id(session_id, todo_id)` 走索引,O(log n) |
| R6 | 并发:`append_todo` 和 `add_todo`(插队)同时跑,MAX(position)/MAX(todo_id) 竞态 | 中 | 都在单事务里 `SELECT ... +1` + INSERT;SQLite 事务隔离保证;`busy_timeout` 已设 |
| R7 | 前端从 position 定位改 todo_id 定位的迁移 | 中 | `todo_updated` payload 加 todo_id;前端操作逐步切;老前端按 position 仍能显示(排序键没废) |
| R8 | **M1 加列不与 M4b 同步** → `_row_to_todo` 位置解包少一列 → 所有读路径炸(get_todos/todoread/update_todos read-back 全崩) | 高 | M1+M4b 必须同 PR 同事务;CI 加迁移后 `get_todos` 不报错的门禁 |

## 落地顺序

1. **阶段 1(schema + 回填 + 读返回 + reminder 格式化)**:M1(加列+索引)+ M2(老行回填)+ M3(`_next_todo_id`)+ M4(写路径填 todo_id)+ **M4b(`_row_to_todo` 补 todo_id,读路径返回)** + **M4c(`_build_todo_block` 格式化加 todo_id,system-reminder 可见)**。todo_id 落库 + 模型可见(全量替换/todoread/push/reminder 都带 todo_id),但局部 API 还没用它 —— 安全,行为不变(只是返回/reminder 多字段)。**M1+M4b 必须同 PR**(位置解包陷阱);M4c 紧跟(reminder 是模型拿 todo_id 主路径,跟 M4b 同批最稳)。
2. **阶段 2(读 API)**:M6 的 `get_todo_by_id`。前端 `todo_updated` payload 加 todo_id 字段。
3. **阶段 3(局部 API 切 todo_id + 错误处理)**:`patch_todo`/`remove_todo`/`set_todo_flow_id` 改 todo_id 签名(替代 position)。`todowrite` 的 patch/remove/reattach action 转 todo_id。**M8:todo_id 不存在 → 提醒 + 返回完整 todolist**(service 层抛 `TodoIdNotFoundError` 带合法列表,工具层 catch 格式化)。
4. **阶段 4(追加+插队合一)**:M5 `add_todo(after_todo_id)` + `todowrite` action="add"(after_todo_id=None 追加,指定插队)。最后做,依赖 todo_id 稳定(阶段 3 完)。

阶段 1-2 安全可单独发布(无行为变化);阶段 3 切定位标识;阶段 4 解锁按 todo_id 插队。

## 测试清单

### 阶段 1
- [ ] `todos` 加 `todo_id` 列,老库迁移成功
- [ ] 老行回填 `todo_id = position`,回填后 todo_id 唯一 per session
- [ ] `append_todo` 新行 todo_id = MAX(todo_id)+1
- [ ] `replace_all` 新行 todo_id 全新(不保留老)
- [ ] 回填幂等(todo_id != 0 的行不动)
- [ ] `_row_to_todo` 返回带 `todo_id` 字段(位置解包与 SELECT * 列序匹配)
- [ ] `update_todos` 返回的 read-back list 每项含 todo_id(模型可见)
- [ ] `todowrite` 工具 `json.dumps(result)` 输出含 todo_id
- [ ] `todoread` 工具返回含 todo_id
- [ ] `todo_updated` WS push payload 每项含 todo_id
- [ ] 迁移后 `get_todos` 不报位置解包错(M1+M4b 同 PR 验证)
- [ ] `_build_todo_block`(system-reminder)输出每行带 `#<todo_id>` 前缀
- [ ] reminder_catalog 的 `todo_block` source 注入的 system-reminder 含 todo_id
- [ ] 模型从 system-reminder 能读到 todo_id(端到端:建 todo → 下一轮 reminder 带 #id)
- [ ] `_build_todo_block` 格式化失败仍 fail-open(不破坏 prompt,现有行为)

### 阶段 2
- [ ] `get_todo_by_id(session_id, todo_id)` 走索引返回正确 todo
- [ ] `todo_updated` push payload 带 todo_id 字段
- [ ] 不存在 todo_id → 返回 None

### 阶段 3
- [ ] `patch_todo(todo_id=42, content="新名")` 只改 content,flow_id/position 不变
- [ ] patch 用 todo_id 定位,position 重排后仍指向同一 todo
- [ ] `remove_todo(todo_id=42)` 删单条,按 todo_id 定位
- [ ] `set_todo_flow_id(todo_id=42, flow_id=F)` 重绑,按 todo_id 定位
- [ ] patch/remove/reattach 用错 todo_id → 抛 `TodoIdNotFoundError` 带合法列表
- [ ] 工具层 catch 后返回"todo_id N not found + 完整 todolist(含 #id)"可读格式
- [ ] `add(after_todo_id=<不存在>)` → 同样提醒 + 返回列表(不再抛 ValueError)
- [ ] 错误返回里的 todolist 每项带 `#<todo_id>`(模型能直接读合法 id 重试)
- [ ] patch/remove/reattach/add 正常路径(合法 todo_id)不受影响

### 阶段 4(追加+插队合一)
- [ ] `add_todo(after_todo_id=None, content="新")` 追加到队尾(position=MAX+1)
- [ ] `add_todo(after_todo_id=42, content="新")` 插到 #42 后面(position=#42+1)
- [ ] 插队后原 position >= #42+1 的全部 +1,无 PK 冲突
- [ ] 新 todo 的 todo_id = MAX(todo_id)+1(position 可能重排,todo_id 单调递增)
- [ ] 插队后老 todo 的 todo_id 不变(position 变了但 todo_id 稳定)
- [ ] `patch_todo(todo_id=<老 todo>)` 在插队后仍正确定位(不漂移)
- [ ] `after_todo_id` 是队尾(MAX position)→ 等价追加(shift 命中 0 行,无需特判)
- [ ] `after_todo_id` 不存在 → `ValueError`(模型引用了不存在的 id)
- [ ] 双步 UPDATE 在单事务原子(中途失败回滚,无半状态)
- [ ] 连续插队多次(#42 后插 3 次),无 PK 撞

### 端到端
- [ ] 模型 `add(after_todo_id=42, content="新步骤")` → 老 todo 重排 → `patch(todo_id=<老>, content="改名")` 仍准 → flow_id 保留 → 不孤儿
- [ ] `add(after_todo_id=None)` 追加 + `add(after_todo_id=N)` 插队 + `patch`/`remove(todo_id=...)`,全程不动其它 todo 的 flow_id → 引用完整性保持
- [ ] `remove` 不带 todo_id → 报错(必填校验)

## 与其他方案的关系

| 方案 | 关系 |
|------|------|
| `方案_Todo局部改API与孤儿复用.md`(局部改 + 孤儿复用) | **强依赖** —— 该方案的 M2/M4/M5(patch/remove/reattach)原用 position 定位,本方案把它们改用 todo_id。两方案合并实施:本方案先上(schema + todo_id),该方案的局部 API 跟进(用 todo_id 定位 + `add` action 带 `after_todo_id`) |
| `方案_Todo与TaskFlow引用完整性.md`(补救层) | **解耦** —— 补救层不依赖 todo_id(它管 flow_id 引用);本方案管 todo 身份。两层独立 |
| `方案_最终校验波_FinalVerificationWave.md`(校验波) | **协同** —— 校验波给 todo 加 `kind`/`accept_criteria`,本方案加 `todo_id`;两者都是 todo schema 扩展,可并行迁移(不同的 ALTER TABLE) |
| `方案_证据台账_加flow_id列.md` | 无关 —— 那是证据表加列,本方案是 todo 表加列 |

本方案是 todo schema 的**身份层补丁**,是局部改 API 完整支持(含中间插入)的前提,跟局部改 API 方案合并实施。
