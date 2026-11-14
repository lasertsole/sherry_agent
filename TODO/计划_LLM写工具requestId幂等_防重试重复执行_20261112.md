# 计划：LLM 写工具 requestId 幂等 — 防重试重复执行

- 日期：2026-11-12
- 状态：**待评审**
- 来源：借鉴同类工具 MCP 管理服务的写接口安全模式；LLM 重试写工具时用 requestId 去重
- 风险等级：**低-中** —— 加法式（工具调用层加 requestId 去重表），不动首次执行
- 关系：补已有 `计划_非幂等动作防重放防重试`（那个管重放层 PTC/LDW，本计划管调用层 LLMRetry）

---

## 0. 和已有幂等计划的关系（已核实）

`计划_非幂等动作防重放防重Retry_20261111.md` 覆盖的是 **Sherry 重放层**：
- PTC pre-run / LDW journal / boot recovery 不重放非幂等动作
- session 级被动账本 + system-reminder 提醒

**本计划覆盖的是调用层**——LLMRetry 中间件重试写工具时去重：
- LLM 调一个写工具（如 `send_email`/`create_order`/`deploy`）
- LLMRetry 因超时/429 重试同一调用
- server 用 requestId 去重：相同 requestId 的写工具只执行一次，重试命中回放首次结果

| | 已有计划（重放层） | 本计划（调用层） |
|---|---|---|
| 防什么 | PTC/LDW 不重放非幂等 | LLMRetry 不重复执行写工具 |
| 层 | Sherry 优化层（重放） | 工具调用层（LLMRetry） |
| 机制 | 账本 + 重放降级 | requestId 去重表 |

两层互补，不重叠。

---

## 1. 设计

### 1.1 requestId 幂等键

写工具（声明 `idempotent: false` 或 `side_effect: write` 的工具）调用时带 `requestId`：
- LLM 调工具时，LLMRetry 中间件给每次调用生成 `requestId`（或 LLM 自己传）
- 工具执行前查去重表：`SHA256(session_id + tool_name + requestId)` 命中 → 回放首次结果（`IDEMPOTENT_REPLAY`），不重复执行
- 未命中 → 正常执行 + 写去重表（记录结果摘要 + 时间戳）

### 1.2 去重表

```sql
CREATE TABLE IF NOT EXISTS tool_call_idempotency (
    key TEXT PRIMARY KEY,           -- SHA256(session_id + tool_name + request_id)
    session_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    request_id TEXT NOT NULL,
    result_summary TEXT,            -- 结果摘要（截断）
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL     -- 24h 过期
);
CREATE INDEX idx_idempotency_expires ON tool_call_idempotency(expires_at);
```

- SQLite（复用 MesMemory DB 或独立）
- 24h 过期（自动清理过期行）
- key = SHA256(session_id + tool_name + request_id)，防跨 session/跨工具碰撞

### 1.3 哪些工具走幂等

- 工具声明 `idempotent: false` 或 `side_effect: write`/`side_effect: non_idempotent` 的 → 走 requestId 去重
- `idempotent: true`（读工具/纯函数）→ 不走去重（重试无副作用）
- 现有工具 metadata 加 `idempotent` 字段（参照 Madorin 的 `[Tool(Idempotent=true/false)]`）

### 1.4 LLMRetry 中间件集成

`LLMRetryMiddleware`（现有）重试时：
- 给重试的工具调用带相同 `requestId`（首次生成的）
- 工具执行层查去重表 → 命中回放 / 未命中执行

### 1.5 其他安全模式（一并采纳）

| 模式 | 来源 | 适用 |
|---|---|---|
| snake_case 工具命名 | MCP 实战 | 工具名用 snake_case 不用 `.`（兼容性） |
| 列表精简 + 详情完整双层 | MCP 实战 | 工具返回列表只给摘要（pageSize 上限），详情单独查 |
| filter 子对象收纳 | MCP 实战 | 工具参数的 filter 收进 `filter` 子对象，不污染顶层 schema |
| 写工具强制 requestId | MCP 实战 | 写工具必须有 requestId 参数（无 requestId 拒绝） |

---

## 2. 实施

| 步骤 | 内容 | 依赖 |
|---|---|---|
| 1 | 去重表 schema + CRUD（`tool_call_idempotency.py`） | 无 |
| 2 | 工具 metadata 加 `idempotent` 字段 + 现有工具标注 | 无 |
| 3 | 工具执行层（wrap_tool_call）加去重：写工具执行前查表/命中回放/未命中执行+写表 | 步骤 1、2 |
| 4 | LLMRetry 中间件：重试时带相同 requestId | 步骤 3 |
| 5 | 写工具强制 requestId 参数（无 requestId 拒绝） | 步骤 2 |
| 6 | 过期清理（启动时 + 定时扫 expires_at） | 步骤 1 |

---

## 3. 变更清单

| 文件 | 变更 |
|---|---|
| `agent/tools/pub_base/tool_call_idempotency.py` | **新建**：去重表 + 查/写/回放 |
| `agent/tools/pub_base/scaffolding.py`（wrap_tool_call） | 写工具执行前去重检查 |
| `agent/middlewares/llm_retry/` | 重试时带相同 requestId |
| 工具 metadata | 加 `idempotent` 字段 + 现有写工具标注 |
| 现有写工具（terminal/write_file/patch_file/skill_manage/...） | 标注 `idempotent`（file 写=幂等可重试 vs terminal=非幂等） |

**不改**：首次执行行为（去重只影响重试路径）、读工具（不走去重）。

---

## 4. 风险与缓解

| 风险 | 缓解 |
|---|---|
| requestId 生成/传递链路断（LLM 不传） | LLMRetry 自动生成 requestId（不依赖 LLM 传）；写工具无 requestId 拒绝 |
| 去重表膨胀 | 24h 过期 + 启动清理 + 定时扫 |
| 误去重（不同意图相同 requestId） | key 含 session_id + tool_name + requestId，三要素才碰撞 |
| 回放结果与首次不完全一致 | 存结果摘要（截断），不是完整结果；回放是"防重复执行"不是"保证一致" |

### Kill criteria

- 若去重表性能影响工具调用延迟（每次写工具查表），改为"只对声明 non_idempotent 的工具查表"（窄范围）
- 若 LLM 不配合传 requestId 导致去重失效，退为"LLMRetry 自动生成 requestId 注入 tool call args"（不依赖 LLM）

---

## 5. 验收

- 写工具（non_idempotent）首次执行 → 写去重表
- LLMRetry 重试同一调用（相同 requestId）→ 命中去重表 → 回放首次结果（`IDEMPOTENT_REPLAY`），不重复执行
- 读工具（idempotent）不走去重
- 写工具无 requestId → 拒绝
- 去重表 24h 过期自动清理
- 现有首次执行行为不变（回归）

---

## 6. 备注

- 与已有 `计划_非幂等动作防重放防重试` 互补：那个管重放层（PTC/LDW），本计划管调用层（LLMRetry）
- 工具 `idempotent` 标注也服务三通道插件体系（插件声明 `Idempotent=true/false`）
- snake_case 命名 + 双层列表 + filter 子对象是通用工具设计模式，一并采纳
