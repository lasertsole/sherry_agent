# 计划:SWE-bench HARD 整改——竞争修复与执行顺序

- 日期:2026-10-27
- 上游分析:[分析_SWE-bench_HARD_运行与整改方案_20261026.md](分析_SWE-bench_HARD_运行与整改方案_20261026.md)
- 范围:`evals/swebench`(host harness)+ `evals/coding_capability/suite.py` 的 `_drive` / `_attempt`
- 结论:这不是单纯的 `.sherry` 清理问题,而是 **harness 与 agent 共享同一个 worktree、且 turn 结束不等于所有写者结束** 的生命周期竞争。先做隔离与 quiesce,再让 harness 原子化,最后才是 no-edit、预算和报告。

---

## 一、竞争问题拆解

### 1.1 参与者

| 写者 / 操作 | 生命周期 | 会写什么 |
|---|---|---|
| 主 agent 的图(turn) | `_drive` 的 stream 结束 | 源文件、测试文件、`.sherry` |
| detached 子代理(executor/researcher…) | 主 turn 结束后仍可运行 | 同一个 repo 的源文件、`.sherry` |
| 后台终端(`terminal(run_in_background=True)`) | 主 turn 结束后仍可运行 | 任意文件、构建产物 |
| shadow-git 快照 | 在写工具线程内**同步**执行 | `<project>/.sherry/shadow-git/**` |
| harness 的 diff / test patch / pytest / revert | `_drive` 返回之后 | 工作树、`.git`、`.sherry` 的删除 |

### 1.2 时间线(当前实现)

```
_drive 返回(只代表主图结束)
  │
  ├─ 子代理 / 后台终端可能仍在写 ───────────────┐
  ├─ 工具线程的收尾(超时/取消)可能仍在写 ──────┤
  ▼                                            │
instances.diff() / changed_paths()  ←──────────┘  (读与写竞争)
  ▼
git apply <test_patch>              ←────────────── (写与写竞争)
  ▼
pytest F2P / P2P                    ←────────────── (测试被后台写者污染)
  ▼
instances.revert(): checkout + clean -e .sherry   (删除与写者竞争)
```

### 1.3 失败模式

| # | 竞争 | 后果 |
|---|---|---|
| F1 | `.sherry/shadow-git` 被 `git clean -qfd` 遍历,同时快照/子代理在写 | `lstat` EPERM / `git clean` exit 1 → 整轮 `RuntimeError`(已观察:Django 结果丢失) |
| F2 | 子代理/后台终端在 diff 之后、pytest 之前写文件 | 保存的 diff 与实际测试的树不一致;F2P/P2P 假阴/假阳 |
| F3 | 子代理写测试文件,或写与 `git apply <test_patch>` 同一文件 | test patch apply 失败,或测试被 agent 改过 → 误判 unresolved |
| F4 | 后台终端创建 untracked 文件,`git clean` 在它写时删除 | agent 的中间产物丢失 → 假 `no_edit` |
| F5 | `wait_for` 超时只取消外层 task;子代理/后台终端继续 | 超时实例污染下一个实例 |
| F6 | 进程级状态(`shadow_git._dedup` / registries / sessions)跨实例残留 | 串行跑多个实例时状态串味 |
| F7 | 一个实例的异常直接冒泡到 `_run_instances` | 后续实例不执行,已完成的分数不落盘 |
| F8 | `git diff` 只含 tracked 文件,`git clean` 又删 untracked | agent 新建的文件既不在保存的 diff 里,也可能在 clean 时丢失 → 手工复判/复现缺文件 |

### 1.4 现有修复与不足

`instances.py::revert()` 当前是:

1. `shutil.rmtree(.sherry)` 最多 3 次,每次 200ms;
2. `git checkout -- .`;
3. `git clean -qfd -e .sherry`;
4. 再 `rmtree(.sherry)`。

它能挡住"`git clean` 直接踩 `.sherry`"这一种形态,但:

- **是计时修复,不是屏障**:写者不停,重试只是赌它 600ms 内结束;
- **只保护 `.sherry`**:tracked 文件 / 测试文件仍可被后台写者改动;
- **顺序偏晚**:diff 在 quiesce 之前,测试也在 quiesce 之前;
- **没有处理超时路径**:`wait_for` 取消外层后直接进入 diff;
- **仍把 `.sherry` 留在实例 repo 内**,让 harness 每次都要绕开它。

> 修正上游分析的一个假设:shadow-git 快照在写工具线程内**同步**完成,不存在"turn 返回后还在跑的快照 GC"。跨 turn 存活的是 detached 子代理、后台终端,以及被取消/超时的工具线程。它们与快照的 `git add -A`、harness 的 `git clean` 竞争,`lstat` EPERM 只是最先暴露的症状。

---

## 二、解决方案

原则:**先隔离,再停写者,最后才动 worktree。** 不靠 sleep 和重试。

### 2.1 Layer 0:把 agent 私有状态移出实例仓库(结构性,首选)

`shadow_store()` 目前固定返回 `<project>/.sherry/shadow-git`。新增一个 store 根覆盖:

```python
# config/features/agent_side/shadow_git.py
class ShadowGitConfig(TypedDict):
    ...
    #: Optional store root; empty keeps <project>/.sherry/shadow-git.
    store_dir: str
```

`shadow_store(root)`:

```python
override = SHADOW_GIT.get("store_dir") or os.getenv("SHERRY_SHADOW_GIT_DIR", "")
if override:
    return Path(override).expanduser().resolve() / sha256(str(root.resolve()))[:16]
return root.resolve() / ".sherry" / "shadow-git"
```

- eval harness 在 `_run_instance` 之前设置进程级 `SHADOW_GIT["store_dir"] = <results>/shadow`(或按 session 再分层),实例 repo 里**不会再出现 `.sherry`**;
- `checkpoint` 工具照常可用(指向外部 store),不牺牲产品路径;
- 生产默认不变;打包/桌面不受影响;
- **注意 `.sherry` 不只装 shadow-git**:同目录还有 `codeintel/index.db`(子代理的 code-intel 索引)。store override 只移走影子库;`codeintel` 仍可能由活着的子代理写入,所以 `info/exclude` 与 quarantine 都以**整个 `.sherry` 目录**为单位,quiesce 负责停掉子代理。
- 备选:`SHADOW_GIT["enabled"]=False` 只用于 eval(最省事,但 checkpoint 能力被跳过;仅当 override 来不及实现时用)。

同时给实例 repo 加本地排除(不改用户的 `.gitignore`):

```python
(repo_dir / ".git" / "info" / "exclude").append("\n.sherry/\n")
```

这是第二道保险:即使 override 漏配、有旧残留或第三方代码又创建了 `.sherry`,`git status` / `git clean` 也不会碰它。

**测试**:`tests/evals/test_swebench_instances.py` 新增 `test_shadow_store_override_keeps_the_repo_clean`:设置 override → 调用 `ensure_checkpoint` → 断言 repo 内无 `.sherry`、外部 store 有 commit、`list_checkpoints` 仍可读。

### 2.2 Layer 1:quiesce——先停写者,再碰 worktree

新增 `evals/swebench/teardown.py::quiesce_session(session_id, root, *, timeout_s=30)`,**幂等**,返回结构化结果:

```python
async def quiesce_session(session_id: str, root: Path, *, timeout_s: float = 30) -> dict:
    # 1) 主 turn(若还被 turn_runner 登记)
    await cancel_session_turn(session_id)

    # 2) 子代理:整棵子树,含 detached children
    requester = denormalize_session_key(session_id)
    await kill_all_controlled_subagent_runs(requester, reason="eval_teardown")
    descendants_ok = await wait_until(
        lambda: count_active_descendant_runs(requester) == 0, timeout=10
    )

    # 3) 后台终端:杀进程组,等 status != running
    background_registry.kill_session_tasks(session_id)
    background_ok = await wait_until(
        lambda: not background_registry.has_running(session_id), timeout=5
    )

    # 4) shadow-git:等当前写者,冻结后续快照
    shadow_ok = shadow_git.wait_for_idle(root, timeout=5)
    shadow_git.freeze(root)      # 直到 harness 完成 revert 再 unfreeze

    return {
        "ok": descendants_ok and background_ok and shadow_ok,
        "timed_out": not (descendants_ok and background_ok and shadow_ok),
        "active_descendants": count_active_descendant_runs(requester),
        "background_running": background_registry.running_count(session_id),
    }
```

需要的两个小 seam:

- `background_registry.has_running(session_id)` / `running_count(session_id)`(读 `list_session` 的 status,不新增状态);
- `shadow_git` 的 per-root 写者围栏:
  - `_active_writers: dict[str, int]` + `_writers_lock`;
  - `take_snapshot` / `run_shadow` 进入时计数 +1,退出 -1;
  - `wait_for_idle(root, timeout)` 等计数归零;
  - `freeze(root)` 置位后 `ensure_checkpoint` 直接返回 `True`(fail-open,不阻塞写;eval 场景下写工具应已被杀);`unfreeze(root)` 在 revert 后调用。

**调用点**:
- `evals/swebench/suite.py::_run_instance` 的 `finally`(覆盖正常结束、异常、`wait_for` 超时)先 quiesce,再 diff / changed_paths / test patch / pytest / revert;
- `evals/coding_capability/suite.py::_attempt` 同样在测试/diff 之前 quiesce(它是 SWE harness 的 `_drive` 宿主,也是同一竞争);
- quiesce 返回 `timed_out=True` 时:记录 `harness_error`,继续执行 quarantine + revert,**不要**让该实例 abort 整轮。

**测试**:
- `test_quiesce_kills_descendants_and_background_tasks`:注册一个假 child run + 一个假 running background task,断言 quiesce 后二者都不再 active,且调用顺序正确;
- `test_quiesce_timeout_marks_harness_error`:让等待永不归零,断言返回 `timed_out` 而不是抛异常;
- `test_shadow_freeze_blocks_new_snapshots`:freeze 后 `ensure_checkpoint` 不再写 store。

### 2.3 Layer 2:harness 原子性与增量落盘

`_run_instances` 目前无 `try/except`,一个实例抛错就终止整轮。改为:

```python
for instance in prepared:
    try:
        result = await _run_instance(instance, results_dir, hard=hard)
    except Exception as exc:
        result = _harness_error_result(instance, exc)   # failure_kind="harness"
    results.append(result)
    _write_instance_result(results_dir, result)          # 立即落盘
```

- `_run_instance` 内部:`try/finally` 保证 quiesce + revert 都执行;
- 每实例写 `results_dir/instances/<id>/instance_result.json`(含 diff 路径、tokens、changed_paths、quiesce 结果、harness_error);
- `_publish` 在 `finally` 里执行(或至少把已收到的 results 写完),不要再等整切片结束;
- 报告新增 `failure_kind="harness"`、`harness_error`、`quiesce` 字段。

**测试**:mock 一个会抛 `RuntimeError` 的实例,断言下一个实例仍执行、两个 result 都落盘、最终报告含 `harness` 类型。

### 2.4 Layer 2b:revert 原子化(quarantine + exclude)

把 `revert()` 的 3×200ms 循环换成原子搬迁:

```python
def revert(instance, *, trash_root: Path) -> None:
    sherry = instance.repo_dir / ".sherry"
    trash = trash_root / f"{instance.instance_id}-{uuid.uuid4().hex[:8]}"
    trash_root.mkdir(parents=True, exist_ok=True)
    if sherry.exists():
        os.replace(sherry, trash)          # 原子 rename;跨设备时退回 rmtree
    _run(["git", "checkout", "--", "."], cwd=instance.repo_dir)
    _run(["git", "clean", "-qfd", "-e", ".sherry"], cwd=instance.repo_dir)
    if sherry.exists():
        os.replace(sherry, trash.with_name(trash.name + "-late"))
    shutil.rmtree(trash, ignore_errors=True)
    shutil.rmtree(trash.with_name(trash.name + "-late"), ignore_errors=True)
```

- `os.replace` 是原子目录 rename,不遍历对象文件,不会踩只读对象;
- 即使有晚到写者重新创建 `.sherry`,`-e .sherry` 和 `info/exclude` 保证 clean 不碰它,下一次 quarantine 再收;
- 不再需要 sleep/重试。
- `os.replace` 要求同一文件系统;跨设备时退回 `shutil.move` / `rmtree`。trash 放在实例目录同级或 `results/trash`(同盘),避免 EXDEV。

**测试**:`test_revert_quarantines_sherry_without_timing_retry`(构造 `.sherry` 只读对象,断言 rename 成功、repo 干净、trash 删除)。

### 2.5 diff 保真:untracked 新文件也要进 patch

`instances.diff()` 现在只跑 `git diff`,agent 新建的 untracked 文件不会进保存的 patch;正常判分因为直接在活树上跑测试还能过,但手工复判、复现和 `diff-*.patch` 会缺文件。做法:

```python
# 在 quiesce 之后、git clean 之前
_run(["git", "add", "-N", "--", ".", ":(exclude).sherry"], cwd=repo)
patch = _run(["git", "diff", "--binary", "HEAD"], cwd=repo)
_run(["git", "reset", "-q"], cwd=repo)   # 恢复 index,test patch 仍可 apply
```

排除 `.sherry`(以及 `instances.tampered_paths` 里的测试文件规则),避免把 agent 私有状态或测试改动混进 patch。`changed_paths()` 也要用 `git diff HEAD --name-only` + untracked(否则会漏掉 agent 自己 `git add` 过的文件)。测试:新建一个源文件 + 一个 `.sherry` 文件,断言 patch 含前者、不含后者,`git reset` 后 test patch 仍能 apply。

### 2.6 Layer 3:进程隔离(可选,Phase 3)

如果 Phase 0–2 之后仍有跨实例泄漏:把每个实例的 agent 放进子进程/进程组,teardown `killpg(SIGTERM→SIGKILL)` 并 `wait`,再把 repo 交给 harness。这样 quiesce 变成进程级确定性操作;代价是结果收集、日志与 session 绑定要重做,放在最后评估。

---

## 三、下一步执行顺序

> 先修竞争(Phase 0),再修能力(Phase 1),再做可观测性(Phase 2/3)。每项完成即跑对应测试,不要攒到最后。

### Phase 0:竞争修复(下一轮 HARD 前必须完成)

- [ ] **0.1 影子库移出实例 repo**
  - `SHADOW_GIT["store_dir"]` + `SHERRY_SHADOW_GIT_DIR`;`shadow_store()` 支持覆盖;
  - eval 在 `_run_instance` 前设置 per-run store;实例 `.git/info/exclude` 加 `.sherry/`;
  - 测试:`test_shadow_store_override_keeps_the_repo_clean`。
- [ ] **0.2 shadow-git 写者围栏**
  - `_active_writers` + `wait_for_idle` + `freeze/unfreeze`;
  - 测试:freeze 阻断新快照、wait 能等到在途写者。
- [ ] **0.3 `quiesce_session`**
  - turn 取消 → 子代理 cascade kill → 后台终端 kill → shadow wait/freeze;
  - 有界等待;超时返回 `timed_out`,不抛;
  - 测试:子代理/后台/超时三条路径。
- [ ] **0.4 `_run_instance` / `_attempt` 接入 quiesce**
  - 在 diff / test patch / pytest / revert 之前;
  - `wait_for` 超时路径也走 `finally`;
  - 测试:超时后 diff 仍干净。
- [ ] **0.5 每实例隔离 + 增量落盘**
  - `_run_instances` try/except;`instance_result.json`;`harness_error`;最终报告 `finally`;
  - 测试:一个实例抛错,后续继续,结果都在。
- [ ] **0.6 revert quarantine**
  - `os.replace` 原子搬迁 + `-e .sherry`;删除 3×200ms 计时循环;
  - 测试:只读对象 / 晚到 `.sherry` 都能恢复干净。
- [ ] **0.7 diff 保真**
  - `git add -N`(排除 `.sherry`)+ `git diff --binary` + `git reset`;
  - 保存的 patch 含 agent 新建文件;不含 `.sherry` / 测试改动;
  - 测试:新文件 + `.sherry` 文件。
- [ ] **0.8 复跑验收**
  - HARD 2 实例连续跑 3 次:无 `.sherry` 崩溃;每实例结果都落盘;Django 仍 resolved;Sympy 至少非空 diff 或明确 `no_edit`。

### Phase 1:能力(P0,紧接 Phase 0)

- [ ] **1.1 no-edit gate**
  - 先做 MVP(不改生产中间件):`_drive` 结束后若 `changed_paths == []` 且预算耗尽,启动 **一个** repair turn,注入“停止广泛搜索,选一个最小修复并编辑”的 directive,给有界追加预算(如 30);再次无 diff 才判 `no_edit`;
  - 若要把 gate 放进回合内:再评估一个 eval-only 的 `before_model`/tool-count 中间件(20 次调用无写操作 → 提示;70% 预算无 diff → 强制 patch);
  - 报告新增 `no_edit` failure kind。
- [ ] **1.2 HARD 预算**
  - 仅 SWE-bench HARD 的主 agent 预算 120–160(普通任务保持 90);与“是否已产生 diff”绑定,有进展才续期;
  - 测试:预算配置在 eval 路径生效,普通路径不变。

### Phase 2:可观测性(P1)

- [ ] **2.1 报告补全**:`final_text`(最终 assistant message,而非首条 stream)、`cache_read`、`cost_usd`、`sandbox_backend`、`harness_error`、`run_meta.json`;逐实例增量落盘。
- [ ] **2.2 context-trim 占位符**:不把裁剪占位符标成 `tool_status=error`;保留首尾/摘要或可回读引用;统计出现次数。
- [ ] **2.3 materialize 稳定性**:fetch/install 有界重试;按 instance+commit 缓存 checkout/venv(`--instance-cache-dir`);网络失败计 `invalid_run`,不终止整轮。
- [ ] **2.4 路径提示**:文件工具对 `/django/...` 这类输入返回项目根与相对路径提示(或安全重写);SWE prompt 明确项目根约定。

### Phase 3:P2

- [ ] **3.1 prompt/模型**:SWE arm 的 prompt 增加“先找目标测试/相关 printer,再最小编辑”;20 次调用无写操作时强提示。
- [ ] **3.2 成本/缓存指标**:报告加 cache hit、每实例 cost、总 cost;低 cache_read 实例单独标记。

---

## 四、验收标准

1. HARD 2 实例连续 3 次:不再因 `.sherry` / `git clean` 崩溃。
2. 任一实例抛错:其余实例继续,`instance_result.json` 与最终报告都包含失败实例的 `harness_error`。
3. `quiesce_session` 超时:该实例标 `harness_error`,不中断整轮;revert 仍执行。
4. diff / test patch / pytest 期间没有子代理或后台终端写实例 repo;保存的 diff 与实际测试树一致,且包含 agent 新建的 untracked 源文件。
5. Django 保持 resolved;Sympy 至少产生非空 diff;若仍 unresolved,报告必须能区分 `no_edit`、F2P 失败与 `harness`。
6. 报告含 input/output/cache_read/cost、最终文本、`sandbox_backend`、`harness_error`、`run_meta`。
7. 现有 `tests/evals/test_swebench_instances.py` 与新增 quiesce / store override / revert 用例通过。

---

## 五、风险与边界

| 风险 | 缓解 |
|---|---|
| quiesce 等不到子代理/后台结束 | 有界超时(总计 ≤30s);超时标 `harness_error` 并继续;Phase 3 进程隔离兜底 |
| 杀子代理丢掉可能有用的中间结果 | eval 实例是一次性目录;生产不走 eval teardown;SWE-bench 只判最终补丁 |
| shadow store override 改变路径 | 默认不设即保持原行为;eval 的 store 放 run 目录并在 run 结束清理;测试覆盖 checkpoint 读写 |
| PRoot 的 `lstat` EPERM 仍偶发 | 隔离 + `info/exclude` + `-e .sherry` + 原子 quarantine 四层;不依赖 sleep |
| 进程隔离改动大 | 仅当 Phase 0–2 后仍观察到跨实例泄漏时再做;先记录指标 |
| `cancel_session_turn` 只覆盖 turn_runner 登记的任务 | eval 的 `_drive` 自身 await 主图;真正要停的是 detached 子代理/后台终端;两者都有显式 kill API |
| 顺序执行导致时间变长 | quiesce 正常应 <1s;等待只发生在确有活跃写者时;超时才是慢路径 |

---

## 六、代码位置

- `evals/swebench/instances.py` — `revert` / `diff` / `changed_paths` / `materialize`
- `evals/swebench/suite.py` — `_run_instance` / `_run_instances` / `_self_check` / `_publish`
- `evals/coding_capability/suite.py` — `_drive` / `_attempt`(SWE harness 的宿主)
- `agent/tools/pub_base/shadow_git.py` — `shadow_store` / `take_snapshot` / `ensure_checkpoint` / `run_shadow`
- `agent/tools/subagent/control/kill.py` — `kill_all_controlled_subagent_runs` / `kill_subagent_run_with_cascade`
- `agent/tools/terminal_bg_registry.py` — `background_registry.kill_session_tasks`
- `server/service/turn_runner.py` — `cancel_session_turn`
- `tests/evals/test_swebench_instances.py` — 现有 revert 用例;新增 store override / quiesce / quarantine 用例

---

## 七、一句话总结

**不要让 harness 和 agent 共用可写的私有状态,也不要假设 turn 结束就没有写者。** 把影子库移出 repo,先 quiesce 再 diff/test/revert,并让每个实例的失败只影响它自己——`.sherry` 的 `lstat` EPERM 自然消失,后续 no-edit / 预算 / 报告才有意义。
