# 修复计划: estimate_json_tokens CJK 感知

> **状态**: 已实施（2026-09-30）——验收 4 条全过（含 perf guard ×5.6 / 预算 ×30），落地记录见文末
> **创建日期**: 2026-09-30
> **目标**: 让 `estimate_json_tokens` 与 `estimate_text_tokens` 一样区分 CJK 和非 CJK 字符，消除中英夹杂 JSON 负载的 token 低估

---

## 1. 现状与问题

### 现状

`pub/func/estimate_tokens.py` 提供三层 token 估算，其中 `estimate_text_tokens` 已实现 CJK 感知：

```python
def estimate_text_tokens(text: str) -> int:
    cjk = count_cjk(text)           # CJK 字符数
    non_cjk = len(text) - cjk       # 非 CJK 字符数
    return (cjk // CHARS_PER_TOKEN_CJK) + (non_cjk // CHARS_PER_TOKEN)
    #      ↑ CJK: 2字符/token           ↑ 非CJK: 4字符/token
```

但 `estimate_json_tokens` 仍是**全字符统一比例**：

```python
def estimate_json_tokens(text: str) -> int:
    if not text:
        return 0
    return max(len(text) // CHARS_PER_TOKEN_JSON, 0)   # ← 全部按 7字符/token，不区分 CJK
```

### 问题

JSON 负载中含 CJK 字符时，`len // 7` 对 CJK 部分严重低估：

| 文本示例                                    | 字符数 | CJK | 当前估算 | CJK-aware 估算 | 低估倍数 |
| ------------------------------------------- | ------ | --- | -------- | -------------- | -------- |
| `{"description": "读取文件内容并返回结果"}` | 38     | 10  | 5        | 9              | 1.8x     |
| 纯中文 JSON 值 (100 CJK chars)              | 100    | 100 | 14       | 50             | 3.5x     |

**触发路径**：

`server/service/context_usage_service.py:63` — `_tool_schema_tokens()` 对工具 schema 做 `json.dumps(schema, ensure_ascii=False)` 后调用 `estimate_json_tokens`。

**当前实际影响**：实测工具 schema 共 60,594 字符，其中 0 个 CJK 字符 — **目前是潜在问题，不是实际 bug**。但如果工具描述写成中文，或用户自定义工具含中文描述，context usage 面板的 token 估算会偏低，导致 `messages` 部分虚高。

### 常量参考

`config/features/agent_side/token_estimation.py`：

```python
"chars_per_token": 4,       # 非CJK prose
"chars_per_token_cjk": 2,   # CJK prose
"chars_per_token_json": 7,  # JSON 结构（非CJK部分）
```

---

## 2. 改动清单

### 2.1 `pub/func/estimate_tokens.py` — 核心修复

| 行号  | 当前代码                                                                      | 改为   | 说明              |
| :---: | ----------------------------------------------------------------------------- | ------ | ----------------- |
| 96-98 | `if not text: return 0`<br>`return max(len(text) // CHARS_PER_TOKEN_JSON, 0)` | 见下方 | 拆分 CJK 与非 CJK |

```python
def estimate_json_tokens(text: str) -> int:
    """Estimate the token cost of a serialized JSON payload.

    Same contract as :func:`estimate_text_tokens` but for wire-shaped JSON
    (tool schemas, tool arguments): the repeated structural keys make these
    payloads tokenize at roughly ``chars_per_token_json`` characters per token,
    so prose ratios overshoot them — which matters when the estimate is
    subtracted from a provider-reported prompt.

    CJK characters inside JSON string values tokenize at the same rate as CJK
    prose (~``chars_per_token_cjk``), so they are split out and estimated
    separately — the flat JSON ratio would underestimate them by ~3.5x.
    """
    if not text:
        return 0
    cjk = count_cjk(text)
    non_cjk = len(text) - cjk
    return (cjk // CHARS_PER_TOKEN_CJK) + (non_cjk // CHARS_PER_TOKEN_JSON)
```

> `count_cjk` 已从模块顶部导入（`pub/func/estimate_tokens.py:54`），无需新增 import。

### 2.2 `tests/pub/func/test_estimate_tokens.py` — 补充测试

在 `TestJsonEstimate` 类中新增 3 个测试：

```python
class TestJsonEstimate:
    # ... 现有 3 个测试保持不变 ...

    def test_cjk_in_json_uses_cjk_ratio(self):
        payload = '{"description": "' + "读" * (CHARS_PER_TOKEN_CJK * 10) + '"}'
        # CJK 部分: 10 tokens; JSON 结构部分: 剩余字符 // 7
        cjk_count = 10  # CHARS_PER_TOKEN_CJK * 10 CJK chars
        non_cjk_len = len(payload) - cjk_count * CHARS_PER_TOKEN_CJK
        expected = 10 + non_cjk_len // CHARS_PER_TOKEN_JSON
        assert estimate_json_tokens(payload) == expected

    def test_cjk_json_estimate_above_flat_ratio(self):
        payload = '{"description": "读取文件内容并返回结果"}'
        flat = len(payload) // CHARS_PER_TOKEN_JSON
        assert estimate_json_tokens(payload) > flat

    def test_pure_ascii_json_keeps_flat_ratio(self):
        payload = '{"description": "a rather long tool description", "type": "string"}'
        assert estimate_json_tokens(payload) == len(payload) // CHARS_PER_TOKEN_JSON
```

### 2.3 `tests/perf/test_perf_hot_paths.py` — 确认线性增长不变

`test_perf_hot_paths.py:68` 有一个 `estimate_json_tokens` 的性能 guard：

```python
("estimate_json_tokens (tool schemas)", lambda factor: estimate_json_tokens(serialized * (200 * factor))),
```

改动后 `count_cjk` 多一次全文遍历，但仍为 O(n) 线性，guard 逻辑不受影响。确认 CI 通过即可。

---

## 3. 影响评估

### 不受影响

| 调用点                                                  | 原因                                                     |
| ------------------------------------------------------- | -------------------------------------------------------- |
| `estimate_text_tokens`                                  | 不涉及 `estimate_json_tokens`                            |
| `estimate_content_tokens`                               | 文本块走 `estimate_text_tokens`，媒体块走固定值          |
| `estimate_msg_tokens`                                   | 工具调用 args 走 `estimate_text_tokens`（非 JSON ratio） |
| `estimate_messages_tokens`                              | Tier 1 用 API 上报值；Tier 2 走 `estimate_msg_tokens`    |
| summarization 全链路                                    | 用 `estimate_messages_tokens` / `estimate_msg_tokens`    |
| context_limit wrapper                                   | 用 `estimate_text_tokens`                                |
| overflow_router / overflow_clip                         | 用 `estimate_msg_tokens`                                 |
| tool_result_ttl / tool_output_prune / tool_output_dedup | 用 `estimate_text_tokens` / `estimate_msg_tokens`        |
| tool_args_truncate / target_truncation                  | 用 `estimate_text_tokens`                                |
| slice_last_turn                                         | 用 `estimate_msg_tokens`                                 |
| context_usage_service (persona/skills)                  | 用 `estimate_text_tokens`                                |

### 受影响

| 调用点                                        | 变化                                                            |
| --------------------------------------------- | --------------------------------------------------------------- |
| `context_usage_service._tool_schema_tokens()` | 当 schema 含 CJK 时估算值上升，`messages` 占比相应下降 — 更准确 |
| `tests/perf/test_perf_hot_paths.py`           | `count_cjk` 增加一次全文遍历，仍 O(n)，guard 不变               |

### 回归风险

- **纯 ASCII JSON**：`count_cjk(text) == 0`，退化为 `len // CHARS_PER_TOKEN_JSON`，与原逻辑完全一致 — 零回归。
- **CJK JSON**：估算值上升，可能使 context usage 面板的 `tools` 部分变大、`messages` 部分变小，方向正确（更接近真实值）。

---

## 4. 验收标准

1. `uv run --no-sync pytest tests/pub/func/test_estimate_tokens.py tests/pub/func/test_estimate_tokens_cjk.py -q` — 全绿
2. `uv run --no-sync pytest tests/perf -q -s` — 线性增长 guard 不变
3. `uv run --with ruff ruff check pub/func/estimate_tokens.py && uv run --with ruff ruff format --check pub/func/estimate_tokens.py` — lint 通过
4. `uv run --no-sync basedpyright pub/func/estimate_tokens.py` — 类型检查通过

---

## 5. 落地记录（2026-09-30）

- 核心改动与本节 2.1 的方案逐字一致：`estimate_json_tokens` 拆 `count_cjk` / 非 CJK 两段，非 CJK 段保留 JSON 结构比例 `chars_per_token_json`。
- 测试比 2.2 多两条：`test_pure_ascii_json_keeps_the_flat_ratio`（纯 ASCII 回归底线，等于改动前的公式）与 `test_ascii_structure_with_cjk_values_is_split_the_same_way`（结构是 ASCII、成本全在 CJK 值里的形状）。
- **2.3 的 perf guard 实测**：`estimate_json_tokens (tool schemas): 10x input → 75.9ms → 428.1ms (×5.6, budget ×30)` —— 多一次全文遍历，仍是线性。
- 计划外的一条**没有**落地：本想在 `tests/server/service/test_context_usage_service.py` 加一条"`json.dumps` 必须 `ensure_ascii=False`"的守卫，测算后发现假设不成立——转义把每个 CJK 字符变成 6 个 ASCII 字符再按 ÷7 计，得到 ~0.86 token/字，比 CJK 比例（0.5 token/字）**更高**而非更低，即转义是另一种偏差而不是"退回旧公式"。守卫因此删除，不在测试里钉一个错误的因果。
- 受影响面与 3.2 一致，无其他调用点（全仓仅 `context_usage_service._tool_schema_tokens` 一处）。
