# 计划：技能 description 增加使用条件 + 反向条件

- 建档日期：2026-10-09
- 状态：**计划（未开工）**
- 触发问题：nudge 和 curator 自动创建/维护 `skills/auto/` 技能时，frontmatter description 只写能力概述，缺"何时用"和"何时不用"。模型在 `<available_skills>` 索引里看不到触发/排除信号 → auto/ 技能触发率低 → 经验沉淀了用不上

## 现状

### skill_creator 定义了正确标准但 nudge/curator 不遵守

| 谁 | 对 description 的要求 |
|---|---|
| **skill_creator** | "describing what the skill is, **and when it should be used**" — 能力 + 使用条件 |
| **nudge** | 完全没提 description 该写什么 — LLM 自由发挥 |
| **curator fallback** | `Umbrella skill consolidating N related skills.` — 纯能力 |
| **curator LLM prompt** | 只说"must have: name, description" — 不指定内容 |
| **curator LLM body 指令** | "Include a '## When to use' section" — 在正文，不在 frontmatter |

### 断裂的学习闭环

```
nudge 提取经验 → 创建 skills/auto/ 技能
  ↓
description 只有 "Async password hashing utilities"（能力）
  ↓
<available_skills> 里模型看不到"何时用""何时不用"
  ↓
模型不知道什么时候触发 → 不触发
  ↓
经验沉淀了但用不上
```

## 设计

### 新增两个 frontmatter 字段

在 SKILL.md frontmatter 里新增 `when_to_use` 和 `when_not_to_use`：

```yaml
---
name: async-password-hashing
description: Async password hashing utilities with passlib CryptContext.
when_to_use: When implementing authentication that needs non-blocking bcrypt/scrypt verification, or when synchronous hashing blocks the event loop.
when_not_to_use: For simple CLI scripts where blocking is acceptable, or when the project already uses argon2.
created_by: curator
---
```

### 渲染到 `<available_skills>` 块

`skills/loader.py` 的 `get_skills_text()` 渲染逻辑扩展：

```xml
<available_skills>
  <skill name="async-password-hashing">
    <description>Async password hashing utilities with passlib CryptContext.</description>
    <when_to_use>When implementing authentication that needs non-blocking bcrypt/scrypt verification...</when_to_use>
    <when_not_to_use>For simple CLI scripts where blocking is acceptable...</when_not_to_use>
  </skill>
</available_skills>
```

模型在系统提示里就能看到三段信息：能力 + 正向触发条件 + 反向排除条件。

### 三个消费方都扩展

| 消费方 | 当前 | 新增 |
|---|---|---|
| `<available_skills>` 系统提示 | `<description>` | 加 `<when_to_use>` + `<when_not_to_use>` |
| 前端技能面板 tooltip | `description[:240]` | 加 `when_to_use[:200]` + `when_not_to_use[:200]` |
| curator review 列表 | `desc={desc}` | 加 `when={when_to_use}` |

### 长度限制（CJK-aware token-based 截断，与压缩器单条截断一致）

三个字段统一走 CJK-aware token 截断——纯 `[:N]` 字符截断对英文太松（200 字符 = 50 token）、对中文太紧（200 字符 = 100 token）。改用和压缩器 `truncate_by_tokens` 相同的方案：按 token 预算截断，CJK 字符 0.5 token/char、ASCII 字符 0.25 token/char。

上限在 `config/features/agent_side/` 或 `loader.py` 常量里定义：

| 字段 | token 预算 | 英文约字符 | 中文约字符 | 理由 |
|---|---|---|---|---|
| `description` | 60（已有 `_DESCRIPTION_MAX_CHARS=240` 对应） | ~240 | ~120 | 能力概述一行 |
| `when_to_use` | **50** | ~200 | ~100 | 触发场景，比能力概述略短（更聚焦） |
| `when_not_to_use` | **50** | ~200 | ~100 | 排除条件，同理 |

**实现**：复用 `pub/func/message/token_truncation.py` 的 `truncate_by_tokens`（已在压缩器单条截断计划里设计），或内联轻量版：

```python
from pub.func.cjk import is_cjk_codepoint

_DESCRIPTION_MAX_TOKENS = 60
_WHEN_TO_USE_MAX_TOKENS = 50
_WHEN_NOT_TO_USE_MAX_TOKENS = 50

def _truncate_text_to_tokens(text: str, max_tokens: int) -> str:
    """CJK-aware 截断：按 token 预算切，CJK 0.5 token/char、ASCII 0.25 token/char。
    超预算时头 60% + 尾 30% + 省略标记。已短于预算的不动（幂等）。"""
    from pub.func.estimate_tokens import estimate_text_tokens
    total = estimate_text_tokens(text)
    if total <= max_tokens:
        return text

    # 逐字符走，累积 token 成本，找头/尾切点
    head_tokens = int(max_tokens * 0.6)
    tail_tokens = int(max_tokens * 0.3)

    accumulated = 0.0
    head_chars = 0
    for i, ch in enumerate(text):
        cp = ord(ch)
        accumulated += 0.5 if is_cjk_codepoint(cp) else 0.25
        if accumulated >= head_tokens:
            head_chars = i + 1
            break
    else:
        head_chars = len(text)

    accumulated = 0.0
    tail_chars = 0
    for ch in reversed(text):
        cp = ord(ch)
        accumulated += 0.5 if is_cjk_codepoint(cp) else 0.25
        if accumulated >= tail_tokens:
            break
        tail_chars += 1

    if head_chars + tail_chars >= len(text):
        return text  # 头尾加起来比原文长 → 不截

    omitted = len(text) - head_chars - tail_chars
    return f"{text[:head_chars]}…[{omitted} chars omitted]…{text[-tail_chars:] if tail_chars > 0 else ''}"


# get_skills_text() 渲染时：
lines.append(f"    <description>{_xml_text(_truncate_text_to_tokens(s['description'], _DESCRIPTION_MAX_TOKENS))}</description>")
if s.get("when_to_use"):
    lines.append(f"    <when_to_use>{_xml_text(_truncate_text_to_tokens(s['when_to_use'], _WHEN_TO_USE_MAX_TOKENS))}</when_to_use>")
if s.get("when_not_to_use"):
    lines.append(f"    <when_not_to_use>{_xml_text(_truncate_text_to_tokens(s['when_not_to_use'], _WHEN_NOT_TO_USE_MAX_TOKENS))}</when_not_to_use>")
```

**关键特性**：
- 纯英文 50 token → ~200 字符（和旧 200 字符 cap 行为一致）
- 纯中文 50 token → ~100 字符（更紧但 token 等价）
- 混合文本按实际 CJK 比例插值
- 截断格式 `head 60% + …[N chars omitted]… + tail 30%`（和压缩器单条截断格式一致）
- 已短于预算的文本不动（幂等）
- 复用已有的 `is_cjk_codepoint`（`pub/func/cjk.py`）+ `estimate_text_tokens`（`pub/func/estimate_tokens.py`）

**`<available_skills>` 块的 token 预算**：假设 20 个技能 × (60+50+50) = 3,200 token。在 131K 窗口里可忽略。50+ 技能时 = 8,000 token，仍可接受。如果未来需要整块 token 预算上限，可以加一个按优先级截断的机制——这是 follow-up，不在本计划内。

**nudge/curator prompt 也要约束长度**：

nudge 的 frontmatter 标准加一句：

```
- `when_to_use`: max ~50 tokens (~200 chars English / ~100 chars Chinese) — concise
- `when_not_to_use`: max ~50 tokens — concise
```

curator 的 system_msg 加一句：

```
- Keep each frontmatter field under ~50 tokens (description ~60). 
  CJK characters count more (~2 chars/token vs 4 for ASCII).
```

**curator fallback 的启发式提取也要截断**：

```python
def _extract_use_cases_from_reasons(reasons: list[str]) -> str:
    # ... 提取逻辑 ...
    result = "; ".join(triggers[:3])
    return _truncate_text_to_tokens(result, _WHEN_TO_USE_MAX_TOKENS)  # ← CJK-aware 截断
```

### nudge prompt 修改

`agent/middlewares/summarization/nudges.py` 的 Part 2（Skill Library Update）加一段：

```
### Frontmatter fields (IMPORTANT — these are the ONLY text the agent sees
in its <available_skills> index; they are the primary trigger mechanism)

When creating or updating a SKILL.md, the frontmatter must have:

- `name`: class-level kebab-case identifier
- `description`: one-line capability summary (what the skill does)
- `when_to_use`: when the agent should load this skill — specific scenarios,
  triggers, user intents. Example: "When implementing authentication that
  needs non-blocking bcrypt/scrypt verification, or when synchronous hashing
  blocks the event loop."
- `when_not_to_use`: when the agent should NOT use this skill — exclusions,
  alternatives, or conditions where the approach doesn't apply. Example:
  "For simple CLI scripts where blocking is acceptable, or when the project
  already uses argon2."

Both `when_to_use` and `when_not_to_use` are REQUIRED for new skills created
in skills/auto/. When patching an existing skill that lacks these fields,
add them.
```

### curator prompt 修改

`context_engine/curator/umbrella.py` 的 system_msg（`:105-137`）改两处：

**frontmatter 要求**（`:117`）：

```
- The frontmatter of SKILL.md must have: name, description, when_to_use,
  when_not_to_use, created_by: curator.
  - description: one-line capability summary
  - when_to_use: specific scenarios when the agent should load this skill
  - when_not_to_use: exclusions — when NOT to use this skill
```

**body 指令**（`:134` 保留 `## When to use` 但改为引用 frontmatter）：

```
- Include a '## When to use' section that elaborates on the frontmatter
  when_to_use with concrete examples and trigger patterns.
- Include a '## When NOT to use' section for exclusions and alternatives.
```

### curator fallback 修改

`umbrella.py:69-83` 的 `_fallback_umbrella` 生成纯能力 description——改为加入被合并技能的使用场景：

```python
def _fallback_umbrella(umbrella: str, reasons: list[str], source_content: str) -> str:
    # 从 reasons 里提取使用场景关键词
    use_cases = _extract_use_cases_from_reasons(reasons)
    not_use_cases = _extract_exclusions_from_reasons(reasons)

    return (
        "---\n"
        f"name: {umbrella}\n"
        f"description: Umbrella skill consolidating {len(reasons)} related skills.\n"
        f"when_to_use: {use_cases}\n"
        f"when_not_to_use: {not_use_cases}\n"
        "created_by: curator\n"
        "---\n\n"
        f"# {umbrella}\n\n"
        f"## When to use\n\n{use_cases}\n\n"
        f"## When NOT to use\n\n{not_use_cases}\n\n"
        "Consolidated from the following skills:\n\n"
        + "\n".join(reasons)
        + "\n\n"
        + source_content
        + "\n"
    )

def _extract_use_cases_from_reasons(reasons: list[str]) -> str:
    """从合并理由里提取使用场景。启发式：提取 'when'/ 'use' / 'for' 开头的片段。"""
    triggers = []
    for r in reasons:
        lower = r.lower()
        for marker in ("when ", "use ", "for ", "trigger"):
            if marker in lower:
                # 提取包含 marker 的句子
                idx = lower.index(marker)
                sentence = r[idx:idx + 150].split(".")[0].strip()
                if sentence and sentence not in triggers:
                    triggers.append(sentence)
                break
    if not triggers:
        return f"Use when the task involves {reasons[0].split(':')[0].lower() if reasons else 'this skill'}" if reasons else "See skill body for usage conditions."
    return "; ".join(triggers[:3])  # 最多 3 条

def _extract_exclusions_from_reasons(reasons: list[str]) -> str:
    """从合并理由里提取排除场景。启发式：提取 'not'/ 'avoid'/ 'alternative' 片段。"""
    exclusions = []
    for r in reasons:
        lower = r.lower()
        for marker in ("not ", "avoid ", "alternative", "except "):
            if marker in lower:
                idx = lower.index(marker)
                sentence = r[idx:idx + 150].split(".")[0].strip()
                if sentence and sentence not in exclusions:
                    exclusions.append(sentence)
                break
    if not exclusions:
        return "See skill body for exclusion conditions."
    return "; ".join(exclusions[:3])
```

### loader.py 修改

`skills/loader.py` 的 `scan_skills()` 读取新字段：

```python
meta = parse_frontmatter(content)
name = str(meta.get("name", skill_file.parent.name))
desc = str(meta.get("description", ""))
when_to_use = str(meta.get("when_to_use", ""))          # 新增
when_not_to_use = str(meta.get("when_not_to_use", ""))   # 新增
# ...
skills.append({
    "name": name,
    "description": desc,
    "when_to_use": when_to_use,               # 新增
    "when_not_to_use": when_not_to_use,       # 新增
    "location": location,
    "scope": scope,
    "active": active,
})
```

`get_skills_text()` 渲染新字段：

```python
def get_skills_text(skills: list[dict], *, exact: list[str] | None = None) -> str:
    # ...
    lines = ["<available_skills>"]
    for s in filtered:
        lines.append(f'  <skill name="{_xml_attr(s["name"])}">')
        lines.append(f"    <description>{_xml_text(s['description'])}</description>")
        if s.get("when_to_use"):
            lines.append(f"    <when_to_use>{_xml_text(s['when_to_use'])}</when_to_use>")
        if s.get("when_not_to_use"):
            lines.append(f"    <when_not_to_use>{_xml_text(s['when_not_to_use'])}</when_not_to_use>")
        lines.append("  </skill>")
    lines.append("</available_skills>")
    return "\n".join(lines)
```

`skills_catalog()` 前端 API 扩展：

```python
{
    "name": ...,
    "description": ...,
    "when_to_use": ...,           # 新增
    "when_not_to_use": ...,       # 新增
    "builtin": ...,
    "required": ...,
}
```

### 向后兼容

- 现有 SKILL.md 没有 `when_to_use` / `when_not_to_use` 字段 → `meta.get("when_to_use", "")` 返回空串 → `<available_skills>` 块里不渲染该标签 → 行为不变
- 现有 builtin 技能（cron、taskflow、todolist 等）的 frontmatter 不改——它们已有足够的 description 触发信号（skill_creator 标准已覆盖）
- 只有 nudge 和 curator **新创建/合并**的技能会被要求带新字段

### XML 转义

`when_to_use` 和 `when_not_to_use` 的内容来自 LLM 生成——可能含 `<`、`>`、`&`。已有的 `_xml_text()` 函数处理转义，新字段也走它。

## 改动清单

| 文件 | 改动 | 行数 |
|---|---|---|
| `skills/loader.py` | 读取 + 渲染 `when_to_use` / `when_not_to_use` | ~20 |
| `agent/middlewares/summarization/nudges.py` | prompt 加 frontmatter 标准约束 | ~15 |
| `context_engine/curator/umbrella.py` | system_msg 改 + fallback 改 | ~40 |
| `context_engine/curator/helpers.py` | `_read_skill_description` 扩展为读两个字段（如 curator review 需要） | ~10 |
| `context_engine/curator/review.py` | review 列表加 when_to_use 显示 | ~5 |
| `tests/skills/test_loader.py` | 新字段读取 + 渲染测试 | ~30 |
| `tests/agent/middlewares/test_compression_nudges.py` | nudge 创建技能带新字段 | ~20 |
| `tests/context_engine/test_curator_umbrella.py` | fallback + LLM 生成带新字段 | ~20 |

**合计 ~160 行**

## 验收

### nudge 创建的技能

- nudge 自动创建 `skills/auto/xxx/SKILL.md` → frontmatter 含 `description` + `when_to_use` + `when_not_to_use`
- `<available_skills>` 块里模型能看到三个标签
- `when_to_use` 描述具体触发场景（不是泛泛的"when needed"）
- `when_not_to_use` 描述排除条件（不是空的）

### curator 合并的 umbrella 技能

- curator fallback 生成的 SKILL.md 含新字段（从 reasons 启发式提取）
- curator LLM 生成的 SKILL.md 含新字段（prompt 要求）

### 向后兼容

- 现有 builtin 技能无新字段 → `<available_skills>` 不渲染 `<when_to_use>` 标签 → 行为不变
- 现有 `skills/auto/` 技能无新字段 → 同上，下次 nudge 更新时补上

### 前端

- 技能面板 tooltip 显示 `when_to_use` + `when_not_to_use`（截断 240 字符）

## 风险

- **token 成本**：`<available_skills>` 块变大——每个技能多了 2 个标签。假设 20 个技能 × (when_to_use ~100 字符 + when_not_to_use ~80 字符) = ~3600 字符 ≈ ~900 token。在 131K 窗口里可忽略。但可以通过 `_DESCRIPTION_MAX_CHARS` 类似机制给 `when_to_use` / `when_not_to_use` 也加上限（如 200 字符）。
- **LLM 写质量**：nudge/curator 的 LLM 可能写泛泛的"Use when needed"——prompt 里给了具体示例 + 明确说"specific scenarios"来约束。
- **旧技能迁移**：现有 `skills/auto/` 技能没有新字段。不需要批量迁移——下次 nudge 更新时自然补上。如果想在运行时自动补，可以在 loader 里对缺失 `when_to_use` 的技能用 description 做启发式推断——但这增加复杂度，不值得。

## 不做的事

- **不改现有 builtin 技能的 frontmatter**——它们的 description 已经按 skill_creator 标准写了能力 + 使用条件（skill_creator 的示例就是"能力 + Use when..."）。只有 auto/ 技能需要补。
- **不做 `when_to_use` 的自动推断**——不做"从 description 提取使用场景"的 NLP。LLM 生成时直接写。
- **不做 per-session 的 when_to_use 覆盖**——when_to_use 是技能属性，不随 session 变。

---

## 伞状技能的子能力使用条件

### 问题

curator 合并多个技能为 umbrella 时，每个被合并的技能变成 umbrella 的一个子能力（`## 段落`）。umbrella 的 frontmatter `when_to_use` / `when_not_to_use` 只覆盖**整体**——"什么时候加载这个 umbrella 技能"。但子能力各有各的触发/排除条件，和 umbrella 整体不一定一致。

例如 umbrella `async-auth` 合并了三个子能力：

| 子能力 | when_to_use | when_not_to_use |
|---|---|---|
| 密码哈希 | 实现 bcrypt/scrypt 验证时 | 项目已用 argon2 时 |
| JWT 签发 | 需要 stateless token 认证时 | 用 session-based 认证时 |
| OAuth 集成 | 需要第三方登录（Google/GitHub）时 | 只需用户名密码登录时 |

umbrella 的 frontmatter `when_to_use` 是"when implementing authentication"——覆盖整体。但模型加载了 umbrella 后，需要知道**该用哪个子能力**——这需要子能力级别的触发条件。

### 写在哪里

**在 SKILL.md 正文的每个子能力 `##` 段落开头**，作为结构化 block：

```markdown
## Async Password Hashing

**When to use:** When implementing authentication that needs non-blocking
bcrypt/scrypt verification, or when synchronous hashing blocks the event loop.

**When NOT to use:** For simple CLI scripts where blocking is acceptable,
or when the project already uses argon2.

### Steps
1. Import passlib...
2. Configure CryptContext...

## JWT Token Signing

**When to use:** When stateless authentication is needed (APIs, microservices).

**When NOT to use:** When session-based auth is already in place and
token revocation is a hard requirement.

### Steps
1. ...
```

**为什么放这里**：

| 候选位置 | 为什么不选 |
|---|---|
| frontmatter | frontmatter 是技能级的，子能力不是独立技能——没有自己的 SKILL.md |
| `references/sub.md` 独立文件 | 模型要先读 SKILL.md 发现子能力存在，再读 reference——多一步。而且 reference 文件没有 frontmatter，不在 `<available_skills>` 里 |
| 正文 `##` 段落 ✅ | `skill_view` 一次性加载全文，模型直接看到每个子能力的触发/排除条件 |

### curator prompt 修改

`context_engine/curator/umbrella.py` 的 system_msg 的 SYNTHESIS RULES 加一条：

```python
system_msg = (
    ...
    "SYNTHESIS RULES:\n"
    "- Synthesize and deduplicate: merge overlapping instructions, unify code patterns, "
    "remove redundancy, keep every unique technique.\n"
    "- Organize SKILL.md with clear sections. Use ## headings for each concern area.\n"
    "- Each ## section (sub-capability) MUST start with:\n"
    "    **When to use:** <specific scenarios for THIS sub-capability>\n"
    "    **When NOT to use:** <exclusions for THIS sub-capability>\n"
    "  These are sub-capability-level conditions, narrower than the umbrella's\n"
    "  frontmatter when_to_use/when_not_to_use (which cover the whole skill).\n"
    "  A sub-capability's when_to_use should specify what scenario within the\n"
    "  umbrella's scope triggers THIS section, not repeat the umbrella-level\n"
    "  condition.\n"
    ...
)
```

### curator fallback 修改

`umbrella.py` 的 `_fallback_umbrella` 为每个被合并的技能生成子能力段落时加触发/排除 block：

```python
def _fallback_umbrella(umbrella: str, reasons: list[str], source_content: str) -> str:
    use_cases = _extract_use_cases_from_reasons(reasons)
    not_use_cases = _extract_exclusions_from_reasons(reasons)

    # 为每个被合并的技能生成子能力段落
    sections = []
    for reason in reasons:
        # reason 格式通常是 "skill-name: description" 或 "skill-name: why it was merged"
        parts = reason.split(":", 1)
        sub_name = parts[0].strip() if parts else reason[:50]
        sub_desc = parts[1].strip() if len(parts) > 1 else reason
        sub_when = _extract_use_cases_from_reasons([reason])
        sub_not_when = _extract_exclusions_from_reasons([reason])

        sections.append(
            f"## {sub_name}\n\n"
            f"**When to use:** {sub_when}\n\n"
            f"**When NOT to use:** {sub_not_when}\n\n"
            f"{sub_desc}\n"
        )

    return (
        "---\n"
        f"name: {umbrella}\n"
        f"description: Umbrella skill consolidating {len(reasons)} related skills.\n"
        f"when_to_use: {use_cases}\n"
        f"when_not_to_use: {not_use_cases}\n"
        "created_by: curator\n"
        "---\n\n"
        f"# {umbrella}\n\n"
        f"## When to use\n\n{use_cases}\n\n"
        f"## When NOT to use\n\n{not_use_cases}\n\n"
        + "\n\n".join(sections)
        + "\n\n---\n\nConsolidated source content:\n\n"
        + source_content
        + "\n"
    )
```

### 两层触发条件的层次

| 层级 | 位置 | 覆盖范围 | 何时被模型看到 |
|---|---|---|---|
| **umbrella 级** | frontmatter `when_to_use` / `when_not_to_use` | 整个技能——什么时候加载这个 umbrella | `<available_skills>` 块（每轮） |
| **子能力级** | 正文 `## 子能力` 段落开头的 `**When to use:**` / `**When NOT to use:**` | 该子能力——umbrella 加载后该用哪段 | `skill_view` 加载全文后 |

模型决策链：

```
<available_skills> 看到 umbrella 的 when_to_use → 判断是否加载这个技能
  ↓ 加载
skill_view 读到 SKILL.md 正文 → 每个子能力的 When to use / When NOT to use
  ↓ 判断
选择匹配的子能力段落执行
```

### nudge 创建非 umbrella 技能

nudge 创建的 `skills/auto/` 技能通常是单一能力（不是 umbrella），只有一层 `when_to_use` / `when_not_to_use`（在 frontmatter）。不需要子能力级——单一技能的正文不需要再分 `##` 子段落的触发条件。

但如果 nudge 创建的技能后来被 curator 合并成 umbrella，curator 会在合并时为每个原技能生成子能力段落 + 触发条件。原始 frontmatter 的 `when_to_use` 成为子能力段落的 `**When to use:**` 来源。

### 验收

- curator LLM 生成的 umbrella SKILL.md → 每个 `## 子能力` 段落开头有 `**When to use:**` + `**When NOT to use:**`
- curator fallback 生成的 umbrella → 同上
- 子能力的 `When to use` 比 umbrella frontmatter 的 `when_to_use` 更具体（不重复 umbrella 级条件）
- 模型 `skill_view` 加载全文后能看到每个子能力的触发/排除条件
- 测试：fallback 生成的 umbrella 有结构化的子能力段落 + 触发 block

## 关键文件路径

| 文件 | 角色 |
|---|---|
| `skills/loader.py` | 修改：读取 + 渲染新字段 |
| `agent/middlewares/summarization/nudges.py` | 修改：prompt 加 frontmatter 标准 |
| `context_engine/curator/umbrella.py` | 修改：system_msg + fallback |
| `context_engine/curator/helpers.py` | 修改：读取新字段 |
| `context_engine/curator/review.py` | 修改：review 列表显示新字段 |
| `tests/skills/test_loader.py` | 新增：新字段测试 |
| `tests/agent/middlewares/test_compression_nudges.py` | 新增：nudge 创建带新字段 |
| `tests/context_engine/test_curator_umbrella.py` | 新增：fallback + LLM 带新字段 |
