# 计划:shadow-git 迁移到全局共享存储(`~/.sherry/checkpoints/store/`)

> 目标:把 sherry 的 shadow-git 快照从「每个项目一个独立 bare repo(`.sherry/shadow-git/`)」迁移到「一个共享 bare repo + 按项目绝对路径哈希分桶」,布局对齐 hermes-agent 的 `~/.hermes/checkpoints/store/`。
> 日期:2026-11-06 · 主战场:`agent/tools/pub_base/shadow_git.py`

---

## 一、背景

当前 shadow-git 把每个项目根的快照存进 `<root>/.sherry/shadow-git/`——一个独立 bare git repo,worktree 指向项目根,`state.json` 记录 `{boot, floors}`。这有两个问题:

1. **污染用户项目目录**:`.sherry/shadow-git/` 落在用户 checkout 里,需要靠 `info/exclude` 和 `git_head` 读取逻辑把它藏起来,且 `maybe_create_sherry_dir` 还要为它建父目录。
2. **跨项目不可见、不可治理**:一个会话切换项目目录后,旧目录的快照在新目录下 `/rollback` 看不到(hermes 已验证此缺口);也没有全局的「哪些项目有快照、各占多少」视图。

hermes 的方案是把所有项目的快照收进**一个共享 bare repo**,按 `sha256(abs_path)[:16]` 分桶:`refs/hermes/<hash>` 作分支、`indexes/<hash>` 作独立 index、`projects/<hash>.json` 记 `{workdir, created_at, last_touch}`。sherry 要采用同一布局(`refs/sherry/<hash>` 命名)。

> ⚠ 注意:sherry 现有的 `store_dir` override **不是**目标布局——它只是把 N 个独立 repo 挪到 `override/<hash>/` 下,仍是每项目一个 repo。迁移要做的是结构合并:从「N repo × 单 ref」变成「1 repo × N ref」。

---

## 二、现状速览(改动基线)

| 维度 | 现状 | 文件:行 |
|---|---|---|
| 存储根解析 | `shadow_store(project_root)` → `<root>/.sherry/shadow-git`,或 override 下 `<override>/<sha256[:16]>/`(仍是 N 个独立 repo) | `agent/tools/pub_base/shadow_git.py:85-97` |
| **硬编码读路径(忽略 override)** | `pub/git_head.py:23` 直接拼 `root / ".sherry" / "shadow-git"` | `pub/git_head.py:23` ← **必须随迁移一起修** |
| repo 初始化 | `_ensure_store`:`git init --bare`,`symbolic-ref HEAD refs/heads/main`,写 `info/exclude` | `shadow_git.py:154-182` |
| 共享 ref | `SHADOW_REF = "refs/heads/main"`(单一链) | `pub/git_head.py:17` |
| 每项目元数据 | `state.json`(在 store 目录内):`{boot, floors:{sid:{turn,tip}}}` | `shadow_git.py:218-219, 251-271` |
| 快照入口 | `ensure_checkpoint(session_id, reason, project_root)` → `take_snapshot` | `shadow_git.py:596-621, 464-491` |
| 触发点 | write_file / patch_file / python_repl / terminal 写前 preflight | `agent/tools/file_tools/{write_file.py:188, patch_file.py:157}`、`python_repl.py:286,321`、`terminal/core.py:652,843` |
| 恢复入口 | `restore_to_turn(session_id, before_turn)` ← 编辑消息流程调用 | `shadow_git.py:758-801`,调用方 `server/trigger/ws/messages.py:541` |
| 恢复实现 | pre_restore 快照 → `git checkout <floor> -- .` → 删新建文件 → post_restore 快照 | `shadow_git.py:804-830` |
| floor 概念 | 每轮首次快照记录"该轮开始前的链 tip",boot 范围(restart 丢弃) | `shadow_git.py:274-285, 222-248` |
| 修剪 | `max_snapshots=100` 软上限 + `git replace --graft` 重根,不跨越活跃 floor | `shadow_git.py:377-423` |
| 配置 | 代码级 `config/features/agent_side/shadow_git.py`:`{enabled, max_snapshots, git_timeout_s, store_dir}`,**未进 sherry.jsonc** | 同上 |
| 测试 | `tests/agent/tools/pub_base/test_shadow_git.py`(358 行)等 | 见第八节 |

---

## 三、目标布局

```
~/.sherry/checkpoints/store/                  # 单一共享 bare git repo(惰性 init)
├── HEAD                                       # symbolic-ref → refs/sherry/_default(占位,不承载实际链)
├── info/exclude                               # 全局排除(.git/、.sherry/)
├── refs/sherry/<hash16>                       # 每项目一条分支 ref(替代当前 refs/heads/main)
├── indexes/<hash16>                           # 每项目独立 git index(GIT_INDEX_FILE 指向)
└── projects/<hash16>.json                     # 每项目元数据(见下)
```

- `<hash16> = sha256(normalized_abs_path(project_root))[:16]`,与 hermes 一致、与现有 override 的分桶哈希一致。
- `projects/<hash16>.json` schema(扩展 hermes 的 `{workdir, created_at, last_touch}`,**额外承载 sherry 的 floors/boot**,因为 sherry 的 restore_to_turn 依赖 floor 语义,hermes 无此概念):
  ```jsonc
  {
    "workdir": "<abs path>",          // 项目绝对路径(human-readable + 反查)
    "created_at": "<iso8601>",        // 首次快照时间
    "last_touch": "<iso8601>",        // 最近一次快照/恢复时间(供未来保留期清理)
    "boot": "<uuid hex>",             // sherry 进程 boot id(floor 跨进程失效判定)
    "floors": {                       // 每会话 floor(替代当前 state.json 的 floors)
      "<session_id>": {"turn": <int>, "tip": "<sha>"}
    }
  }
  ```

- `.sherry/` 目录不再承载 shadow-git。`maybe_create_sherry_dir` 仍会建 `.sherry/`(给 codeintel 等用),但不再建 `shadow-git` 子目录。

---

## 四、关键设计决策

### D1. 单共享 bare repo vs 保持 N 个独立 repo
**选共享 repo。** 对齐 hermes;省 inode、便于全局治理(列出所有项目、统计占用);一个进程只 init 一次。代价:所有 `run_shadow` 调用需带 `GIT_INDEX_FILE` 和 ref 名,不再依赖 repo 默认 HEAD/index。

### D2. per-project ref:`refs/sherry/<hash>` 替代 `refs/heads/main`
当前 `SHADOW_REF` 是单一全局链;迁移后每个项目一条 ref。`shadow_head(root)` 读 `refs/sherry/<hash>` 而非 `refs/heads/main`。隔离 worktree 的 `baseline_revision` 同样改读该 ref。

### D3. per-project index:`indexes/<hash>` + `GIT_INDEX_FILE`
一个共享 repo 只有一个默认 index,无法同时承载多项目 worktree 的暂存区。**改动点在 `shadow_env()`(`shadow_git.py:185-196`),它被 `run_shadow` 和 `_run_shadow_readonly` 共用,也被隔离后端的 `gitrun.run_git`(`agent/tools/subagent/isolation/gitrun.py:42`)import 使用——改这一处即可覆盖所有 git 调用。** `GIT_INDEX_FILE` 从当前的 `store / "index"`(line 191)改为 `store / "indexes" / shadow_hash(root)`。`GIT_DIR` 仍指向共享 store 根,`GIT_WORK_TREE` 仍指向项目根。

### D4. `projects/<hash>.json` 承载 floors + boot
hermes 的 projects json 只有 `{workdir, created_at, last_touch}`(给修剪/可见性用)。sherry 需要保留 floor 语义(消息编辑恢复依赖),所以扩展 schema 加 `boot` + `floors`。`state.json` 文件取消,其内容并入 projects json。**保留 boot 丢弃逻辑**:加载时 `boot != _BOOT_ID` → floors 视为 stale(行为同现状)。

### D5. floor 语义不变
`_record_floor` / `floor_for` / `_active_floor_tips` 行为保持;只是存储位置从 `state.json` 移到 `projects/<hash>.json`。

### D6. 修复 `pub/git_head.py:23` 硬编码路径 + **存在性检查语义变更**(顺带修,但语义必须改)
该行直接拼 `root / ".sherry" / "shadow-git"`,**忽略 `store_dir`/`SHERRY_SHADOW_GIT_DIR` override**——这是迁移前就存在的 bug(override 模式下该读路径找不到 store)。迁移后该函数改为调用 `shadow_git` helper(或新的 `shadow_ref/shadow_index` helper),不再硬编码路径。

> ⚠ **存在性检查语义必须改,不只是路径。** 当前 `shadow_head`(pub/git_head.py:23-24)和 `_run_shadow_readonly`(shadow_git.py:537)都以 `store / "HEAD"` 是否存在作为「该项目是否有快照」的判据。共享 repo 下 `store/HEAD` 一旦被任意项目初始化就**永远存在**,于是「该项目有快照」会被误判为 True:
> - `code_intel/indexer.py`、`code_intel/query.py` 读 `shadow_head` 决定是否走快照 fast path → 会对无快照项目空跑 git,失败 fail-open 但有噪声。
> - `git_graph_service.py:158,229` 用 `shadow_head(root) is None` 决定是否显示 shadow 历史 → 同上。
> - **`worktree.py:65,72` `ensure_repository`/`baseline_revision` 用 `shadow_head is None` 决定是否补拍 baseline** → 误判为非 None 就**跳过 baseline**,然后 line 74 `rev-parse SHADOW_REF` 失败 → `baseline_revision` 抛 `OSError` → **subagent 隔离直接断**。
>
> 修法:存在性判据从「`store/HEAD` 存在」改为「**该项目的 ref 存在**」:`git show-ref --verify refs/sherry/<hash>`(或 `git rev-parse --verify refs/sherry/<hash>^{commit}`)。`shadow_head` 和 `_run_shadow_readonly` 都改用这个判据。

> 循环依赖处理:`pub/git_head.py` 是 dependency-free seam,不能 import `shadow_git`。方案:把纯字符串 helper(`shadow_hash`/`shadow_ref` 名)留在 `pub/git_head.py`,`shadow_git.py` 反向 import 它(方向与现状一致)。

### D6b. `worktree.py:74` 直接硬编码 `SHADOW_REF`(必须改,否则隔离断)
`agent/tools/subagent/isolation/worktree.py:74`:`head = run_git(project_root, ["rev-parse", shadow_git.SHADOW_REF])`。`SHADOW_REF` 当前是 `refs/heads/main`,共享 repo 下该 ref **不存在**(refs 是 `refs/sherry/<hash>`),`rev-parse` 失败 → `baseline_revision` 抛 `OSError("shadow git has no baseline")` → `create_worktree` 中断 → subagent 派生失败。这是全仓**唯一**在 `pub/` 之外直接引用 `SHADOW_REF` 的地方(已 grep 确认),改为 `["rev-parse", shadow_git.shadow_ref(project_root)]`。`run_git` 已通过 `shadow_env` 带 `GIT_DIR`/`GIT_INDEX_FILE`(gitrun.py:42),无需另改 env。

### D7. `shadow_store()` API 收敛
`shadow_store(project_root)` 仍返回 store 根目录(共享 repo 路径),但语义从「该项目的 repo」变为「全局共享 repo」。新增 helper:
- `shadow_hash(project_root) -> str`:`sha256(normalized_abs_path)[:16]`
- `shadow_ref(project_root) -> str`:`refs/sherry/<hash>`
- `shadow_index(project_root) -> Path`:`<store>/indexes/<hash>`
- `shadow_meta(project_root) -> Path`:`<store>/projects/<hash>.json`
所有需要"该项目在共享 repo 内的位置"的调用点改用这些 helper。

### D8. 默认路径与 override
- 默认 store 根:**`APP_HOME / "checkpoints" / "store"`**(`from config.path import APP_HOME`;`config/path.py:25` `APP_HOME = resolve_app_home(ROOT_DIR)`)。注意代码符号是 `APP_HOME`,不是 `SHERRY_HOME`——后者是 env 变量/`--sherry-home` flag,经 `resolve_app_home` 解析后落到 `APP_HOME`。打包桌面 app 指向 OS app-data dir,checkout 环境下 `APP_HOME == ROOT_DIR`。
- `store_dir` 配置项语义改为「**共享 store 根**」(不再是「每项目 repo 的父目录」)。env `SHERRY_SHADOW_GIT_DIR` 同义。
- `shadow_store()` 不再按项目 sharding,直接返回配置的 store 根(空则 `APP_HOME/"checkpoints"/"store"`)。

### D9. 迁移已有 per-project stores(惰性、可选)
启动时检测旧 `<root>/.sherry/shadow-git/` 是否存在:存在则一次性把其 `refs/heads/main` 链 + objects 导入共享 repo 的 `refs/sherry/<hash>`,迁移 `state.json` → `projects/<hash>.json`,然后把旧目录改名为 `.sherry/shadow-git.migrated/`(保留备份,不直接删)。**惰性触发**:仅在 `ensure_checkpoint` 首次遇到某项目根时检查,避免启动扫描全盘。失败 fail-open(日志告警,旧快照视为不可用,不影响新写入)。

> 迁移细节(执行时落实):
> 1. **ref/objects 导入**:用 `git fetch <legacy_store> refs/heads/main:refs/sherry/<hash>`(从 bare repo 路径 fetch)。`git replace --graft` 的 replace refs **不迁移**——它们是修剪产物,只影响 `rev-list`/`log` 遍历显示,真实 commit 的 parent 指针未改,所以 fetch `refs/heads/main` 拿到的是**完整未修剪的真实链**(无数据丢失),新 repo 的 `_prune_snapshots` 会在下次快照时重建 graft。
> 2. **`created_at` / `last_touch` 派生**:旧 `state.json` 没有这两个字段。迁移时 `created_at` = 旧链最老 commit 的 author date(`git log --reverse --format=%aI refs/sherry/<hash>` 取首行),`last_touch` = 旧链最新 commit 的 author date;链为空则两者都用 `datetime.now().isoformat()`。
> 3. **floors 拷贝**:把旧 `state.json` 的 `{boot, floors}` 原样写进 `projects/<hash>.json`。**注意**:旧 `boot` 是上个进程的 boot id,新进程加载时 `boot != _BOOT_ID` 会把 floors 当 stale 丢弃——这是**现有 restart 行为**,不是回归。floors 拷贝是为完整性,实际新进程会重新记录 floor。
> 4. **幂等**:重试时 `git fetch` 同 ref 会 fast-forward/覆盖(已存在则 `+refs/sherry/<hash>:refs/sherry/<hash>` 强制更新);旧目录已改名 `.migrated` 则跳过迁移走新路径。

### D10. 配置暴露
保持代码级 `config/features/agent_side/shadow_git.py` 为主;`store_dir` 默认值改为 `""`(空)表示用 `SHERRY_HOME/checkpoints/store`。**不**强制进 `sherry.jsonc`(避免引入需维护的配置面),但保留 env override 用于 eval harness 临时隔离(eval suite 已用 `SHADOW_GIT["store_dir"]` 做按实例隔离,迁移后仍可用)。

---

## 五、改动清单(文件级)

### 核心
- **`agent/tools/pub_base/shadow_git.py`**(主战场)
  - `shadow_store()` → 返回共享 store 根;新增 `shadow_hash/shadow_ref/shadow_index/shadow_meta` helper(D7)。
  - `_ensure_store()` → 改为幂等 init 共享 repo(仅一次 `git init --bare`);`info/exclude` 保留 `.git/` + `SUBAGENT_ISOLATION["deny_patterns"]`,**只删掉 `.sherry/shadow-git/` 那一行**(该路径已不存在;`.sherry/` 本身仍留在项目里供 codeintel 用,codeintel 仍按现状被快照,行为不变)。
  - `shadow_env()`(line 185-196)→ `GIT_INDEX_FILE` 从 `store/"index"` 改为 `store/"indexes"/shadow_hash(root)`(D3);`GIT_DIR` 仍指向共享 store。
  - `_run_shadow_readonly`(line 525-550)→ 存在性判据从 `store/HEAD` 存在改为 `shadow_ref(root)` 存在(`show-ref --verify`,D6);`shadow_env` 已自动带新 index 路径,无需另改。
  - `run_shadow(root, args)` → 所有 ref 操作从 `SHADOW_REF` 改为 `shadow_ref(root)`;涉及:`_take_snapshot_locked`(rev-parse/update-ref)、`_prune_snapshots`(rev-list)、`_older_of`(merge-base/rev-list)、`list_checkpoints`(log)、`diff_changed_paths`(diff)。
  - `_state_path/_load_floors/_save_floors` → 改为读写 `projects/<hash>.json`(合并 workdir/created_at/last_touch 字段)。
  - `_restore_to_commit_locked` / `_delete_new_files_since` → ref/index 全部走 helper;`git replace --graft` 的 boundary 计算改为针对 `shadow_ref(root)`。
  - 新增 `_migrate_legacy_store(root)`(D9 惰性迁移)。
- **`pub/git_head.py`**(D6 修 bug + 语义改)
  - `:17` `SHADOW_REF` 常量:保留为兼容别名(**仅供 `__all__` re-export 不破坏 import**,实际不再用于 git 操作),或删除并改 `shadow_git.py` 的 re-export。新增 `shadow_hash(root)` / `shadow_ref(root)` 纯字符串 helper(无 git 调用)。
  - `:23-24` `shadow_head`:硬编码路径改为调 `shadow_store` helper;**存在性判据从 `store/HEAD` 改为 `show-ref --verify refs/sherry/<hash>`**(D6)。
- **`agent/tools/subagent/isolation/worktree.py`**(D6b,**必须改**)
  - `:74` `["rev-parse", shadow_git.SHADOW_REF]` → `["rev-parse", shadow_git.shadow_ref(project_root)]`。
  - `:65,72` `shadow_head` 调用:语义随 D6 自动正确(返回 None 当该项目无 ref),无需改调用方。
- **`agent/tools/code_intel/indexer.py`、`query.py`**(`shadow_head` 读方,确认无改动)
  - 调 `shadow_head(root)`:语义随 D6 自动正确;只需回归测试 `test_shadow_fast_path.py` 覆盖共享 repo 下「该项目无快照返回 None」。
- **`server/service/git_graph_service.py`**(`shadow_head`/`run_shadow` 读方,确认无改动)
  - `:87` 走 `run_shadow`(自动带新 env);`:158,229` 走 `shadow_head`(语义随 D6)。无代码改动,回归测试覆盖。
- **`runtime/session/project_dir.py`**
  - `maybe_create_sherry_dir`(line 276-298):**现状本就只建 `.sherry/` 外壳,不建 `shadow-git` 子目录**(`shadow-git` 由 `_ensure_store` 懒建)。迁移后 `_ensure_store` 不再在项目里建子目录,此函数**无需改动**,仅回归 `test_workspace_notice.py`。

### 不需改(验证后确认)
- `server/trigger/ws/messages.py:541` 调 `restore_to_turn` 的签名不变。
- `agent/tools/file_tools/{write_file,patch_file}`(`write_file.py:188`、`patch_file.py:157`)、`python_repl.py:286,321`、`terminal/core.py:652,843` 调 `ensure_checkpoint` 的签名不变。
- `agent/tools/subagent/isolation/gitrun.py`:`run_git` 已通过 `shadow_env`(line 42)带 env,`shadow_env` 改了它自动跟上,无需改。

### 配置
- `config/features/agent_side/shadow_git.py`:`store_dir` 默认值与文档注释更新(语义从"每项目 repo 父目录"改为"共享 store 根");新增 `migrate_legacy: bool = True`(可关)。

---

## 六、迁移策略(D9 细化)

1. 首次 `ensure_checkpoint(root)` 时,先检查 `shadow_meta(root)` 是否存在 → 存在则走新路径。
2. 不存在则检测 `<root>/.sherry/shadow-git/HEAD`:
   - 存在 → `git fetch <legacy> refs/heads/main:refs/sherry/<hash>` 把旧链导入共享 repo;读旧 `state.json` 写入 `projects/<hash>.json`;旧目录改名 `.sherry/shadow-git.migrated/`。
   - 不存在 → 正常 init + 首次快照。
3. 迁移包在 try/except 内 fail-open;失败则该项目按"全新"处理,旧快照丢弃(日志 WARN)。
4. eval harness 的 `SHADOW_GIT["store_dir"]` 临时隔离仍生效(指向临时目录的共享 store)。

---

## 七、并发与隔离影响

- `_writer(root)` / `_active_writers` / `_frozen` 仍按 root 维度,不变(不同项目写不同 ref/index,互不阻塞;同项目仍串行)。
- `SUBAGENT_ISOLATION` worktree 的 `take_snapshot(project_root, "manual:isolation baseline")`(`agent/tools/subagent/isolation/worktree.py:66,73`)改为用 `shadow_ref`/`shadow_index`,语义不变。
- `evals/swebench/teardown.py` 的 `freeze` 行为不变。

---

## 八、测试改动

- `tests/agent/tools/pub_base/test_shadow_git.py`(主):
  - `test_snapshot_creates_the_store_not_a_user_repository` → 改为断言共享 store 路径、无 `<root>/.sherry/shadow-git`。
  - `test_store_dir_override_moves_the_store_out_of_the_project` → 改为断言所有项目共享一个 store、按 ref 分桶。
  - 新增:多项目共享 repo 的 ref/index 隔离测试(项目 A 写不影响项目 B 的 head/index)。
  - 新增:`projects/<hash>.json` 读写 + boot 丢弃 floor 测试。
  - 新增:惰性迁移测试(旧 `.sherry/shadow-git` → 共享 repo,旧目录改名)。
  - `TestTurnFloors` / `TestRestoreToTurn` 调整断言路径,行为预期不变。
- `tests/agent/tools/code_intel/test_shadow_fast_path.py`:确认 fast path 读 `shadow_head` 兼容。
- `tests/server/trigger/ws/test_messages_edit.py`:编辑恢复 E2E 行为不变。
- `tests/agent/middlewares/test_workspace_notice.py`:`maybe_create_sherry_dir` 断言不再建 `shadow-git` 子目录。
- 新增 `tests/agent/tools/pub_base/test_shadow_git_migration.py`:专门覆盖 D9 迁移路径。

---

## 九、风险与回滚

| 风险 | 缓解 |
|---|---|
| 共享 repo 损坏波及所有项目 | 每项目 ref 独立,单 ref 损坏不影响其他;objects 损坏时可 `git reflog`/`fsck` 修复;fail-open 不阻断写入 |
| `pub/git_head.py` 循环依赖 | 把纯字符串 helper(hash/ref 名)留在 `pub/git_head.py`,`shadow_git.py` import 它(方向与现状一致) |
| 迁移中途中断 | 惰性 + 旧目录改名备份(不删);下次启动重试;迁移幂等(fetch 同 ref 覆盖) |
| eval 隔离被打破 | 保留 `store_dir` override,eval suite 继续按实例指向临时共享 store |
| 性能:共享 repo objects 膨胀 | `git gc` 由 `_prune_snapshots` 顺带触发(现有 `git replace --graft` 后可加 `git gc --auto` 阈值检查) |
| 回滚 | 改动集中在 `shadow_git.py` + `git_head.py`;回滚即恢复这两个文件 + 删 `~/.sherry/checkpoints/store`(旧 `.sherry/shadow-git.migrated` 可手动恢复) |

---

## 十、验收标准

1. 新装/新项目:首次写入后,`~/.sherry/checkpoints/store/` 存在;`<root>/.sherry/` 下**无** `shadow-git` 子目录;`projects/<hash>.json` 含正确 workdir/floors。
2. 多项目:项目 A、B 的 `refs/sherry/<hash>` 互不串读;各自的 `shadow_head` 只返回自己链的 tip。
3. 编辑消息恢复:`restore_to_turn` 行为与现状一致(文件回退 + 删新建文件 + post_restore floor),测试全绿。
4. 惰性迁移:带旧 `.sherry/shadow-git/` 的项目首次写入后,旧链出现在共享 repo 的 `refs/sherry/<hash>`,旧目录改名 `.migrated`。
5. `git_head` 读路径在 override 模式下能正确找到 store(D6 bug 修复)。
6. 全套 shadow-git 测试 + 编辑消息测试 + workspace_notice 测试通过;`basedpyright agent/tools/pub_base/shadow_git.py` 无新错。

---

## 十一、不在本次范围

- 引入「编辑时记录目录 + 自动回切恢复」(解决跨目录切换后旧快照不可见的 hermes 式缺口)——这是另一个独立 TODO;本次只搬存储布局。
- 时间维度保留期清理(hermes 的 `retention_days` + `prune_checkpoints` 删孤儿)——本次只把 `last_touch` 字段记下来,清理逻辑后续做。
- 把 shadow-git 配置进 `sherry.jsonc` UI 面。

---

## 十二、审核修订与可执行细化(2026-11-06 审核追加)

> 本节是对一~十一节的代码级核对结果。已逐文件逐行验证(2026-11-06 当前代码),修正了原文 5 处错误/遗漏,并把每个改动落到 before→after 规格。

### 12.1 审核结论:原文需修正的 5 处

| # | 原文问题 | 修正 |
|---|---|---|
| 1 | D8 写 `SHERRY_HOME / "checkpoints" / "store"` | 代码符号是 **`APP_HOME`**(`config/path.py:25`),`SHERRY_HOME` 是 env/flag,经 `resolve_app_home` 解析为 `APP_HOME`。import 应写 `from config.path import APP_HOME` |
| 2 | D6 只提「路径硬编码」,漏了**存在性检查语义** | `shadow_head`/`_run_shadow_readonly` 以 `store/HEAD` 存在判「该项目有快照」;共享 repo 下 `store/HEAD` 恒存在 → 误判。判据必须改为 `show-ref --verify refs/sherry/<hash>` |
| 3 | 完全没提 `worktree.py:74` | 该行 `rev-parse shadow_git.SHADOW_REF`(`refs/heads/main`)在共享 repo 下 ref 不存在 → `baseline_revision` 抛 `OSError` → **subagent 隔离断**。全仓唯一在 `pub/` 外直接用 `SHADOW_REF` 的地方(已 grep 确认),必须改为 `shadow_ref(project_root)` |
| 4 | D3 没指明改动落点 | 实际落点是 `shadow_env()`(line 185-196),被 `run_shadow`、`_run_shadow_readonly`、`gitrun.run_git`(line 42 import)三方共用,改这一处全覆盖 |
| 5 | 第五节「不需改」把 worktree/code_intel/git_graph 归为无影响 | worktree 需改(line 74);code_intel/git_graph 是 `shadow_head` 读方,语义随 D6 变,**虽不改代码但必须回归测试**(fast path、graph 空项目场景) |

> 另:`maybe_create_sherry_dir` 原计划说「不再建 shadow-git 子目录」——实际现状本就只建 `.sherry/` 外壳(`project_dir.py:290-294` 只 `sherry.mkdir`),`shadow-git` 子目录一直是 `_ensure_store` 懒建。此函数**无需改动**,已从改动清单移除。

### 12.2 改动规格(before → after,精确到行)

> 行号基于 2026-11-06 当前代码。`shadow_git.py` 共 871 行;`git_head.py` 36 行。

#### (1) `pub/git_head.py` — 新增 helper + 改 shadow_head

**新增**(在 `SHADOW_REF` 之后):
```python
SHADOW_REF = "refs/heads/main"  # 保留:兼容 __all__ re-export,实际操作改用 shadow_ref()

import hashlib

def shadow_hash(project_root: Path) -> str:
    """sha16 of the normalized absolute project path (hermes-aligned)."""
    return hashlib.sha256(str(project_root.resolve()).encode("utf-8")).hexdigest()[:16]

def shadow_ref(project_root: Path) -> str:
    """The per-project branch ref inside the shared store."""
    return f"refs/sherry/{shadow_hash(project_root)}"
```
> 循环依赖:`pub/git_head.py` 已 import `config.features.SHADOW_GIT`;新增 `hashlib`/`Path` 不引入新 agent 侧依赖。`shadow_git.py` 已 `from pub.git_head import SHADOW_REF, shadow_head`,追加 import `shadow_hash, shadow_ref`。

**改 `shadow_head`**(line 20-35):判据从 `store/HEAD` 存在改为 ref 存在。
```python
# before
store = root / ".sherry" / "shadow-git"
if not (store / "HEAD").exists():
    return None
result = subprocess.run(["git", "--git-dir", str(store), "rev-parse", "--short", SHADOW_REF], ...)

# after
from config.path import APP_HOME  # 或复用 shadow_git.shadow_store;为保持 seam 无 agent 依赖,这里直接算
store = _shadow_store_root()  # = APP_HOME/"checkpoints"/"store"(或 SHADOW_GIT["store_dir"]/env)
ref = shadow_ref(root)
if not (store / "HEAD").exists():
    return None  # 共享 repo 还没被任何项目 init
result = subprocess.run(
    ["git", "--git-dir", str(store), "show-ref", "--verify", "--hash", ref],
    capture_output=True, text=True, timeout=SHADOW_GIT["git_timeout_s"],
)
# show-ref --verify 成功才返回该 ref 的 sha;失败=该项目无快照
```
> 注意:为保持 `pub/git_head.py` 不 import `shadow_git`,共享 store 根的解析逻辑(`APP_HOME/"checkpoints"/"store"` 或 override)需要在 `git_head.py` 内复制一份小函数 `_shadow_store_root()`,或把该函数也留在 `git_head.py` 让 `shadow_git.py` 反向 import(方向与现状 `shadow_git import pub.git_head` 一致)。**推荐后者**:把 `shadow_store_root()` 也定义在 `pub/git_head.py`,`shadow_git.shadow_store()` 改为 thin wrapper 调它。

#### (2) `shadow_git.py` — shadow_env / shadow_store / _run_shadow_readonly / ref 操作

**`shadow_env`**(line 185-196):`GIT_INDEX_FILE` 改 per-project。
```python
# before
"GIT_INDEX_FILE": str(store / "index"),
# after
"GIT_INDEX_FILE": str(shadow_store(root) / "indexes" / shadow_hash(root)),
```

**`shadow_store`**(line 85-97):返回共享根,不再 per-project sharding。
```python
# before
override = ... 
if override: return Path(override)/.../<digest>
return root / ".sherry" / "shadow-git"
# after
override = str(SHADOW_GIT.get("store_dir") or os.getenv("SHERRY_SHADOW_GIT_DIR","")).strip()
if override: return Path(override).expanduser().resolve()
from config.path import APP_HOME
return APP_HOME / "checkpoints" / "store"
```
> 新增 helper `shadow_index(root)` = `shadow_store(root)/"indexes"/shadow_hash(root)`;`shadow_meta(root)` = `shadow_store(root)/"projects"/f"{shadow_hash(root)}.json"`。`shadow_ref`/`shadow_hash` 从 `pub.git_head` import。

**`_ensure_store`**(line 154-182):幂等 init 共享 repo;`info/exclude` 删 `.sherry/shadow-git/` 行,保留 `.git/` + `deny_patterns`;首次 init 建 `indexes/`、`projects/` 子目录。注意当前实现每次调用都重写 `info/exclude`(line 168-181),共享 repo 下多项目并发可能竞争写同一文件——改为**仅在 init 时写一次**(用 `(store/HEAD).exists()` 守卫,已 init 则跳过 exclude 重写)。

**`_run_shadow_readonly`**(line 525-550):判据从 `store/HEAD` 改为 `show-ref --verify shadow_ref(root)`。
```python
# before
if not (store / "HEAD").exists(): return None
# after
if not (store / "HEAD").exists(): return None  # 共享 repo 未 init
verify = subprocess.run(["git","--git-dir",str(store),"show-ref","--verify","--quiet",shadow_ref(root)], ...)
if verify.returncode != 0: return None  # 该项目无快照
```

**所有 `SHADOW_REF` 引用**(line 394, 404, 439, 444, 472, 487, 515, 635, 643 区等):全局替换为 `shadow_ref(root)`。具体:`_prune_snapshots`(394,404)、`_older_of`(439,444)、`_take_snapshot_locked`(472,487)、`diff_changed_paths`(515)、`list_checkpoints`(635)。`restore_checkpoint`/`_restore_to_commit_locked` 里的 `checkout <target>` 不涉及 SHADOW_REF(target 是传入 sha),但其中的 `_take_snapshot_locked` 调用会走 update-ref,已覆盖。

**`_state_path`/`_load_floors`/`_save_floors`**(line 218-271):`_state_path` 返回 `shadow_meta(root)` 而非 `shadow_store(root)/"state.json"`;`_load_floors`/`_save_floors` 读写 `projects/<hash>.json`,合并 `workdir`/`created_at`/`last_touch` 字段(读写时保留这些字段,floors/boot 仍按现有 boot 丢弃逻辑)。

#### (3) `worktree.py:74`
```python
# before
head = run_git(project_root, ["rev-parse", shadow_git.SHADOW_REF])
# after
head = run_git(project_root, ["rev-parse", shadow_git.shadow_ref(project_root)])
```

#### (4) `config/features/agent_side/shadow_git.py`
`store_dir` 注释更新(语义从「每项目 repo 父目录」改为「共享 store 根」);新增 `migrate_legacy: bool = True`。默认值 `store_dir=""` 含义变为「用 `APP_HOME/checkpoints/store`」。

### 12.3 实施顺序(建议分 4 个 commit,每步可独立验证)

**Phase 1 — helper 与 store 根(无行为变更,可先落)**
- 在 `pub/git_head.py` 加 `shadow_hash`/`shadow_ref`/`shadow_store_root`;`shadow_git.py` 改 import。`shadow_store()` 仍返回旧路径(暂不切默认),仅引入 helper。
- 验证:`basedpyright pub/git_head.py agent/tools/pub_base/shadow_git.py` 无新错;现有测试全绿(行为未变)。

**Phase 2 — 共享 repo + per-project ref/index(核心结构切换)**
- `shadow_store()` 切默认到 `APP_HOME/checkpoints/store`;`shadow_env` 的 `GIT_INDEX_FILE` 改 per-project;`_ensure_store` 改幂等 init 共享 repo + 子目录;`shadow_head`/`_run_shadow_readonly` 判据改 `show-ref --verify`;所有 `SHADOW_REF` → `shadow_ref(root)`;`worktree.py:74` 改;`_state_path`/floors 改 `projects/<hash>.json`。
- **暂不做迁移(D9)**:此 phase 后,旧 `.sherry/shadow-git/` 的项目会被当「无快照」处理(新 repo 里没有它们的 ref),旧快照暂时不可见——这是预期的中间态,新写入正常。
- 验证:`test_shadow_git.py` 改后全绿(见 12.4);**新增多项目隔离测试**;`test_shadow_fast_path.py`、`test_workspace_isolation.py`、`test_messages_edit.py` 全绿;手动跑一次 subagent 派生确认 `baseline_revision` 不抛。

**Phase 3 — 惰性迁移(D9)**
- 加 `_migrate_legacy_store(root)`,在 `ensure_checkpoint` 首次遇到某 root 且 `shadow_meta(root)` 不存在时触发。
- 验证:新增 `test_shadow_git_migration.py`;用一个带旧 `.sherry/shadow-git/` 的项目跑首次写入,确认旧链出现在 `refs/sherry/<hash>`、旧目录改名 `.migrated`、floors 可读(虽会因 boot 丢弃)。

**Phase 4 — 配置与回归收尾**
- `shadow_git.py` config 注释/`migrate_legacy` 字段;`info/exclude` 调整;eval suite 的 `store_dir` 临时隔离确认仍生效(指向临时目录的共享 store)。
- 验证:`uv run --no-sync python tests/run_tests_split.py` 全绿;`basedpyright agent/` 无新错;`ruff check .` 干净。

### 12.4 测试改动清单(test_shadow_git.py,基于当前 358 行版本)

| 现有测试 | 改动 |
|---|---|
| `test_snapshot_creates_the_store_not_a_user_repository` | 断言从 `(project/".sherry"/"shadow-git"/"HEAD").is_file()` 改为断言共享 store 存在 + `shadow_ref(project)` 存在 + 项目下**无** `.sherry/shadow-git` |
| `test_secrets_and_the_store_are_excluded` | `info/exclude` 断言去掉 `.sherry/shadow-git/` 行 |
| `test_store_dir_override_moves_the_store_out_of_the_project` | 改为断言**多项目共享一个 store**、按 `refs/sherry/<hash>` 分桶(而非每项目一个 `<digest>/` 子 repo) |
| `TestSnapshotCap` / `TestTurnFloors` / `TestRestoreToTurn` | 断言路径调整(改读 `projects/<hash>.json`、`shadow_ref`),行为预期不变 |
| `test_freeze_blocks_new_snapshots_and_wait_for_idle_tracks_writers` | 不变(按 root 维度,frozen set 语义不变) |

**新增测试**:
- `test_shared_store_isolates_refs_and_indexes_per_project`:项目 A 写后,项目 B 的 `shadow_head` 仍为 None、`shadow_ref(B)` 不存在、B 的 index 文件独立(A 的 `git add` 不污染 B 的 `ls-files`)。
- `test_shadow_head_returns_none_for_project_without_snapshots_when_store_exists`:共享 repo 已被 A init,B(无快照)`shadow_head`/`_run_shadow_readonly` 返回 None(覆盖 D6 语义)。
- `test_projects_json_roundtrip_preserves_workdir_and_floors`:`projects/<hash>.json` 读写,boot 不匹配时 floors 丢弃、`workdir`/`created_at`/`last_touch` 保留。
- `test_migrate_legacy_per_project_store`:造一个旧 `.sherry/shadow-git/` 带链,首次 `ensure_checkpoint` 后旧链出现在 `refs/sherry/<hash>`、旧目录改名 `.migrated`、`projects/<hash>.json` 的 `created_at` = 旧链最老 commit date。
- `test_migrate_legacy_is_idempotent_and_fail_open`:重复触发不重复迁移;迁移中途失败 → 该项目按全新处理,不阻断写入。

### 12.5 验收清单(实施完成判定)

- [ ] 新项目首次写入:`APP_HOME/checkpoints/store/` 存在;`<root>/.sherry/` 下无 `shadow-git`;`projects/<hash>.json` 含 workdir/floors。
- [ ] 多项目:A、B 的 `refs/sherry/<hash>` 互不串读;`shadow_head(A)` ≠ None 时 `shadow_head(B)` 仍可 None。
- [ ] `restore_to_turn` 行为与现状一致(文件回退 + 删新建 + post_restore floor),`test_messages_edit.py` 全绿。
- [ ] 惰性迁移:带旧 store 的项目首次写入后旧链进 `refs/sherry/<hash>`、旧目录改名 `.migrated`。
- [ ] `pub/git_head.py:23` override 模式下能找到 store(D6 bug 修复)。
- [ ] **subagent 派生不报「shadow git has no baseline」**(D6b 回归)。
- [ ] code_intel fast path 在「该项目无快照但共享 store 存在」时返回 None 而非空跑(D6 回归)。
- [ ] `uv run --no-sync python tests/run_tests_split.py` 全绿;`basedpyright agent/tools/pub_base/shadow_git.py pub/git_head.py` 无新错;`ruff check .` 干净。
- [ ] eval suite 的 `SHADOW_GIT["store_dir"]` 临时隔离仍生效(`evals/swebench/suite.py`、`evals/coding_capability/suite.py` 跑通)。

### 12.6 风险补充(在第九节基础上)

| 风险 | 补充 |
|---|---|
| `_ensure_store` 每次重写 `info/exclude` | 共享 repo 下多项目并发会竞争写同一 exclude 文件。改为仅 init 时写一次(已 init 则跳过) |
| `git replace` refs 在共享 repo 全局共存 | 不同项目 boundary sha 不同(内容寻址),不冲突;但 `refs/replace/` 会累积,未来保留期清理时一并处理(属第十一节后续) |
| 共享 repo objects 膨胀 | 多项目 blob 去重是优势;`_prune_snapshots` 的 graft 只重根不删 object,需周期性 `git gc --auto`(可加在 graft 之后,阈值触发) |
| `shadow_store_root()` 在 `pub/git_head.py` 复制解析逻辑 | 与 `shadow_git.shadow_store` 保持单一真相:让 `git_head.py` 定义 root 解析,`shadow_git` import 它(避免两份逻辑漂移) |
