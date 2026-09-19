# DeepAgents 值得借鉴的防护能力 — 具体实现方案

> 基于 `PROTECTION_COMPARISON.md` 对比报告，筛选 DeepAgents 中 Sherry 可落地的防护能力，给出具体实现方案。
> 优先级：P1(增强体验) → P2(长期优化)
>
> **未执行项清单**：本文件只保留未执行项；已完成项见 git 历史。

---

## 目录

1. [P1-3：模型感知摘要默认值](#p1-3模型感知摘要默认值)
2. [P1-4：增量检查点优化 (DeltaChannel)](#p1-4增量检查点优化)
3. [P1-5：消息增量缩减器 (去重+墓碑)](#p1-5消息增量缩减器)
4. [P1-6：中间件脚手架保护](#p1-6中间件脚手架保护)
5. [P1-7：多模态内容清理](#p1-7多模态内容清理)
6. [P1-8：威胁模型文档](#p1-8威胁模型文档)
7. [P2-1：ripgrep 双重超时看门狗](#p2-1ripgrep-双重超时看门狗)
8. [P2-2：持久化工具审批策略 (字节修订 CAS)](#p2-2持久化工具审批策略-字节修订-cas)
9. [实施优先级与依赖关系总览](#实施优先级与依赖关系总览)

---

## P1-3：模型感知摘要默认值

### 问题

Sherry 的摘要触发阈值需要手动配置，不同模型（128K vs 32K）需要不同阈值，容易配错。

### DeepAgents 做法

`compute_summarization_defaults()` 从模型 profile 的 `max_input_tokens` 自动计算触发(85%)和保留(10%)阈值。

### 具体实现方案

#### 文件清单

| 文件                                          | 修改类型 | 说明             |
| --------------------------------------------- | -------- | ---------------- |
| `config/features/agent_side/summarization.py` | 修改     | 添加自动计算函数 |

#### 实现代码

```python
def compute_summarization_defaults(max_input_tokens: int) -> dict:
    """根据模型 max_input_tokens 自动计算摘要阈值。

    Args:
        max_input_tokens: 模型的最大输入 token 数

    Returns:
        包含 trigger_threshold, keep_threshold, max_output_budget 的字典
    """
    trigger = int(max_input_tokens * 0.85)
    keep = int(max_input_tokens * 0.10)
    # 预留输出 token + 5% 余量
    output_budget = int(max_input_tokens * 0.15)
    return {
        "trigger_threshold": trigger,
        "keep_threshold": keep,
        "max_output_budget": output_budget,
    }
```

在 `agent/core.py` 或 `server/__main__.py` 启动时，从 `MAIN_LLM` 的 model profile 获取 `max_input_tokens`，调用此函数设置 `SUMMARIZATION` 的默认值。

---

## P1-4：增量检查点优化

> **评估结论（2026-09-19）：未落地（评估后判定不安全且无收益）。**
>
> 实测（`N=200` 消息，项目真实 `ThreadSafeAsyncSqliteSaver`，含 1 条初始 HumanMessage）：
>
> | 方案 | checkpoints | writes | live bytes | 剪枝后状态 |
> | --- | ---: | ---: | ---: | --- |
> | 标准 `add_messages`，不剪枝（计划所述 O(N²) 基线） | 603 | 603 | 22,728,481 | 完好（402 条） |
> | 标准 `add_messages` + 现有 `aclean_old_checkpoints`（每线程只留最新） | 1 | 0 | 74,717 | 完好（402 条） |
> | `DeltaChannel(snapshot_frequency=50)` + 现有剪枝 | 1 | 0 | 502 | **重建为空（0 条，静默丢状态）** |
>
> 结论：Sherry 每次 `built_agent()` 都调用 `aclean_old_checkpoints()`（`agent/core.py:166`），每线程仅保留最新检查点，检查点存储已是 O(N)；**计划所述"默认 MessagesState 导致 O(N²)"的前提在本项目中不成立**（生产库实测：38 线程仅 38 个检查点 / 156 KB 有效载荷，30 MB 文件大小是 `auto_vacuum=0` 的空闲页膨胀，与检查点数量无关）。LangGraph 明确文档化：`prune`/keep-latest 会切断 `DeltaChannel` 的祖先写入回放，使 delta 通道静默重建为空（`langgraph/checkpoint/base/__init__.py:387-413` 的 `prune` warning；同一机制即上表第 3 行）。要让 DeltaChannel 安全，必须保留祖先链（磁盘反而增大）或强制快照（等价于现状，外加 beta API 复杂度）。
>
> API 本身可用：`langgraph.channels.delta.DeltaChannel` 存在于已安装的 `langgraph 1.2.5`；`AsyncSqliteSaver.aget_delta_channel_history`（`langgraph-checkpoint-sqlite 3.1.1`）与项目 `ThreadSafeAsyncSqliteSaver.get_delta_channel_history`（`agent/checkpointer/thread_safe_checkpointer.py:270`）均已实现；旧全量快照格式可由 `DeltaChannel.from_checkpoint` 直接读取（`langgraph/channels/delta.py:118-137`，纯值迁移路径）。**阻塞点不是 LangGraph 的 channel API，而是本项目的激进剪枝策略。** `DeltaChannel` 在 1.2.5 仍为 beta，磁盘格式可能变化（`delta.py:29-36`）。
>
> 因此不实现；`messages` 通道保持标准 `add_messages` 快照，由 `tests/agent/core/test_state_messages_reducer.py::TestStateSchemaChannelChoice` 锁定，防止无配套剪枝改造的 DeltaChannel 迁移。

### 问题

Sherry 使用 LangGraph 的默认 MessagesState，长对话中检查点增长为 O(N²)（每轮保存全部消息的快照），导致 SQLite 膨胀和恢复变慢。

### DeepAgents 做法

`DeltaChannel(snapshot_frequency=50)` 每50条消息才做一次全量快照，中间只存增量，将增长降为 O(N)。

### 具体实现方案

#### 文件清单

| 文件                                    | 修改类型 | 说明              |
| --------------------------------------- | -------- | ----------------- |
| `agent/core.py`                         | 修改     | 替换状态 schema   |
| `agent/middlewares/checkpoint_delta.py` | 新建     | DeltaChannel 实现 |

#### 注意事项

- 此方案需要深入 LangGraph 的 reducer 机制
- 需确保 Sherry 的 `CompactionLock` 和 checkpointer 兼容增量格式
- 建议先做 P0 项，此为长期优化
- 可直接参考 DeepAgents 的 `_messages_reducer.py` 实现

---

## P1-5：消息增量缩减器

> **评估结论（2026-09-19）：已被 LangGraph 标准能力覆盖，不重复造轮子（不新增自定义 reducer）。**
>
> 标准 `add_messages`（`langgraph/graph/message.py:60`，`langgraph 1.2.5`）已提供计划要求的全部能力：按 id 去重（同 id **替换**，非跳过）、`RemoveMessage` 墓碑删除、`RemoveMessage(REMOVE_ALL_MESSAGES)` 全量重置、缺失 id 自动 UUID、`BaseMessageChunk` 归一。
>
> 计划样例 `messages_delta_reducer` 是重复实现，且其"同 id 跳过"语义**与 P1-9 原地打标直接冲突**：P1-9（`ContextEvictionMiddleware`）用同 id `model_copy` 更新超大 HumanMessage，依赖标准 reducer 的**替换**语义（`model_copy` 保留 id，替换后标签生效）；若改成跳过，重复计数不变但标签被丢弃。`ToolCallNormalize` 与 `Summarization` 的 `[RemoveMessage(REMOVE_ALL_MESSAGES), *rebuilt]` 全列表替换同样依赖标准能力（计划样例的 `{"__reset__": True}` 哨兵是非标准写法，无法被现有代码触发）。
>
> 真实"重复注入/压缩残留"痛点不在 reducer：子代理完成载体在 `announce` 层已有幂等（`build_idempotency_key(run_id, generation)` + 内容镜像去重，`agent/tools/subagent/announce/delivery.py`）与 steering 队列的 `CONSUMED` 恰好一次语义（`agent/tools/subagent/announce/steering_queue.py`）；且载体消息无 `id`（`build_completion_message` / `_rebuild_message` 不设 id），由 reducer 在合并时才赋 UUID，reducer 无法按 id 去重内容重复。若确需更强幂等，修复点在注入层（给载体稳定 id），而非状态层。
>
> 行为由 `tests/agent/core/test_state_messages_reducer.py` 全量锁定：标准契约（替换/墓碑/重置/自动 id）、P1-9 同 id 替换、`REMOVE_ALL_MESSAGES` 全列表替换。

### 问题

Sherry 的消息列表可能因子代理完成消息重复注入或压缩后残留而产生重复消息。

### DeepAgents 做法

`_messages_delta_reducer()` 支持 ID 去重、`RemoveMessage` 墓碑和全量重置。

### 具体实现方案

#### 文件清单

| 文件            | 修改类型 | 说明                    |
| --------------- | -------- | ----------------------- |
| `agent/core.py` | 修改     | 自定义 messages reducer |

#### 实现代码

```python
from langchain_core.messages import RemoveMessage

def messages_delta_reducer(existing: list, update: list) -> list:
    """支持增量的消息缩减器。

    - RemoveMessage: 从列表中删除对应ID
    - 普通消息: 按ID去重后追加
    - 特殊指令: 全量替换
    """
    if not update:
        return existing

    # 检查是否为全量替换指令
    if len(update) == 1 and isinstance(update[0], dict) and update[0].get("__reset__"):
        return update[0]["messages"]

    # 构建现有消息ID集合
    existing_ids = {getattr(m, "id", None) for m in existing if hasattr(m, "id")}

    # 处理 RemoveMessage
    to_remove: set[str] = set()
    to_add: list = []
    for msg in update:
        if isinstance(msg, RemoveMessage):
            to_remove.add(msg.id)
        else:
            mid = getattr(msg, "id", None)
            if mid and mid in existing_ids:
                continue  # 去重
            if mid:
                existing_ids.add(mid)
            to_add.append(msg)

    # 过滤已移除的消息
    filtered = [m for m in existing if getattr(m, "id", None) not in to_remove]
    filtered.extend(to_add)
    return filtered
```

---

## P1-6：中间件脚手架保护

### 问题

Sherry 的中间件链没有"必需不可排除"的机制，配置错误可能导致安全中间件被跳过。

### DeepAgents 做法

`_REQUIRED_MIDDLEWARE` 列表标记不可排除的中间件，`_validate_excluded_middleware_config()` 在启动时验证。

### 具体实现方案

#### 文件清单

| 文件                            | 修改类型 | 说明               |
| ------------------------------- | -------- | ------------------ |
| `agent/middlewares/__init__.py` | 修改     | 添加必需中间件标记 |

#### 实现代码

```python
# agent/middlewares/__init__.py

_REQUIRED_MIDDLEWARE = {
    "ToolGuardrails",
    "HITLCore",
    "Summarization",
    "IterationBudget",
}

def validate_required_middleware(active_middleware: list[str]) -> None:
    """启动时验证所有必需中间件已加载。"""
    missing = _REQUIRED_MIDDLEWARE - set(active_middleware)
    if missing:
        raise RuntimeError(
            f"Required middleware missing: {missing}. "
            f"Cannot start agent without safety scaffolding."
        )
```

在 `agent/core.py` 的 `built_agent()` 中调用 `validate_required_middleware()`。

---

## P1-7：多模态内容清理

### 问题

当模型不支持某些内容类型（如视频帧），Sherry 没有清理机制，可能导致 API 调用失败。

### DeepAgents 做法

`_scrub_unsupported_multimodal_content()` 替换模型不支持的内容块为文本占位符。

### 具体实现方案

#### 文件清单

| 文件                                    | 修改类型 | 说明           |
| --------------------------------------- | -------- | -------------- |
| `agent/middlewares/multimodal_scrub.py` | 新建     | 多模态内容清理 |

#### 实现代码

```python
from langchain_core.messages import HumanMessage

def scrub_unsupported_content(
    messages: list,
    supported_types: set[str],
) -> list:
    """替换模型不支持的多模态内容块为文本占位符。"""
    result = []
    for msg in messages:
        if not isinstance(msg, HumanMessage):
            result.append(msg)
            continue
        if not isinstance(msg.content, list):
            result.append(msg)
            continue
        new_content = []
        for block in msg.content:
            block_type = block.get("type") if isinstance(block, dict) else None
            if block_type and block_type in supported_types:
                new_content.append(block)
            else:
                new_content.append({
                    "type": "text",
                    "text": f"[unsupported content type: {block_type}]",
                })
        result.append(HumanMessage(content=new_content))
    return result
```

---

## P1-8：威胁模型文档

### 问题

Sherry 缺少威胁模型文档，安全评审缺少系统性参考。

### DeepAgents 做法

`THREAT_MODEL.md` 文档化信任边界、数据分类和威胁分析。

### 具体实现方案

#### 文件清单

| 文件                   | 修改类型 | 说明         |
| ---------------------- | -------- | ------------ |
| `docs/THREAT_MODEL.md` | 新建     | 威胁模型文档 |

#### 文档结构

```markdown
# Sherry Agent 威胁模型

## 信任边界

1. 用户 ↔ WS 网关
2. 主代理 ↔ 子代理
3. 工具 ↔ 外部系统（终端/文件/网络）
4. LLM 提供商 ↔ Agent
5. MCP 服务器 ↔ Agent

## 数据分类

| 类别 | 示例             | 存储位置                                        |
| ---- | ---------------- | ----------------------------------------------- |
| 敏感 | API keys, tokens | env vars (scrub_env)                            |
| 私密 | 对话历史         | SQLite (WAL)                                    |
| 内部 | 工具结果         | 消息列表 + 驱逐文件（`sessions/{id}/evicted/`） |

## 威胁分析

| 威胁         | 现有防护                  | 差距                                                                                                                                                                                             |
| ------------ | ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 路径遍历     | 三道结构门禁 + O_NOFOLLOW | 已落地（`path_utils.py`）                                                                                                                                                                        |
| 符号链接攻击 | `O_NOFOLLOW` + 循环检测   | 已落地（`agent/tools/pub_base/path_utils.py` 的 `_open_no_follow` / `_raise_if_symlink_loop`，read/write/patch 全走）                                                                            |
| Shell注入    | 正则黑名单                | 已评估并否决（base64+`eval` 对以 shell 语义执行的 `terminal` 无增益，且置于 `_check_dangerous`/`_check_sensitive_file_access` 之前会让明文绕过防线；真实读屏障是 OS 沙箱读遮蔽） |
| 工具结果OOM  | 截断 (head+tail)          | 压缩时截断已落地（`target_truncation.py`等）；工具执行时驱逐已落地（`ContextEvictionMiddleware` 的 `wrap_tool_call` 主动写文件+预览）                                                                  |
| ...          | ...                       | ...                                                                                                                                                                                              |
```

---

## P2-1：ripgrep 双重超时看门狗

**疑似不适用：搜索走 `os.walk` + `fnmatch`，无 ripgrep 进程。** 判据：`agent/tools/file_tools/search_scan.py::bounded_walk` 直接以 `os.walk` 遍历文件树，不 spawn 任何子进程（全模块无 `subprocess` 调用），因此本项没有适用对象；若未来搜索后端引入 ripgrep 子进程，再启用本项。

DeepAgents 参考：`threading.Timer` 看门狗 → SIGTERM → 5s 等待 → SIGKILL → 放弃（`_reap_ripgrep()`），届时在文件搜索工具中添加双重超时清理。

---

## P2-2：持久化工具审批策略

> **状态：已落地（2026-09-19）** — 实现位置：`agent/middlewares/humanInTheLoop/approval_scope.py`（操作员 ContextVar + 无人轮次判定）、`agent/middlewares/humanInTheLoop/approval_store.py`（JSON 持久化 + 字节修订 CAS + 无操作员自动拒绝）、HITL 集成（`approval.py` / `strategies.py` / `core.py`）。存储位置：`SRC_DIR/data/approvals.json`（Sherry 自有运行时数据，非 `.omo`）。测试：`tests/agent/middlewares/humanInTheLoop/test_approval_store.py`、`test_approval_persistence.py`。提交：`a8b70c0e`（store）、`9144f8e7`（HITL 集成）、`688720da`（测试）。

### 问题

Sherry 的 HITL 审批是会话级的，重启后丢失。无操作员场景缺少自动拒绝。

### DeepAgents 做法

`ToolApprovalStore` JSON 文件持久化 + 字节修订 CAS + 操作员范围 ContextVar + 无操作员自动拒绝。

### 具体实现方案

在中期迭代中实现 `ToolApprovalStore`，将审批策略持久化到 `workspace/.approvals.json`，使用乐观锁 CAS 更新。

---

## 实施优先级与依赖关系总览

- **独立实施**：P1-3 模型感知摘要默认值、P1-6 中间件脚手架保护、P1-7 多模态内容清理、P1-8 威胁模型文档
- **已评估不落地**：P1-4 增量检查点优化（前提被现有 `aclean_old_checkpoints` 剪枝消除，DeltaChannel+现剪枝会静默丢状态）、P1-5 消息增量缩减器（标准 `add_messages` 已覆盖，且样例语义会破坏 P1-9）
- **增量改进**：P2-1 ripgrep 双重超时看门狗（疑似不适用，见小节）、P2-2 持久化审批策略（依赖现有 HITL）
