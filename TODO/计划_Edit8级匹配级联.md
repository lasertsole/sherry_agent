# 计划：patch_file 模糊匹配链补缺口（行号前缀剥离 + \u 转义 + replace_all 保护 + 引号保留）

- 建档日期：2026-10-09
- 状态：**计划（未开工）**
- 触发问题：sherry 的 `patch_file` 已经有 9 级模糊匹配链（`agent/tools/pub_base/text_matcher.py`），但对齐计划 §4.1 写"无匹配降级策略"是**误判**。实际缺的是 4 个具体保护

## 现状：sherry 已有的 9 级

`agent/tools/pub_base/text_matcher.py` 的 `_STRATEGIES`：

```python
_STRATEGIES = [
    ("exact", _strategy_exact),                    # 1. 精确子串
    ("line_trimmed", _strategy_line_trimmed),      # 2. 每行 strip() 后比较
    ("whitespace_normalized", _strategy_whitespace_normalized),  # 3. 空白归一化
    ("indentation_flexible", _strategy_indentation_flexible),    # 4. 剥公共缩进
    ("escape_normalized", _strategy_escape_normalized),          # 5. 反转义 \n \t
    ("trimmed_boundary", _strategy_trimmed_boundary),           # 6. 边界 trim
    ("unicode_normalized", _strategy_unicode_normalized),       # 7. NFKC + 弯引号归一化
    ("block_anchor", _strategy_block_anchor),                   # 8. 首尾精确 + 中间相似度
    ("context_aware", _strategy_context_aware),                # 9. 上下文感知
]
```

已有的唯一性检查（`fuzzy_find_and_replace` line 354）：

```python
if len(matches) > 1 and not replace_all:
    return (content, 0, None,
            "Found N matches. Provide more context or use replace_all=True.")
```

✅ 多个匹配 + 非 replace_all → 拒绝，要求加上下文。

## 缺口 1：行号前缀剥离（LLM 编辑失败最大来源）

### 问题

模型调 `read_file` 看到带行号的输出：

```
10\tfunction foo() {
11\t    return bar;
12\t}
```

模型写 `old_string` 时把行号 `10\t`、`11\t` 一起抄进去了。文件里实际是 `function foo() {`（无行号）。现有 9 级策略**没有一级能剥离行号前缀** → 匹配失败 → 重试 → 大概率又抄行号 → 又失败。

### 修复：加一级 `_strategy_line_number_prefix_stripped`

`agent/tools/pub_base/text_matcher.py` 新增：

```python
import re

def _strip_line_number_prefix(text: str) -> str | None:
    """剥掉每行的行号前缀（\\d+: 或 \\d+\\t）。所有行都必须有前缀。"""
    lines = text.split("\n")
    stripped = []
    for line in lines:
        m = re.match(r"^(\d+): (.*)$", line) or re.match(r"^(\d+)\t(.*)$", line)
        if m:
            stripped.append(m.group(2))
        else:
            return None  # 不是所有行都有前缀 → 不触发这级
    result = "\n".join(stripped)
    return result if result != text else None

def _strategy_line_number_prefix_stripped(content: str, pattern: str) -> list[tuple[int, int]]:
    stripped = _strip_line_number_prefix(pattern)
    if stripped is None:
        return []
    return _strategy_exact(content, stripped)
```

在 `_STRATEGIES` 列表中插入（在 `exact` 之后、`line_trimmed` 之前）：

```python
_STRATEGIES = [
    ("exact", _strategy_exact),
    ("line_number_prefix_stripped", _strategy_line_number_prefix_stripped),  # ← 新增
    ("line_trimmed", _strategy_line_trimmed),
    ...
]
```

## 缺口 2：`\uXXXX` 转义剥离

### 问题

模型写 `it\u2019s`（反斜杠+u+4 位 hex），文件里是 `it's`（弯撇号 U+2019）。现有 `unicode_normalized` 做的是 NFKC 归一化（全角→半角等），不是 `\uXXXX` 反转义。

### 修复：加一级 `_strategy_unicode_escape_normalized`

```python
_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")

def _unescape_unicode(text: str) -> str:
    return _UNICODE_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), text)

def _strategy_unicode_escape_normalized(content: str, pattern: str) -> list[tuple[int, int]]:
    unescaped = _unescape_unicode(pattern)
    if unescaped == pattern:
        return []  # 无 \u 转义 → 这级没用
    return _strategy_exact(content, unescaped)
```

在 `_STRATEGIES` 列表中插入（在 `escape_normalized` 之后）：

```python
_STRATEGIES = [
    ...
    ("escape_normalized", _strategy_escape_normalized),
    ("unicode_escape_normalized", _strategy_unicode_escape_normalized),  # ← 新增
    ...
]
```

## 缺口 3：replace_all 禁用宽泛策略

### 问题

`replace_all=True` 时，现有代码跑全部 9 级策略——包括模糊的 `line_trimmed`、`block_anchor`、`context_aware`。模糊匹配 + 全局替换可能匹配到"只是长得像"的位置 → 批量改错。

ZCode 的做法：`replace_all=True` 时跳过 Level 6-8（`BROAD_MATCHERS`）。

### 修复

```python
BROAD_MATCHERS = frozenset({
    "line_trimmed",
    "whitespace_normalized",
    "indentation_flexible",
    "trimmed_boundary",
    "block_anchor",
    "context_aware",
})

def fuzzy_find_and_replace(content, old_string, new_string, replace_all=False):
    ...
    for name, fn in _STRATEGIES:
        if replace_all and name in BROAD_MATCHERS:
            continue  # ← replace_all 时跳过宽泛策略
        matches = fn(content, old_string)
        ...
```

安全策略（`exact` + `line_number_prefix_stripped` + `escape_normalized` + `unicode_escape_normalized`）是可逆变换——每个匹配确定是模型想改的那处。宽泛策略是有损的——可能匹配错误位置。

## 缺口 4：引号风格保留

### 问题

`unicode_normalized` 策略把弯引号归一化后匹配成功——但替换文本 `new_string` 里的直引号会直接写入弯引号文件，导致文件里引号风格不一致（有的弯、有的直）。

ZCode 的做法：匹配后把 `new_string` 的直引号转回弯引号，保持文件排版风格。

### 修复

在 `fuzzy_find_and_replace` 的替换逻辑里加一步：

```python
def _preserve_quote_style(file_region: str, new_string: str) -> str:
    """匹配后把替换文本的引号转为文件已有的风格。"""
    if "\u201c" in file_region or "\u201d" in file_region:
        # 文件用弯双引号 → 替换文本的直引号转弯
        new_string = _apply_curly_double_quotes(new_string)
    if "\u2018" in file_region or "\u2019" in file_region:
        new_string = _apply_curly_single_quotes(new_string)
    return new_string

def _apply_curly_double_quotes(text: str) -> str:
    result = []
    for i, ch in enumerate(text):
        if ch == '"':
            prev = text[i-1] if i > 0 else " "
            if prev in " \t\n\r([{":
                result.append("\u201c")  # 开引号 "
            else:
                result.append("\u201d")  # 闭引号 "
        else:
            result.append(ch)
    return "".join(result)

def _apply_curly_single_quotes(text: str) -> str:
    result = []
    for i, ch in enumerate(text):
        if ch == "'":
            prev = text[i-1] if i > 0 else " "
            next_ch = text[i+1] if i + 1 < len(text) else " "
            if prev.isalpha() and next_ch.isalpha():
                result.append("\u2019")  # 撇号（it's）
            elif prev in " \t\n\r([{":
                result.append("\u2018")  # 开引号 '
            else:
                result.append("\u2019")  # 闭引号 '
        else:
            result.append(ch)
    return "".join(result)
```

在 `fuzzy_find_and_replace` 的替换循环里调用：

```python
for start, end in sorted_matches:
    if name != "exact":
        adjusted = _reindent(content[start:end], old_string, effective)
        adjusted = _preserve_quote_style(content[start:end], adjusted)  # ← 新增
    else:
        adjusted = effective
    result = result[:start] + adjusted + result[end:]
```

## 改动清单

| 文件 | 改动 | 行数 |
|---|---|---|
| `agent/tools/pub_base/text_matcher.py` | 加 2 级策略 + `BROAD_MATCHERS` + 引号保留 | ~80 |
| `tests/agent/tools/pub_base/test_text_matcher.py` | 新增 4 类缺口测试 | ~100 |

**合计 ~180 行**（不是之前写的 530——sherry 已有 9 级基础，只补缺口）

## 验收

### 行号前缀剥离

- `old_string = "10\tfunction foo() {\n11\t    return bar;\n12\t}"` → 剥离行号后精确匹配
- 不是所有行都有行号前缀 → 不触发这级（跳到下一级）
- 剥离后和原文相同 → 不触发这级

### \uXXXX 转义

- `old_string = "it\u2019s"` → 反转义为 `it's`（U+2019）→ 匹配
- 无 `\u` 转义 → 不触发这级

### replace_all 禁用宽泛策略

- `replace_all=True` + `old_string` 模糊匹配多处 → 只走安全策略（exact + line_number + escape + unicode_escape）
- 模糊策略在 `replace_all=True` 时被跳过

### 引号风格保留

- 文件用弯引号 + `unicode_normalized` 匹配成功 → 替换文本的直引号自动转弯
- `it's` 中的撇号 → 右弯撇号 `'`
- 行首 `"` → 开弯引号 `"`；行中 `"` → 闭弯引号 `"`

## 不做的事

- **不重写已有 9 级**——已有的 `line_trimmed`/`whitespace_normalized`/`block_anchor`/`context_aware` 等不动
- **不改 patch_file 的入参**——`PatchFileInput` 的 4 个字段不变
- **不把策略暴露给模型**——`strategy` 已在返回结果里（`"strategy": strategy`），模型不需要知道有几级
- **不做值不同的歧义检查**——sherry 现有的 count > 1 检查够用（ZCode 的值不同→ambiguous 是更精细的检查，但实际场景罕见）

## 关键文件路径

| 文件 | 角色 |
|---|---|
| `agent/tools/pub_base/text_matcher.py` | 修改：加 2 级 + BROAD_MATCHERS + 引号保留 |
| `agent/tools/pub_base/__init__.py` | 参考：`fuzzy_find_and_replace` 导出（不改） |
| `agent/tools/file_tools/patch_file.py` | 参考：调用 `fuzzy_find_and_replace`（不改） |
| `tests/agent/tools/pub_base/test_text_matcher.py` | 新增：4 类缺口测试 |
