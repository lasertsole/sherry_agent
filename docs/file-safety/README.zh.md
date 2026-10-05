# 🛡️ 文件写入安全

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> 并发写者——主 Agent、最多 8 个子 Agent、以及同一项目上的第二个 Sherry 进程——如何被阻止静默摧毁彼此的编辑：原子写、两层 CAS、进程内按路径锁、跨进程 `flock`、`write_file` 的先读后写许可证，以及可选的隔离工作区（其改动在锁下合并回主树）。

事实来源：`agent/tools/pub_base/atomic_write.py`、`agent/tools/pub_base/path_lock.py`、`agent/tools/pub_base/file_lock.py`、`agent/tools/pub_base/read_state.py`、`agent/tools/file_tools/write_file.py`、`agent/tools/file_tools/read_file.py`、`agent/tools/file_tools/patch_file.py`、`agent/tools/subagent/isolation/`、`agent/tools/subagent/announce/workspace_merge.py`。本文档中的每一个常量都已对照该代码核对。

## 目录

- [概述](#-概述)
- [各层机制](#-各层机制)
  - [1. 原子写](#1-原子写)
  - [2. 两层 CAS](#2-两层-cas)
  - [3. 按路径锁](#3-按路径锁)
  - [4. 残留清扫](#4-残留清扫)
- [先读后写许可证](#-先读后写许可证)
- [隔离子代理工作区（git worktree）](#-隔离子代理工作区git-worktree)
- [备选方案与实测](#%EF%B8%8F-备选方案与实测)
- [资源文件与编码](#%EF%B8%8F-资源文件与编码)
- [边界](#-边界)
- [测试](#-测试)
- [文件地图](#%EF%B8%8F-文件地图)

## 🎯 概述

所有文件工具与全部子 Agent 共享同一个进程，而工具又是进程级单例——所以"同一路径两个写者"是家常便饭而非边角情况。没有保护时，故障是静默的：后写者覆盖先写的编辑、两边都报告成功；读者读到半写文件；写到一半崩溃把目标截断。

六种机制按一次写入遇到它们的顺序回应这些问题：

1. `file_write_lock`——按路径的 `threading.Lock`，然后跨进程 `flock`。
2. `atomic_write_text_no_follow`——同目录临时文件、`fsync`、权限保留、`os.replace`。
3. `patch_file` 的两层 CAS——读取时指纹、`replace` 前紧贴再断言的 `expected_revision`。
4. `write_file` 的先读后写许可证——已存在的文件只能被读过它的会话覆盖（见下文对应小节）。
5. `sweep_stale_temp_files`——`kill -9` 的残留由下一次写入该目录时清扫。
6. 隔离子代理工作区——可选的 git worktree，改动在锁下合并回主树，冲突被如实报告。

## 🧱 各层机制

### 1. 原子写

`atomic_write_text_no_follow`（以及其下的字节版 `atomic_write_bytes_no_follow`）把内容写进目标同目录的 `.sherry-tmp-*.swp` 文件，`fsync` 它，把目标原有权限位拷上去——脚本的执行位必须存活——然后 `os.replace` 就位。于是读者只会看到旧内容或新内容，绝不会看到撕裂文件；写到一半崩溃也不会截断目标。

两条刻意的边界：

- 终节点是符号链接时一律拒绝（`ELOOP`，经由 `_open_no_follow`），因此该助手不能复用 `pub/func/atomic_replace.py`——后者是故意跟随链接的。
- 文件系统拒绝 rename 时，助手降级为就地写——可用性优先于原子性，并以 WARNING 记日志。

### 2. 两层 CAS

`patch_file` 读取文件、做模糊匹配，然后校验两次：读取时取下的指纹（`mtime_ns` + `size`，并带内容哈希豁免，使无实义的 `touch` 不算冲突），以及原子写在 `os.replace` 之前紧贴再断言的 `expected_revision`（`mtime:<整毫秒>:size:<字节>`）。任何其他人的改动都会被拒绝，返回"重新读取再重试"错误，而不是被覆盖。

"校验到写入"的窗口不是零，本文档也不如此宣称；它已被收窄到不引入文件系统级事务前提下的最小间隔。

### 3. 按路径锁

`path_lock.py` 维护一张引用计数的 `threading.Lock` 注册表——每个解析后路径一把——包住整个"读-改-写"周期，使本进程内的两个 Agent 串行而非竞争。`file_lock.py` 随后补上跨进程的一半：在 `src/data/locks/` 下的 sidecar 文件上取建议性 `flock`，超时返回 `FileBusyError`（可操作的错误，而不是挂起）。

一条让人意外的规则：**锁文件永不删除。** 锁住在 inode 上，因此 `unlink` 一个 sidecar——哪怕它看起来陈旧——会让下一个写入者新建文件、在没人排它的新锁上上锁；实测此时两个持有者同时进入临界区。0 字节 sidecar 是惰性的，且持有者死亡时内核会自动释放 `flock`，所以不存在需要实现的死主协议。

`terminal`、`python_repl` 与 ast-grep 重写跑在子进程里、不取任何锁：兜住它们的是 CAS，这条边界是刻意保留的。

### 4. 残留清扫

只有在临时文件创建到 replace 之间被硬杀，才可能留下 `.sherry-tmp-*.swp`；其余每个失败路径都会 unlink 它。下一次写入同一目录时会清扫其中超过 1 小时的残留（`sweep_stale_temp_files`）：活着的写者的临时文件只有几秒寿命，年龄阈值正是用来与它保持距离的。清扫从不抛异常——一次读不动目录的扫描绝不能让它所服务的那次写入失败。

## 📖 先读后写许可证

`write_file` 是盲覆盖：它从不读取目标，因此没有可带入 CAS 的 revision，可能摧毁本会话从未见过内容。`read_state.py` 补上缺失的前置条件——一张进程内的 `(会话, 解析后路径) → revision` 映射，只由两种事件喂给：

- 一次**完整**的 `read_file`（第一页、未被截断；分页读不发放任何许可），其 revision 取自打开描述符的 `fstat`，因此读到一半被换掉的路径无法给会话没见过的字节发证；
- 本会话自己整文件的 `write_file`，因为内容出自该会话之手。

会话自己的 `append` 或 `patch_file` 只会**推进**它已持有的许可证，绝不凭空造出一张——增量不等于对整文件的认知。许可证作为 `expected_revision` 进入原子写，于是：

- 会话从未读过的已存在文件会被直接拒绝，报"先读"错误并提示 `read_file`/`patch_file`；
- 读取之后发生变化的文件会被与并发写者相同的断言拒绝；
- 尚不存在的路径按 `absent` 发证，因此被别的写者抢先创建时会被拒绝而不是覆盖；
- 目录或符号链接目标跳过门禁——它们自己的报错才是对的答案，"先读"在此毫无意义。

注册表有上限（4096 条，LRU），且**不持久化**：重启即遗忘所有许可证，下一次覆盖已存在文件会得到"先读"——代价是一次重读，绝不是丢失编辑。淘汰只会丢掉保护，绝不会发放保护。

## 🌱 隔离子代理工作区（git worktree）

`sessions_spawn(isolation=True)` 让子代理获得项目的 **git worktree**（实现见 `agent/tools/subagent/isolation/`，工作区位于 `src/data/isolated/`，子代理的 cwd 是 `<workspace>/tree`，分支为 `sherry/<run8>`）。子代理在那里端到端工作——它的工具、它的 `terminal`、它的测试——运行进入终态时由 announce 流程把树合并回去，发生在交付完成消息之前（静默的子代理同样合并）。

基线是**脏工作区**：`git stash create` 把用户尚未提交的改动变成建树所用的 revision（stash 栈不受影响），因此子代理看到的是用户手里真实的内容——不是最后一次提交，也绝不会静默回退到它。检出只带已提交内容，其余由运行按 `SUBAGENT_ISOLATION` 两张表物化（项目的 `.worktreeinclude` 可以追加，路径下一行写 `# wti=symlink|copy`）：

- **软链**——共享机器状态：`src/`、`workspace/memory`、`workspace/sessions`、`cron_jobs.json`、`skills/auto`、`client/node_modules`。所有树共用一个 inode，这正是 SQLite 的 `-wal`/`-shm` 保持一致、以及经链接取到的锁能排除父树写者的原因（这是正确语义）；
- **复制**——两棵树绝不能共享的可变状态：`.omo`（计划与进度）；
- **未跟踪文件**逐个复制：`git stash create` 从不携带它们，少了这一步子代理就看不到用户刚建的文件；
- 只有 git 真正忽略的路径才会被物化；缓存（`node_modules`、`.git`、`.venv`、`__pycache__`、`dist`、`build` 等）既不复制也不合并。

不是仓库的项目会先被初始化：`git init`，把拒入规则写进 `.git/info/exclude`（**在** `git add -A` **之前**——秘密、体积与仓库状态永远不会进入那次基线提交的历史，事后删除也救不回来），提交一次，并写下 `.sherry-isolation-repo` 标记说明恢复方式（`rm -rf .git .sherry-isolation-repo`）。sherry 永不 push、不加 remote、不改写历史。

经物化链接的读取仍算隔离内部：`resolve_external_path` 只豁免该运行清单里记录过的路径，所以树内 `src/…` 不会触发 HITL「外部文件」审批，而树外任意软链依然会问。

合并就是把同一套规则反过来用，在按父根取的 `flock` 之下进行：

- 工作区清单（`snapshot.json`）记录创建时每个普通文件的 revision（成员取自检出树、revision 取自父树，并把 mtime 对齐，使未被触碰的文件不会被读成已改），外加基线 revision 与分支；
- 子代理改过的文件，只有在父树仍持有快照 revision 时才应用——否则记为**冲突**，父树文件保持原样；
- 新文件只创建到空位上；删除要求父树仍然匹配；符号链接绝不穿透合并（跳过并报告）。

干净合并会注销 worktree、删除其分支并移除工作区；有冲突则保留 `<workspace>/tree` 供检查，并消费掉清单，使同一棵树永远不可能被合并两次。父代理读到的完成回复中附带报告（`applied`、`created`、`deleted`，以及按路径列出的一一冲突），被合并的路径同时会在父会话的证据账本里标记为陈旧。

## ⚖️ 备选方案与实测

**行哈希编辑（omo 的 hashline）**没有实现：它比整文件 CAS 更精细——只比对编辑触及的行——但需要改造 read 工具自身的输出格式，而上面四层已经消除了它要解决的静默丢失故障。

**rename 之后的目录 `fsync`** 没有启用。本机实测（f2fs）：一次写入带目录 `fsync` 为 1.34 ms，不带为 0.52 ms——写路径的 2.6 倍，绝对值约每个文件 0.8 ms。它买到的东西很窄：文件数据本就已 `fsync`，掉电绝不会留下撕裂文件——只可能让一次编辑静默退回先前内容，而主流编辑器同样如此。进程崩溃则完全不需要它：页缓存还在。如果某个部署将来承诺"已确认的写入必须挺过掉电"，它就是开关后的一行代码。

## 🗂️ 资源文件与编码

工具编辑的是**文本**，而许可证让这件事变成了承重结构：过去读一张 PNG 会给模型一屏
替换字符、随后还发一张覆盖许可证，于是图片可能被乱码覆盖。现在由
`file_utils.sniff_text_encoding` 判定，先看 BOM，再看 NUL 字节 / UTF-8 解码：

- **UTF-8、带 BOM 的 UTF-8、UTF-16（LE/BE）** 正常读写，且许可证会把编码一并带下去——
  `write_file` 与 `patch_file` 按文件**自己的**编码回写，UTF-16 配置保留 BOM 与字节序而
  不会静默变成 UTF-8，大端文件保持大端。
- **其余一律视为资源文件**：`read_file` 只回大小与 `terminal` 提示、**不发放任何许可证**，
  `patch_file` 拒绝，`write_file` 在查许可证之前就拒绝。这正是图片与归档不会被文本覆盖的
  原因：二进制文件永远拿不到许可证，也就没有任何东西能用文本写它。
- **无 BOM 的旧编码（GBK/GB18030）拒绝而不是猜测**——猜错编码去重写文件，比一次可操作的
  拒绝更糟。逃生通道是 `terminal`（cp / python）。
- **`append` 只支持 UTF-8**：往 UTF-16 文件里追加要么写入 UTF-8 字节、要么在文件中部再插
  一个 BOM，所以它回的是"读取后整文件重写"。
- 隔离工作区的合并全程按字节进行，因此资源文件能精确合并，尽管文本工具不会去编辑它们。

## 🚧 边界

- 跨进程锁是建议性的：外部编辑器不会去取它。那里的答案是 CAS 各层。
- `terminal`、`python_repl` 与 ast-grep 重写完全绕过锁与许可证（子进程），因此通过 shell 命令写文件的 Agent 不在这份保证之内。
- 网络文件系统：`flock` 语义在 NFS 上不可靠；本地磁盘才是目标场景。
- 硬链接按设计会被断开：`os.replace` 换了 inode，指向旧内容的硬链接保留旧内容。

## 🧪 测试

`tests/agent/tools/file_tools/` 钉住写路径：`test_atomic_write.py`（原子性、软链拒绝、两层 CAS、残留清扫）、`test_file_write_concurrency.py`（并发补丁、无撕裂读）、`test_file_lock_cross_process.py`（两个真实进程、`kill -9` 释放）、`test_read_before_write.py`（许可证矩阵）。`tests/agent/tools/subagent/test_workspace_isolation.py` 钉住脏基线、auto-init 的拒入清单、物化（软链、复制、未跟踪文件）、外部路径豁免、合并 CAS、冲突、软链跳过与按父根串行化。

## 🗺️ 文件地图

| 路径 | 职责 |
| --- | --- |
| `agent/tools/pub_base/atomic_write.py` | 原子写、revision 标识、残留清扫 |
| `agent/tools/pub_base/path_lock.py` | 进程内按路径锁注册表 |
| `agent/tools/pub_base/file_lock.py` | `flock` sidecar、`file_write_lock`、`FileBusyError` |
| `agent/tools/pub_base/read_state.py` | 先读后写许可证注册表 |
| `agent/tools/subagent/isolation/worktree.py` | 脏基线、auto-init 与拒入清单、worktree 生命周期 |
| `agent/tools/subagent/isolation/materialize.py` | 忽略/未跟踪路径物化 |
| `agent/tools/subagent/isolation/tree.py` | 工作区 worktree、清单、销毁 |
| `agent/tools/subagent/isolation/merge.py` | 加锁、CAS 校验的合并回主树 |
| `agent/tools/subagent/announce/workspace_merge.py` | 合并钩子 + 完成回复中的报告 |
