# 计划：统一 sherry 项目目录隐藏条目到 `.sherry/`

- 建档日期：2026-10-22
- 状态：**计划（未开工）**
- **文档范围**：三部分——(1) 把 sherry 散落在用户项目根目录的隐藏条目统一收进 `.sherry/`（`.codeintel/index.db` → `.sherry/codeintel/`、`.sherry-isolation-repo` → `.sherry/isolation-repo`）；(2) FACTS.md 从全局 `WORKSPACE_DIR/memory/` 搬到 `.sherry/memory/FACTS.md`（per-project，内容是项目级陷阱）；(3) `.sherry/` 的创建时机——仅在用户绑定项目目录、目录与上次不同、目录中无 `.sherry/`、且用户开始发送对话时才创建。`.sherry-tmp-*.swp`（同文件系统约束）和 `sherry/<run8>` 分支（git 概念）不搬。MEMORY.md / USER.md 不搬（全局，见 §4）。
- **硬约束**：不改 `.gitignore`、不影响 `CODE_INTEL_DIR`（SHERRY_HOME 里的 ast-grep 缓存）、不破坏 isolation 的 deny_patterns 语义、迁移期旧索引自然重建，详见 §3
- 关联代码：
  - `.codeintel/index.db`（要搬）：
    - `agent/tools/code_intel/roots.py:20-36`（`index_db_for_root`——per-project 索引路径解析）
    - `config/features/agent_side/code_intel.py:65-75`（`prune_dirs`——索引器跳过的目录列表）
    - `agent/tools/code_intel/indexer.py:175`（`Path(self._db_path).parent.mkdir`——懒创建）
    - `agent/tools/code_intel/semantic/indexer.py:115`（语义索引同路径）
    - `agent/tools/code_intel/tools.py:55`（`_resolve_db_path` 调 `index_db_for_root`）
  - `.sherry-isolation-repo`（要搬）：
    - `config/features/agent_side/subagent_isolation.py:70`（`repo_marker_name`）
    - `config/features/agent_side/subagent_isolation.py:103`（`deny_patterns` 包含该文件名）
    - `agent/tools/subagent/isolation/worktree.py:118`（`project_root / SUBAGENT_ISOLATION["repo_marker_name"]`）
  - 不搬的（已分析）：
    - `agent/tools/pub_base/atomic_write.py:206`（`.sherry-tmp-*.swp`——`os.replace` 需同文件系统）
    - `agent/tools/subagent/isolation/worktree.py:171-174`（`sherry/<run8>` 分支——git 概念）
  - `SHERRY_HOME` 里的 `CODE_INTEL_DIR`（不混淆）：
    - `config/path.py:153`（`CODE_INTEL_DIR = APP_HOME / ".codeintel"`——ast-grep 二进制缓存，在 SHERRY_HOME，不在项目目录）
- 关联 TODO：跟 `计划_项目级指令文件读取.md` 相关（`.sherry/` 未来可放 `AGENTS.md` 等项目级配置）

---

## 0. 背景：当前散落的隐藏条目

sherry 在用户项目目录创建的隐藏条目：

| 条目 | 位置 | 类型 | 能搬？ | 理由 |
|---|---|---|---|---|
| `.codeintel/index.db` | 项目根 `.codeintel/` | 持久（SQLite + WAL） | ✅ | 存相对路径，位置不影响正确性 |
| `.sherry-isolation-repo` | 项目根 | 持久（标记文件） | ✅ | 纯文本，无约束 |
| `.sherry-tmp-*.swp` | 目标文件同目录 | 瞬态（立即 replace） | ❌ | `os.replace` 需同文件系统；瞬态 + 自动清理 |
| `sherry/<run8>` 分支 | `.git/refs/heads/` | 瞬态（cleanup 删） | ❌ | git 分支不是文件系统条目 |
| `.git/` | 项目根 | 持久 | ❌ | git 仓库，不能搬 |
| `.git/info/exclude` | `.git/` 内部 | 持久 | ❌ | git 内部文件 |

**问题**：`.codeintel/` 和 `.sherry-isolation-repo` 分散在项目根——用户 `git status` 看到两个不相关的 sherry 条目，要分别处理。统一到 `.sherry/` 后只看到一个目录。

### 0.1 `.codeintel/index.db` 为什么能搬

`index_db_for_root`（`roots.py:20-36`）当前逻辑：

```python
if root is not None and root.is_dir():
    return str(Path(root) / ".codeintel" / "index.db")  # 项目内
return str(default_dir / "index.db")                      # SHERRY_HOME 回退
```

索引 DB 存的是**相对路径**（`index_meta` 表的文件路径相对于 root）——只要每个项目有自己独立的 DB，DB 的物理位置不影响正确性。从 `root/.codeintel/index.db` 改到 `root/.sherry/codeintel/index.db` 只是改路径，数据格式不变。

### 0.2 与 `CODE_INTEL_DIR` 的区别（不混淆）

| 路径 | 位置 | 内容 | 搬？ |
|---|---|---|---|
| `CODE_INTEL_DIR = APP_HOME / ".codeintel"` | SHERRY_HOME | ast-grep 二进制缓存 + 无 root 时的回退索引 | ❌ 不搬（在 SHERRY_HOME） |
| `Path(root) / ".codeintel" / "index.db"` | 项目目录 | per-project 代码索引（AST + 语义嵌入） | ✅ 搬到 `root/.sherry/codeintel/index.db` |

两个同名但不同位置、不同用途——SHERRY_HOME 的不搬，项目内的搬。

---

## 1. 目标布局

```
项目根/
├── .sherry/                        ← 统一的 sherry 目录（仅满足创建条件时才创建，见 §5）
│   ├── codeintel/
│   │   └── index.db                ← 从 .codeintel/index.db 搬来
│   │       (+ index.db-wal/-shm)   ← SQLite WAL sidecars
│   ├── isolation-repo             ← 从 .sherry-isolation-repo 搬来
│   └── memory/
│       └── FACTS.md                ← 从全局 WORKSPACE_DIR/memory/ 搬来（per-project）
├── .git/                           ← 不搬
│   └── info/exclude               ← 含 .sherry/ 排除规则（isolation 用）
│   └── refs/heads/sherry/<run8>    ← 不搬（git 分支）
├── .sherry-tmp-*.swp               ← 不搬（瞬态，同文件系统约束）
└── ...（项目本身的文件）
```

**不进 `.sherry/` 的全局文件**（留在 `WORKSPACE_DIR`）：

| 文件 | 位置 | 理由 |
|---|---|---|
| `MEMORY.md` | `WORKSPACE_DIR/memory/MEMORY.md` | agent 的通用工作笔记（跨项目知识），不随项目走 |
| `USER.md` | `WORKSPACE_DIR/memory/USER.md` | 用户身份和偏好（跨项目不变），不随项目走 |
| `AGENTS.md` / `SOUL.md` / `ROLE.md` | `WORKSPACE_DIR/` | persona 文件（全局），不随项目走 |

详见 §4（MEMORY.md / USER.md 不搬的理由）。

---

## 2. 改动清单

### 2.1 `agent/tools/code_intel/roots.py` — 索引 DB 路径

**改动前**（line 35）：

```python
    if root is not None and root.is_dir():
        return str(Path(root) / ".codeintel" / "index.db")
```

**改动后**：

```python
    if root is not None and root.is_dir():
        return str(Path(root) / ".sherry" / "codeintel" / "index.db")
```

**连带改动**：`roots.py:23` docstring 更新路径。

### 2.2 `config/features/agent_side/code_intel.py` — prune 目录

索引器遍历源码时跳过的目录列表。

**改动前**（line 71）：

```python
        ".codeintel",
```

**改动后**：

```python
        ".sherry",
```

索引器原来跳过 `.codeintel/`（不索引自己的索引）；现在跳过 `.sherry/`（不索引整个 sherry 目录——包括 codeintel 索引本身）。

### 2.3 `config/features/agent_side/subagent_isolation.py` — marker 名 + deny_patterns

**改动前**（line 70）：

```python
    "repo_marker_name": ".sherry-isolation-repo",
```

**改动后**：

```python
    "repo_marker_name": ".sherry/isolation-repo",
```

`Path(project_root / ".sherry" / "isolation-repo")` — `worktree.py:118` 的 `project_root / SUBAGENT_ISOLATION["repo_marker_name"]` 自然解析为子目录路径。

**改动前**（line 103，deny_patterns 列表）：

```python
        ".sherry-isolation-repo",
```

**改动后**：

```python
        ".sherry",
```

整个 `.sherry/` 目录被排除出 isolation baseline commit（包括 codeintel 索引和 marker）。

### 2.4 `agent/tools/subagent/isolation/worktree.py` — 确保父目录存在

**改动前**（line 118-131，简化）：

```python
    marker = project_root / SUBAGENT_ISOLATION["repo_marker_name"]
    marker.write_text(content, encoding="utf-8")
```

**改动后**：

```python
    marker = project_root / SUBAGENT_ISOLATION["repo_marker_name"]
    marker.parent.mkdir(parents=True, exist_ok=True)  # 确保 .sherry/ 存在
    marker.write_text(content, encoding="utf-8")
```

`.sherry/isolation-repo` 的父目录 `.sherry/` 需要先创建——`write_text` 不会自动创建父目录。

### 2.5 `worktree.py:89-100` — exclude 文件追加 `.sherry/`

isolation auto-init 时写入 `.git/info/exclude` 的 deny_patterns 已经从 `subagent_isolation.py` 读取——§2.3 把 deny_patterns 里的 `.sherry-isolation-repo` 改成 `.sherry` 后，这里自动生效，不需要额外改 `worktree.py` 的 exclude 写入逻辑。

isolation auto-init 时写入 `.git/info/exclude` 的 deny_patterns 已经从 `subagent_isolation.py` 读取——§2.3 把 deny_patterns 里的 `.sherry-isolation-repo` 改成 `.sherry` 后，这里自动生效，不需要额外改 `worktree.py` 的 exclude 写入逻辑。

### 2.6 `agent/tools/memory.py` — FACTS.md 路径 per-project 化

**当前**：FACTS.md 全局存在 `WORKSPACE_DIR/memory/FACTS.md`，`MemoryStore` 在 boot 时 `load_from_disk()` 统一加载，冻结快照注入系统提示。

**改动后**：FACTS.md 路径改为 per-project——`session_workspace_root(session_id) / ".sherry" / "memory" / "FACTS.md"`。MEMORY.md 和 USER.md 路径不变（全局）。

核心改动点：

- [ ] **2.6.1** `MemoryStore` 的 `_path_for("facts")` 改为动态解析

```python
def _path_for(self, target: str, session_id: str | None = None) -> Path:
    if target == "facts":
        # FACTS.md 是 per-project：解析到项目目录的 .sherry/memory/
        if session_id:
            root = session_workspace_root(session_id)
            if root:
                return Path(root) / ".sherry" / "memory" / "FACTS.md"
        # fallback：未绑定项目时用全局（向后兼容）
        return MEMORY_DIR / "FACTS.md"
    # MEMORY.md 和 USER.md 始终全局
    return MEMORY_DIR / f"{target.upper()}.md"
```

- [ ] **2.6.2** FACTS.md 冻结快照改为 per-session

当前 `load_from_disk()` 在 boot 时一次性加载所有三个文件。改为：
- boot 时仍加载 MEMORY.md + USER.md（全局，不变）
- FACTS.md 的快照在 **session 首次构建系统提示时** 按需加载（`build_system_prompt(session_id)` → 读项目级 FACTS.md → 存入 per-session 快照缓存）
- 项目切换时（`PROJECT_DIR_PENDING` promote）触发 FACTS.md 快照失效 + 重新加载

- [ ] **2.6.3** nudge 的 FACTS.md 读写改为 per-session 路径

plan-extraction nudge（`nudges.py:_run_plan_extraction`）和 memory-review nudge（`nudges.py:_MEMORY_REVIEW_PROMPT`）当前通过 `memory_store.format_live_content("facts")` 和 `memory` 工具读写 FACTS.md。改为传入 `session_id`，让 `MemoryStore` 解析到当前项目的 `.sherry/memory/FACTS.md`。

- [ ] **2.6.4** `format_for_system_prompt("facts")` 按 session_id 返回 per-session 快照

```python
def format_for_system_prompt(self, target: str, session_id: str | None = None) -> str | None:
    if target == "facts" and session_id:
        # per-session 快照（项目切换时重新加载）
        return self._get_session_facts_snapshot(session_id)
    # MEMORY.md / USER.md 仍用全局冻结快照
    return self._system_prompt_snapshot.get(target, "") or None
```

- [ ] **2.6.5** HTTP `GET/PUT /memory` 支持 per-project FACTS.md

`server/service/memory.py::_MemoryFileStore` 当前从 `MEMORY_DIR` 读写。FACTS.md 的读写需要根据请求的 `session_id` 解析到项目级路径。

- [ ] **2.6.6** 首次使用时 seed FACTS.md 模板到 `.sherry/memory/`

`workspace/file_sync.py` 当前在全局 `MEMORY_DIR` seed `FACTS.md` 模板。改为：session 绑定项目后，首次访问 FACTS.md 时从 `workspace/template/{lang}/FACTS.md` seed 到 `.sherry/memory/FACTS.md`（如果文件不存在）。

**迁移策略**：旧的全局 `WORKSPACE_DIR/memory/FACTS.md` 不删除——作为 fallback。项目级 `.sherry/memory/FACTS.md` 不存在时读全局（向后兼容）；首次写入时 seed 到项目级。用户可手动迁移（复制全局 FACTS.md 到 `.sherry/memory/`）。

---

## 3. 硬约束

| 约束 | 含义 | 验收方式 |
|---|---|---|
| **C1**：不改 `.gitignore` | sherry 不碰用户项目的 `.gitignore`；用户自己加 `.sherry/` | 不变 |
| **C2**：`CODE_INTEL_DIR` 不动 | SHERRY_HOME 里的 ast-grep 缓存路径不改 | `config/path.py:153` 不变 |
| **C3**：`os.replace` 同文件系统 | `.sherry-tmp-*.swp` 不搬——留在目标文件同目录 | `atomic_write.py` 不变 |
| **C4**：deny_patterns 语义不变 | `.sherry/` 替换 `.sherry-isolation-repo` 后，整个目录被排除出 baseline | isolation 测试 |
| **C5**：旧 `.codeintel/` 自然废弃 | 不做主动迁移——索引器在新路径重建，旧 DB 成为孤儿文件 | 用户可手动删 `.codeintel/` |
| **C6**：MEMORY.md / USER.md 全局不动 | 只搬 FACTS.md，不搬 MEMORY.md 和 USER.md | 两个文件仍在 `WORKSPACE_DIR/memory/` |
| **C7**：FACTS.md fallback | 项目级 `.sherry/memory/FACTS.md` 不存在时回退到全局 | 未迁移的项目读全局 |
| **C8**：`.sherry/` 创建需满足四个条件 | 用户绑定项目 + 目录与上次不同 + 目录中无 `.sherry/` + 用户开始发对话 | 见 §5 |

---

## 4. 为什么 MEMORY.md / USER.md 不搬（留全局）

三个记忆文件的语义本质不同：

| 文件 | 语义 | 随项目？ | 理由 |
|---|---|---|---|
| **FACTS.md** | 项目级陷阱（"这个项目的 pytest 超时"、"迁移脚本要先跑 alembic upgrade"） | ✅ 随项目 | 内容绑定特定项目的技术栈/工具链/代码库；对另一个项目无意义甚至误导 |
| **MEMORY.md** | agent 的通用工作笔记（"uv 需要 --no-sync"、"ssh dev server 用 2222 端口"） | ❌ 全局 | agent 跨项目的通用知识——工具链经验、环境配置、通用教训，不绑定特定项目 |
| **USER.md** | 用户是谁（"Alice，高级后端，偏好简洁回答"） | ❌ 全局 | 用户身份和偏好跨项目不变——换项目不换人 |

**关键区分**：FACTS.md 的定义是"broad, module-independent pitfalls that recur across **plans/tasks**"——"跨计划"指的是同一个项目的不同计划，不是"跨项目"。nudge 在特定 session 上下文里提取教训——提取的是当前项目的工作经验。内容本质是项目级的，应该跟着项目走。

MEMORY.md 和 USER.md 没有这个项目绑定——它们是 agent 和用户的关系档案，跨项目通用。

---

## 5. `.sherry/` 创建时机

`.sherry/` 目录不是绑定项目时立即创建，而是满足以下**全部四个条件**后才创建：

| 条件 | 检查方式 | 理由 |
|---|---|---|
| ① 用户显式设置了项目目录 | `project_dir_source(session_id) == "session"` | 进程默认目录（`SHERRY_PROJECT_DIR` / `sherry.jsonc` / `ROOT_DIR`）不算——用户没有主动选择 |
| ② 目录与上次绑定的不同 | `current_project_dir(session_id) != last_announced_dir` | 用户可能切换后切回——不重复创建 |
| ③ 目录中不存在 `.sherry/` | `not (root / ".sherry").exists()` | 已有则不重建（幂等） |
| ④ 用户开始向模型发送对话 | `DynamicReminderMiddleware.abefore_model` 首次注入时触发 | 仅绑定目录不代表会用——用户可能绑定后切走；首条消息发送才确认要在此项目工作 |

**实现位置**：在 `DynamicReminderMiddleware.abefore_model` 的 `resolve_injections` 调用之前，或者在 `WorkspaceNoticeMiddleware` 的 project_dir 通知检查之后——两者都在"首轮首次模型调用"时机执行。

**伪代码**：

```python
def maybe_create_sherry_dir(session_id: str) -> bool:
    """在首轮首次模型调用时检查是否需要创建 .sherry/ 目录。

    四个条件全满足才创建。创建后写入 .sherry/ 下需要的子结构。
    """
    root = current_project_dir(session_id)
    source = project_dir_source(session_id)
    last_announced = read_state(session_id, StateKey.PROJECT_DIR_ANNOUNCED)

    # ① 必须是用户显式绑定
    if source != "session":
        return False
    # ② 与上次不同
    if last_announced and str(root) == str(last_announced):
        return False
    # ③ 不已存在
    sherry_dir = Path(root) / ".sherry"
    if sherry_dir.exists():
        return False
    # ④ 已在首轮模型调用上下文中（调用者保证）

    # 创建
    sherry_dir.mkdir(parents=True, exist_ok=False)
    (sherry_dir / "memory").mkdir(exist_ok=True)
    # seed FACTS.md 模板（如果模板存在）
    template = WORKSPACE_TEMPLATE_DIR / current_lang() / "FACTS.md"
    if template.is_file():
        (sherry_dir / "memory" / "FACTS.md").write_text(template.read_text(), encoding="utf-8")
    return True
```

**调用点**：`DynamicReminderMiddleware.abefore_model`（确认是首轮首次模型调用之后），在 `resolve_injections` 之前调 `maybe_create_sherry_dir(session_id)`。

**幂等性**：条件 ② + ③ 保证重复调用不会重复创建。条件 ④ 保证只有在用户真正开始工作时才创建——仅绑定目录不发消息不创建。

**`.sherry/codeintel/` 的创建**：不受此时机限制——代码索引是 `index_db_for_root` 在首次索引时 `mkdir` 的，独立于 `.sherry/` 的创建时机。如果 `.sherry/` 已存在（用户在发消息前手动建了或之前 session 创建过），`mkdir(parents=True, exist_ok=True)` 不报错。

**`.sherry/isolation-repo` 的创建**：不受此时机限制——isolation auto-init 是子代理隔离时触发的，独立于用户对话。

---

## 6. 迁移策略

**不做主动迁移**——零迁移代码。

| 旧条目 | 迁移方式 | 用户感知 |
|---|---|---|
| `.codeintel/index.db` | 索引器在新路径 `.sherry/codeintel/index.db` 重建；旧 DB 不读不写 | 项目根多一个废弃的 `.codeintel/`（可手动删）；新索引在 `.sherry/` 下 |
| `.sherry-isolation-repo` | 新 isolation 运行写 `.sherry/isolation-repo`；旧标记文件不读不写 | 项目根可能有一个旧的 `.sherry-isolation-repo`（可手动删） |

**为什么不做主动迁移**：
1. `.codeintel/index.db` 可能很大（几十 MB）——`os.rename` 跨目录在同文件系统下可行，但增加代码复杂度
2. 旧标记文件是纯信息文本——留着无害，用户看到可以手动删
3. 迁移逻辑只在首次运行时触发，之后是死代码——不值得维护

**可选的迁移提示**：`worktree.py` 检测到旧的 `.sherry-isolation-repo` 存在时，在 marker 内容里追加一行提示："旧标记文件 .sherry-isolation-repo 已废弃，可安全删除"。但这是锦上添花，不是必须。

---

## 7. 为什么不搬另外两个

### 7.1 `.sherry-tmp-*.swp` — 同文件系统约束

`atomic_write.py:206` 用 `tempfile.mkstemp(dir=str(path.parent))` 在目标文件**同目录**创建临时文件，然后 `os.replace(tmp, target)` 原子替换。

`os.replace` 的原子性依赖源和目标在**同一文件系统**（同一 inode 表）。如果把临时文件移到 `project_dir/.sherry/tmp/`：

- 项目根和 `.sherry/tmp/` 通常同文件系统 → 可工作
- 但 `path.parent` 可能是项目子目录（如 `project_dir/src/`），`.sherry/tmp/` 到 `project_dir/src/main.py` 的 rename 在同文件系统下也可工作
- **边界风险**：如果项目根是一个 mount point，`.sherry/` 在另一个 mount point 上，`os.replace` 会失败（`EXDEV: Invalid cross-device link`）

而且 `.sherry-tmp-*.swp` 是**瞬态**的——创建后立即 `os.replace`，只在进程被硬杀时残留。`sweep_stale_temp_files`（`atomic_write.py:198`）在每次写入时清理上次残留。用户 `git status` 看到它们的概率极低，且它们出现在**写入的目标文件所在目录**而非项目根——通常在子目录里，不显眼。

### 7.2 `sherry/<run8>` 分支 — git 概念，非文件

`worktree.py:171-174` 跑 `git worktree add -b sherry/<run8slug>`，创建的是 git 分支（存储在 `.git/refs/heads/sherry/<run8>`）。这不是文件系统条目——不能"放进隐藏目录"。

能做的只有改分支前缀（如 `sherry/isolation/<run8>`），但它仍然是用户 repo 里的分支，`git branch` 能看到。cleanup 时 `git branch -D` 删除——瞬态，不持久。

---

## 8. 验收标准

### 8.1 功能验收

- [ ] 首次代码索引创建 `.sherry/codeintel/index.db`（不是 `.codeintel/index.db`）
- [ ] 索引器跳过 `.sherry/` 目录（prune_dirs 生效）
- [ ] isolation auto-init 创建 `.sherry/isolation-repo`（不是 `.sherry-isolation-repo`）
- [ ] `.git/info/exclude` 包含 `.sherry`（deny_patterns 生效）
- [ ] FACTS.md 在项目级 `.sherry/memory/FACTS.md` 读写（不是全局）
- [ ] FACTS.md 不存在时回退读全局（C7 fallback）
- [ ] MEMORY.md 和 USER.md 仍在全局 `WORKSPACE_DIR/memory/` 读写
- [ ] `.sherry/` 创建满足四个条件（§5）
- [ ] 仅绑定目录不发消息时不创建 `.sherry/`（条件 ④）
- [ ] 空项目无 `.sherry/` 目录
- [ ] 旧 `.codeintel/` 存在时不报错——新索引写到 `.sherry/codeintel/`
- [ ] 旧全局 `FACTS.md` 存在时不报错——读 fallback 全局

### 8.2 回归验收

- [ ] `CODE_INTEL_DIR`（SHERRY_HOME ast-grep 缓存）路径不变
- [ ] `SHERRY_CODE_INTEL_DB` 环境变量覆盖仍生效
- [ ] isolation worktree 创建/删除正常
- [ ] `atomic_write.py` 不变（`.sherry-tmp-*.swp` 行为不变）
- [ ] nudge（plan-extraction + memory-review）写 FACTS.md 到项目级路径
- [ ] 系统提示冻结快照：FACTS.md per-session，MEMORY.md/USER.md 全局
- [ ] 现有 code_intel 测试通过
- [ ] 现有 memory_store 测试通过

---

## 9. 改动量

| 文件 | 改动 | 行数 |
|---|---|---|
| `agent/tools/code_intel/roots.py` | 路径 + docstring | ~2 行 |
| `config/features/agent_side/code_intel.py` | prune_dirs 名替换 | ~1 行 |
| `config/features/agent_side/subagent_isolation.py` | marker_name + deny_patterns | ~2 行 |
| `agent/tools/subagent/isolation/worktree.py` | marker 父目录创建 | ~1 行 |
| `agent/tools/memory.py` | FACTS.md `_path_for` per-project + per-session 快照 | ~40 行 |
| `workspace/prompt_builder.py` | `format_for_system_prompt("facts")` 传 session_id | ~5 行 |
| `agent/middlewares/summarization/nudges.py` | nudge 读写 FACTS.md 传 session_id | ~10 行 |
| `server/service/memory.py` | HTTP `/memory` 支持 session_id 路径解析 | ~15 行 |
| 新建 `maybe_create_sherry_dir` | 创建时机逻辑（§5 伪代码） | ~25 行 |
| 测试 | 路径断言 + FACTS.md per-project + 创建时机 | ~40 行 |
| **总计** | | **~141 行** |

前四项（codeintel + isolation）是 §2.1-2.5 的改动，~16 行，可独立先做。FACTS.md 搬迁（§2.6）是 ~70 行，改动量更大因为要改 MemoryStore 的路径解析和快照加载逻辑。

---

## 10. 不做的事（边界）

| 项 | 不做的原因 |
|---|---|
| 搬 `.sherry-tmp-*.swp` | `os.replace` 需同文件系统；瞬态文件（立即替换 + 自动清理），不值得增加复杂度 |
| 搬 `sherry/<run8>` 分支 | git 分支不是文件系统条目，不能"放进目录" |
| 搬 MEMORY.md | agent 的通用工作笔记（跨项目知识），不随项目走（§4） |
| 搬 USER.md | 用户身份和偏好（跨项目不变），不随项目走（§4） |
| 主动迁移旧 `.codeintel/` | 零迁移代码策略——索引器自然重建，旧 DB 成为无害孤儿 |
| 主动迁移旧全局 `FACTS.md` | fallback 策略——项目级不存在时读全局，用户可手动复制 |
| 改 `.gitignore` | sherry 从不碰用户项目的 `.gitignore` |
| 搬 `CODE_INTEL_DIR`（SHERRY_HOME） | 那是 ast-grep 二进制缓存，在 SHERRY_HOME，不在项目目录 |
| 搬 `.git/` | git 仓库不能搬 |
| 绑定目录时立即创建 `.sherry/` | 必须等用户发送首条对话（§5 条件 ④） |

---

## 11. 关键参考

### sherry 内部

- `agent/tools/code_intel/roots.py:20-36`（`index_db_for_root`——per-project 索引路径解析）
- `agent/tools/code_intel/indexer.py:175`（`Path(self._db_path).parent.mkdir`——懒创建父目录）
- `config/features/agent_side/code_intel.py:65-75`（`prune_dirs`——索引器跳过的目录）
- `config/features/agent_side/subagent_isolation.py:70`（`repo_marker_name`）
- `config/features/agent_side/subagent_isolation.py:75-104`（`deny_patterns`——isolation baseline 排除列表）
- `agent/tools/subagent/isolation/worktree.py:89-100`（`.git/info/exclude` 写入——读 deny_patterns）
- `agent/tools/subagent/isolation/worktree.py:118-131`（marker 文件写入）
- `agent/tools/pub_base/atomic_write.py:206`（`.sherry-tmp-*.swp`——不搬的原因）
- `config/path.py:121`（`MEMORY_DIR = WORKSPACE_DIR / "memory"`——FACTS.md 当前全局位置）
- `config/path.py:128`（`SESSIONS_DIR = WORKSPACE_DIR / "sessions"`——不搬）
- `config/path.py:153`（`CODE_INTEL_DIR`——SHERRY_HOME 的 ast-grep 缓存，不搬）
- `agent/tools/memory.py:129`（`MemoryStore`——FACTS.md 的存储/快照/写入逻辑）
- `agent/tools/memory.py:196-202`（`_path_for`——目标文件路径解析，要改）
- `agent/tools/memory.py:514-525`（`format_for_system_prompt`——冻结快照返回，要改）
- `workspace/prompt_builder.py:422-433`（`format_memory_for_system_prompt("facts")` 调用——要传 session_id）
- `agent/middlewares/summarization/nudges.py:54-63`（`_render_facts_context`——nudge 读 FACTS.md，要传 session_id）
- `agent/middlewares/summarization/nudges.py:274-301`（plan-extraction Part 3——写 FACTS.md）
- `server/service/memory.py`（`_MemoryFileStore`——HTTP 读写 memory 文件，要支持 session_id）
- `runtime/session/project_dir.py:153`（`current_project_dir`——per-call 解析，FACTS.md 路径解析用）
- `agent/tools/pub_base/path_utils.py:129-141`（`session_workspace_root`——session 级项目根解析）

### 关联 TODO

- 跟 `计划_项目级指令文件读取.md` 相关——`.sherry/` 未来可放项目级 AGENTS.md 或 `.sherry/skills/`
- 跟 `调研_项目级配置能力与FACTS归属分析.md` 相关——FACTS.md per-project 的完整分析
