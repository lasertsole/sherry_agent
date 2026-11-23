# 计划：Memory 字符上限按 CJK 加权

- 日期：2026-11-23
- 状态：**待评审**
- 来源：Memory 的 USER/MEMORY/FACTS 字符上限用原始 `len(text)`，不区分 CJK。1 个中文字符算 1，1 个英文字符也算 1，但实际 token 消耗差 2 倍（CJK: 2 字符/token，ASCII: 4 字符/token）。2200 字符纯中文 ≈ 1100 token，纯英文 ≈ 550 token——同样的上限，中文的 token 开销是英文的 2 倍。
- 风险等级：**低** — 改一个计数函数，所有调用点指向它
- 依赖：无
- 可独立执行：✅

---

## 0. 问题

```
memory_char_limit = 2200（原始字符数）

纯英文 2200 字符 → 550 token（4 字符/token）← 设计意图
纯中文 2200 字符 → 1100 token（2 字符/token）← 超支 2 倍
```

Memory 的 `_char_count` 用 `len(ENTRY_DELIMITER.join(contents))`——原始字符数，不区分 CJK。导致 CJK 内容的实际 token 消耗是英文的 2 倍，系统提示词里 memory 块占的 token 比设计意图多。

## 1. 设计

用 CJK 加权字符数替代原始 `len()`：

```
weighted_chars = non_cjk + cjk × CJK_WEIGHT

CJK_WEIGHT = chars_per_token / chars_per_token_cjk = 4 / 2 = 2
```

- 1 个 ASCII 字符 = 1 weighted char（不变）
- 1 个 CJK 字符 = 2 weighted chars（反映 2 倍的 token 消耗）

改后：
```
纯英文 2200 字符 → 2200 weighted chars → 550 token ← 不变
纯中文 2200 字符 → 4400 weighted chars → 超 2200 上限 → 只能放 1100 个中文字符 → 550 token ← 与英文对齐
```

英文容量不变，CJK 容量缩减到与英文相同的 token 预算。

**CJK_WEIGHT 从配置派生**：`chars_per_token / chars_per_token_cjk`（都在 `config/features/agent_side/token_estimation.py`），不硬编码。改 token 估算常量时 memory 权重自动跟随。

## 2. 现有工具（不改）

| 函数 | 位置 | 作用 |
|------|------|------|
| `count_cjk(text)` | `pub/func/cjk.py:30` | 统计 CJK 字符数 |
| `chars_per_token` | `config/features/agent_side/token_estimation.py:27` | ASCII 字符/token（默认 4） |
| `chars_per_token_cjk` | `config/features/agent_side/token_estimation.py:28` | CJK 字符/token（默认 2） |

`pub/func/estimate_tokens.py` 已经用 `count_cjk` 做 CJK 分裂估算，memory 只需要复用同一个函数。

---

## 3. 改动

| # | 文件 | 改动 | 说明 |
|---|------|------|------|
| 1 | `agent/tools/memory.py` | 新增 `_weighted_char_count(text)` 方法 | CJK 加权字符数，复用 `count_cjk` |
| 2 | `agent/tools/memory.py` | 所有 `len(self._joined(...))` 替换为 `self._weighted_char_count(self._joined(...))` | 9 处调用点 |
| 3 | `agent/tools/memory.py` | `_render_block` 和 `_success_response` 的显示同步用加权数 | 用户看到的用量与实际限制一致 |

**不改**：config 值（2200/1375/1375 不变）、`pub/func/cjk.py`、`pub/func/estimate_tokens.py`、token 估算常量。

---

## 4. 改动细节

### 4.1 新增 `_weighted_char_count`（memory.py）

```python
from pub.func.cjk import count_cjk
from config.features import TOKEN_ESTIMATION

class MemoryStore:
    # ...

    @staticmethod
    def _weighted_char_count(text: str) -> int:
        """CJK 加权字符数：CJK 字符按 token 消耗比例加权。

        CJK 字符的 token 消耗是 ASCII 的 ``chars_per_token / chars_per_token_cjk`` 倍
        （默认 4/2 = 2），所以 1 个 CJK 字符算 ``CJK_WEIGHT`` 个 weighted char。

        这让字符上限对 CJK 和 ASCII 内容产生一致的实际 token 预算，
        而不是让 CJK 内容超支 2 倍 token。
        """
        if not text:
            return 0
        cjk = count_cjk(text)
        non_cjk = len(text) - cjk
        cjk_weight = (
            TOKEN_ESTIMATION["chars_per_token"]
            / TOKEN_ESTIMATION["chars_per_token_cjk"]
        )
        return int(non_cjk + cjk * cjk_weight)
```

### 4.2 替换所有 `len(self._joined(...))` 调用点

9 处，全在 `agent/tools/memory.py`：

| 行 | 函数 | 现有 | 改为 |
|----|------|------|------|
| 388 | `_char_count` | `len(ENTRY_DELIMITER.join(contents))` | `self._weighted_char_count(ENTRY_DELIMITER.join(contents))` |
| 446 | `_evict_least_active_to_fit` | `len(self._joined(...)) > limit` | `self._weighted_char_count(self._joined(...)) > limit` |
| 534 | `add` | `len(self._joined(...))` | `self._weighted_char_count(self._joined(...))` |
| 537 | `add` | `len(self._joined(contents))` | `self._weighted_char_count(self._joined(contents))` |
| 595 | `_append_to_target` | `len(self._joined(...)) > limit` | `self._weighted_char_count(self._joined(...)) > limit` |
| 602+627 | `_append_to_target` | `len(combined)` | `self._weighted_char_count(combined)` |
| 750 | `replace` | `len(self._joined(test_contents))` | `self._weighted_char_count(self._joined(test_contents))` |
| 922 | `_render_block` | `current = len(self._joined(contents))` | `current = self._weighted_char_count(self._joined(contents))` |

**注意**：`_char_count`（line 384-388）是其他调用点的入口，改了它就覆盖了 `_success_response`（line 884）的调用。但 `_render_block`（line 922）和各 `add`/`replace`/`_append_to_target`/`_evict_least_active_to_fit` 直接调 `len(self._joined(...))` 不经过 `_char_count`，需要逐处替换。

### 4.3 显示文案

`_render_block`（line 922-931）的 header 显示 `[NN% — X/Y chars]`。改后 X 是加权字符数，Y 是 limit（不变）。用户看到 CJK 内容的百分比会更高（因为加权后 X 更大），这是正确的——反映了实际 token 占用更接近上限。

`_success_response`（line 882-897）的 usage 字符串 `f"{current}/{limit} chars"` 同步——`current` 来自 `_char_count`（已改），显示加权数。

不需要改 Y（limit 值本身）——保持 2200/1375/1375，只是现在 Y 的含义从"原始字符数"变成"加权字符数"。

### 4.4 不需要改的

- **config 值（2200/1375/1375）**：不变。英文内容容量不变（weighted = raw for ASCII），CJK 内容容量缩减到相同的 token 预算。
- **`ENTRY_DELIMITER`**：`"\n§\n"` 中的 `§` 是非 CJK 字符，weighted = 1，不影响。
- **`pub/func/cjk.py`**：已有，不改。
- **token 估算常量**：不改。`_weighted_char_count` 从中派生 `CJK_WEIGHT`。
- **数据库**：存储的 `content` 字段不变，只是计数方式变了。
- **UI 设置**：用户在 UI 里设的 char limit 值不变，只是计算方式变了。

---

## 5. 改后效果

### 英文内容（不变）

```
memory_char_limit = 2200
内容：2200 个 ASCII 字符
weighted = 0 × 2 + 2200 × 1 = 2200 ≤ 2200 → 通过
token ≈ 550 ← 和改前一样
```

### 中文内容（缩减到对齐）

```
memory_char_limit = 2200
改前：2200 个中文字符 → len = 2200 ≤ 2200 → 通过 → token ≈ 1100 ← 超支
改后：2200 个中文字符 → weighted = 2200 × 2 + 0 = 4400 > 2200 → 只能放 1100 个中文字符 → token ≈ 550 ← 对齐
```

### 混合内容

```
memory_char_limit = 2200
内容：1000 个中文字符 + 200 个 ASCII 字符
weighted = 1000 × 2 + 200 × 1 = 2200 ≤ 2200 → 通过
token ≈ 500 + 50 = 550 ← 合理
```

---

## 6. 测试

| 测试 | 内容 |
|------|------|
| `test_weighted_char_count_ascii` | 纯 ASCII 100 字符 → weighted = 100 |
| `test_weighted_char_count_cjk` | 纯中文 100 字符 → weighted = 200 |
| `test_weighted_char_count_mixed` | 50 中文 + 50 ASCII → weighted = 150 |
| `test_weighted_char_count_empty` | 空字符串 → 0 |
| `test_memory_add_cjk_respects_limit` | memory 已有 1100 中文字符（weighted=2200），再加 1 个中文字符 → 拒绝或 evict |
| `test_memory_add_ascii_unchanged` | memory 已有 2200 ASCII 字符（weighted=2200），再加 1 个 ASCII 字符 → 拒绝或 evict（行为同改前） |
| `test_render_block_shows_weighted_count` | 1000 中文字符 → header 显示 `X=2000`（不是 1000） |
| `test_evict_cjk_faster_than_ascii` | 混合内容超限时，evict CJK 条目比 evict ASCII 条目省更多 weighted chars |
| 现有 memory 测试 | 回归：英文内容的 add/evict/replace 行为不变 |

---

## 7. 风险

| 风险 | 缓解 |
|------|------|
| CJK 内容容量缩减 | 这是正确的——改前 CJK 超支 2 倍 token，改后与英文对齐 |
| 用户已有的 CJK memory 条目超限 | 写入时限制不追溯——已有的不删，下次 add/append 时触发 evict |
| `CJK_WEIGHT` 随 token 估算常量变化 | 从配置派生，不硬编码；常量变时权重自动跟随 |
| 显示的百分比变了 | CJK 内容现在显示更高的百分比，反映真实 token 占用——是正确的 |
| `_weighted_char_count` 性能 | `count_cjk` 是 O(n) 遍历，memory 条目通常 < 200 条 × ~100 字符 = ~20K 字符，可忽略 |

### Kill criteria

- 若 CJK 内容缩减过多影响用户体验，降低 `CJK_WEIGHT` 到 1.5（手动覆盖配置派生的值）
- 若 `count_cjk` 性能有问题（超大 memory），缓存 weighted count per entry

---

## 8. 验收

- 纯 ASCII 100 字符 → `_weighted_char_count` 返回 100
- 纯中文 100 字符 → 返回 200
- 混合 50 中文 + 50 ASCII → 返回 150
- memory add 1100 个中文字符（weighted=2200）→ 刚好到达上限
- memory add 1101 个中文字符 → 超限，触发 evict 或拒绝
- memory add 2200 个 ASCII 字符 → 刚好到达上限（行为同改前）
- `_render_block` 显示的 X 是加权数
- `_success_response` 的 usage 字符串是加权数
- 英文内容的 add/evict/replace 行为不变（回归）
- 所有现有 memory 测试通过
