# 计划：browser 工具合并（8→1，action 模式）

- 建档日期：2026-10-23
- 状态：**计划（未开工）**
- **文档范围**：把 8 个 `browser_*` 工具合并为 `browser(action=navigate/snapshot/click/type/press/scroll/screenshot/evaluate)`。低风险——1 个 redaction prefix bug + manager 错误消息。
- 关联：`调研_工具合并影响面分析.md` §7、`计划_工具合并推荐方案.md` §1.2

---

## 1. 当前状态

### 1.1 八个工具

| 工具 | 文件 | 参数（除共享的 page/session_id） | metadata |
|---|---|---|---|
| `browser_navigate` | `browser/tools/browser_navigate.py` | `url: str` | `MAIN_ONLY={"scope":"main_only","idempotent":False}` |
| `browser_snapshot` | `browser/tools/browser_snapshot.py` | `max_elements: int\|None` | `MAIN_ONLY` |
| `browser_click` | `browser/tools/browser_click.py` | `ref: str\|None`, `x: float\|None`, `y: float\|None`, `button="left"`, `double=False` | `MAIN_ONLY` |
| `browser_type` | `browser/tools/browser_type.py` | `text: str`, `ref: str\|None`, `submit=False` | `MAIN_ONLY` |
| `browser_press` | `browser/tools/browser_press.py` | `keys: str` | `MAIN_ONLY` |
| `browser_scroll` | `browser/tools/browser_scroll.py` | `delta_y=0`, `delta_x=0` | `MAIN_ONLY` |
| `browser_screenshot` | `browser/tools/browser_screenshot.py` | `full_page=False` | `MAIN_ONLY` |
| `browser_evaluate` | `browser/tools/browser_evaluate.py` | `expression: str` | `MAIN_ONLY`（条件注册：`allow_evaluate` 时才加入） |

全部继承 `BrowserTool(BaseTool)`（async-only），共享 `BrowserPageInput` 基类（`page` + `session_id`）。

### 1.2 引用点（全量）

| 文件 | 行 | 引用内容 | 合并后 | 影响 |
|---|---|---|---|---|
| **`config/features/agent_side/redaction.py`** | 45 | `tool_output_prefixes: ("mcp_","browser_")` — **前缀匹配** | 改为 `("mcp_","browser")` | 🔴 **必须修**——`"browser"` 不匹配 `"browser_"` 前缀 |
| `agent/tools/catalog.py` | 83-92 | `TOOL_GROUPS["browser"]` 8 个名 | 改为 `("browser",)` | 注册 |
| `agent/tools/browser/tools/__init__.py` | 20-27 | 导入 8 个类 | 改为导入 merged tool | 注册 |
| `agent/tools/browser/__init__.py` | 23-28 | docstring | 更新 | 文本 |
| `server/service/browser_manager.py` | 321, 644, 816 | 错误消息提到 `browser_navigate`/`browser_click`/`browser_evaluate` | 更新 | 文本 |
| `agent/security/untrusted_wrapper.py` | — | 不含 browser 工具 | 不变 | ✅ |
| `agent/tools/parallel_safety.py` | — | 不含 browser 工具（用 metadata） | 不变 | ✅ |
| `config/features/agent_side/plan_mode.py` | — | 不含 browser 工具 | 不变 | ✅ |
| 客户端 | — | 不引用 browser 工具名 | 不变 | ✅ |

---

## 2. 合并方案

### 2.1 合并后的工具

```python
# agent/tools/browser/tools/browser_merged.py

class BrowserActionSchema(BrowserPageInput):
    action: Literal["navigate","snapshot","click","type","press","scroll","screenshot","evaluate"]
    url: str | None = None              # navigate
    max_elements: int | None = None     # snapshot
    ref: str | None = None              # click/type
    x: float | None = None              # click
    y: float | None = None              # click
    button: str = "left"                # click
    double: bool = False                # click
    text: str | None = None             # type
    submit: bool = False                # type
    keys: str | None = None             # press
    delta_y: int = 0                    # scroll
    delta_x: int = 0                    # scroll
    full_page: bool = False             # screenshot
    expression: str | None = None       # evaluate

class BrowserMergedTool(BrowserTool):
    name: str = "browser"
    description: str = "..."
    args_schema: type = BrowserActionSchema
    metadata: dict = MAIN_ONLY

    async def _arun(self, action, page=None, session_id="", **kwargs):
        # action-aware 校验
        err = _validate_browser_action(action, kwargs)
        if err:
            return err
        if action == "navigate":
            return await _do_navigate(page, session_id, kwargs["url"])
        elif action == "snapshot":
            return await _do_snapshot(page, session_id, kwargs.get("max_elements"))
        # ... 8 个分支
```

### 2.1a action-aware 校验层

合并后所有参数都是 optional——Pydantic 不拦"缺必填参数"。dispatch 前加校验表，缺失时返回**结构化错误 + 该 action 的完整参数规则**：

```python
_BROWSER_ACTION_RULES: dict[str, dict] = {
    "navigate":   {"required": {"url": "str — absolute URL to open"},
                   "optional": {}},
    "snapshot":   {"required": {},
                   "optional": {"max_elements": "int — max interactive elements to list (1-1000)"}},
    "click":      {"required": {},  # ref 或 x/y 二选一
                   "optional": {"ref": "str — snapshot element ref (e.g. 'e5')",
                                "x": "float — viewport x coordinate",
                                "y": "float — viewport y coordinate",
                                "button": "str — mouse button (default 'left')",
                                "double": "bool — double click (default false)"}},
    "type":       {"required": {"text": "str — text to type"},
                   "optional": {"ref": "str — snapshot element ref to type into",
                                "submit": "bool — press Enter after typing (default false)"}},
    "press":      {"required": {"keys": "str — named key (e.g. 'Enter', 'Tab', 'Escape')"},
                   "optional": {}},
    "scroll":     {"required": {},
                   "optional": {"delta_y": "int — vertical scroll (positive = down, default 0)",
                                "delta_x": "int — horizontal scroll (default 0)"}},
    "screenshot": {"required": {},
                   "optional": {"full_page": "bool — capture full scrollable page (default false)"}},
    "evaluate":   {"required": {"expression": "str — JavaScript expression to run"},
                   "optional": {}},
}

def _validate_browser_action(action: str, kwargs: dict) -> str | None:
    """校验必填参数 + 特殊规则，返回给模型的错误消息（含完整参数规则）。"""
    rules = _BROWSER_ACTION_RULES.get(action)
    if rules is None:
        return (f"Unknown action '{action}'. "
                f"Available: {', '.join(sorted(_BROWSER_ACTION_RULES.keys()))}")
    # 检查必填
    missing = [p for p in rules["required"] if not kwargs.get(p)]
    # click 的特殊规则：ref 或 x/y 至少有一个
    if action == "click" and not kwargs.get("ref") and not (kwargs.get("x") is not None and kwargs.get("y") is not None):
        missing.append("ref OR (x, y)")
    if not missing:
        return None  # 校验通过
    # 构建错误反馈
    parts = [f"Error: action='{action}' has incorrect parameters."]
    parts.append(f"Missing required: {', '.join(missing)}")
    if rules["required"]:
        parts.append("Required: " + ", ".join(f"{k} ({v})" for k, v in rules["required"].items()))
    if rules["optional"]:
        parts.append("Optional: " + ", ".join(f"{k} ({v})" for k, v in rules["optional"].items()))
    req_example = ", ".join(f'{p}=<value>' for p in (missing if missing else list(rules["required"])))
    parts.append(f'Example: browser(action="{action}"{", " + req_example if req_example else ""})')
    return " ".join(parts)
```

**模型看到的错误反馈示例**：

```
模型调用: browser(action="navigate")  # 缺 url

返回: Error: action='navigate' has incorrect parameters.
      Missing required: url
      Required: url (str — absolute URL to open)
      Example: browser(action="navigate", url=<value>)

模型下一轮: browser(action="navigate", url="https://example.com")  ✅
```

```
模型调用: browser(action="click")  # 缺 ref 和 x/y

返回: Error: action='click' has incorrect parameters.
      Missing required: ref OR (x, y)
      Optional: ref (str — snapshot element ref (e.g. 'e5')), x (float — viewport x coordinate),
                y (float — viewport y coordinate), button (str — mouse button (default 'left')),
                double (bool — double click (default false))
      Example: browser(action="click", ref=<value>)

模型下一轮: browser(action="click", ref="e5")  ✅
```

```
模型调用: browser(action="jump")

返回: Unknown action 'jump'. Available: click, evaluate, navigate, press, screenshot, scroll, snapshot, type

模型下一轮: browser(action="navigate", url="...")  ✅
```
        elif action == "evaluate":
            if not BROWSER_AGENT["allow_evaluate"]:
                return "Error: browser_evaluate is disabled..."
            return await _do_evaluate(page, session_id, kwargs["expression"])
```

每个 `_do_xxx` 函数委托给原 8 个工具类的 `_arun` 逻辑（原类保留为内部实现，不删除）。

### 2.2 `browser_evaluate` 的条件注册 → action 级检查

当前 `browser_evaluate` 在 `BROWSER_AGENT["allow_evaluate"]=true` 时才注册（`build_browser_tools` 里条件 append）。合并后改为 merged tool 内 action 级运行时检查——`action=="evaluate"` 时检查 `allow_evaluate`，否则返回错误。语义等价。

### 2.3 feature gate 不变

`build_browser_tools()` 在 `BROWSER_AGENT["enabled"]=false` 时返回 `[]`——不变。

### 2.4 redaction prefix 修复

```python
# config/features/agent_side/redaction.py:45
# 改动前:
tool_output_prefixes: ("mcp_", "browser_")
# 改动后:
tool_output_prefixes: ("mcp_", "browser")  # 精确名匹配（"browser" 不含下划线前缀）
```

### 2.5 改动文件清单

| 文件 | 改动 |
|---|---|
| `agent/tools/browser/tools/browser_merged.py` | 新建——merged tool + schema + handler |
| `agent/tools/browser/tools/__init__.py` | `build_browser_tools()` 返回 merged tool |
| `agent/tools/browser/__init__.py` | docstring 更新 |
| `agent/tools/catalog.py` | `TOOL_GROUPS["browser"]` 8 个名 → `("browser",)` |
| `config/features/agent_side/redaction.py` | `tool_output_prefixes` 改 `"browser_"` → `"browser"` |
| `server/service/browser_manager.py` | 错误消息更新（`browser_navigate` → `browser(action=navigate)`） |
| 原 8 个 `browser_*.py` 文件 | **保留**——作为 merged tool 的 handler 实现，不删除 |

---

## 3. 测试集（验证功能无影响）

### 3.1 现有测试（必须全部通过）

| 测试文件 | 验证内容 | 合并后改动 |
|---|---|---|
| `tests/agent/tools/test_browser_tools.py` | 8 个工具的 mock 调用 + 参数验证 | 调用方式从 `browser_navigate._arun(url=...)` 改为 `browser._arun(action="navigate", url=...)` |
| `tests/server/service/test_browser_manager.py` | BrowserManager 的 navigate/click/screenshot/evaluate | 不变（测的是 manager 不是工具） |
| `tests/server/test_browser_endpoints.py` | HTTP 端点 | 不变（端点签名不变） |
| `tests/server/test_browser_ws.py` | WS 通道 | 不变 |
| `tests/agent/security/test_untrusted_wrapper.py:37` | untrusted wrapper 分类 | 不含 browser——不变 |
| `tests/docs/test_threat_model_claims.py:94` | `is_untrusted_tool("browser_snapshot")` | 改为 `is_untrusted_tool("browser")` |

### 3.2 新增测试

| 测试 | 验证内容 |
|---|---|
| `test_browser_action_navigate` | `action="navigate", url="..."` 返回与原 `browser_navigate` 相同 |
| `test_browser_action_snapshot` | `action="snapshot"` 返回与原 `browser_snapshot` 相同 |
| `test_browser_action_click_by_ref` | `action="click", ref="e5"` 返回与原 `browser_click` 相同 |
| `test_browser_action_click_by_xy` | `action="click", x=100, y=200` |
| `test_browser_action_type` | `action="type", text="hello", ref="e3"` |
| `test_browser_action_press` | `action="press", keys="Enter"` |
| `test_browser_action_scroll` | `action="scroll", delta_y=500` |
| `test_browser_action_screenshot` | `action="screenshot"` |
| `test_browser_action_screenshot_full` | `action="screenshot", full_page=True` |
| `test_browser_action_evaluate_allowed` | `allow_evaluate=True` + `action="evaluate"` → 正常执行 |
| `test_browser_action_evaluate_blocked` | `allow_evaluate=False` + `action="evaluate"` → 错误消息 |
| `test_browser_metadata_main_only` | `metadata["scope"]=="main_only"` |
| `test_browser_in_catalog` | catalog 返回 `browser`，组 `browser`，`required=False` |
| `test_browser_old_names_absent` | 8 个旧名不在 catalog / `build_main_tools()` 输出中 |
| `test_browser_redaction_still_applies` | `browser` 工具的输出经过 redaction 包装（prefix 匹配生效） |
| `test_browser_feature_gate` | `BROWSER_AGENT["enabled"]=false` → `build_browser_tools()` 返回 `[]` |

---

## 4. 评判标准（功能无影响）

| 标准 | 验收方式 | 通过条件 |
|---|---|---|
| **S1：navigate 结果不变** | mock `BrowserManager.navigate` → `action="navigate", url=X` | 返回值与原 `browser_navigate._arun(url=X)` 完全相同 |
| **S2：snapshot 结果不变** | mock → `action="snapshot"` | 同上 |
| **S3：click 结果不变** | mock → `action="click", ref="e5"` 和 `action="click", x=1, y=2` | 两种调用方式（ref / xy）都与原 `browser_click` 结果相同 |
| **S4：type/press/scroll/screenshot 结果不变** | 逐个 mock 验证 | 与原工具结果相同 |
| **S5：evaluate 条件检查不变** | `allow_evaluate=False` → `action="evaluate"` 返回错误 | 错误消息与原 `browser_evaluate` disabled 时一致 |
| **S6：redaction 保护生效** | 检查 `browser` 工具输出是否经过 `redaction` 包装 | `tool_output_prefixes` 里的 `"browser"` 精确匹配生效（修复后） |
| **S7：feature gate 不变** | `BROWSER_AGENT["enabled"]=False` | `build_browser_tools()` 返回 `[]`；`build_main_tools()` 不含 `browser` |
| **S8：作用域不变** | `metadata["scope"]` | `== "main_only"` |
| **S9：catalog 正确** | `tool_catalog()` | 含 `browser`，组 `browser`，`required=False`；不含 8 个旧名 |
| **S10：HTTP 端点不变** | `GET /browser/status`、`POST /browser/page` 等 | 端点签名和返回不变（后端内部用 `BrowserManager`，不经过工具） |
| **S11：现有测试全过** | `pytest tests/agent/tools/test_browser_tools.py tests/server/test_browser_endpoints.py tests/server/test_browser_ws.py -q` | 全 pass |
| **S12：untrusted wrapper 不变** | browser 工具不经过 untrusted wrapper（与原一致——browser 不在 `UNTRUSTED_TOOL_NAMES`） | `is_untrusted_tool("browser")` 返回 `False` |
