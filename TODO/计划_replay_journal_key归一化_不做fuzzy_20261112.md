# 计划：Replay Journal Key 归一化（不做 fuzzy）

- 日期：2026-11-12
- 状态：**待评审**
- 来源：TaskFlow 弱点补救核实——replay 匹配优化；结论：fuzzy 不做（任务执行系统错配风险 > 收益），改做输入归一化（确定性、安全）
- 风险等级：**低** —— 只改 journal key 构建的归一化预处理，不改匹配算法、不改降级路径

---

## 0. 背景

### 0.1 现状（已核实）

- journal key = SHA-256 of canonical JSON(prompt + agent_type + model + schema + isolation)（`vendor/ldw/_journal.py:41-73`）
- 精确匹配（`InMemoryJournalStore.get` / `_persistence.py:178-194`）
- miss → leaf 真实执行
- partial 失败 → LDW degrade 到 TaskFlow（`auto_degrade_enabled` + `max_failed_steps`），成功结果**回写 journal**
- label/phase 排除（仅显示用）

**miss 非脆性**——降级到真实执行 + 回写，系统随使用自我改进（越跑越命中）。replay 脆性的"降级"部分已解决。

### 0.2 为什么不做 fuzzy

fuzzy 匹配"相似但不完全相同"的 variant。**在任务执行系统里，返回错的缓存结果引发连锁错误**——下游 step 建在错误输出上，debug 成本远超省的一次执行。

key 的每个字段（prompt / agent_type / model / schema / upstream output）都因**可能影响输出**才参与哈希。fuzzy 跨这些差异匹配，正好命中"看起来像但其实不一样"的最危险场景。

精确匹配安全（相同输入→保证相同输出），fuzzy 概率安全（相似输入→可能相似但也可能不）。任务执行系统不该赌概率。

### 0.3 归一化是安全替代

归一化 = 哈希前规范化输入。两个输入归一化到相同 = **语义相同**（确定性的），安全命中。捕获"措辞微差/顺序不同但语义相同"的场景（fuzzy 唯一有价值的部分），用确定性方式做，无错配风险。

---

## 1. 设计

### 1.1 归一化什么（安全：不影响语义的变换）

在 `journal_key()` 构建哈希前，对输入做归一化：

| 归一化项 | 做法 | 为什么安全 |
|---|---|---|
| prompt 文本 | 行尾去空白 + 统一换行（\n）+ 折叠连续空白 | 空白差异不影响任务语义 |
| Unicode | NFKC 规范化 | 同文不同 Unicode 形式语义相同 |
| extra_tools（agent_type 组成部分）| 排序后再拼 | 工具集顺序不影响语义（集合语义） |
| 路径（若出现在 prompt/args） | 规范化（resolve + 统一分隔符 + Windows 大小写不敏感） | 同路径不同表示指向同文件 |

### 1.2 不归一化什么（影响语义，必须参与区分）

| 字段 | 不归一化原因 |
|---|---|
| model | 不同模型输出不同 |
| schema | 不同 schema 期望输出不同 |
| upstream output（variant_input） | 下游依赖它，不同则结果应不同 |
| functional_role / step_id | 不同角色/步骤语义不同 |
| prompt 的语义内容 | 不改写措辞/不改语义（只去空白/Unicode 形式） |

**关键边界**：归一化只做"不影响语义的表示层变换"（空白/Unicode/顺序/路径形式），不做"语义层变换"（不改写措辞、不近义替换、不提取语义）。这是 fuzzy 和归一化的本质区别——fuzzy 跨语义差异，归一化只在表示层。

### 1.3 实现

`vendor/ldw/_journal.py::journal_key`（行 41-73）在 canonical JSON 序列化前加归一化预处理：

```python
def _normalize_prompt(prompt: str) -> str:
    """表示层归一化——不影响语义的变换。"""
    import unicodedata
    # NFKC + 行尾去空白 + 统一换行 + 折叠连续空白
    s = unicodedata.normalize("NFKC", prompt)
    lines = [line.rstrip() for line in s.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    s = "\n".join(lines)
    s = " ".join(s.split())  # 折叠连续空白（保留换行结构：先按行处理再 join）
    return s

def _normalize_agent_type(agent_type: str) -> str:
    """extra_tools 部分排序——工具集顺序不影响语义。"""
    # agent_type = functional_role + extra_tools + step_id
    # 若 extra_tools 是列表拼接，排序后再拼
    ...  # 实现时解析 agent_type 结构，对 extra_tools 部分排序

def journal_key(*, prompt, agent_type, model, schema, isolation, ...) -> str:
    prompt = _normalize_prompt(prompt)
    agent_type = _normalize_agent_type(agent_type)
    # 其余不变
    payload = _canonical_json({**{"prompt": prompt, "agent_type": agent_type, ...}})
    return hashlib.sha256(payload).hexdigest()
```

### 1.4 兼容性

- 归一化改变 key → 已有 journal 缓存的 key 会 miss（旧 key 是未归一化的）
- 影响：旧缓存失效，下次跑会重新执行 + 回写新 key。**一次性损失**，不是破坏
- 可选：迁移期双查（先查归一化 key，miss 再查旧 key）——但 journal 是性能优化缓存，重跑成本可控，**不做双查**，直接切归一化 key，旧缓存自然过期

---

## 2. 不做清单

- **不做 fuzzy/编辑距离/相似度匹配**——任务执行系统错配风险不可接受
- **不做语义归一化**（近义替换、prompt 改写、语义提取）——这等于 fuzzy，有错配风险
- **不做双查迁移**——journal 是缓存，旧 key 失效重跑即可
- **不改降级路径**（miss→执行→回写 已正确）
- **不改 PTC 精确文件名查找**（PTC 是 step-scoped 精确匹配，不涉及 variant key）

---

## 3. 变更清单

| 文件 | 变更 |
|---|---|
| `vendor/ldw/_journal.py` | `journal_key()` 加 `_normalize_prompt` + `_normalize_agent_type` 预处理 |

**不改**：`_persistence.py`（SQLite 查询不变，只是 key 值变了）、`replay.py`（降级逻辑不变）、`taskflow_adapter.py`（fallback 不变）、PTC compiler。

---

## 4. 验收

- 同 prompt 不同空白（行尾空格/换行符差异）→ 归一化后同 key → 命中
- 同 prompt 不同 Unicode 形式 → NFKC 后同 key → 命中
- extra_tools 顺序不同 → 排序后同 key → 命中
- 不同 model/schema/upstream output → key 不同 → miss（正确区分）
- 归一化不改语义内容（只去空白/Unicode 形式/顺序）→ 语义不同的 prompt 不会误命中
- 旧 journal 缓存自然失效（新 key 不匹配旧），重跑后回写新 key
- 回归：`tests/agent/tools/ldw/test_replay.py` + `test_journal.py` 全过

---

## 5. 备注

- 这是个**小改**（一个文件加两个归一化函数），低风险
- 收益：捕获"语义相同、表示微差"的 variant（措辞空白/Unicode/工具顺序），提高命中率
- 不做 fuzzy 的根本原因：任务执行系统返回错缓存会连锁错误，确定性归一化安全、概率性 fuzzy 危险
- 如果实测命中率已经够高（精确匹配 + 回写自改进够用），这个归一化可以不做——它是锦上添花，非必须
