# 06 Executor：SherryExecutor、WorkerSlots、后台作业

复刻原型 `src/executor/` + `003-execution.md`。这是 Python 版最大的工程差异：原型用 DeepSeek Harness 的 Session API，Python 版用 **Sherry 的 subagent 系统**作为 Executor。

## 1. 命名与定位

- 原型 `DshExecutor` → Python `SherryExecutor`（`superagent/executor/sherry.py`）。
- 原型 Executor Session = Harness Session → Python = **Sherry subagent run**（`spawn_subagent_direct`，父 = superagent main session，见 `12-sherry-integration.md §3-§4`）。
- 原型 `dsh-superagent-worker` preset → Python = Sherry subagent 的 functional_role=executor + MCP `orchestrator_*` 工具 + WORK_TOOLS。
- 原型 `dsh-superagent-router` preset → Python = superagent 持久 main session（router role，受限工具 + MCP）。

> **修正（承重问题 1、2、3）**:workers 不再是"无父独立 spawn",而是 superagent main session 的子代理。`orchestrator_*` 工具经 MCP 暴露(不传 spawn_kwargs)。Assignment 模型选 A:每次 Trigger 唤醒起一个**新** subagent run,而非持久 worker session。详见 `12-sherry-integration.md`。

## 2. SherryExecutor（`superagent/executor/sherry.py`）

```python
class SherryExecutor:
    """一 Ticket 一 Session（Sherry subagent run）一目录。
    通过 runtime/hooks 调 Sherry 后端，不直接 import agent/。"""
    def __init__(self, host: ExecutorHost, settings_getter, deployment):
        self.host = host                    # hooks 接缝（spawn/cancel/...）
        self.slots = WorkerSlots(settings_getter().max_workers)
        self.settings_getter = settings_getter
        self.deployment = deployment

    def start(self, ticket_id, assignment_id) -> None:
        """launch effect：prepare → 起 subagent → assignment message。
        在 Engine 事务内 enqueue，commit 后执行。"""
        enqueue("launch", ticket_id, payload={assignment_id})

    def deliver(self, ticket_id, assignment_id, input_ids) -> None:
        """delivery effect：把 waiting Inputs 送给 running subagent。
        通过 hooks 向 subagent session 注入消息。"""
        enqueue("delivery", ticket_id, payload={assignment_id, input_ids})

    def stop(self, ticket_id) -> None:
        """cancel subagent turn。after_commit 后执行（rollback 不停）。"""

    def prepare(self, ticket, outcome) -> PrepareResult:
        """Engine.prepareTicket hook：short name/dir/assignment text。
        不跑命令，仅生成元数据。"""
        ...

    def worker_session_id(self, ticket_id: str) -> str:
        return f"worker-{ticket_id}"
```

### 2.1 launch effect handler

```python
async def _handle_launch(self, ticket_id, payload):
    ticket = self.host.engine.tickets.get(ticket_id)  # read-only
    # 1. 占 slot
    self.slots.acquire_launch()  # 满则 throw Later
    # 2. prepare 命令（首次）
    if not os.path.exists(ticket.execution.directory):
        await self._run_prepare(ticket)
    # 3. 起 Sherry subagent（父 = superagent-main session）
    session_key = self.worker_session_id(ticket_id)
    spawn_req = SpawnRequest(
        requester_session_key="superagent-main",   # ← 父是 superagent main session
        functional_role="executor",
        step_model=settings.executor_model,
        run_timeout_seconds=_work_budget(ticket),
        cwd=ticket.execution.directory,
        env=self._worker_env(ticket),
        expects_completion_message=True,            # drain 回 main session
        announce=True,
        # tools 不在这里传——orchestrator_* 经 MCP 加载,WORK_TOOLS 来自 Sherry registry
    )
    result = await self.http.post(f"{self.sherry_url}/internal/superagent/spawn",
                                  json=spawn_req.dict())
    # 4. 通知 Engine
    self.host.engine.assignment_started(ticket_id, payload.assignment_id)
```

**关键修正**:`requester_session_key="superagent-main"`(非 None)。worker 完成 drain 回 main session,既触发下一 routing turn,又重置 main session 续跑 latch(L7 核心)。详见 `12-sherry-integration.md §4`。

### 2.2 spawn_kwargs（修正）

tools **不在 spawn 时传**。Sherry subagent 的工具来自两部分:

- **Sherry 工具注册表的 WORK_TOOLS**:file/search/shell/job/web/skill/todo/subagent(Sherry 既有,经 preset/role 配置裁剪)。
- **MCP `orchestrator_*` 工具**:superagent MCP server 暴露,Sherry 以 MCP client 加载(配在 `sherry.jsonc` 的 `mcp.servers.superagent`)。

worker 收到 6 个 worker 应用工具(`orchestrator_read`/`ack`/`report`/`thread_unrelated`/`thread_follow`/`trigger_handback`)+ WORK_TOOLS。移除 ask/present/profile/plugin_manager/sessions_send——这些在 Sherry 工具集里就不给 executor role(`SPAWN_ALLOWED_CHILD_ROLES` 既定)。

role 裁剪 + MCP 工具暴露见 `12-sherry-integration.md §5` + `10-sessions-prompts.md §3-§4`。

## 3. WorkerSlots（`superagent/executor/sherry.py` 同文件）

```python
class WorkerSlots:
    """launch/wake 占槽；max_workers + 1 人消息槽；wake 优先。"""
    def __init__(self, max_workers: int):
        self.max = max_workers
        self.running = 0
        self._lock = asyncio.Lock()

    async def acquire_launch(self):
        async with self._lock:
            if self.running >= self.max:
                raise Later()  # 不计数失败，5s 后重试
            self.running += 1

    async def acquire_wake(self):
        """人消息/status 请求可用额外槽"""
        async with self._lock:
            if self.running >= self.max + 1:
                raise Later()
            self.running += 1

    def release(self):
        self.running -= 1

    @property
    def active_count(self): return self.running
```

wake 优先于 launch：人消息/status 请求用额外槽，且 launcher 排队时让 wake 先。`can_wake_extra` 在满载时仍允许一个 wake。

## 4. 后台作业（`superagent/executor/background.py`）

复刻原型 `src/executor/background.ts`。**注意 Assignment 模型选 A 后的差异**(见 `12-sherry-integration.md §4.2`):

- MVP(A):每次 Trigger 唤醒起**新** subagent run,goal-bounded,完成 drain。background job 的完成**不**唤醒当前 executor(executor 已完成),而是由下一个 wake 的 subagent 读 `job_output`。
- 这符合原型"Executor reads killed-job results on its next turn"——只是"next turn" = 下一个 subagent run。
- P2 评估选 B(持久 worker session)是否值得:若要支持"background job 完成唤醒当前 executor 续 turn",需新 Sherry 机制(state_port 注入 + turn_runner 续 turn),工程量大。MVP 不做。

```python
class BackgroundWork:
    """worker turn 结束后仍在跑的 shell job + subagent。
    MVP(A):executor 完成 drain 后,这些 job 的结果由下一个 wake 读。
    handoff 时停 subagent;shell job(如 review server)可继续到 Assignment 结束。"""
    def running_jobs(self) -> list[Job]: ...
    def kill_jobs(self): ...
    def stop_subagents(self): ...
    def stop_background(self): ...
```

规则(选 A 后简化):

- turn 结束仍有 shell job/subagent → keep slot,不报 waiting(等待 subagent 完成 drain)。
- subagent 完成(CompletionJudge goal loop 结束)→ drain 回 main session → release slot。
- 30 分钟上限:杀 shell job;subagent 由 `run_timeout_seconds` 管(Sherry subagent 既有 wall-clock timeout)。
- handoff(ready/问题/blocked/failed)→ release slot;subagent 停;shell job 可继续到 Assignment 结束。
- Assignment 结束 → 杀 shell job(不唤醒),释放 subagent。

## 5. prepare 命令（`sh -c`）

```python
async def _run_prepare(self, ticket):
    prepare = self.settings_getter().executor.prepare
    if not prepare:
        return  # 空 dir
    env = self._prepare_env(ticket)
    proc = await asyncio.create_subprocess_exec(
        "/bin/sh", "-c", prepare,
        cwd=self.deployment.execution_root,
        env={**os.environ, **env},
        stdout=PIPE, stderr=PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=600)
        if proc.returncode != 0:
            self.host.engine.report(ticket.id, Report(
                kind=ReportKind.FAILED,
                summary=f"The prepare command failed: {err.decode()[:500]}",
                ...
            ))
            raise PrepareFailed()
    except asyncio.TimeoutError:
        proc.kill()
        raise PrepareFailed("prepare timed out after 600s")
```

典型 prepare（来自 006-deployment.md）：

```sh
git -C /home/me/src/widgets fetch --no-tags "https://github.com/$DSH_SUPERAGENT_GITHUB_REPOSITORY.git" "${DSH_SUPERAGENT_PR_HEAD:-HEAD}:refs/dsh-superagent/$DSH_SUPERAGENT_TICKET" &&
git -C /home/me/src/widgets worktree add -b "agent/$DSH_SUPERAGENT_NAME" "$DSH_SUPERAGENT_DIR" "refs/dsh-superagent/$DSH_SUPERAGENT_TICKET"
```

Windows 注意：Sherry 跑在 win32（见 AGENTS.md environment），但 superagent 的 worker 用 git worktree 需要 POSIX sh。**策略**：worker shell 强制用 Git Bash（`bash -c` 而非 `/bin/sh -c`），与 Sherry 的 terminal 工具一致。prepare 命令的 `git -C` 跨平台。

## 6. 执行环境变量（`superagent/app/execution_environment.py`）

复刻原型变量表（003-execution.md §Execution variables）。**保留 `DSH_SUPERAGENT_*` 前缀**（prompt 文本依赖）。

| Variable | 注册者 | 值 |
|---|---|---|
| `DSH_SUPERAGENT_DIR`, `DSH_SUPERAGENT_NAME`, `DSH_SUPERAGENT_TICKET` | Executor | Ticket 目录/short name/id |
| `DSH_SUPERAGENT_BLOCKED_COMMANDS` | Executor | 禁止命令列表 |
| `DSH_SUPERAGENT_GITHUB_REPOSITORY` | GitHub | `owner/name` |
| `DSH_SUPERAGENT_GITHUB_ACCOUNT`, `DSH_SUPERAGENT_GITHUB_COMMIT_EMAIL` | GitHub | agent 账号 + commit email |
| `DSH_SUPERAGENT_GITHUB_NUMBER`, `DSH_SUPERAGENT_GITHUB_PR_HEAD` | GitHub | 起源 Issue/PR + head ref |
| `DSH_SUPERAGENT_FEISHU_BOT`, `DSH_SUPERAGENT_FEISHU_CHAT` | Feishu | bot + 起源 chat |

Sherry 自有变量（如 `SHERRY_SUPERAGENT_TICKET_TITLE`）按需加，但不进 prepare 命令文本（保持跨实现兼容）。

prepare 命令读 env；worker 读 env 在 prompt 的 `runtime/environment` 段。无凭据。

## 7. 失败重试（transient）

```python
TRANSIENT_HTTP = {408, 429, 500, 502, 503, 504}

async def _handle_launch(self, ticket_id, payload):
    try:
        ...
    except TransientError as e:
        # provider/network：HTTP 408/429/5xx、连接 refused/reset/timeout
        if self._attempts(payload) < 3:
            await asyncio.sleep(5 * 2 ** (self._attempts(payload) - 1))  # 5s→300s
            raise RetryEffect()  # 重排，不计数失败
        raise  # 第 4 次真正失败
```

failed turn：subagent 收到 continuation message after 15/30/60s。

## 8. Restart 恢复（`resume`）

`SherryExecutor.install` 在 Runtime 启动时调 `assignment_interrupted` 重放：

```python
def resume(self):
    """Runtime startup：报告 interrupted Assignments + 重排 pending launches。
    oldest Ticket first。"""
    for ticket in self.host.engine.tickets.open_tickets_oldest_first():
        assignment = latest_assignment(ticket)
        if assignment.status == RUNNING:
            # 中断中的 Assignment：起 recovery note 新 Assignment
            self.host.engine.assignment_interrupted(ticket.id, assignment.id)
        elif assignment.status == PENDING and not waits_for_person(ticket):
            enqueue("launch", ticket.id, ...)  # 重排
```

recovery note 内容：检查目录、git history、GitHub 状态，再决定是否重复外部动作。

restart 后 Session 持久化：subagent 的 checkpoint 在 Sherry `src/data/sessions/`，superagent 进程重启不丢（Sherry 后端没重启）。这是**旁进程部署的优势**——superagent 重启时 Executor Session 还在 Sherry 那边活着。

## 9. Pull-request 跟随与结束

复刻 003-execution.md §Pull requests：

- Ticket 报告它开了 PR → `claim_pull` → `bind_thread` own=True。
- 起源 PR（pull request 起的 Ticket）→ `observe_pull` → bind own=False。
- reopened PR 重跟。
- Ticket done = 所有跟随 PR 关闭（own 的全关，或非 own 的全关）。
- close 不在 resumed/retried/recovered 未完成时 end。
- Chat Channel 收一条最终 outcome notice；GitHub 自己显示。
- 删 Subscription → 停止跟随。

## 10. 自动合并（Delivery requirements prompt 管）

- 默认：Executor 不以 agent 账号开 auto-merge（GitHub 归因到开启者）。
- Delivery requirements 必须命名**另一个账号的 CLI** 来合并；否则人合并。
- 默认允许 auto-merge 的条件：Ticket 自己的 PR + ready + 必要 approval 覆盖 head commit + additions+deletions <200 行。
- 推新 revision 前撤 auto-merge。
- 这条规则在 prompt（`prompts/delivery.md`），不在代码。代码只 enforce 工具不能改 profile。

## 11. stateDir 与 setup

### 11.1 worker-bin shims（`superagent/infra/environment.py`）

```
<stateDir>/worker-bin/
├── gh              # → exec agent gh wrapper，移 PATH 自身
├── lark-cli        # → exec bot lark wrapper
└── <blocked cmd>   # 失败退出
```

setup 写；非 setup 写的文件阻止启动。

### 11.2 worker-gitconfig（`superagent/infra/environment.py`）

```
<stateDir>/worker-gitconfig/
└── config          # 替换 global+system git config
```

内容：

- `credential.helper` = agent gh CLI（github.com）。
- SSH→HTTPS 重写。
- LFS filter。
- 不设 commit identity。
- 不提示凭据。
- 删 `GH_TOKEN`/`GITHUB_TOKEN`。

### 11.3 setup（`superagent/app/setup.py`）

探活（用 subprocess service，剥 KEY/PASSWORD/SECRET/TOKEN env）：

1. GitHub repo + agent 推送权（`gh repo view` + `gh repo edit --test-push`）。
2. 写 worker env（shims + gitconfig）。
3. 检查 git 只用 agent 凭据（`git config --list` 无其他 github.com 凭据）。
4. Feishu setup 失败仅禁 Feishu Source。

setup 失败 → Overview 显示 + 链到设置；启动拒绝直到 setup 成。

### 11.4 凭据隔离

- worker 可执行绝对路径程序、读 operator home —— **非安全边界**。
- 强隔离：专用 OS 账号只装 agent 凭据。
- worker-bin/worker-gitconfig 内非 setup 写的文件 → 启动失败。
- Ticket 目录在含其他凭据的 git repo → Ticket 失败。

## 12. Concurrency

- `max_workers` 配置（默认 1）。
- launch/wake 占正常槽；人消息/status 请求可用额外槽（wake 优先）。
- CI 等 Input 等正常槽。
- launch 并发：slow prepare 只延迟自己 Ticket。
- pause：停 dispatch + new Eval + Eval timeout + Input delivery + reply；running Executor turn 不打断；Sources 仍记 Trigger。

## 13. 与 Sherry subagent 的细节差异

| 原型 Harness Session | Sherry subagent | 处理 |
|---|---|---|
| preset（dsh-superagent-worker） | functional_role + tool 集 | `_spawn_kwargs` 裁剪 |
| preset（dsh-superagent-router） | 主 agent 受限工具子集 | main Session 用一个独立 subagent |
| shell 5s 转后台 | Sherry terminal 工具的 background job | 复用 Sherry terminal 的 bg 机制 |
| job_output/job_list/job_kill | Sherry terminal 的 job 接口 | 经 hooks 调 |
| subagent（Harness standard） | Sherry subagent | 直接复用 |
| inbox（消息注入 turn） | Sherry state_port.add_messages | 经 hooks |
| turn event（user/message 等） | Sherry trajectory_events + completion | hooks 转发 |

详见 `12-sherry-integration.md`。

## 14. 该册验收门

- `SherryExecutor` + `WorkerSlots` + `BackgroundWork` 实现 §2-§4。
- prepare 命令成功/失败/超时三态单测（mock subprocess）。
- `WorkerSlots`：满载 launch throw `Later`；wake 用额外槽；wake 优先。
- 后台作业：turn 结束有 job → keep slot；30 分钟杀 shell 留 subagent；handoff 释放 slot。
- Restart resume：interrupted Assignment 报告 + recovery note；pending launch 重排；oldest first。
- PR 跟随：claim/observe/reopen/close/end 全路径单测。
- 执行环境变量：所有 `DSH_SUPERAGENT_*` 注入 worker env，无凭据。
- setup：探活成功/失败/Feishu-only-fail 三态；worker-bin/worker-gitconfig 写正确。
- transient 重试：3 次 5s→300s 后真正失败。
- `tests/superagent/executor/test_sherry.py` 镜像 `tests/runtime.spec.ts` + `tests/background.spec.ts`。
- mock `runtime/hooks`：不真起 Sherry subagent，spy spawn/cancel 调用。
