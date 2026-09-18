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

### 问题

Sherry 的 HITL 审批是会话级的，重启后丢失。无操作员场景缺少自动拒绝。

### DeepAgents 做法

`ToolApprovalStore` JSON 文件持久化 + 字节修订 CAS + 操作员范围 ContextVar + 无操作员自动拒绝。

### 具体实现方案

在中期迭代中实现 `ToolApprovalStore`，将审批策略持久化到 `workspace/.approvals.json`，使用乐观锁 CAS 更新。

---

## 实施优先级与依赖关系总览

- **独立实施**：P1-3 模型感知摘要默认值、P1-6 中间件脚手架保护、P1-7 多模态内容清理、P1-8 威胁模型文档
- **长期优化（高复杂度）**：P1-4 增量检查点优化（深入 LangGraph reducer）、P1-5 消息增量缩减器（需修改状态 schema）
- **增量改进**：P2-1 ripgrep 双重超时看门狗（疑似不适用，见小节）、P2-2 持久化审批策略（依赖现有 HITL）
