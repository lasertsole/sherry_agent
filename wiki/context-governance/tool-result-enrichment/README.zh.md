# 🔧 工具结果增强队列

[English](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> [Context Governance](../README.zh.md) 的一部分：基于注册表的队列，在每次工具调用后往 `ToolMessage` 的 content 里追加提示信息。模仿 [system-reminder 目录](../system-reminder/README.zh.md)，但触发时机是每次工具调用而非每轮。

---

增强中间件（`ToolResultEnrichmentMiddleware`，即 result-appendix 结果附录层）是 `wrap_tool_call` 链的**最外层**——注册在 `ToolGuardrails` 之前，看到的是所有其他中间件处理后的最终结果。handler 返回后，它跑所有注册的注入器，收集非 `None` 输出，注入到 `ToolMessage.content`。

**顺序契约：**正因为处于最外层，增强在返回路径上最后执行——必须等 `ContextEvictionMiddleware` 已完成驱逐或切片之后才生效。提示追加到模型可见的（可能已被驱逐的）`ToolMessage` 上，绝不写进原始文本；驱逐层看不到、也不会改写追加的提示，已被驱逐的结果以其预览形态接收增强。

## JSON 感知双模式注入

中间件自动检测 content 类型，选择注入方式：

| content 类型 | 注入方式 | 例子 |
|---|---|---|
| 合法 JSON dict | 在 JSON 对象内加 `"advisory"` 字段，重新序列化 | `read_file`: `{"content": "...", "truncated": false}` → `{"content": "...", "truncated": false, "advisory": "[Advisory: ...]"}` |
| 纯文本 | 末尾追加 `\n\n{suffix}` | `terminal`: `"Exit code: 0\noutput..."` → `"Exit code: 0\noutput...\n\n[Advisory: ...]"` |

**为什么 JSON 字段注入安全**：`read_file` 返回 `json.dumps({"content": ..., "truncated": bool, ...})`。eviction / truncation 管线里三处对其做 `json.loads`（`eviction.py`、`target_truncation.py`）——只读 `content` 和 `truncated` key，新增的 `"advisory"` 字段被忽略。不需要工具声明或排除集——`try json.loads` 自动检测一切。

**error 结果跳过**：`status="error"` 的 ToolMessage（ToolGuardrails BLOCK/HALT、HITL 拒绝等）原样返回——失败的工具调用不接收提示。

## 注册的注入器

目录通过 `ToolResultInjector` dataclass 注册注入器（`agent/middlewares/tool_result_enrichment/core.py`）。每个声明 `injector_id`、`builder(tool_name, tool_args, result, session_id) → str | None`、`priority`、可选的 `tools_filter` 和 `only_if_changed`。

| 注入器 | 优先级 | `only_if_changed` | 适用工具 | 注入内容 |
|---|---|---|---|---|
| `hook_context` | 60 | `True` | 所有工具 | `PostToolUse` hook 的 `additionalContext`（`UserHooksMiddleware`），由 `_dispatch` stash、注入器 drain。 |

**优先级排序**：按优先级升序排列——低优先级先追加（在文本上方），高优先级后追加（在文本底部，最靠近模型注意力）。

## UserHooksMiddleware 接线

`UserHooksMiddleware._dispatch` 从每个 hook 的 stdout JSON 解析 `additionalContext`（`HookOutcome.additional_context`）。Step 1（只读事件）中此字段被解析但丢弃。增强队列接上它：`_dispatch` 通过 `stash_hook_context(session_id, text)` 暂存文本，`hook_context` 注入器 drain 暂存并作为 `[Hook context]: ...` 追加到工具结果。hook 不能阻断、替换或改写结果——只能追加文本。

## 与 system-reminder 目录的对比

| | reminder 目录 | 工具结果增强 |
|---|---|---|
| 触发时机 | 每轮首个模型调用（`before_model`） | 每次工具调用返回（`wrap_tool_call`） |
| 载体 | `HumanMessage`（独立消息） | `ToolMessage.content`（字段或追加） |
| 优先级排序 | budget 分配（丢弃低优先级） | 追加顺序（低优先级先，高优先级后） |
| 关系 | 正交——共存 | 正交——共存 |

提示搭载在模型本来就要处理的工具结果上——不产生独立消息，不会抢夺注意力。JSON 工具（`read_file`）的提示是模型本来就要解析的 JSON 里的一个字段；纯文本工具的提示是 content 末尾的后缀。两种情况下模型的注意力都在工具的真实输出上，提示是附带看到的。
