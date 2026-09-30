# 改造计划: ripgrep 统一搜索（文件搜索工具 + 代码搜索子代理）

> **状态**: 待评估（本文件是方案，未实施）
> **创建日期**: 2026-09-30
> **目标**: 把 Sherry 的**关键词层**搜索统一到 ripgrep（保留 Python 兜底），并迁移 DeepAgents 的**双重超时清理**（看门狗 + 有界回收），一并消除现存的「超时后无界等待」
> **对标参考**: 同目录 `../deepagents`（`libs/deepagents/deepagents/backends/filesystem.py`）
> **证据分级**: `[实测]` 本机执行过；`[源码确认]` 可定位到文件行号；`[未验证]` 明确未确认

---

## 〇、结论摘要

1. **该换的只有一处**：`search_files`（主代理的内容/文件名搜索）。同范围实测 **16.6s → 1.19s**（带确定序 2.04s）。真正的收益不是"快"，而是**把"预算内扫不完"变成"扫得完"**——当前 5s 预算下，在仓库根目录做内容搜索拿到的是**残缺结果**。
2. **代码搜索的子代理确实存在**：`RESEARCHER` / `LIBRARIAN`（`CODE_INTEL_ROLES`）。但它们的**关键词层已经是 rg**——靠 `terminal` 跑 `rg`/`grep`。缺的不是 rg，而是**可控性**：没有解析器（PATH 上没有就静默降级成系统 `grep`）、没有结构化输出、没有匹配上限/时间预算/分页。把它们接上同一套 rg 引擎（或直接授予 `search_files`）才是"用 rg 替换"的落点。
3. **语义层不能换**：`explore` / `callers` / `callees` / `impact` / `semantic_code_search` / 8 个 LSP 工具 / `ast_grep` 是 Sherry 明显领先的部分，rg 与它们不同层。
4. **DeepAgents 的双重超时清理可以迁移，而且是本文件里收益最确定的一段**：Sherry 现存 **三处「超时后无界等待」**（`terminal.py` 同步/异步两处 + `ast_grep` 的 `subprocess.run` 隐式 re-communicate），一条挂死的命令/子进程能拖住整个 turn。迁移后同时解决"挂死"与"超时丢部分结果"两个问题。

---

## 一、现状与实测

### 1.1 实测数据（本仓库，2026-09-30）

同一范围内对比（遍历 9,871 个文件 / 8,437 个文本文件 / 45.1 MB）：

| 方式 | 耗时 | 命中 |
| --- | --- | --- |
| 现状：Python `re` + `bounded_walk` | **16.6 s**（遍历 0.95 + 二进制嗅探 3.54 + 扫描约 12） | 2 |
| rg，同一范围（排除同一批目录） | **1.19 s** | 2 |
| rg，同一范围 + `--sort=path`（分页需要的确定序） | **2.04 s** | 2 |
| rg，默认范围（尊重 `.gitignore`） | 0.24 s | 2 |
| 文件名模式 `search_*.py`：Python 0.60 s → rg 0.19 s | | |

**关键实测**：用真实工具跑一个全仓无命中模式，`search_files` 在 `5.00s` 返回
`{"truncated": true, "scan_truncated": true, "scan_stop_reason": "time_budget"}`——即**今天在本仓库根目录做内容搜索拿到的是残缺结果**（同一模式在 `agent/tools/file_tools` 子目录下 0.01s 返回完整结果）。稀疏/无命中查询（"X 到底有没有定义"）正是最常见的用法。

### 1.2 现状实现（关键词层）

| 项 | 位置 | 说明 |
| --- | --- | --- |
| 内容搜索 / 文件名搜索 | `agent/tools/file_tools/search_files.py` | Python `re`（**恒 `re.IGNORECASE`**）与 `fnmatch`；行长截断 `line[:500]`（`:88`） |
| 有界遍历 | `agent/tools/file_tools/search_scan.py:130-136` | `os.walk` + 目录剪枝 + 时间预算 |
| 结果信封 | `search_scan.py:96-125` | `truncated` / `scan_truncated` / `scan_stop_reason` / `hint`（**Sherry 领先设计，必须保住**） |
| 预算/上限/剪枝 | `config/features/agent_side/tools_timeouts.py:59-64` | `file_tools_search_time_budget_s = 5.0`、`max_matches = 10_000`、`prune_dirs = [proc, sys, dev]` |
| 目录跳过 | `agent/tools/pub_base/file_utils.py:16-33` | 点目录（`.git`/`.venv`/`.mypy_cache`…）、`node_modules`、`venv`、`__pycache__` |
| 二进制嗅探 | `agent/tools/pub_base/file_utils.py:4-13` | 每文件读头部 4 KB 找 `\x00`（实测吃 3.54s） |
| 越界防护 | `search_files.py:37-47` | `_stays_within_root`：解析后路径必须落在搜索根内 |
| 同步/异步 | `search_files.py:235-264` | `_run` 与 `_arun` **都调用同一个同步 `_core()`**——异步路径会把事件循环堵住整段扫描时间 |

### 1.3 代码搜索机制盘点（确认结果）

| 机制 | 谁在用 | 关键词层现状 | 能否 rg 化 |
| --- | --- | --- | --- |
| `search_files`（content / files 两模式） | **仅主代理**（`agent/tools/__init__.py:6` 进 `build_main_tools`；**四个角色定义都没有授予它**） | Python，见 1.2 | **是（主落点）** |
| `terminal` 里的 `rg` / `grep` / `find` | 所有子代理，尤其 `RESEARCHER`/`LIBRARIAN` | 碰运气：PATH 上有 `rg` 才快，否则系统 `grep`；输出非结构化、无上限/预算/分页 | **是（可控化）** |
| `explore` / `callers` / `callees` / `impact` / `semantic_code_search` | `RESEARCHER`+`LIBRARIAN`（`agent/tools/subagent/spawn/core.py:1008-1020` 注入） | tree-sitter 索引 + 向量检索 | 否（不同层） |
| 8 个 LSP 工具 | 同上（`agent/tools/code_intel/lsp/tools.py:757`） | 语言服务器 | 否 |
| `ast_grep_search` / `ast_grep_rewrite` | 同上（`agent/tools/code_intel/ast_grep/runner.py`） | `sg` CLI 子进程（结构化 AST 搜索） | 否（但见 §4 的超时清理） |
| `code_intel` 索引器的文件枚举 | `agent/tools/code_intel/indexer.py:166-170` | `os.walk` | 不值（瓶颈是解析不是遍历） |

**角色声明印证**（`agent/tools/subagent/roles/definitions/*/AGENTS.md`）：

- `researcher`：`tools: [read_file, terminal, web_search]`，正文写「Codebase search via terminal commands (grep, find, **rg**, ls)」
- `librarian`：同上（定位是"外部代码库检索：clone/index/search"）
- `executor`：`[read_file, write_file, patch_file, terminal, python_repl]`
- `reviewer`：`[read_file, terminal]`

**code-intel 指导语的收尾**（`agent/tools/subagent/spawn/system_prompt.py:133-165`）明确写着：

> Workflow: explore(query) → lsp_find_references / callers for precision → **terminal (rg/grep) as keyword fallback**.

即：**"用 rg 搜代码"Sherry 已经在做**，只是走 shell、不可控、不可兜底。这份文件要做的就是把这件事变成正式路径。

---

## 二、改造目标与不变量

**目标**：关键词层由 rg 承担；rg 不可用/模式不兼容时**自动回退**到现有 Python 实现（不是删掉它）。

**必须保住的不变量**（每一条都能在 §7 找到验收方式）：

1. **确定序**：分页 `offset`/`limit` 跨调用稳定（rg 需要 `--sort=path`，代价实测 1.19→2.04s）。
2. **大小写**：现状恒 `re.IGNORECASE` → `rg -i`。
3. **越界防护**：结果路径解析后必须落在搜索根内（`_stays_within_root`）。
4. **二进制跳过**、**行长 500 截断**、**信封三档截断标记**、**5s 预算 + 10k 上限**、**剪枝语义**（`should_skip_dir` + `proc/sys/dev`）。
5. **绝不把不完整当完整**：rg 退出码 `≥2`（模式非法、目录不可读、glob 畸形）时必须回退，不能返回部分结果冒充完整（DeepAgents 同款契约，见 §4.1）。
6. **不动语义层**：LSP / AST / 向量检索的工具面与行为不变。

---

## 三、方案 A：rg 引擎

### 3.1 二进制解析（照抄现存先例，不新发明）

`agent/tools/code_intel/ast_grep/resolver.py:1-22` 已经有成套实现：五级发现顺序（env 覆写 → `~/.sherry/runtime/<slug>/` → `CODE_INTEL_DIR/.../bin/` → PATH（含 Windows `PATHEXT`）→ Homebrew/Linuxbrew）+ **`--version` 探针**（输出必须含 `ast-grep`）+ 进程内缓存 + 可失效。rg 版对应：

1. `SHERRY_RG_PATH` env 覆写
2. `~/.sherry/runtime/ripgrep/<platform>-<arch>/rg`
3. `CODE_INTEL_DIR/ripgrep/bin/rg`
4. PATH 查找（`rg` / `rg.exe`）
5. `/opt/homebrew/bin`、`/home/linuxbrew/.linuxbrew/bin`

探针：`rg --version` 输出含 `ripgrep`。**exec 期失败要清缓存**（安装被卸载、权限变更、`which` 与 exec 竞态）——DeepAgents 的处理是 `cache_clear()` + WARNING（`filesystem.py:960-973`），照抄。

**未找到 rg 时的行为**：`INFO` 一次（不是每调用一次），然后走 Python 路径。DeepAgents 专门为此留了日志（`filesystem.py:72-88`：`rg` 装了但 agent 的 PATH 看不到——沙箱/剥离环境下的常见故障）。

### 3.2 调用形状

```
rg --json --no-config -i --sort=path \
   [-m <limit+offset+1>] [--glob <file_glob>] [--max-columns 500] \
   -- <pattern> .
```

- **目录搜索时 `cwd=root` + 搜索路径用 `.`**：DeepAgents 记录了这个坑（`filesystem.py:944-952`，#2732）——给绝对路径时，含目录组件的 `--glob`（如 `docs/*.md`）会**静默匹配不到**；单文件搜索则相反（用绝对路径，不能把文件当 `cwd`，会 `NotADirectoryError`）。
- **流式解析 `--json`**，逐行读，只保留解析后的命中，**不 buffer 全部 stdout**（`filesystem.py:930-945`）。
- **argv 列表、绝不用 shell**，模式用 `--` 与搜索路径分隔，防选项注入（模式以 `-` 开头时必须走 `--`）。
- **env 走 `scrub_env(os.environ.copy())`**，与 `ast_grep/runner.py:171-177` 一致。
- `--max-columns 500` 对上现状的 `line[:500]`（`search_files.py:88`）。
- 匹配上限：`-m` 是**每文件**上限，管不住总数；总数由收集循环自己卡（+1 的余量用于区分"正好到上限"与"还有更多"）。DeepAgents 的做法见 `filesystem.py:930-940`。

### 3.3 必须拍板的行为决策：要不要尊重 `.gitignore`

rg 默认尊重 ignore 文件；**这是行为变更，不是优化**：本仓库 `.gitignore` 里有 `/workspace/`、`skills/auto`、`*.db`、`/logs/` 等，尊重它会让 agent **搜不到**这些路径。实测"rg 默认范围"0.24s 的快，绝大部分来自这个差异（同一范围 `--no-ignore` 是 1.19s）。

**建议**：默认**不改变**现有语义 → 显式 `--no-ignore`，并用 `--glob` 显式排除，与 `should_skip_dir` + `prune_dirs` 对齐；把"尊重 gitignore"做成独立开关，单独评估。

### 3.4 正则方言差异

Python `re` 支持反向引用与环视；rg 默认的 Rust regex **不支持**。现状能用的模式（`(?<=x)y`、`\1`）在 rg 下会报错。处理：

- 出 rg 前做一次**廉价能力检测**（是否含 `\1`–`\9`、`(?=`, `(?!`, `(?<=`, `(?<!`），命中就直接走 Python 路径；
- 或探测本机 rg 是否支持 `-P`（PCRE2）——**未验证**本机 14.1.1 的 `-P` 行为，实施时先跑一次探针再决定。

---

## 四、方案 B：迁移 DeepAgents 的双重超时清理

### 4.1 DeepAgents 机制（逐条，带行号）

`libs/deepagents/deepagents/backends/filesystem.py`：

| 机制 | 行号 | 做法 |
| --- | --- | --- |
| 看门狗 | `:991-997` | `threading.Timer(DEFAULT_GREP_TIMEOUT=15s, _kill_on_timeout)`：置 `timed_out` 事件 + `proc.kill()`。理由写在注释里：**阻塞读 `proc.stdout` 自己不可能遵守 deadline，看门狗才是那个上界** |
| 命中上限的优雅停 | `:1013-1016` | 超上限时 `truncated=True` + `proc.terminate()`（SIGTERM）后 break |
| 收尾 | `:1017-1025` | `finally`: `timer.cancel()` → `_reap_ripgrep(proc)` → `stderr_thread.join()` → 关 stderr |
| **有界回收** | `:1129-1148` | 关 stdout → `proc.wait(timeout=5)` → 超时 `proc.kill()`（SIGKILL）→ **再** `proc.wait(timeout=5)` → 仍超时则 `logger.warning("did not exit after SIGKILL; abandoning process handle")` 并**放弃句柄**。注释点名场景：卡在不可中断 IO（如挂掉的 NFS）连 SIGKILL 都忽略，无界等待会把调用永远挂住 |
| stderr 排空 | `:1115-1127` | 守护线程逐块（8192）读，**保留上限 500 字符**用于诊断。不放任 stderr 满管——否则子进程阻塞在写 stderr，而我们阻塞在读 stdout，双向死锁 |
| 超时语义 | `:1027-1033` | 有部分结果 → 返回 `(results, truncated=True)` + WARNING；完全没输出 → 回退 Python |
| 退出码契约 | `:1042-1045` | `0`=有命中、`1`=无命中（都正常）、`≥2`=硬错 → **回退 Python**，因为"返回硬错前收集到的结果会把不完整的搜索呈现成完整的" |
| 预算排序 | `:52-61` | 内层 `_DEFAULT_GLOB_TIMEOUT=5s` 必须小于外层 `GLOB_TIMEOUT=10s`，由测试 `test_glob_backend_budget_below_middleware_deadline` 钉住——内层先返回部分结果，外层才不会被拖死 |

### 4.2 Sherry 现状：三处「超时后无界等待」

| 位置 | 问题 |
| --- | --- |
| `agent/tools/terminal.py:264-267` | `except TimeoutExpired: proc.kill(); proc.communicate()` —— kill 之后的**第二次等待没有超时**。子进程若已 fork 出持有 stdout 的后台孙进程，管道不关闭，`communicate()` 会**永远等下去**（`subprocess.run(timeout=…)` 的经典坑） |
| `agent/tools/terminal.py:384-387` | 同上，异步版：`proc.kill(); await proc.communicate()`，同样无界 |
| `agent/tools/code_intel/ast_grep/runner.py:169-177` | `subprocess.run(..., timeout=…)`：超时后标准库内部的再次 `communicate()` 无上界，且**超时会丢掉已产出的部分结果**（搜索类工具应当"有部分结果就返回并标截断"） |
| `agent/tools/file_tools/search_files.py:251-264` | `_arun` 直接调用同步 `_core()`：异步路径把事件循环堵住整段扫描时间（现状 16.6s） |

### 4.3 迁移设计

1. **新增共享工具** `agent/tools/pub_base/process_reap.py`（依赖无关，`terminal` / `search_files` / `ast_grep` 都能引）：

   ```python
   def reap_process(proc, *, grace: float = 5.0, kill_grace: float = 5.0) -> str:
       """关流 → wait(grace) → kill() → wait(kill_grace) → 放弃。
       返回 'exited' / 'killed' / 'abandoned'（放弃时 WARNING，绝不无界等待）。"""
   ```

   同步与异步各一个变体（异步用 `asyncio.wait_for` / `asyncio.timeout` 包裹；`asyncio.subprocess.Process.kill()` 之后同样要有界等待 `proc.wait()`）。

2. **新 rg 调用点**：看门狗 → 有界回收 → 有部分结果就返回 `truncated=True`，无结果才回退 Python（与 §4.1 的超时语义一致，也与 Sherry 现有截断标记哲学一致）。

3. **顺带修 `terminal.py` 两处**：把无界 `communicate()` 换成有界回收。这是迁移机制后**用户可见收益最直接**的一条：一条挂死的命令不再拖住整个 turn。

4. **预算排序**：rg 看门狗（建议 5–15s，对齐 DeepAgents 的 15s）必须**小于** tool-call 外层超时；工具的 5s 扫描预算继续保留（它是"返回部分结果"的语义来源）。参照 DeepAgents 用测试钉住这个顺序。

5. **stderr 排空**：rg 调用点必须带守护线程排空 stderr（带上限），不要指望 `capture_output` 之外的形状。

---

## 五、落点清单（换 / 不换）

| 位置 | 判定 | 理由 |
| --- | --- | --- |
| `search_files` content 模式 | **换**（rg 主路径 + Python 兜底） | §1.1 实测；预算内从"残缺"变"完整" |
| `search_files` files 模式 | **换** | 0.60→0.19s，与上面共用解析器与回收工具 |
| `search_files._arun` | **改** | 不再直接调同步 `_core()`：`asyncio.to_thread` 或 async subprocess（rg 路径天然支持） |
| `RESEARCHER` / `LIBRARIAN` 的关键词层 | **接线**（二选一） | 现状只有 shell：① 解析器就绪后，让它们的 shell 里**稳定有 rg**（PATH 注入/包装）；② 或直接把 `search_files` 加进这两个角色的 `tools`（结构化、有预算、无需 HITL 审批）——**建议②**，因为它同时解决"非结构化输出"与"每次 shell 都要过审批" |
| `LIBRARIAN` 的外部代码库检索 | **可选** | 克隆下来的外部大仓库正是 rg + gitignore 语义的主场；但要先确认它现在用什么（**未验证**） |
| `terminal.py` 两处超时分支 | **改**（§4.3） | 无界等待 → 有界回收 |
| `ast_grep/runner.py` | **改**（仅超时语义） | 换成有界回收 + 保住部分结果；AST 搜索本身不换 |
| `code_intel/indexer.py` 文件枚举 | 不换 | 瓶颈是 tree-sitter 解析，不是遍历 |
| skill 发现三处（`skill_utils` / `skill_usage` / `skill_view`） | 不换 | 目录小 + 要结构化解析，spawn 成本反超 |
| `skill_scan_cache` / `skill_scanner`（安全扫描） | 不换（不建议） | 安全路径，目录小，要逐规则结论；新增二进制与参数面要单独审 |
| `curator/usage.py`、`knowledge_graph.py` 的 rglob | 不适用 | 它们是列目录，不是内容搜索 |
| `message_search.py` | 不适用 | SQL 查 MesMemory |
| `scripts/check_doc_links.py` / `check_docs_parity.py` | 不换 | 开发期工具，要解析 markdown |

**附带的另一半（Zcode 真正在用的那半）**：把 `rg`/`ugrep`/`bfs` 注入 **bash prelude**，让模型自己敲的 `grep`/`find` 变快。Sherry 的等价物是 `terminal` 的子进程环境（`env_scrub.py` 控制）+ HITL 门，属于另一个设计议题——建议与本文分开评估，别混在一个改动里。

---

## 六、分阶段实施

| 阶段 | 内容 | 独立可验收 |
| --- | --- | --- |
| 1 | 新建 `agent/tools/pub_base/rg_resolver.py`（五级解析 + `--version` 探针 + 进程内缓存）与 `rg_backend.py`（content 模式的 argv 构造、`--json` 流式解析、退出码契约、Python 兜底） | 用**假 rg 脚本**（可注入延迟/坏退出码/巨量 stderr）+ 真 rg 各跑一组单测 |
| 2 | `files` 模式接入；`_arun` 不再堵事件循环；`--sort=path` 与分页语义对接 | 分页测试（同一 pattern 多次 offset 不重不漏）+ 事件循环不阻塞的计时测试 |
| 3 | `process_reap.py` + rg 看门狗 + `terminal.py` 两处 + `ast_grep` 超时语义 | 挂死子进程的真实 e2e（fork 出持有 stdout 的孙进程 → 工具必须在 grace 内返回） |
| 4 | 子代理接线：`search_files` 授予 `RESEARCHER`/`LIBRARIAN`（改角色定义 + 提示语），或 shell rg 稳定化；`LIBRARIAN` 外部检索另议 | 角色提示语与工具面一致性测试（现有 `spawn/system_prompt.py` 与注入点的同源约束要一起改） |

---

## 七、验收标准

1. **完整性**：`search_files` 在仓库根目录做无命中搜索，返回 `scan_truncated` 为 **False**（现状是 `time_budget`）——这是本次改动的核心断言。
2. **确定性分页**：同一 pattern 连续 `offset=0/50/100`，三次结果拼接后与单次 `limit=∞` 一致，且无重复。
3. **兜底**：`SHERRY_RG_PATH` 指向不存在的路径（或探针失败）时，全部用例仍绿（走 Python 实现），且只记录一次 INFO。
4. **硬错不冒充完整**：假 rg 返回退出码 2 → 结果必须来自 Python 路径（不是部分 rg 结果）。
5. **超时有界**：真 e2e——命令/子进程挂住（含孙进程持有 stdout 的场景），工具在 grace（5s+5s）内返回，日志出现 `abandoning process handle` 或等价的 WARNING，且**不丢已产出的部分结果**。
6. **perf guard 不变**：`tests/perf` 的线性增长预算仍过（rg 路径与 Python 路径各测一次）。
7. lint / type / 门禁：`ruff check`、`ruff format --check`、`basedpyright agent/`、`tests/run_tests_split.py` A/B/C 全绿。

---

## 八、风险与回滚

| 风险 | 说明 | 缓解 |
| --- | --- | --- |
| 两套实现长期并存 | rg 路径与 Python 路径都要测，容易漂移 | 用同一组语义测试参数化跑两条路径（`SHERRY_RG_PATH` 开关切换） |
| ignore 语义 | 一旦默认尊重 `.gitignore`，agent 会"看不见"某些路径 | §3.3：默认显式 `--no-ignore` + 显式排除，开关另议 |
| 正则方言 | 现状模式里有环视/反向引用 | §3.4 能力检测 → 回退 Python |
| 新 spawn 面 | 引入子进程执行 | argv 列表 + `--` + `scrub_env` + 有界回收；不进 shell；结果路径仍过 `_stays_within_root` |
| 二进制分发 | 本机 rg 来自 ZCode，不是 Sherry 自带（Windows 默认没有） | §3.1 五级解析 + Python 兜底；Windows 常态走 Python，不是故障 |
| 回滚 | —— | 改动集中在 `rg_backend.py` 与调用点开关：把开关置为"从不使用 rg"即回到现状（Python 实现不删） |

---

## 九、附：证据来源

**DeepAgents（`../deepagents`）**

| 位置 | 内容 | 分级 |
| --- | --- | --- |
| `libs/deepagents/deepagents/backends/filesystem.py:72-88` | `_resolve_ripgrep_path`：`shutil.which("rg")` + 进程内缓存 + 未找到时 INFO 一次 | 源码确认 |
| 同文件 `:929-952` | 命令构造：`--json -F`、`-m max+1`、`--glob`、目录用 `cwd` + `.`（#2732） | 源码确认 |
| 同文件 `:955-989` | `Popen` + exec 失败→`cache_clear()`；stderr 守护线程 | 源码确认 |
| 同文件 `:991-1025` | `threading.Timer` 看门狗 → `kill()`；命中上限 `terminate()`；`finally` 收尾 | 源码确认 |
| 同文件 `:1027-1045` | 超时语义（部分结果 + truncated）、退出码契约（0/1/≥2） | 源码确认 |
| 同文件 `:1115-1148` | stderr 排空（8192/500）与 `_reap_ripgrep`（5s → SIGKILL → 5s → 放弃） | 源码确认 |
| 同文件 `:52-61` | 内层预算 < 外层 deadline 的排序约定 | 源码确认 |
| `libs/deepagents/deepagents/backends/protocol.py:20` | `DEFAULT_GREP_TIMEOUT = 15` | 源码确认 |

**Sherry（本仓库）**

| 位置 | 内容 | 分级 |
| --- | --- | --- |
| `agent/tools/file_tools/search_files.py`、`search_scan.py` | Python 引擎、信封、分页、`_stays_within_root` | 源码确认 |
| `config/features/agent_side/tools_timeouts.py:59-64` | 5.0s 预算 / 10k 上限 / 剪枝表 | 源码确认 |
| `agent/tools/pub_base/file_utils.py:4-33` | 二进制嗅探与目录跳过 | 源码确认 |
| `agent/tools/terminal.py:264-267,384-387` | 超时后无界等待（两处） | 源码确认 |
| `agent/tools/code_intel/ast_grep/runner.py:169-180` | `subprocess.run(timeout=)` 的隐式无界 re-communicate + 丢弃部分结果 | 源码确认 |
| `agent/tools/code_intel/ast_grep/resolver.py:1-22` | 五级二进制解析（rg 解析器照抄来源） | 源码确认 |
| `agent/tools/subagent/types/functional_role.py:27-32` | `CODE_INTEL_ROLES = {RESEARCHER, LIBRARIAN}` | 源码确认 |
| `agent/tools/subagent/spawn/core.py:1008-1022`、`spawn/system_prompt.py:133-165` | code-intel 工具注入与指导语（含 "terminal (rg/grep) as keyword fallback"） | 源码确认 |
| `agent/tools/subagent/roles/definitions/*/AGENTS.md` | 四个角色的 `tools` 声明（都没有 `search_files`） | 源码确认 |
| 本机 `rg --version` / 计时实验 | ripgrep 14.1.1；16.6s vs 1.19s / 2.04s；5.00s 触发 `time_budget` | 实测 |
