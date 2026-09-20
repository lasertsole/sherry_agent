# DeepAgents 值得借鉴的防护能力 — 具体实现方案

> 基于 `PROTECTION_COMPARISON.md` 对比报告，筛选 DeepAgents 中 Sherry 可落地的防护能力，给出具体实现方案。
> 优先级：P1(增强体验) → P2(长期优化)
>
> **未执行项清单**：P1-3 模型感知摘要默认值、P1-6 中间件脚手架保护、P1-8 威胁模型文档、P2-1 ripgrep 双重超时看门狗（已完成项见 git 历史）。
>
> **已评估不落地**：P1-4 增量检查点优化（`aclean_old_checkpoints` 每线程只留最新，检查点存储已是 O(N)；`DeltaChannel` 与 keep-latest 剪枝不兼容，实测静默丢状态）、P1-5 消息增量缩减器（标准 `add_messages` 已覆盖去重/墓碑/重置，自定义 reducer 会破坏 P1-9 同 id 替换语义）。
>
> **已落地**：P1-7 多模态内容清理（每请求能力擦洗 + 按块部分剥离）。落点与提案不同——未新建 `agent/middlewares/multimodal_scrub.py`，实现为 `MultimodalProcessor.wrap_model_call` + `agent/middlewares/media_pipeline/scrub.py`（每请求、request-only、按块部分剥离；state/checkpointer/MesMemory 不动）。

---

## 目录

1. [P1-3：模型感知摘要默认值](#p1-3模型感知摘要默认值)
2. [P1-6：中间件脚手架保护](#p1-6中间件脚手架保护)
3. [P1-8：威胁模型文档](#p1-8威胁模型文档)
4. [P2-1：ripgrep 双重超时看门狗](#p2-1ripgrep-双重超时看门狗)
5. [实施优先级与依赖关系总览](#实施优先级与依赖关系总览)

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

## 实施优先级与依赖关系总览

- **独立实施**：P1-3 模型感知摘要默认值、P1-6 中间件脚手架保护、P1-8 威胁模型文档
- **已落地**：P1-7 多模态内容清理（见头部"已落地"行；落点为 `MultimodalProcessor.wrap_model_call` + `agent/middlewares/media_pipeline/scrub.py`，非提案中的 `agent/middlewares/multimodal_scrub.py`）
- **已评估不落地**：P1-4 增量检查点优化（前提被现有 `aclean_old_checkpoints` 剪枝消除，DeltaChannel+现剪枝会静默丢状态）、P1-5 消息增量缩减器（标准 `add_messages` 已覆盖，且样例语义会破坏 P1-9）
- **增量改进**：P2-1 ripgrep 双重超时看门狗（疑似不适用，见小节）
