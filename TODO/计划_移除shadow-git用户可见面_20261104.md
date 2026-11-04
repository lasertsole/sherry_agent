# 计划_移除shadow-git用户可见面_20261104

取消 shadow-git 对用户的可见性和手动回退能力。
内部自动快照（文件写入前的 `ensure_checkpoint`）保留——只删除用户/模型可触达的
list/show/restore 路径和前端回退 UI。

---

## 保留 vs 删除边界

| 组件 | 性质 | 决定 |
|------|------|------|
| `shadow_git.py` 模块（`ensure_checkpoint`/`take_snapshot`/`restore_checkpoint`/`list_checkpoints`/`run_shadow`/`shadow_head`） | 内部基础设施 | **保留** |
| `ensure_checkpoint` 的所有调用方（write_file/patch_file/python_repl/terminal/worktree） | 内部自动快照 | **保留** |
| `pub/git_head.py`（只读 `shadow_head`） | 内部，git-notice 卡片 | **保留** |
| `config/features/agent_side/shadow_git.py`（enabled/max_snapshots/git_timeout_s/store_dir） | 内部配置 | **保留** |
| git-graph **读** 函数（`read_graph`/`read_commit_files`/`read_commit_diff`） | 只读历史浏览 | **保留** |
| `GET /git/graph`、`GET /git/commit`、`GET /git/commit/file` 路由 | 只读 HTTP | **保留** |
| `checkpoint` agent 工具（list/show/restore） | 模型可调用 → 用户可见 | **删除** |
| git-graph **写** 函数（`reset_to`/`checkout_ref`） | 手动回退 | **删除** |
| `POST /git/reset`、`POST /git/checkout` 路由 | 手动回退 HTTP | **删除** |
| 前端回退/切换 UI（GitGraphPanel 的 reset 菜单 + checkout 菜单） | 用户可操作 | **删除** |

> **注意**：`shadow_git.restore_checkpoint` 函数本身**保留**——它被内部机制使用
>（pre-restore 快照逻辑、编辑消息文件回滚计划 `计划_编辑消息限制与文件回滚_20261104.md`
> 中新增的 `restore_to_turn` API 将直接调用它）。删除的只是**模型可通过工具调用的入口**
> 和**用户可通过 UI 触发的入口**。

---

## 第 1 步：删除 `checkpoint` agent 工具

### 1.1 删除工具文件

**删除**：`agent/tools/checkpoint_tools.py`（整个文件，108 行）。

工具暴露三个 action 给模型：
- `list` → `shadow_git.list_checkpoints`
- `show` → `shadow_git.read_commit_diff` / `read_commit_files`
- `restore` → `shadow_git.restore_checkpoint`（手动回退）

### 1.2 删除工具注册

**`agent/tools/__init__.py`**：
- Line 26：删 `from .checkpoint_tools import build_checkpoint_tool`
- Line 72：删 `_MAIN_TOOLS_BUILDERS` 中的 `build_checkpoint_tool,`

### 1.3 删除工具目录条目

**`agent/tools/catalog.py`**：
- Line 44：从 `TOOL_GROUPS["files"]` 删 `"checkpoint"`
  ```python
  # 改前
  "files": ("read_file", "write_file", "patch_file", "search_files", "checkpoint"),
  # 改后
  "files": ("read_file", "write_file", "patch_file", "search_files"),
  ```
- Line 112：从 `REQUIRED_TOOLS` 删 `"checkpoint"`

### 1.4 删除工具超时配置

**`config/features/agent_side/tools_timeouts.py`**（如有 `checkpoint` 超时条目）：
删除 `checkpoint` 的超时配置项。

---

## 第 2 步：删除 git-graph 写操作

### 2.1 后端 service

**`server/service/git_graph_service.py`**：
- 删 `reset_to`（lines 239-259）——调用 `shadow_git.restore_checkpoint` 的手动回退。
- 删 `checkout_ref`（lines 262-264）——已硬编码拒绝但函数和导出仍在。
- 删 `_RESET_MODES`（line 236）。
- `__all__`（lines 33-43）：删 `"reset_to"` 和 `"checkout_ref"`。

**保留**：`read_graph`、`read_commit_files`、`read_commit_diff`、所有 dataclass、
`_run`、`_parse_log`、`_header`、`_refs_of`、`_blob`、`_aligned_rows`、
`_parse_name-status`、`_parents_of`。

### 2.2 后端 HTTP 路由

**`server/trigger/http/git.py`**：
- 删 `POST /git/reset`（lines 92-115，`git_reset_handler`）。
- 删 `POST /git/checkout`（lines 118-136，`git_checkout_handler`）。
- 删 imports：line 28（`checkout_ref`）、line 32（`reset_to`）。
- 更新 module docstring（lines 9-12）移除写操作描述。

**保留**：`GET /git/graph`（line 64）、`GET /git/commit`（line 139）、
`GET /git/commit/file`（line 182）。

---

## 第 3 步：删除前端回退/切换 UI

### 3.1 bridge

**`client/app/composables/bridge/git.ts`**：
- 删 `resetGitBranch`（lines 167-179）→ `POST /git/reset`。
- 删 `checkoutGitRef`（lines 188-196）→ `POST /git/checkout`。
- 删 `readPage` helper（lines 104-121）——仅被上述两个写操作使用。
- 更新文件头注释（lines 10-12）移除写操作描述。

**保留**：`fetchGitGraph`、`fetchCommitFiles`、`fetchCommitDiff`、`toPage`、
`requirePayload`、类型接口。

### 3.2 GitGraphPanel

**`client/app/pages/home/components/GitGraphPanel.vue`**：
- 删 `resetTo`（lines 611-640）——确认对话框包装。
- 删 context-menu reset 项（lines 663-672）：`menuResetSoft` / `menuResetHard`。
- 删 ref-chip checkout（lines 691-697）：`checkoutBranch` 项。
- 删 branch switcher（`openBranchMenu` lines 735+）和 header branch dropdown
  调用 `checkoutGitRef` 的逻辑——后端已 pin `branches: []`，UI 本已 inert。
- 删 import（line 246）：从 `~/composables/bridge/git` 移除 `checkoutGitRef, resetGitBranch`。
- 删 i18n 字符串（四语言内联块）：`menuResetSoft`、`menuResetHard`、`resetAction`、
  `resetFailed`、`confirmSoftTitle`、`confirmSoftMessage`、`confirmHardTitle`、
  `confirmHardMessage`、`modeHard`、`modeSoft`、`checkoutBranch`、`checkoutFailed`。

**保留**：lane 渲染、commit 详情 drawer、file diff 面板、分页加载——这些是只读的。

---

## 第 4 步：删除测试

### 后端

**`tests/agent/tools/test_tool_idempotency_metadata.py`**：
- 删 `test_checkpoint_lists_only_its_read_actions_as_repeatable`（lines 5, 11-16）
  和 `build_checkpoint_tool` import。

**`tests/server/test_git_graph.py`**：
- 删 `test_reset_restores_the_checkpoint_without_touching_user_git`（line 76）。
- 删 `test_checkout_is_refused_because_there_are_no_branches`（line 87）。
- 更新 module docstring（line 5）移除 "restore action and the branchless checkout refusal"。

**保留**：
- `test_the_graph_serves_the_shadow_chain`（34）
- `test_paging_reports_more_and_does_not_overlap`（53）
- `test_commit_files_and_diff_come_from_the_shadow_tree`（62）
- `test_an_empty_project_is_available_with_an_empty_chain`（93）
- `tests/agent/tools/pub_base/test_shadow_git.py`（测试内部模块函数）
- `tests/agent/tools/test_concurrent_write_protection.py`（测试 `ensure_checkpoint` dedup）

### 前端

**`client/app/composables/__tests__/git-bridge.test.ts`**：
- 删 `checkoutGitRef` 测试块（lines 72, 96）。
- 删 `resetGitBranch` 测试块（line 110）。
- 删对应 imports（line 23）。
- 保留 `fetchGitGraph`/`fetchCommitFiles`/`fetchCommitDiff` 读测试。

**`client/app/pages/home/components/__tests__/GitGraphPanel.integration.test.ts`**：
- 删 line 369（"lists the local branches ... checks one out on click"）。
- **删 line 464**（"keeps the page and shows git's reason when a checkout is refused"）—
  此测试用 `checkoutGitRef.mockRejectedValueOnce` 测试 checkout 拒绝场景，
  计划初稿遗漏此处。
- 删 `checkoutGitRef: vi.fn()` mock（line 19）。
- 该文件**无** reset 测试——两个写操作测试都是 checkout 测试（lines 369 + 464）。
- 保留 lane/diff/paging 渲染测试。

---

## 第 5 步：更新文档

### AGENTS.md

- **Lines 839-841**：删除"回退/切换 — `POST /git/reset` ... `POST /git/checkout`"
  描述，改为"Git Graph 面板为只读，不支持回退或切换"。
- **Lines 853-855**：删除"the two write actions go through the raw transport so a
  refusal reaches the toast with git's own reason"——写操作已删除，此描述过期。
- **Lines 859-865**：删除 ref-chip checkout 和 `branches: []` 相关的"切换"描述。
- **Lines 2334-2336**：删除"the `checkpoint` tool exposes list/show/restore"描述。

### Wiki

- `wiki/file-safety/README.md`（+ ja/zh/ko）：
  - **Line 73**：删除"`checkpoint` tool exposes list / show / restore to the agent"句子。
  - **Line 220**：模块表中 `shadow_git.py` 行删除"and checkpoint reads"，
    改为只描述自动快照和恢复。
  - 移除用户可手动回退的描述，保留自动快照机制的描述。
- `wiki/loop-prevention/README.md`（+ ja/zh/ko）：
  - **Line 56**：删除 `checkpoint` 作为 `idempotent_actions` 示例的引用
    （"`checkpoint` hashes `list`/`show`"），工具已删除。
- `wiki/threat-model/README.md`（+ ja/zh/ko）：检查无 checkpoint 工具引用，
  实际无需改动（threat-model 中的 "checkout" 是 git 工作树概念，"shadow-git"
  列为内部写入路径，保留）。

### 其他文档

- **`pub/git_head.py:4`**：module docstring 中"checkpoint tools"消费者引用已过期
  （`checkpoint_tools.py` 被删除），删除"(checkpoint tools)"，
  保留 `agent/tools/pub_base/shadow_git.py` 作为真实 importer 的描述。

### TODO 文件

- `TODO/计划_code-intel性能优化_shadow-git.md`：更新（如有用户可见面描述）。
- `TODO/计划_编辑消息限制与文件回滚_20261104.md`：该计划的 shadow-git 回滚
  调用 `restore_to_turn`（内部函数直接调用），**不经过被删除的 `checkpoint` 工具**，
  不受影响。但需确认文中不引用已删除的 `checkpoint` 工具。

### README

- `README.md` / `README.zh.md` / `README.ja.md` / `README.ko.md`：
  移除 shadow-git rollback 作为用户特性的描述。

---

## 第 6 步：契约测试更新

**`tests/server/test_agent_config.py::test_every_main_tool_lands_in_a_named_group`**
（line 149）：该测试遍历 `_MAIN_TOOLS_BUILDERS`（排除 `build_mcp_tools`）并断言
每个工具名在 `TOOL_GROUPS` 中。删除 `build_checkpoint_tool` 后，`checkpoint`
工具名不再产生——测试自动通过，不需改动。

**`tests/server/test_agent_config.py::test_the_catalog_orders_the_groups_and_flags_the_required_tools`**
（line 171）：断言 `REQUIRED_TOOLS` 中的工具标记为 required。删除 `checkpoint`
后，该工具不再在 `REQUIRED_TOOLS` 中——测试自动通过。

---

## 验证

- `checkpoint` 工具不再出现在 `GET /agent/catalog` 的工具列表中。
- `POST /git/reset` 和 `POST /git/checkout` 路由返回 404。
- GitGraphPanel 的 commit row context menu 不再有"回退"项。
- GitGraphPanel 的 ref-chip / header branch dropdown 不再有"切换"项。
- 文件写入工具（write_file/patch_file/terminal/python_repl）仍正常创建 shadow-git 快照
  （`ensure_checkpoint` 调用不变）。
- GitGraphPanel 的只读功能（lane 图、commit 详情 drawer、file diff）正常工作。
- 全文搜索 `checkpoint_tools`/`build_checkpoint_tool`/`reset_to`/`checkout_ref`/
  `resetGitBranch`/`checkoutGitRef` 确认无残留。
- `shadow_git.restore_checkpoint` / `shadow_git.list_checkpoints` /
  `shadow_git.ensure_checkpoint` 仍存在（内部使用）。

## 与编辑消息回滚计划的关系

`TODO/计划_编辑消息限制与文件回滚_20261104.md` 中的 shadow-git 文件回滚
通过后端直接调用 `shadow_git.restore_to_turn`（新增的内部 API）实现，
**不经过被删除的 `checkpoint` 工具或 `/git/reset` 路由**。两个计划互不影响，
可独立实施。但建议先实施本计划（移除用户可见面），再实施编辑回滚计划
（新增内部回滚路径），避免混淆。
