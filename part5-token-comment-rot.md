# 十九、三级降级 Token 估算：usage_metadata → tiktoken → CJK 启发式

## 背景

压缩中间件（`agent/middlewares/summarization.py`）在决定是否触发上下文压缩时，需要估算当前消息列表的 token 总量。当前估算方式：

```python
# pub_func/message/estimate_msg_tokens.py
def estimate_msg_tokens(msg: BaseMessage) -> int:
    total = len(content) + len(tool_calls_str) + len(tool_call_id)
    return total // CHARS_PER_TOKEN  # CHARS_PER_TOKEN = 4
```

**问题：** `chars / 4` 对英文准确（4 字符 ≈ 1 token），但对中日韩严重低估：

| 语言 | 示例                           | 字符数 | 实际 token | 当前估算 (÷4) | 误差            |
| ---- | ------------------------------ | ------ | ---------- | ------------- | --------------- |
| 英文 | `"Hello world"`                | 11     | ~3         | 2             | 基本准确        |
| 中文 | `"你好世界，今天天气怎么样？"` | 13     | ~13-20     | 3             | **低估 4-6 倍** |
| 日文 | `"こんにちは、今日の天気は？"` | 13     | ~13-18     | 3             | **低估 4-6 倍** |
| 韩文 | `"안녕하세요, 오늘 날씨는?"`   | 16     | ~13-20     | 4             | **低估 3-5 倍** |

### 影响

| 环节                         | 表现                                                   |
| ---------------------------- | ------------------------------------------------------ |
| T1/T2 预估触发（API 调用前） | 80,000 token 触发线迟迟不触发，实际已超 context window |
| preserve budget 计算         | 预算 15,000 token 以为还能放，实际早已溢出             |
| 路由决策（`decide_route`）   | pressure 被低估，该压缩时不压缩                        |

### 现有补救（不够）

压缩中间件已有 `compute_pressure()` 取 `max(estimated + system_prompt, reported)`：

```python
# pub_func/message/overflow_router.py
def compute_pressure(estimated_tokens, reported_tokens, system_prompt_tokens):
    pressure = estimated_tokens + system_prompt_tokens
    if reported_tokens is not None:
        pressure = max(pressure, reported_tokens)
    return pressure
```

但 `reported_tokens` 来自 `usage_metadata`，有两个局限：

1. **本地模型不一定填充** — llama-cpp-python 集成可能不回传 `usage_metadata`
2. **时序差** — `usage_metadata` 是上一次 API 调用的值，之后新增的消息不在其中

## 思路

三级降级链：

```
Tier 1: usage_metadata（精确，API 返回的实际 token 数）
  ↓ 不可用（本地模型未填充 / 首次调用前 / 无 AIMessage）
Tier 2: tiktoken（OpenAI BPE tokenizer，近似）
  ↓ 不可用（未安装 tiktoken / 非 OpenAI 模型不适用）
Tier 3: CJK 启发式（CJK 1:1 + English 4:1，零依赖兜底）
```

### 各层适用场景

| Tier           | 来源            | 精度                                  | 何时可用                   | 限制                                            |
| -------------- | --------------- | ------------------------------------- | -------------------------- | ----------------------------------------------- |
| usage_metadata | 模型 API 返回值 | 100%（该模型实际值）                  | API 调用后，模型集成回传时 | 本地模型可能不填充；时序差                      |
| tiktoken       | OpenAI BPE 编码 | 高（OpenAI 模型）；中（其他模型近似） | 安装了 tiktoken 包时       | 非 OpenAI tokenizer 有偏差，但比 chars/4 好得多 |
| CJK 启发式     | 字符类别检测    | 中（比 chars/4 准 4-6 倍）            | 始终可用                   | 启发式，不精确                                  |

### 降级逻辑

```
estimate_messages_tokens(messages):
  1. 尝试 Tier 1: 扫描 messages 中最后一个 AIMessage 的 usage_metadata
     ├── total_tokens > 0 → 返回 usage_metadata.total_tokens
     └── 不可用 → 继续
  2. 尝试 Tier 2: 用 tiktoken 编码所有消息内容
     ├── tiktoken 已安装 → 返回 len(enc.encode(text))
     └── 未安装 / 编码失败 → 继续
  3. Tier 3: CJK 启发式
     └── CJK 字符 × 1.0 + 非CJK 字符 / 4.0
```

## 实现

### 1. 修改 `pub_func/message/estimate_msg_tokens.py`

```python
import json
import unicodedata
from langchain_core.messages import BaseMessage, AIMessage

from config.num import CHARS_PER_TOKEN


def _count_cjk(text: str) -> int:
    """Count CJK + fullwidth characters (Chinese, Japanese, Korean)."""
    return sum(
        1 for c in text
        if unicodedata.east_asian_width(c) in ("W", "F")
    )


def _heuristic_tokens(text: str) -> int:
    """CJK-aware heuristic: CJK chars ≈ 1 token, others ≈ 4 chars/token."""
    cjk = _count_cjk(text)
    other = len(text) - cjk
    return int(cjk * 1.0 + other / CHARS_PER_TOKEN)


def _tiktoken_tokens(text: str) -> int | None:
    """Best-effort tiktoken encoding; returns None if unavailable."""
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except ImportError:
        return None
    except Exception:
        return None


def _get_usage_metadata_tokens(messages: list[BaseMessage]) -> int | None:
    """Tier 1: extract total_tokens from the last AIMessage's usage_metadata."""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and msg.usage_metadata:
            total = msg.usage_metadata.get("total_tokens", 0)
            if total > 0:
                return total
    return None


def _extract_text(msg: BaseMessage) -> str:
    """Extract all text content from a message (content + tool_calls + tool_call_id)."""
    parts: list[str] = []
    content = msg.content
    if isinstance(content, str):
        parts.append(content)
    elif content is not None:
        parts.append(json.dumps(content, ensure_ascii=False))

    tool_calls = getattr(msg, "tool_calls", None)
    if tool_calls:
        for tc in tool_calls:
            parts.append(str(tc.get("name", "")))
            parts.append(str(tc.get("args", "")))

    tool_call_id = getattr(msg, "tool_call_id", None)
    if tool_call_id:
        parts.append(str(tool_call_id))

    return "".join(parts)


def estimate_msg_tokens(msg: BaseMessage) -> int:
    """Estimate tokens for a single message (Tier 3 only — per-message).

    For the full 3-tier chain use estimate_messages_tokens().
    """
    text = _extract_text(msg)
    return _heuristic_tokens(text)


def estimate_messages_tokens(messages: list[BaseMessage]) -> int:
    """Three-tier token estimation: usage_metadata → tiktoken → CJK heuristic.

    Tier 1 (usage_metadata): exact, but only from the LAST API call.
    Tier 2 (tiktoken):       OpenAI BPE, good approximation for any text.
    Tier 3 (CJK heuristic):  zero-dependency fallback, 4-6x better than chars/4 for CJK.
    """
    if not messages:
        return 0

    # Tier 1: usage_metadata from last AIMessage
    reported = _get_usage_metadata_tokens(messages)
    if reported is not None:
        return reported

    # Gather all text from all messages
    full_text = "".join(_extract_text(m) for m in messages)

    # Tier 2: tiktoken
    tik_tokens = _tiktoken_tokens(full_text)
    if tik_tokens is not None:
        return tik_tokens

    # Tier 3: CJK heuristic
    return _heuristic_tokens(full_text)
```

### 2. 关键变更

| 变更点                        | 原逻辑                         | 新逻辑                         |
| ----------------------------- | ------------------------------ | ------------------------------ |
| `estimate_messages_tokens`    | `sum(chars) // 4`              | 三级降级链                     |
| `estimate_msg_tokens`（单条） | `chars // 4`                   | CJK 启发式（保持单条接口不变） |
| Tier 1                        | 无（仅 compute_pressure 层有） | 移入估算函数，统一入口         |
| Tier 2                        | 无                             | tiktoken best-effort           |
| Tier 3                        | `chars // 4`                   | CJK 1:1 + English 4:1          |

### 3. 不改动的部分

- `compute_pressure()` — 仍然取 `max(estimated + system_prompt, reported)`，但现在 estimated 本身可能就是 reported（Tier 1），`max` 操作不会产生问题
- `config/num.py` — `CHARS_PER_TOKEN = 4` 保留，作为 Tier 3 中非 CJK 部分的除数
- 溢出路由 `decide_route()` — 仍接收 pressure_tokens，不感知估算来源

### 4. tiktoken 可选依赖

tiktoken 不加入 `pyproject.toml` 的硬依赖。降级链设计为 tiktoken 不可用时自动跳过。仅在需要 Tier 2 精度时：

```bash
uv pip install tiktoken
```

> 项目已依赖 `langchain-openai`，tiktoken 是其间接依赖之一，大概率已安装。

## 效果对比

| 场景                                | 原估算             | 新估算                          | 实际    |
| ----------------------------------- | ------------------ | ------------------------------- | ------- |
| 英文对话 50 轮                      | 12,000             | Tier 2: 12,500 / Tier 3: 12,000 | ~12,300 |
| 中文对话 50 轮                      | 3,000 (❌严重低估) | Tier 3: 12,000 (CJK 1:1)        | ~12,500 |
| DeepSeek API 调用后                 | 3,000 (❌)         | Tier 1: 14,000 (reported)       | 14,000  |
| 本地 llama（无 usage_metadata）中文 | 2,500 (❌)         | Tier 2: N/A → Tier 3: 10,000    | ~10,500 |

## 涉及文件清单

### A. 核心重写

| 操作 | 文件                                                    |
| ---- | ------------------------------------------------------- |
| 重写 | `pub_func/message/estimate_msg_tokens.py`               |
| 不变 | `config/num.py`（CHARS_PER_TOKEN 保留供 Tier 3 用）     |
| 不变 | `pub_func/message/overflow_router.py`（接口不变）       |
| 不变 | `pub_func/message/slice_last_turn.py`（已调用统一函数） |
| 不变 | `pub_func/message/tool_result_ttl.py`（已调用统一函数） |

### B. 硬编码 `// 4` 迁移（6 处）

以下文件绕过了中央估算函数，直接用 `len(...) // 4` 计算 token，需迁移为调用 `estimate_msg_tokens` 或 `estimate_messages_tokens`：

| 文件                                     | 行  | 当前代码                                            | 迁移方式                                                                            |
| ---------------------------------------- | --- | --------------------------------------------------- | ----------------------------------------------------------------------------------- |
| `agent/middlewares/summarization.py`     | 644 | `return len(prompt) // 4`                           | 改为 `return estimate_text_tokens(prompt)`                                          |
| `pub_func/message/tool_output_prune.py`  | 36  | `sum(len(str(...content...)) // 4 for m in msgs)`   | 改为 `estimate_messages_tokens(msgs)`                                               |
| `pub_func/message/tool_output_prune.py`  | 58  | `token_est = content_len // 4`                      | 改为 `token_est = estimate_text_tokens(content)`                                    |
| `pub_func/message/tool_output_dedup.py`  | 67  | `tokens_reduced += (old_len - new_len) // 4`        | 改为 `tokens_reduced += estimate_text_tokens_diff(old, new)`                        |
| `pub_func/message/tool_args_truncate.py` | 101 | `freed_total += max((len(old) - len(new)) // 4, 0)` | 改为 `freed_total += max(estimate_text_tokens(old) - estimate_text_tokens(new), 0)` |
| `pub_func/message/target_truncation.py`  | 73  | `reduced_tokens = (old_len - new_len) // 4`         | 改为 `reduced_tokens = estimate_text_tokens(old) - estimate_text_tokens(new)`       |

> **设计要点**：`estimate_text_tokens(text: str) -> int` 是新增的纯字符串估算入口（对单条 message 估算的底层复用）。上述 6 处迁移后，全项目不再有 `// 4` 硬编码（`config/num.py` 定义除外）。

### C. 测试文件同步更新

| 文件                                        | 改动                                       |
| ------------------------------------------- | ------------------------------------------ |
| `tests/unit/test_overflow_router.py`        | mock 估算函数返回值，不再依赖 `// 4` 常数  |
| `tests/unit/test_pub_func_message_tools.py` | 断言值从 `// 4` 改为调用估算函数后的返回值 |
| `tests/unit/test_tool_result_ttl.py`        | 同上                                       |

### D. 不改动

| 文件                            | 原因                                                                          |
| ------------------------------- | ----------------------------------------------------------------------------- |
| `agent/tools/message_search.py` | L127 `max_chars // 4` 是窗口偏移比例（25% before / 75% after），非 token 估算 |
| `agent/tools/memory.py`         | 使用字符限制（非 token），模型无关                                            |
| `context_engine/core.py`        | 使用 Jieba 分词，非 token 估算                                                |

---

# 二十、注释腐化清理记录（Comment Rot Remediation）

> 原独立文件 `COMMENT_ROT_REMEDIATION.md` 已合并至此。

## Overview

系统化清理四类注释腐化：

1. **Comment drift** — 注释与代码不匹配
2. **Dead code comments** — 被注释掉的代码块
3. **Noise comments**（废话注释） — 仅复述代码、无附加信息
4. **Zombie TODOs** — 过期的 `Task N` 引用和死 TODO 标记

## Phase 1: Dead Code Comment Removal

移除了无文档用途的被注释代码块：

| File                       | Location | Removed                            |
| -------------------------- | -------- | ---------------------------------- |
| `agent/core.py`            | L26-27   | Commented `patch_tool_node()` call |
| `server/DAO/__init__.py`   | L1       | Commented import statement         |
| `sandbox_bwrap.py`         | L24-36   | Dead try/except fallback block     |
| `STT_model/model_bin.py`   | multiple | Commented code blocks              |
| `STT_model/infer_utils.py` | multiple | Commented code blocks              |
| `STT_model/frontend.py`    | multiple | Commented code blocks              |
| `STT_model/export_meta.py` | multiple | Commented code blocks              |
| `STT_model/core.py`        | multiple | Commented code blocks              |

## Phase 2: Noise Comment Removal

删除了仅复述相邻代码、不提供额外上下文的注释：

| File                 | Line       | Removed comment               |
| -------------------- | ---------- | ----------------------------- |
| `agent/core.py`      | L127       | `# create table before using` |
| `agent/core.py`      | L136       | `# Build the agent`           |
| `skill_list.py`      | L40        | Redundant restating comment   |
| `skill_manage.py`    | L616, L625 | Redundant restating comments  |
| `skill_view.py`      | L346       | Redundant restating comment   |
| `message_search.py`  | L293       | Redundant restating comment   |
| `server/__main__.py` | L21        | Redundant restating comment   |
| `STT_model/core.py`  | L553       | `# forward encoder1`          |

## Phase 3: Zombie TODO / Task N Cleanup

分两轮清理了全项目 **159 个过期 `Task N` 引用**：

- **Round 1**：对 33 个文件执行定向 regex 清理，移除 122 个引用
- **Round 2**：全项目扫描，移除剩余 37 个引用

同时清理了以下位置的过期引用：

- `.omo/` 目录
- `issues.md`
- `notepad problems.md`

**Result**: 159 `Task N` references → 0

## Phase 4: Comment Drift Fixes

| File            | Line   | Fix                                                            |
| --------------- | ------ | -------------------------------------------------------------- |
| `agent/core.py` | L30-31 | Fixed syntax-broken comment                                    |
| `agent/core.py` | L163   | Updated RepetitionGuardWrapper comment to match implementation |

## Incident: Regex Script Damage & Recovery

### What happened

Round 2 的 Task N 清理 regex 脚本使用了两个过于激进的模式：

1. `\(\s*\)` — 移除了**所有**空括号 `()`，破坏了所有零参数函数调用、构造器和方法调用
2. `'  +'` → `' '` — 将**所有**多空格序列折叠为单空格，破坏了 Python 缩进

### Impact

- **374 个 Python 文件**受损（共扫描 374 个）
- 缺失 `()`：`time.time()`, `time.monotonic()`, `set()`, `threading.Lock()`, `.fetchone()`, `super().__init__()`, `asyncio.Lock()`, `ZoneInfo()`, `CronStore()`, `CronJobFailureState()` 等
- 浮点字面量损坏：`10.0` → `10.0()`
- 所有缩进折叠为单空格，导致 try/except/finally、if/elif/else、类方法和嵌套块无法解析

### Recovery process

1. **`fix_parens.py`** — 自动恢复已知方法调用和构造器缺失的 `()`（恢复 ~370 个文件）
2. **`fix_indentation.py`** + **`fix_iterative.py`** — 自动恢复常见模式的缩进
3. **10 个并行 `yalu-code` 任务** — 手动重建 203 个严重受损文件，每个任务处理一个目录子集
4. **手动重写** 3 个最严重受损文件：
   - `agent/tools/taskflow/registry/store_sqlite.py`（378 行）
   - `plugins/channels/qq/core.py`（384 行）
   - `skills/builtin/core/cron/scripts/base.py`（871 行）
5. **深度扫描** — 基于模式扫描发现 14 个仍存在错误 `pass` + 错误嵌套（语法有效但逻辑损坏）的文件：
   - `agent/tools/taskflow/tools/taskflow_run_task.py`（重写）
   - `agent/tools/taskflow/tools/taskflow_set_waiting.py`（重写）
   - `agent/tools/taskflow/tools/taskflow_finish.py`（重写）
   - `agent/tools/taskflow/tools/taskflow_resume.py`（重写）
   - `agent/tools/taskflow/tools/taskflow_summary.py`（重写）
   - `agent/tools/taskflow/tools/taskflow_cancel.py`（重写）
   - `agent/tools/taskflow/tools/taskflow_create.py`（重写）
   - `agent/tools/taskflow/tools/taskflow_fail.py`（重写）
   - `agent/tools/taskflow/tools/__init__.py`（重写）
   - `agent/tools/pub_base/path_utils.py`（重写 — 同时修复 `p.is_absolute` → `p.is_absolute()`）
   - `agent/tools/pub_base/sandbox_guard.py`（重写 — 同时修复类方法缩进）
   - `agent/tools/pub_base/tool_utils.py`（重写）
   - `agent/tools/subagent/capabilities/core.py`（重写 — 同时修复 `get_config` → `get_config()`）
   - `agent/middlewares/humanInTheLoop/detection.py`（修复 2 个函数）
6. **定向 `()` 修复** — 7 处单独编辑：
   - `taskflow_run_task.py:100` — `time.time` → `time.time()`
   - `taskflow_resume.py:75` — `time.time` → `time.time()`
   - `taskflow_set_waiting.py:40` — `time.time` → `time.time()`
   - `subagent/spawn/thread_binding.py:106` — `uuid.uuid4` → `uuid.uuid4()`
   - `channels/manager.py:238` — `channel.is_running` → `channel.is_running()`
   - `skills/builtin/llm_wiki/scripts/__init__.py:10` — `Path.resolve.parent` → `Path.resolve().parent`
   - `subagent/types/registry.py:155-158` — `Field(default_factory= list())` → `Field(default_factory=list)`（3 处）

### Final verification

```
OK: 374, Broken: 0
```

全部 374 个 Python 文件通过 `python -m py_compile`。

基于模式的深度扫描确认 0 个剩余的错误 `pass` + 错误嵌套模式（`if+pass`、`loop+pass`、`try+pass`）。

### Temp files cleaned up

- `fix_parens.py` — removed
- `fix_indentation.py` — removed
- `fix_iterative.py` — removed
- `broken_files.txt` — removed

## Summary

| Phase                  | Items fixed        |
| ---------------------- | ------------------ |
| Dead code comments     | 8+ files           |
| Noise comments         | 9 instances        |
| Zombie TODOs           | 159 references → 0 |
| Comment drift          | 2 fixes            |
| Script damage recovery | 374 files restored |
| Temp file cleanup      | 4 files removed    |
