# 计划：read_file token 感知截断 + 未变更重读 stub

- 建档日期：2026-10-12
- 状态：**计划（未开工）**
- 父文档：[对标计划_Zcode可借鉴能力.md](./对标计划_Zcode可借鉴能力.md) §4.3
- 涉及文件：`agent/tools/file_tools/read_file.py`、`pub/func/message/token_truncation.py`、`pub/func/message/target_truncation.py`、`config/features/agent_side/tools_timeouts.py`

## 是什么

把 `read_file` 的截断口径从**行数 / 字符数**改为 **token 数**，复用 sherry 现有的 CJK token 估算器（`_chars_for_tokens` / `estimate_text_tokens`），使同样 token 预算在英文 / CJK / 混合文本下返回尽量多的内容。附带一个"文件没变就别重复吐内容"的 stub。

## 现状问题

### 截断口径不对

`read_file.py::_paginate`（L169-203）按行数截断：`limit` 默认 500、硬上限 2000 行。`truncated = total_lines > end_line`——纯行数比较。

同样 2000 行，英文代码 ~5000 token，CJK 文档 ~20000-40000 token。同样的行数预算在不同语言下 token 开销差 4-8 倍。

### 配置键脱节

`TOOLS_TIMEOUTS["file_tools_read_default_limit"]` = 500、`["file_tools_read_max_limit"]` = 2000 存在于 `config/features/agent_side/tools_timeouts.py`，但 **`read_file.py` 硬编码 500/2000，不读配置**。只有中间件的恢复通知（`target_truncation.py:24`）读 `file_tools_read_default_limit`。

### 中间件二次截断

`pub/func/message/target_truncation.py::_truncate_read_file_content`（L127-153）在工具返回后再截一次：`len(content) > max_output_tokens * 4` 时触发，取头 30% + 尾 30%。这已经是 token 感知的（用了 `_chars_for_tokens`），但它是**事后补救**——`read_file` 先返回可能很大的内容，中间件再砍。两头做截断，逻辑分散。

### 重复读浪费

模型经常重复读同一个没变的文件（读过后在上下文里已经有了，但不确定有没有改过，又读一遍）。现在每次都返回全文——上下文里同一份文件内容出现两次。

## 设计

### 核心思路：不需要二分搜索

Zcode 用二分搜索是因为没有逐字符 token 估算器。sherry 的 `_chars_for_tokens`（`token_truncation.py:23-42`）已经是**逐字符线性扫描 + 提前终止**——遍历文本，每个字符按 CJK（`1/chars_per_token_cjk`）或非 CJK（`1/chars_per_token`）累加，到达 token 预算就返回字符数。O(返回内容大小)，不是 O(文件大小)。

所以 sherry 不需要二分——线性扫描 + 提前终止比二分更快更精确。

### 1. 新增配置

`config/features/agent_side/tools_timeouts.py` 新增：

```python
#: read_file 单次返回的 token 预算。截断按 token 口径，CJK 感知。
#: 默认 4000——约为 SUMMARIZATION["max_tool_output_tokens"](500) 的 8 倍，
#: 因为 read_file 返回的是文件内容（需要给模型足够上下文），
#: 不是工具调用的临时输出。设为 0 = 回退到行数口径（当前行为）。
read_file_token_budget: int   # 默认 4000

#: 截断时取预算的比例（留余量给行号 + JSON wrapper）。
read_file_budget_ratio: float  # 默认 0.85
```

### 2. token 感知截断：逐行读取 + 提前终止

`read_file.py::_paginate` 改为逐行读取，用 `_chars_for_tokens` 累加 token 开销，到预算就停。不全量读文件——大文件只读返回的那部分。

```python
from pub.func.message.token_truncation import _chars_for_tokens
from pub.func.cjk import is_cjk_codepoint
from config.features import TOKEN_ESTIMATION

_CHARS_PER_TOKEN = TOKEN_ESTIMATION["chars_per_token"]        # 4
_CHARS_PER_TOKEN_CJK = TOKEN_ESTIMATION["chars_per_token_cjk"]  # 2


def _paginate_token_aware(
    raw_text: str,
    total_lines: int,
    offset: int,
    token_budget: int,
    budget_ratio: float,
) -> tuple[str, int, bool, str | None]:
    """Token 感知分页。返回 (numbered_content, end_line, truncated, hint)。

    逐行累加 token 开销，到达 budget * ratio 时停止。
    CJK 字符按 1/chars_per_token_cjk 计，其他按 1/chars_per_token 计。
    """
    all_lines = raw_text.splitlines(keepends=True)
    target_tokens = token_budget * budget_ratio

    accumulated = 0.0
    page_lines: list[str] = []
    start_idx = offset - 1

    for i in range(start_idx, len(all_lines)):
        line = all_lines[i]
        # 逐字符累加这一行的 token 开销
        for ch in line:
            accumulated += (
                1.0 / _CHARS_PER_TOKEN_CJK if is_cjk_codepoint(ord(ch))
                else 1.0 / _CHARS_PER_TOKEN
            )
        page_lines.append(line)
        if accumulated >= target_tokens:
            break

    end_line = offset + len(page_lines) - 1
    truncated = end_line < total_lines

    page_text = "".join(page_lines)
    if page_text.endswith("\n"):
        page_text = page_text[:-1]
    numbered = _add_line_numbers(page_text, offset) if page_text else ""

    hint = None
    if truncated:
        hint = (
            f"Use offset={end_line + 1} to continue reading "
            f"(showing {offset}-{end_line} of {total_lines} lines, "
            f"~{int(accumulated)} tokens used of {token_budget} budget)"
        )

    return numbered, end_line, truncated, hint
```

**为什么逐行而不是逐字符跨行截断**：行是 `read_file` 的天然边界——截断点必须在行尾（否则行号会乱）。逐行累加 token，到达预算就在当前行后停。可能略微超出预算（多了一行），但行号完整性比精确预算更重要。

**为什么不用 `_chars_for_tokens` 直接**：`_chars_for_tokens` 返回字符数，不保证在行边界截断。我们要的是"最多 N token 的**完整行**"。逐行累加 + 行边界停止是自然做法。但每行内部的 token 计算完全复用 `_chars_for_tokens` 的 CJK 逻辑（同样的 `is_cjk_codepoint` + 同样的比率）。

### 3. 未变更重读 stub

复用 `read_state.licensed()` + 文件 stat 的 revision 比对。模型重复读同一个没变的文件 → 返回 stub，不吐内容。

```python
from agent.tools.pub_base.read_state import licensed
from agent.tools.pub_base.file_stat import revision_id  # 现有的 revision 计算

_UNCHANGED_STUB = (
    "[File unchanged since your last read. "
    "The content is already in your conversation — "
    "do not re-read it. If you need a specific section, "
    "use offset/limit to read only that part.]"
)

def _check_unchanged(session_id: str, resolved: Path, offset: int) -> bool:
    """True if the session already has this file's current revision in context.

    Only triggers for offset=1 (full-file read from start) AND a prior
    complete read (licensed) AND revision unchanged. A partial/truncated
    prior read does NOT license → re-read returns content.
    """
    if offset != 1:
        return False  # partial read — always return content
    license = licensed(session_id, resolved)
    if license is None:
        return False  # never read this file → return content
    try:
        current_rev = revision_id(resolved.stat())
    except Exception:
        return False  # stat failed → return content (fail-open)
    return license.revision == current_rev
```

在 `_paginate` 入口处调：

```python
if _check_unchanged(session_id, resolved, offset):
    return json.dumps({
        "content": _UNCHANGED_STUB,
        "total_lines": total_lines,
        "file_size": file_size,
        "truncated": False,
        "unchanged": True,
    }, ensure_ascii=False)
```

**注意**：`note_read`（写前读 license）只在 `offset == 1 and not truncated` 时调——完整读才发 license。所以 `_check_unchanged` 只在之前有过完整读 + 文件没变时触发 stub。之前是截断读的 → 没 license → 正常返回内容。

### 4. 与中间件 `_truncate_read_file_content` 的协调

`target_truncation.py::_truncate_read_file_content` 的触发条件是 `len(content) > max_output_tokens * 4`（= 2000 chars）。如果 `read_file` 返回 ≤ 4000 token（默认预算），ASCII 下 ≈ 16000 chars > 2000 chars → 中间件仍会二次截断。

**两种协调方式**：

**方式 A（推荐）**：把 `read_file` 从中间件的 `excluded_tools` 里**保持排除**（它已经在里面了），但中间件的 `target_truncate_tool_outputs` dispatcher 对 read_file 的特殊分支（`len(content) > max_output_tokens * 4`）改为检查 `read_file` 返回的 JSON 里的 `truncated` 标志——如果 `read_file` 自己已经截断了，中间件不再二次截断。

```python
# target_truncation.py — target_truncate_tool_outputs
if file_path is not None and len(content) > max_output_tokens * 4:
    # read_file already token-budgeted? check its truncated flag
    try:
        parsed = json.loads(content)
        if parsed.get("truncated") is not None:
            # read_file handled its own truncation — skip middleware clip
            return content
    except Exception:
        pass
    truncated = _truncate_read_file_content(content, max_output_tokens, file_path)
```

**方式 B（更简单）**：把 `read_file_token_budget` 设为 ≤ `max_tool_output_tokens`（500），这样 `read_file` 返回 ≤ 500 token ≈ 2000 chars = 中间件阈值，中间件不触发。但 500 token 太小（~50 行代码），不实用。

**选方式 A**——4000 token 预算够用（~1000 行代码 / ~500 行 CJK），中间件只在 `read_file` 没自己截断时兜底。

### 5. 保留 offset/limit 参数

`offset`（起始行）和 `limit`（最大行数）保留，但语义变为**上限**而非**主要截断维度**：

```python
# _paginate 里：
# 旧：truncated = total_lines > end_line（纯行数）
# 新：先按 token 预算截断，再按 limit 上限截断（两者取严）
token_end, token_truncated, token_hint = _paginate_token_aware(...)
limit_end = min(offset + limit - 1, total_lines)
end_line = min(token_end, limit_end)
truncated = end_line < total_lines
```

`limit` 仍然是硬上限（防一个超长行 + 大 token 预算返回太多行）。默认值从配置读（修复硬编码脱节）：

```python
from config.features import TOOLS_TIMEOUTS

default_limit = TOOLS_TIMEOUTS["file_tools_read_default_limit"]  # 500
max_limit = TOOLS_TIMEOUTS["file_tools_read_max_limit"]           # 2000
```

### 6. line number 格式不变

`_add_line_numbers` 保持 `N|content` 格式。中间件的 `_read_file_next_offset`（解析行号前缀算续读 offset）依赖这个格式，不能改。

## 改动清单

| 文件 | 改动 | 行数 |
|---|---|---|
| `agent/tools/file_tools/read_file.py` | `_paginate` 改为 token 感知 + `_check_unchanged` + 修配置硬编码 | ~80 改动 |
| `config/features/agent_side/tools_timeouts.py` | 新增 `read_file_token_budget` / `read_file_budget_ratio` | ~6 |
| `pub/func/message/target_truncation.py` | `target_truncate_tool_outputs` 对 read_file 的分支：已截断则跳过 | ~10 改动 |
| `tests/agent/tools/test_read_file_token_truncation.py` | 新建：CJK vs ASCII 预算一致性 + 截断点在行边界 + unchanged stub + 续读 offset | ~100 |

**合计 ~196 行**

## 验收

### token 感知截断

- ASCII 文件（4000 行代码）+ `read_file_token_budget=4000` → 返回 ~16000 chars（4000 token × 4 chars/token）≈ ~400 行，`truncated=True`，hint 含续读 offset
- CJK 文件（4000 行中文）+ `read_file_token_budget=4000` → 返回 ~8000 chars（4000 token × 2 chars/token）≈ ~200 行，`truncated=True`
- 混合文件 → CJK 行占更多 token 预算，ASCII 行占更少，总量自动平衡
- **同一 token 预算下，英文返回更多行、中文返回更少行，但 token 开销相同**
- 截断点永远在行尾——行号连续，不会有半行
- `limit` 仍是硬上限——token 预算内但超 limit 时按 limit 截

### 未变更重读

- 模型读文件 A → 完整读（不截断）→ `note_read` 记 license → 再读文件 A（没改）→ 返回 stub，不吐内容
- 模型读文件 A → 截断读 → 没 license → 再读文件 A → 正常返回内容（不给 stub）
- 模型读文件 A → 完整读 → 文件 A 被改 → revision 变 → 再读 → 正常返回内容
- `offset=2` 的偏移读 → 不触发 stub（总是返回内容）

### 中间件协调

- `read_file` 返回截断内容（`truncated=True`）→ 中间件不再二次截断
- `read_file` 返回未截断内容但 > 中间件阈值 → 中间件兜底截断（安全网）
- `read_file_token_budget=0` → 回退行数口径（当前行为，向后兼容）

### 配置修复

- `file_tools_read_default_limit` / `file_tools_read_max_limit` 从配置读入 `ReadFileInput` 的 default 和 max，不再硬编码 500/2000

## 风险

- **token 估算不精确**：`chars_per_token=4` / `chars_per_token_cjk=2` 是经验值，不是真实 tokenizer。实际 token 数可能偏差 ±20%。但比字符数口径好得多（字符数偏差 ±300%）。且中间件是兜底——估算偏低导致返回略多，中间件砍掉；估算偏高导致返回略少，模型调 offset 续读。
- **逐行读取性能**：大文件（500MB）逐行读 + 逐字符算 token——只读返回的部分（~400 行），不读全文。`_paginate` 当前已经 `raw.splitlines()` 全量切（内存 = 文件大小）。改为逐行读后内存 = 返回内容大小。但 `raw` 仍然是 `f.read()` 全量读——**需要同时改读取方式为逐行流式读**，否则只改截断逻辑不省内存。
- **unchanged stub 的误判**：`revision_id` 基于 stat（mtime + size），一个 `touch` 不改内容但改 mtime → stub 不触发 → 正常返回内容（安全方向：宁可多读不漏读）。如果用内容 hash 则精确但慢——stat 足够。
- **中间件分支改动**：`target_truncation.py` 的 dispatcher 改动影响所有走 read_file 路径的结果。需要测试确保"已截断跳过"不影响未截断的兜底。

## 不做的事

- **不做二分搜索**——sherry 的 `_chars_for_tokens` 是逐字符线性扫描 + 提前终止，O(返回内容大小)。二分搜索需要随机访问文件内容（mmap 或全量读），且收敛到近似值（二分停在 chunk 边界）。线性扫描更精确（停在字符级）且更快（只扫返回的部分）。
- **不替换 `truncate_by_tokens`**——它是压缩时的 head+tail 切片，用途不同（压缩 vs 工具输出截断）。本计划只改 `read_file` 的分页逻辑 + 中间件对 read_file 的分支。
- **不做 mmap 流式读**——首期保留 `f.read()` 全量读 + `splitlines()`，只改截断逻辑。流式逐行读（省内存）是独立优化，可后续做（和 terminal fd 直写类似的内存优化，但 read_file 的文件通常比 terminal 输出小得多）。
- **不改 `search_files` 的截断**——它有独立的双截断标记（`page_truncated` + `scan_truncated`），是 Sherry 比 Zcode 精细的地方，不动。

## 关键文件路径

| 文件 | 角色 |
|---|---|
| `agent/tools/file_tools/read_file.py` | 修改：`_paginate` token 感知 + `_check_unchanged` + 配置修复 |
| `pub/func/message/token_truncation.py` | 参考：`_chars_for_tokens` 的 CJK 累加逻辑（复用同款） |
| `pub/func/cjk.py` | 复用：`is_cjk_codepoint(ord(ch))` |
| `pub/func/estimate_tokens.py` | 参考：`estimate_text_tokens`（预算校验用） |
| `config/features/agent_side/token_estimation.py` | 参考：`chars_per_token=4` / `chars_per_token_cjk=2` |
| `config/features/agent_side/tools_timeouts.py` | 新增：`read_file_token_budget` / `read_file_budget_ratio` |
| `pub/func/message/target_truncation.py` | 修改：read_file 已截断时跳过中间件二次截断 |
| `agent/tools/pub_base/read_state.py` | 复用：`licensed()` 判断文件是否已读 + revision 比对 |
