# Cron-Skill 绑定改造计划

> **状态**: 待实施
> **创建日期**: 2026-09-30
> **目标**: 为 sherry_agent 的 cron 系统实现与 hermes-agent 一致的 Cron-Skill 强绑定机制
>
> **参照来源**: hermes-agent `cron/` 模块（7 条创建路径，`create_job()` 单一汇聚点，`_build_job_prompt()` 预加载 skill，`rewrite_skill_refs()` 引用维护，`referenced_skill_names()` 保护机制）

---

## 1. 现状对比

### 1.1 hermes-agent（目标参照）

| 层           | 机制                                                                                                                  |
| ------------ | --------------------------------------------------------------------------------------------------------------------- |
| **存储**     | job dict 含 `skills: List[str]`（有序 skill 名称列表）                                                                |
| **执行**     | `_build_job_prompt()` 在 job 触发时逐个加载 skill（`skill_view()`），内容包裹在 `[IMPORTANT: ...]` 中拼接到 prompt 前 |
| **维护**     | `rewrite_skill_refs()` — curator 整合/裁剪 skill 后重写 job 的 skill 引用                                             |
| **保护**     | `referenced_skill_names()` — 返回所有 job 引用的 skill 名集合，curator 据此保护被引用 skill 不被归档                  |
| **备份恢复** | `_restore_cron_skill_links()` — curator 回滚时外科手术式恢复 `skills`/`skill` 字段                                    |
| **创建入口** | `cronjob()` 工具 `skills` 参数 + 蓝图预定义 `AutomationBlueprint.skills`                                              |

#### hermes-agent 的 `skills` 字段绑定 vs 文本提及

hermes-agent 的 `skills` 字段在所有创建路径中都是**可选的**（默认 `None` → `[]`），用户可以创建不绑定任何 skill 的 cron job。这引出两种 skill 使用方式：

| 方式                       | 机制                                                                                                                                                                                                                                      | 可靠性                                                               |
| -------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| **通过 `skills` 字段绑定** | 调度器在 `_build_job_prompt()` 中预加载 skill 内容（`skill_view()`），注入到 prompt 第一条消息前。Agent 从第一轮就看到完整 skill 指令。                                                                                                   | **确定性** — 保证加载                                                |
| **仅在 prompt 文本中提及** | Agent 启动时只有用户 prompt 文本。hermes-agent 的 cron agent **拥有 `skill_view` 工具**（`toolsets.py` 中 `_HERMES_CORE_TOOLS` 含 `skill_view`/`skills_list`，且不在禁用列表 `["cronjob", "messaging", "clarify"]` 中），可自主调用加载。 | **非确定性** — 依赖 LLM 是否识别为 skill 名称并主动调用 `skill_view` |

> sherry_agent 的 cron agent 工具集只有 `[python_repl, read_file, write_file]`（通过 `BUILD_BACKGROUND_AGENT_TOOLS` hook），**不含 `skill_view` 工具**。因此 sherry_agent 的"文本提及 skill"路径完全不可靠 — agent 无法主动加载 skill 内容，只能依赖 prompt 文本中的指令描述。本次改造采用**预加载注入**模式（方案 B）正好弥补此差距。

#### hermes-agent 的前端 skill 选择 UI

hermes-agent 的 Dashboard 有完整的 skill 选择器：

**Cron job 创建/编辑表单**（`web/src/pages/CronPage.tsx:401-414`）：

```tsx
<Label>Skills (optional)</Label>
<NameCheckboxPicker
  available={availableSkills}
  selected={form.skills}
  onChange={(skills) => update("skills", skills)}
  emptyLabel="No skills installed for this profile."
/>
<p>Selected skills are loaded before the prompt runs — the cron sets when, the skill sets how.</p>
```

- **复选框多选列表**，从 API 加载已安装的 skills（`api.getSkills(resourceProfile)`）
- 标签明确标注 "Skills (optional)" 和说明 "Selected skills are loaded before the prompt runs"
- 创建和编辑表单都包含此选择器

**Blueprint 表单**（`web/src/components/AutomationBlueprints.tsx`）：

- **无 skill 选择器** — blueprint 的 skills 在 catalog 中预定义（`AutomationBlueprint.skills: tuple = ()`），实例化时自动注入，用户不可在 UI 上增删
- blueprint 表单字段只支持 `text`/`time`/`enum`/`weekdays` 类型，无 skill 类型

#### hermes-agent 的 cron 安全机制

| 安全层                  | 机制                                                                                        | sherry_agent 对应                                                                 |
| ----------------------- | ------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| **生命周期守卫**        | `cron/lifecycle_guard.py:112` — 拒绝 gateway 生命周期命令注入到 cron prompt                 | **无** — sherry 无 gateway 生命周期概念                                           |
| **Prompt 注入扫描**     | `cronjob_tools.py` 的 `_scan_cron_prompt()` — 创建/更新时扫描 prompt 文本                   | **无** — 依赖 skill 本身的注入检测（`skill_view.py:26` 的 `_INJECTION_PATTERNS`） |
| **组装后扫描**          | `scheduler.py` 的 `_scan_assembled_cron_prompt()` — 运行时扫描 skill 内容 + prompt 组装结果 | **无** — 本次改造应在预加载后增加扫描                                             |
| **渗出检测**            | `cronjob_tools.py` — 检测 prompt 中可能泄露系统信息的内容                                   | **无**                                                                            |
| **递归防护**            | `scheduler.py:130` — cron agent 禁用 cronjob 工具                                           | **天然防护** — cron agent 工具集不含 cron skill                                   |
| **不可见 Unicode 净化** | `scheduler.py` — skill 内容中的不可见 Unicode 被 strip + log                                | **无**                                                                            |

> 本次改造应在 `_on_cron_job()` 的 skill 预加载完成后，增加简单的注入扫描（复用 `skill_view.py` 的 `_INJECTION_PATTERNS`）。

hermes-agent 的 cron job **由 agent 和用户共同创建**，所有路径汇聚到 `cron/jobs.py::create_job()`（L1039），应用相同的安全守卫：

| #   | 创建者                 | 入口                         | 代码位置                                | 调用链                                                |
| --- | ---------------------- | ---------------------------- | --------------------------------------- | ----------------------------------------------------- |
| 1   | **Agent（LLM 工具）**  | `cronjob` 工具               | `tools/cronjob_tools.py:659`            | `cronjob(action="create")` → `create_job()`           |
| 2   | **用户 CLI**           | `hermes cron create`         | `hermes_cli/cron.py:294`                | CLI → `_cron_api()` → `cronjob` 工具 → `create_job()` |
| 3   | **用户 slash 命令**    | `/cron add`                  | `hermes_cli/cli_commands_mixin.py:1177` | slash → `cronjob` 工具 → `create_job()`               |
| 4   | **用户 HTTP REST API** | `POST /api/jobs`             | `gateway/platforms/api_server.py:3727`  | aiohttp handler → `create_job()`                      |
| 5   | **用户 Dashboard UI**  | `POST /api/cron/jobs`        | `hermes_cli/web_server.py:10597`        | FastAPI handler → `create_job()`                      |
| 6   | **用户 Blueprint**     | `/blueprint <name> slot=val` | `hermes_cli/blueprint_cmd.py:306`       | `fill_blueprint()` → `create_job()`                   |
| 7   | **用户 Suggestion**    | `/suggestions accept N`      | `hermes_cli/suggestions_cmd.py:100`     | `accept_suggestion()` → `create_job()`                |

hermes-agent 的关键特点：

- **无系统任务自动创建** — 启动时只启动 ticker 线程，不创建任何 job
- **蓝图是模板**（16 个预定义参数化蓝图），不是预创建的 job — 用户必须显式实例化
- **建议系统是 consent-first** — `cron/suggestions.py:20` 声明 "Suggestions never auto-create jobs; acceptance is always explicit"
- **Agent 自我防护** — cron 产生的 agent 禁用 `cronjob` 工具集（`scheduler.py:130`：`disabled = ["cronjob", "messaging", "clarify"]`），防止递归调度
- **两个独立 HTTP 服务器** 都暴露 cron API — API Server（`gateway/platforms/api_server.py`，OpenAI 兼容）+ Dashboard（`hermes_cli/web_server.py`，FastAPI）
- **前端 UI 完整** — `web/src/pages/CronPage.tsx`（React cron 管理页面，含表单创建、ScheduleBuilder 可视化调度器、编辑、暂停/恢复、立即触发、删除）

### 1.2 sherry_agent（当前状态）

| 层           | 现状                                                                                                                  | 差距                   |
| ------------ | --------------------------------------------------------------------------------------------------------------------- | ---------------------- |
| **存储**     | `CronPayload` 只有 `message`/`deliver`/`channel`/`to` — **无 skill 字段**                                             | 缺 `skills` 字段       |
| **执行**     | `_on_cron_job()` (base.py:430) 构建 agent 时只给 `[python_repl, read_file, write_file]` 工具 — **无 skill_view 工具** | agent 无法加载 skill   |
| **注入**     | `build_system_prompt()` 注入 `<available_skills>` XML 列表 — 但 agent 无 `skill_view` 工具，无法加载 skill 内容       | 只能看到列表，无法加载 |
| **维护**     | 无任何 skill 引用追踪/重写机制                                                                                        | 完全缺失               |
| **保护**     | 无 `referenced_skill_names` 等价物                                                                                    | 完全缺失               |
| **API**      | POST /cron body 无 `skills` 参数                                                                                      | 完全缺失               |
| **已有痕迹** | `skill_manage.py` 第 216/976 行注释提到 "cron-job skill reference rewriting"                                          | 仅为注释，未实现       |

#### sherry_agent 的 cron job 创建路径（当前 3 条）

sherry_agent 的 cron job **由 agent 和用户共同创建**，比 hermes-agent 精简：

| #   | 创建者                      | 入口                            | 代码位置                                      | 调用链                                     |
| --- | --------------------------- | ------------------------------- | --------------------------------------------- | ------------------------------------------ |
| 1   | **Agent（LLM 工具）**       | `Cron` 类 `add_job` 方法        | `skills/builtin/core/cron/scripts/core.py:53` | `Cron.add_job()` → `CronService.add_job()` |
| 2   | **用户 HTTP REST API**      | `POST /cron`                    | `server/trigger/http/cron.py:130`             | Robyn handler → `cron_service.add_job()`   |
| 3   | **Agent 自主调用 SKILL.md** | cron SKILL.md 中的 add_job 工具 | `skills/builtin/core/cron/SKILL.md`           | Agent 调用 cron skill → `Cron.add_job()`   |

sherry_agent 当前特点（与 hermes-agent 对比）：

- **无 CLI 命令** — 没有 `hermes cron create` 等价物（桌面 Tauri 应用通过 HTTP API 操作）
- **无 slash 命令** — 没有 `/cron add` 等价物
- **无蓝图模板** — 没有预定义参数化蓝图目录
- **无建议系统** — 没有 consent-first 的 suggestion 机制
- **无前端 Cron 管理 UI** — `client/` 中无 cron 管理页面（仅通过 REST API 操作）
- **无递归防护** — cron 产生的 agent 未禁用 cron 工具（潜在递归调度风险）
- **无系统任务自动创建** — 与 hermes-agent 一致，启动时不创建任何 job
- **单一 HTTP 服务器** — 只有 Robyn（`server/trigger/http/cron.py`），无双服务器

### 1.3 sherry_agent 架构特点

与 hermes-agent 不同之处：

- Cron 引擎是 **builtin skill**（`skills/builtin/core/cron/`），不在 `server/` 或 `agent/` 中
- 存储用 **JSON 文件**（`cron_jobs.json`），非 SQLite
- Cron agent 通过 `runtime/hooks.py::BUILD_BACKGROUND_AGENT_TOOLS` 获取工具集
- Skill 工具已有：`skill_view`（读取 SKILL.md）、`skill_list`（列出 skills）、`skill_manage`（增删改）
- Skill 加载器已有：`skills/loader.py::scan_skills()` + `get_skills_text()`

#### 两者的 cron 架构差异总结

| 维度       | hermes-agent                                                       | sherry_agent                                          |
| ---------- | ------------------------------------------------------------------ | ----------------------------------------------------- |
| 引擎位置   | 独立模块 `cron/`                                                   | builtin skill `skills/builtin/core/cron/`             |
| 存储       | JSON 文件 `~/.hermes/cron/jobs.json`                               | JSON 文件 `ROOT_DIR/cron_jobs.json`                   |
| 调度器     | `InProcessCronScheduler`（60s ticker 循环）                        | `CronService`（timer-based，按最早到期 job 精准唤醒） |
| 创建路径数 | 7 条                                                               | 3 条                                                  |
| 前端 UI    | 完整 React CronPage（表单+ScheduleBuilder+蓝图画廊）               | 无独立 cron UI（仅 REST API）                         |
| 蓝图模板   | 16 个预定义参数化蓝图                                              | 无                                                    |
| 建议系统   | consent-first suggestion catalog                                   | 无                                                    |
| 递归防护   | cron agent 禁用 cronjob 工具                                       | **无** — cron agent 可再次调用 cron skill（潜在递归） |
| 熔断器     | 无（或 minimal）                                                   | 有（5次失败退避，10次失败自动禁用）                   |
| 安全扫描   | lifecycle guard + prompt 注入扫描 + 渗出检测                       | 无（依赖 skill 本身的安全扫描）                       |
| Skill 注入 | `_build_job_prompt()` 预加载 skill 内容                            | **无** — 当前完全缺失                                 |
| 引用追踪   | `referenced_skill_names()` + `rewrite_skill_refs()` + curator 集成 | **无** — 仅 `skill_manage.py` 注释提及                |

---

## 2. 改造目标

实现与 hermes-agent 一致的 Cron-Skill 绑定：

```
创建 cron job 时 → 指定 skills（有序 skill 名称列表）
                → 存储到 cron_jobs.json

job 触发时       → _on_cron_job 读取 payload.skills
                → 逐个预加载 skill 内容（skill_view）
                → 包裹在 [IMPORTANT: ...] 中拼接到 message 前
                → agent 带完整 skill 上下文执行

skill 变更时     → skill_manage delete/absorb 时重写 cron job 的 skill 引用
```

---

## 3. 改动清单

### 3.1 数据结构 — `skills/builtin/core/cron/scripts/types.py`

**CronPayload 新增 `skills` 字段**（L22-31）：

```python
@dataclass
class CronPayload:
    """What to do when the job runs."""

    kind: Literal["system_event", "agent_turn"] = "agent_turn"
    message: str = ""
    deliver: bool = False
    channel: str | None = None
    to: str | None = None
    # NEW: 有序 skill 名称列表，job 触发时预加载这些 skill 的完整内容
    skills: list[str] | None = None
```

> 向后兼容：`skills=None` 表示不绑定 skill（与现有行为一致）。

### 3.2 存储层 — `skills/builtin/core/cron/scripts/base.py`

#### 3.2.1 `_load_store()` 反序列化新增 skills 字段（L194-200）

当前：

```python
payload=CronPayload(
    kind=j["payload"].get("kind", "agent_turn"),
    message=j["payload"].get("message", ""),
    deliver=j["payload"].get("deliver", False),
    channel=j["payload"].get("channel"),
    to=j["payload"].get("to"),
),
```

改为：

```python
payload=CronPayload(
    kind=j["payload"].get("kind", "agent_turn"),
    message=j["payload"].get("message", ""),
    deliver=j["payload"].get("deliver", False),
    channel=j["payload"].get("channel"),
    to=j["payload"].get("to"),
    skills=j["payload"].get("skills"),  # NEW — None when absent (backward compat)
),
```

#### 3.2.2 `_save_store()` 序列化新增 skills 字段（L243-249）

当前：

```python
"payload": {
    "kind": j.payload.kind,
    "message": j.payload.message,
    "deliver": j.payload.deliver,
    "channel": j.payload.channel,
    "to": j.payload.to,
},
```

改为：

```python
"payload": {
    "kind": j.payload.kind,
    "message": j.payload.message,
    "deliver": j.payload.deliver,
    "channel": j.payload.channel,
    "to": j.payload.to,
    "skills": j.payload.skills,  # NEW — null when no skills bound
},
```

#### 3.2.3 `add_job()` 方法新增 skills 参数（L614-665）

当前签名：

```python
def add_job(
    self, name, schedule, message, deliver=False, channel=None, to=None, delete_after_run=False
) -> CronJob:
```

改为：

```python
def add_job(
    self, name, schedule, message, deliver=False, channel=None, to=None,
    delete_after_run=False, skills: list[str] | None = None
) -> CronJob:
```

并在 `CronPayload(...)` 构造中传入 `skills=skills`。

#### 3.2.4 `_on_cron_job()` — 核心：运行时预加载 skill 内容（L430-524）

在当前 `_on_cron_job()` 的 message 构建部分（L468）之后、agent 调用（L498）之前，插入 skill 预加载逻辑：

```python
message: str = payload.message
# —— NEW: 预加载绑定的 skill 内容 ——
skill_names: list[str] = payload.skills or []
if skill_names:
    from agent.tools.skill_tools.skill_view import _skill_view
    from agent.tools.pub_base.skill_usage import bump_use

    skill_blocks: list[str] = []
    for skill_name in skill_names:
        try:
            result_json = _skill_view(skill_name, caller_scope="background")
            import json as _json
            parsed = _json.loads(result_json)
            if parsed.get("success"):
                content = parsed.get("content", "")
                skill_blocks.append(
                    f'[IMPORTANT: The user has invoked the "{skill_name}" skill. '
                    f"Follow the skill's instructions below.]\n\n{content}"
                )
                # 记录 skill 使用（让 curator 看到 skill 活跃）
                try:
                    bump_use(skill_name)
                except Exception:
                    pass  # 遥测失败不影响执行
            else:
                logger.warning("Cron: skill '{}' not found or not loadable: {}", skill_name, parsed.get("error"))
        except Exception as e:
            logger.warning("Cron: failed to load skill '{}': {}", skill_name, e)

    if skill_blocks:
        message = "\n\n".join(skill_blocks) + "\n\n" + message

        # 注入扫描（参照 hermes-agent 的 _scan_assembled_cron_prompt + skill_view.py 的模式）
        from agent.tools.skill_tools.skill_view import _INJECTION_PATTERNS
        _msg_lower = message.lower()
        _injection_detected = any(p in _msg_lower for p in _INJECTION_PATTERNS)
        if _injection_detected:
            logger.warning(
                "Cron: assembled prompt (skill + message) contains patterns that may indicate prompt injection"
            )
            # 不硬阻断 — hermes-agent 的组装后扫描也是宽松层，只 log warning
            # 因为 skill 内容已在安装时通过 skills_guard.py 审核过
# —— END skill 预加载 ——

channel: str = payload.channel
to: str = payload.to
# ... 后续构建 agent + 调用逻辑不变
```

> **设计决策**：采用**预加载注入**模式（方案 B），而非给 cron agent 加 `skill_view` 工具（方案 A）。理由：
>
> 1. hermes-agent 也是预加载模式（`_build_job_prompt()` 在调用 agent 前加载 skill 内容）
> 2. 预加载更可控 — skill 内容在 prompt 中可见，agent 不需要额外工具调用
> 3. cron agent 工具集保持精简（background sandbox 安全）

### 3.3 REST API — `server/trigger/http/cron.py`

#### 3.3.1 `_job_to_dict()` 序列化新增 skills（L37-69）

```python
"payload": {
    "kind": pd.kind,
    "message": pd.message,
    "deliver": pd.deliver,
    "channel": pd.channel,
    "to": pd.to,
    "skills": pd.skills,  # NEW
},
```

#### 3.3.2 POST /cron handler 接受 skills 参数（L130-181）

在 `add_cron_job_handler` 中：

```python
job = cron_service.add_job(
    name=name.strip(),
    schedule=schedule,
    message=message.strip(),
    deliver=bool(body.get("deliver", False)),
    channel=body.get("channel"),
    to=body.get("to"),
    delete_after_run=bool(body.get("delete_after_run", False)),
    skills=body.get("skills"),  # NEW — list[str] | None
)
```

并在 docstring 中更新 body 结构说明。

#### 3.3.3 PUT /cron handler 传递 skills（L184-257）

```python
job = cron_service.add_job(
    name=name.strip(),
    schedule=schedule,
    message=message.strip(),
    deliver=body.get("deliver", existing.payload.deliver),
    channel=body.get("channel", existing.payload.channel),
    to=body.get("to", existing.payload.to),
    delete_after_run=body.get("delete_after_run", existing.delete_after_run),
    skills=body.get("skills", existing.payload.skills),  # NEW
)
```

### 3.4 Skill Python API — `skills/builtin/core/cron/scripts/core.py`

#### 3.4.1 `add_job()` 方法新增 skills 参数（L53-109）

当前签名：

```python
def add_job(self, name, message, every_seconds, cron_expr, tz, at, deliver=True) -> str:
```

改为：

```python
def add_job(self, name, message, every_seconds, cron_expr, tz, at, deliver=True, skills=None) -> str:
```

在 `self._cronService.add_job(...)` 调用中传入 `skills=skills`。

#### 3.4.2 `list_jobs()` 显示绑定的 skills（L151-165）

在 job 展示中追加 skill 信息：

```python
for j in jobs:
    timing = self._format_timing(j.schedule)
    parts = [f"- {j.name} (id: {j.id}, {timing})"]
    if j.payload.skills:  # NEW
        parts.append(f"  Skills: {', '.join(j.payload.skills)}")
    # ... 现有的 state 展示
```

### 3.5 Cron SKILL.md — `skills/builtin/core/cron/SKILL.md`

在 `add_job` 工具的参数说明中新增 `skills` 参数文档。

### 3.6 Skill 引用追踪 — 新增模块

#### 3.6.1 新增 `skills/builtin/core/cron/scripts/skill_refs.py`

实现 hermes-agent 的三个维护机制：

```python
"""Cron job skill reference tracking and maintenance."""

import json
from pathlib import Path
from loguru import logger
from config import ROOT_DIR

CRON_STORE_PATH = ROOT_DIR / "cron_jobs.json"


def referenced_skill_names() -> set[str]:
    """Return all skill names referenced by any cron job (including disabled).

    Used by the curator to protect referenced skills from inactivity archival.
    """
    try:
        if not CRON_STORE_PATH.exists():
            return set()
        data = json.loads(CRON_STORE_PATH.read_text(encoding="utf-8"))
        names: set[str] = set()
        for job in data.get("jobs", []):
            skills = job.get("payload", {}).get("skills")
            if skills:
                names.update(skills)
        return names
    except Exception as e:
        logger.warning("skill_refs: failed to read cron store: %s", e)
        return set()


def rewrite_skill_refs(
    consolidated: dict[str, str],
    pruned: set[str],
) -> None:
    """Rewrite cron job skill references after curator consolidation/pruning.

    Args:
        consolidated: mapping old_skill_name -> new_skill_name (umbrella target)
        pruned: set of skill names that were archived with no forwarding target

    For each cron job:
        - consolidated skills → replaced with umbrella target (dedup, preserve order)
        - pruned skills → dropped from the list
    """
    if not consolidated and not pruned:
        return
    try:
        if not CRON_STORE_PATH.exists():
            return
        data = json.loads(CRON_STORE_PATH.read_text(encoding="utf-8"))
        changed = False
        for job in data.get("jobs", []):
            payload = job.get("payload", {})
            skills = payload.get("skills")
            if not skills:
                continue
            new_skills: list[str] = []
            seen: set[str] = set()
            for s in skills:
                # Apply consolidation
                target = consolidated.get(s, s)
                # Apply pruning
                if target in pruned:
                    continue
                if target not in seen:
                    new_skills.append(target)
                    seen.add(target)
            if new_skills != skills:
                payload["skills"] = new_skills if new_skills else None
                changed = True
        if changed:
            CRON_STORE_PATH.write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            logger.info("skill_refs: rewrote cron job skill references")
    except Exception as e:
        logger.warning("skill_refs: failed to rewrite skill refs: %s", e)
```

#### 3.6.2 在 `__init__.py` 中导出

```python
# skills/builtin/core/cron/scripts/__init__.py
from .skill_refs import referenced_skill_names, rewrite_skill_refs
```

#### 3.6.3 Curator 集成 — `context_engine/curator/`

在 curator 的 skill 整合/裁剪流程中调用 `rewrite_skill_refs()`。

> **注意**：需要探索 curator 的实际整合/裁剪代码路径来确定插入点。AGENTS.md 提到 curator 在 `context_engine/curator/`，负责 auto-skills 维护。搜索 curator 中 skill delete/consolidate 的调用点，在操作完成后调用 `rewrite_skill_refs()`。

#### 3.6.4 `skill_manage.py` 集成

在 `skill_manage` 的 delete 操作中（已有 `absorbed_into` 参数的注释提到 cron-job 引用重写），调用 `rewrite_skill_refs()`：

```python
# skill_manage.py delete 操作完成后：
from skills.builtin.core.cron.scripts.skill_refs import referenced_skill_names, rewrite_skill_refs

# 如果有 absorbed_into（合并），重写引用
if absorbed_into:
    rewrite_skill_refs(consolidated={deleted_name: absorbed_into}, pruned=set())
else:
    # 纯删除（无转发目标），从 cron job 中移除引用
    rewrite_skill_refs(consolidated={}, pruned={deleted_name})
```

> **注意**：`skills/builtin/core/cron/scripts/` 是 builtin skill，`agent/tools/skill_tools/skill_manage.py` 在 `agent/` 层。按 AGENTS.md 的 import rules：`skills/**` MUST NOT import `server/**`，但 `skills/builtin/**` 可以 import `models`/`bus`/`workspace`/`runtime`。`agent/tools/` 可以 import `skills/`。所以 `skill_manage.py` import `skills.builtin.core.cron.scripts.skill_refs` 是合法的。

### 3.7 `bump_use` 函数检查

`agent/tools/pub_base/skill_usage.py` 中应有 `bump_use(skill_name)` 函数。需确认其签名和调用方式，在 `_on_cron_job` 中正确调用。

> 如果 `bump_use` 需要更多参数（如 skill 路径），需要适配。

---

## 4. 不需要改动的部分

| 组件                                                              | 原因                                                                |
| ----------------------------------------------------------------- | ------------------------------------------------------------------- |
| `CronService` 调度逻辑（`_arm_timer`/`_on_timer`/`_execute_job`） | 调度与 skill 无关                                                   |
| 失败熔断器（`_record_failure`/`DEGRADED_THRESHOLD`等）            | 熔断与 skill 无关                                                   |
| `build_system_prompt()`                                           | 已注入 `<available_skills>` XML，不需要改                           |
| `skill_view` 工具本身                                             | 预加载模式直接调用 `_skill_view()` 函数，不需要给 cron agent 加工具 |
| `BUILD_BACKGROUND_AGENT_TOOLS` hook                               | cron agent 工具集不变（仍只有 python_repl/read_file/write_file）    |
| `runtime/hooks.py`                                                | 不需要新增 hook                                                     |
| 前端 cron UI                                                      | 可后续独立迭代，后端 API 已兼容                                     |
| JSON 存储格式版本号                                               | `version: 1` 保持不变，`skills` 字段是可选的向后兼容扩展            |

---

## 4.7 前端 Skill 选择器 UI

> sherry_agent **已有完整的 cron 管理 UI**：`CronPanel.vue`（`client/app/pages/home/components/CronPanel.vue`，661 行，右侧边栏 tab 面板），包含任务列表 + 添加/编辑 Dialog 表单。当前表单字段：name → scheduleType → schedule fields → message → deliver → deleteAfterRun。
>
> 参照 hermes-agent 的 `NameCheckboxPicker`（标注 "Skills (optional)"），在 CronPanel Dialog 表单的 `message` 字段之后、`deliver` checkbox 之前插入 **Skill 复选框多选组件**。skill 列表从已有的 `listSkills()` API（`client/app/composables/bridge/skills.ts`）加载。

### 4.7.1 `client/app/composables/bridge/cron.ts` — 类型扩展

**CronPayload 接口新增 `skills` 字段**（L20-31）：

```typescript
export interface CronPayload {
  kind: string;
  message: string;
  deliver: boolean;
  channel?: string | null;
  to?: string | null;
  skills?: string[] | null; // NEW: 有序 skill 名称列表
}
```

**`addCronJob` 参数新增 `skills`**（L104-118）：

```typescript
export async function addCronJob(input: {
  name: string;
  message: string;
  schedule: CronSchedule;
  deliver?: boolean;
  channel?: string | null;
  to?: string | null;
  delete_after_run?: boolean;
  skills?: string[] | null;  // NEW
}): Promise<CronMutateResponse> {
```

**`updateCronJob` 参数新增 `skills`**（L134-151）：

```typescript
export async function updateCronJob(
  id: string,
  patch: {
    // ... 现有字段 ...
    skills?: string[] | null;  // NEW
  }
): Promise<CronMutateResponse> {
```

### 4.7.2 `client/app/pages/home/components/CronPanel.vue` — 表单扩展

#### 模板：在 message 之后、deliver 之前插入 Skill 选择器

在 L178（message Textarea 所在 div 结束后）和 L180（deliver checkbox div 开始前）之间插入：

```vue
<!-- Skill selector (optional): selected skills are pre-loaded before the prompt runs -->
<div class="flex flex-col gap-1">
  <label class="text-sm">{{ t('config.cron.skills') }}</label>
  <div
    v-if="availableSkills.length === 0"
    class="text-xs text-gray-400 dark:text-gray-500">
    {{ t('config.cron.skillsEmpty') }}
  </div>
  <div
    v-else
    class="flex flex-wrap gap-2 max-h-40 overflow-y-auto p-2 border border-gray-200 dark:border-gray-700 rounded-lg">
    <div
      v-for="skill in availableSkills"
      :key="skill.name"
      class="flex items-center gap-1.5">
      <Checkbox
        v-model="form.skills"
        :value="skill.name"
        :inputId="`cron-skill-${skill.name}`" />
      <label
        :for="`cron-skill-${skill.name}`"
        class="text-xs cursor-pointer select-none">
        {{ skill.name }}
      </label>
    </div>
  </div>
  <small class="text-xs text-gray-400 dark:text-gray-500">
    {{ t('config.cron.skillsHint') }}
  </small>
</div>
```

> 使用 PrimeVue `Checkbox` 的多选模式（`v-model` 绑定数组 + `:value="skill.name"`），与 CronPanel 现有的 `Checkbox :binary="true"` 单选模式不同。PrimeVue v4.5.0 自动导入，无需手动 import。

#### 任务列表卡片：显示绑定的 skills

在 job 卡片的 message 预览之后（L62 之后）新增：

```vue
<div v-if="job.payload.skills?.length" class="flex flex-wrap gap-1 mt-0.5">
  <span
    v-for="skillName in job.payload.skills"
    :key="skillName"
    class="text-xs px-1.5 py-0.5 rounded bg-sky-100 dark:bg-sky-900/30 text-sky-600 dark:text-sky-300">
    {{ skillName }}
  </span>
</div>
```

#### Script：form 状态扩展

**form ref 新增 `skills` 字段**（L291-303）：

```typescript
const form = ref({
  name: "",
  scheduleType: "every" as "at" | "every" | "cron",
  atDate: new Date() as Date | null,
  everyValue: 5 as number | null,
  everyUnit: "m" as string,
  expr: "" as string,
  message: "",
  skills: [] as string[], // NEW
  deliver: false,
  channel: "" as string | null,
  to: "" as string | null,
  deleteAfterRun: false,
});
```

**resetForm 重置 skills**（L338-352）：

```typescript
function resetForm() {
  form.value = {
    // ... 现有字段 ...
    skills: [], // NEW
    // ...
  };
}
```

**openEditJob 从 job 恢复 skills**（L390-403）：

```typescript
form.value = {
  name: job.name,
  scheduleType,
  atDate,
  everyValue,
  everyUnit,
  expr: s.expr ?? "",
  message: job.payload.message,
  skills: job.payload.skills ?? [], // NEW
  deliver: job.payload.deliver,
  channel: job.payload.channel ?? null,
  to: job.payload.to ?? null,
  deleteAfterRun: job.deleteAfterRun,
};
```

**handleSaveJob 传递 skills**（L415-423）：

```typescript
const payload = {
  name: form.value.name.trim(),
  message: form.value.message.trim(),
  schedule: buildSchedule(),
  deliver: form.value.deliver,
  channel: form.value.deliver ? form.value.channel : null,
  to: form.value.deliver ? form.value.to : null,
  delete_after_run: form.value.deleteAfterRun,
  skills: form.value.skills.length > 0 ? form.value.skills : null, // NEW — 空数组传 null
};
```

**导入 listSkills + 加载 skill 列表**：

```typescript
// 在 <script setup> 顶部导入区（L244 附近）添加
import { listSkills, type SkillInfo } from "@/composables/bridge";

// 在 ref 声明区（L266 附近）添加
const availableSkills = ref<SkillInfo[]>([]);

// 加载 skills
async function loadSkills() {
  try {
    const resp = await listSkills();
    availableSkills.value = resp.skills ?? [];
  } catch (e) {
    logUtil.e("[CronPanel] Failed to load skills:", e);
    availableSkills.value = [];
  }
}

// onMounted 中同时加载 jobs 和 skills（L495）
onMounted(() => {
  loadJobs();
  loadSkills();
});
```

> `listSkills()` 已在 `client/app/composables/bridge/skills.ts:8` 定义，返回 `{ skills: SkillInfo[] }`。`SkillInfo` 含 `name`/`description`/`location`/`category`/`pinned` 字段。已在 `bridge.ts` 中 re-export。

### 4.7.3 CronPanel.vue i18n — 四语言新增

在 `<i18n>` 块（L498-660）的 `config.cron` 对象中新增三组键，四语言全部补齐：

| i18n 键       | zh                                                                  | en                                                                                         | ja                                                                                                | ko                                                                                     |
| ------------- | ------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| `skills`      | 绑定技能（可选）                                                    | Skills (optional)                                                                          | スキル（任意）                                                                                    | 스킬 (선택)                                                                            |
| `skillsEmpty` | 暂无可用技能                                                        | No skills installed                                                                        | 利用可能なスキルがありません                                                                      | 사용 가능한 스킬이 없습니다                                                            |
| `skillsHint`  | 选中的技能会在执行前预加载 — 定时任务决定何时执行，技能决定如何执行 | Selected skills are loaded before the prompt runs — the cron sets when, the skill sets how | 選択したスキルは実行前にロードされます — クーロンが実行タイミングを、スキルが実行方法を決定します | 선택한 스킬은 실행 전에 로드됩니다 — 크론이 실행 시점을, 스킬이 실행 방법을 결정합니다 |

---

## 4.5 递归防护（参照 hermes-agent，可选增强）

> hermes-agent 在 `scheduler.py:130` 禁用 cron 产生的 agent 的 `cronjob` 工具集（`disabled = ["cronjob", "messaging", "clarify"]`），防止递归调度。
>
> sherry_agent 当前**无此防护** — cron 产生的 agent 可通过 `BUILD_BACKGROUND_AGENT_TOOLS` 获得的工具集中不包含 cron skill 工具（只有 `[python_repl, read_file, write_file]`），所以实际上**已天然防止递归**（cron agent 没有 cron 工具可调用）。
>
> 但 cron agent 的 system prompt 中包含 `<available_skills>` XML，其中列出了 cron skill — 如果 agent 被注入 skill 内容后尝试调用 cron skill（通过 system prompt 中的技能描述），它会发现自己没有对应工具而放弃。**当前无需额外改动**，但如果未来给 cron agent 增加更多工具，需注意不要加入 cron 相关工具。

---

## 4.6 蓝图模板与建议系统（hermes-agent 有，sherry_agent 无 — 后续可选）

> hermes-agent 的蓝图模板（`cron/blueprint_catalog.py`，16 个预定义参数化蓝图）和建议系统（`cron/suggestions.py`，consent-first）是 cron job 创建的辅助 UX 层，与 cron-skill 绑定**无直接关系**，但可以增强用户体验：
>
> - **蓝图**：预定义"每日晨报"等场景的 job 模板，含预设的 skills 绑定
> - **建议系统**：根据用户行为推荐自动化方案，用户 accept 后才创建 job
>
> 这两个功能属于独立的 UX 增强方向，不在本次 cron-skill 绑定改造范围内，但 `skills` 字段的设计已为其预留了扩展空间（蓝图可直接携带预设 `skills` 列表）。

---

## 5. 数据流

### 5.1 创建绑定了 skill 的 cron job

```
用户/Agent 调用 cron.add_job(name, message, ..., skills=["news-digest", "github-auth"])
  → CronService.add_job(..., skills=["news-digest", "github-auth"])
    → CronPayload(skills=["news-digest", "github-auth"])
    → _save_store() → cron_jobs.json: {"payload": {"skills": ["news-digest", "github-auth"], ...}}
```

### 5.2 Job 触发时预加载 skill

```
timer 到期 → _on_timer → _execute_job → _on_cron_job
  → 读取 payload.skills = ["news-digest", "github-auth"]
  → 对每个 skill_name:
      → _skill_view(skill_name, caller_scope="background")
        → 读取 SKILL.md 内容
      → bump_use(skill_name)
      → 包裹在 [IMPORTANT: The user has invoked the "skill_name" skill. ...] 中
  → 拼接所有 skill block + message → 最终 prompt
  → build_background_tools() → [python_repl, read_file, write_file]
  → create_agent(system_prompt=build_system_prompt(), model=main_llm, tools=tools)
  → agent.ainvoke({"messages": [HumanMessage(content=最终prompt)]})
  → 结果发布到 channel
```

### 5.3 Skill 变更时维护引用

```
curator 整合 skill X → umbrella Y
  → rewrite_skill_refs(consolidated={"X": "Y"}, pruned=set())
    → 读取 cron_jobs.json
    → 遍历每个 job 的 payload.skills
    → X 替换为 Y（去重保序）
    → 写回 cron_jobs.json

curator 裁剪 skill Z（无转发目标）
  → rewrite_skill_refs(consolidated={}, pruned={"Z"})
    → Z 从所有 job 的 skills 列表中移除

skill_manage delete skill W (absorbed_into="V")
  → rewrite_skill_refs(consolidated={"W": "V"}, pruned=set())

skill_manage delete skill W (无 absorbed_into)
  → rewrite_skill_refs(consolidated={}, pruned={"W"})
```

### 5.4 Curator 保护被引用的 skill

```
curator 评估 skill 是否应归档
  → referenced_skill_names() → {"news-digest", "github-auth", ...}
  → 如果 skill 在此集合中 → 标记为 "使用中"，不归档
```

---

## 6. 向后兼容

| 场景                                    | 处理                                                                                                        |
| --------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| 旧的 `cron_jobs.json`（无 skills 字段） | `_load_store()` 用 `j["payload"].get("skills")` → 返回 `None` → `CronPayload(skills=None)` → 行为与当前一致 |
| 旧 job 触发（skills=None）              | `_on_cron_job` 中 `skill_names = payload.skills or []` → 空列表 → 跳过 skill 加载 → 行为与当前一致          |
| API 不传 skills                         | `body.get("skills")` → `None` → `add_job(skills=None)` → 行为与当前一致                                     |
| `core.py` `add_job` 不传 skills         | `skills=None` 默认值 → 行为与当前一致                                                                       |

---

## 7. 实施步骤

| 步骤 | 内容                                                         | 文件                                             |
| :--: | ------------------------------------------------------------ | ------------------------------------------------ |
|  1   | CronPayload 新增 `skills` 字段                               | `types.py`                                       |
|  2   | `_load_store` / `_save_store` 序列化/反序列化 skills         | `base.py`                                        |
|  3   | `add_job()` 新增 skills 参数                                 | `base.py`                                        |
|  4   | `_on_cron_job()` 预加载 skill 逻辑 + 注入扫描                | `base.py`                                        |
|  5   | REST API `_job_to_dict` / POST / PUT 接受 skills             | `cron.py` (http)                                 |
|  6   | `core.py` `add_job()` / `list_jobs()` 显示 skills            | `core.py`                                        |
|  7   | 新增 `skill_refs.py` 模块                                    | `skills/builtin/core/cron/scripts/skill_refs.py` |
|  8   | `__init__.py` 导出新模块                                     | `scripts/__init__.py`                            |
|  9   | `skill_manage.py` delete 时调用 `rewrite_skill_refs`         | `skill_manage.py`                                |
|  10  | Curator 集成 `referenced_skill_names` + `rewrite_skill_refs` | `context_engine/curator/`                        |
|  11  | SKILL.md 文档更新                                            | `skills/builtin/core/cron/SKILL.md`              |
|  12  | 测试                                                         | `tests/`                                         |

---

## 8. 文件变更清单

|   操作   | 文件                                             | 说明                                                             |
| :------: | ------------------------------------------------ | ---------------------------------------------------------------- |
|   修改   | `skills/builtin/core/cron/scripts/types.py`      | CronPayload 新增 `skills: list[str] \| None`                     |
|   修改   | `skills/builtin/core/cron/scripts/base.py`       | `_load_store`/`_save_store`/`add_job`/`_on_cron_job` 支持 skills |
|   修改   | `server/trigger/http/cron.py`                    | `_job_to_dict`/POST/PUT 接受 skills 参数                         |
|   修改   | `skills/builtin/core/cron/scripts/core.py`       | `add_job()` 新增 skills 参数 + `list_jobs()` 显示                |
| **新增** | `skills/builtin/core/cron/scripts/skill_refs.py` | `referenced_skill_names()` + `rewrite_skill_refs()`              |
|   修改   | `skills/builtin/core/cron/scripts/__init__.py`   | 导出 skill_refs                                                  |
|   修改   | `agent/tools/skill_tools/skill_manage.py`        | delete 时调用 rewrite_skill_refs                                 |
|   修改   | `context_engine/curator/` (具体文件待探索)       | 集成 referenced_skill_names + rewrite_skill_refs                 |
|   修改   | `skills/builtin/core/cron/SKILL.md`              | add_job 文档新增 skills 参数                                     |
|   修改   | 测试文件                                         | 新增 cron-skill 绑定相关测试                                     |

---

## 9. 测试要点

| 测试                                      | 说明                                           |
| ----------------------------------------- | ---------------------------------------------- |
| `test_cron_payload_skills_field`          | CronPayload 序列化/反序列化含 skills 字段      |
| `test_cron_job_skill_loading`             | `_on_cron_job` 正确预加载 skill 内容到 message |
| `test_cron_job_no_skills_backward_compat` | 旧 job（无 skills 字段）仍正常执行             |
| `test_cron_job_skill_injection_warning`   | 含注入模式的 skill 内容触发 warning log        |
| `test_referenced_skill_names`             | 正确返回所有 cron job 引用的 skill 名集合      |
| `test_rewrite_skill_refs_consolidate`     | curator 整合后正确替换 skill 引用              |
| `test_rewrite_skill_refs_prune`           | curator 裁剪后正确移除 skill 引用              |
| `test_rewrite_skill_refs_preserve_order`  | 重写后 skill 列表顺序保持                      |
| `test_skill_manage_delete_updates_cron`   | skill_manage delete 后 cron job 引用更新       |
| `test_cron_api_skills_param`              | POST/PUT /cron 正确接受和返回 skills           |
| `test_cron_skill_not_found`               | 绑定的 skill 不存在时 warning log + 继续执行   |
| `test_cron_skill_bump_use`                | 预加载 skill 后 bump_use 被调用                |
