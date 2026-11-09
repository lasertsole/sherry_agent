# 15 分阶段交付里程碑

12 个分册的工程量约等于原型 `src/`（约 50 个 TS 模块 + 50 个 spec）的 Python 移植 + Sherry 集成层。建议分 5 期交付，每期有可验收的里程碑。

## 1. 里程碑总览

| 期 | 名称 | 周期估计 | 验收门 |
|---|---|---|---|
| **P-Spike** | **Sherry 接缝验证** | **3 天** | **3 个承重假设验证通过** |
| P0 | 骨架 + 域 + Store | 1.5 周 | Engine 在内存仓库跑通域规则 |
| P1 | Engine 五部件 + Effects + Policy | 2 周 | Ticket 全生命周期 on simulated host |
| P2 | Sources（GitHub）+ Executor + Sherry 接缝 | 2.5 周 | GitHub issue→Ticket→PR 单链路（mock CI） |
| P3 | Feishu + 面板 + HTTP API + Prompts | 2.5 周 | 面板可视 + Settings 可改 + Feishu mention→Ticket |
| P4 | 部署 + service 脚本 + E2E + 文档 | 1.5 周 | 真实仓库 E2E + 四语言 README + service 脚本 |

总计约 10 周 + 3 天 spike（全职单人体；多人并行可压到 6-7 周）。**P-Spike 必须先做**——验证不过 P2 返工。每期详细如下。

## 1b. P-Spike Sherry 接缝验证（3 天，开工前必做）

### 交付

- `tests/superagent/test_spike_completion_resets_latch.py`：Sherry main session spawn subagent → subagent 完成 → 观察 main session 续跑 latch 重置。
- `tests/superagent/test_spike_mcp_tool.py`：最小 MCP server → Sherry 配 `mcp.servers.test` → subagent 调 MCP 工具。
- `tests/superagent/test_spike_http_loopback.py`：Sherry `/internal/test` 路由 + 双向 HTTP。

### 验收门

- spike 1：subagent 完成**重置** main session 续跑 latch（AGENTS.md 既定行为验证）。不重置 → 整个 L7 模型返工，停。
- spike 2：subagent 能调到 MCP 工具。调不到 → 评估 SSE 或退到改 `build_main_tools()`（侵入 Sherry）。
- spike 3：HTTP loopback 双向通。

**失败处理**：任一 spike 不过，回到 `12-sherry-integration.md` 重新设计集成层。不盲开 P2。

## 2. P0 骨架 + 域 + Store（1.5 周）

### 交付

- `superagent/` 包结构（`01-architecture.md §1`）。
- `superagent/core/model.py`（`02-domain-model.md` 全 enum + dataclass）。
- `superagent/core/ids.py` + `reasons.py` + `errors.py` + `stage.py`。
- `superagent/core/memory.py`（内存仓库）。
- `superagent/app/store.py`（SQLite schema + Repository + KV + 迁移）。
- `superagent/kv.py`。
- `lint-imports` layered contract + `basedpyright` + `ruff` 通过。
- `tests/superagent/core/test_memory.py` + `test_store.py` + `test_stage.py`。

### 验收门

- `uv run --no-sync basedpyright superagent/` 无 error。
- `uv run --no-sync lint-imports` 通过。
- `uv run pytest tests/superagent/core tests/superagent/test_store.py -q` 全绿。
- `MemoryRepositories` 与 SQLite Store 行为一致（同序同嵌套回滚）。
- schema 11 建表 + 生成列 + 索引正确；schema 10→11 升级通过。
- 目录锁：第二进程开同 stateDir 拒绝。

## 3. P1 Engine 五部件 + Effects + Policy（2 周）

### 交付

- `superagent/core/ports.py` + `engine.py` + `part.py` + `intake.py` + `evals.py` + `tickets.py` + `assignments.py` + `replies.py`。
- `superagent/app/effects.py` + `outbox.py` + `passes.py` + `notices.py` + `cache.py` + `health.py`（infra）。
- `superagent/policy.py` + `admission.py` + `limits.py`。
- mock `Executor`/`Assessor`/`Outbox`/`SourceScope`（stand-in ports，不真调 Sherry/Source）。
- `tests/superagent/core/test_engine.py`（镜像 `core-engine.spec.ts`）+ `test_policy.py` + `test_effects.py` + `test_outbox.py` + `test_passes.py` + `test_health.py` + `test_followups.py`。

### 验收门

- Engine 五部件全命令实现；事务模型（commit 后 after_commit）。
- Ticket 全生命周期 on simulated host：Trigger→Eval→accept→Assignment→Report ready→Ticket end。
- Policy：gate/revalidate/admission 三 cell/accept mode 三态。
- Effects 六种 + coalescing + 退避 + Later。
- Outbox 排序 + gone + idempotency。
- `uv run pytest tests/superagent -q -k "not e2e"` 全绿（P0+P1 用例）。

## 4. P2 Sources（GitHub）+ Executor + Sherry 接缝（2.5 周）

### 交付

- `superagent/sources/kind.py` + `registry.py` + `host.py` + `poll.py`。
- `superagent/sources/github*.py`（5 文件）+ `admission_context.py`。
- `superagent/executor/sherry.py`（`SherryExecutor` + `WorkerSlots`）+ `background.py`。
- `superagent/app/sessions.py`（Sessions，先 mock hooks）+ `assessment.py`（MasterAssessor）+ `role_models.py` + `delivery.py` + `retry_health.py`。
- `superagent/app/tools/main.py` + `executor.py` + `tool.py` + `resources.py` + `cards.py` + `links.py` + `context.py` + `skill_files.py` + `source_context.py` + `execution_environment.py` + `setup.py`（app）。
- `superagent/infra/commands.py` + `environment.py` + `paths.py` + `text.py`。
- `superagent/deployment.py` + `setup.py`（top）+ `navigation.py`。
- `runtime/hooks.py`（Sherry 侧）加 `SuperAgentHooks`。
- Sherry `/internal/superagent/*` loopback 路由。
- `tests/superagent/sources/test_github*.py` + `test_registry.py` + `test_admission_context.py` + `test_commands.py` + `executor/test_sherry.py` + `test_background.py` + `test_runtime_concurrency.py` + `test_assessment.py` + `test_sherry_integration.py`（mock Sherry HTTP）。
- Sherry 侧 `tests/server/test_superagent_hooks.py`。

### 验收门

- GitHub 源：通知轮询 + Trigger 翻译 + PR 跟随 + Channel send 三态。
- `SherryExecutor`：launch/deliver/stop；WorkerSlots；后台作业；restart resume；transient 重试。
- Sherry hooks：spawn/cancel/add_messages/truncate/heal/read_state/list_jobs/kill_job/read_trajectory 全 loopback 端点。
- Ticket↔flow 映射；ready 前 finish gate。
- HITL fail-closed → blocked + Attention。
- 长时无人：worker 跑满续跑 cap 报 waiting → Ticket idle → Trigger 唤醒新 Assignment（mock CI Trigger）。
- GitHub issue→Ticket→PR 单链路（mock CI，mock Sherry subagent）。

## 5. P3 Feishu + 面板 + HTTP API + Prompts（2.5 周）

### 交付

- `superagent/sources/feishu*.py`（6 文件）+ `feishu_image.py`。
- `superagent/app/prompts.py` + `versions.py` + `settings.py` + `settings_history.py`。
- `superagent/prompts/`（内置 prompt 文本，从原型移植）+ `skills/`。
- `superagent/panel/http.py` + `api.py` + `views/*.py`。
- Sherry Nuxt `/superagent` 页面（或静态 HTML MVP）。
- `superagent/panel/locales.py`（zh+en）。
- `tests/superagent/sources/test_feishu*.py` + `test_prompts.py` + `test_cards.py` + `test_model_surface.py` + `test_settings.py` + `test_views.py` + `test_timeline.py` + `test_migration.py` + `test_http.py` + `panel/test_panel_*.py` + `test_draft.py` + `test_prompt_config.py` + `test_markdown.py` + `test_marks.py` + `test_locales.py` + `test_styles.py` + `test_navigation.py`。

### 验收门

- Feishu 源：live 事件 + 历史恢复 + 订阅话题轮询 + 图片 + chat 词限。
- Prompts：team sections 4 + built-in parts 全 + 生成模板 + 装配顺序 + 优先级。
- HTTP API 全路由 + 守卫。
- 面板 6 Tabs + Settings 5 组 + 搜索 + i18n。
- Feishu mention→Ticket 单链路。
- 面板可视：Overview/Source/Trigger/Eval/Execution/Settings 全可读 + 可改设置。

## 6. P4 部署 + service 脚本 + E2E + 文档（1.5 周）

### 交付

- `superagent/app/setup.py` 完整 setup 探活 + worker-bin/worker-gitconfig。
- `superagent/__main__.py`（进程入口）+ `superagent/index.py`（startup/shutdown）。
- `scripts/superagent`（service 脚本 start/stop/restart/status，tmux，退出码 0/1/2/3）。
- `superagent.jsonc` 配置 + Sherry `sherry.jsonc` 加 `superagent` 段。
- hourly check cron prompt（原型 `006 §Hourly check`）。
- `wiki/super-agent/` 四语言 README（en/zh/ja/ko）+ `roadmap.md` + `integration.md` + `deployment.md`。
- `tests/superagent/test_runtime.py`（whole app on simulated host）+ `test_service_script.py` + `test_deployment.py` + `test_environment.py` + `test_index.py` + `test_class_docs.py` + `test_docs.py` + `test_e2e.py`（`llm_e2e`）。
- `scripts/check_docs_parity.py` 覆盖 `wiki/super-agent/`。

### 验收门

- setup：探活 + worker env + gitconfig。
- service 脚本：start/stop/restart/status + 退出码 + tmux + 拒重复实例。
- 真实 GitHub 测试仓库 E2E：issue→Ticket→worktree→PR→CI（真 CI）→修→merge→Ticket `merged`。
- 重启韧性：杀进程重启，running Assignment recovery note；idle Ticket 不丢；outbox 待发续投。
- 四语言 README parity 通过。
- 文档 gate：`check_docs_parity.py` + `check_doc_links.py` + `check_doc_language.py` 覆盖 `wiki/super-agent/`。
- `test_class_docs.py`：每类 docstring。
- `test_docs.py`：编号文档链 + 模块表/测试表完整。
- 总验收门（README §验收标准）：`basedpyright` + `ruff` + `pytest tests/superagent -q` + E2E + 重启韧性 + 四语言文档 全过。

## 7. service 脚本（`scripts/superagent`）

```bash
#!/usr/bin/env bash
# scripts/superagent — start/stop/restart/status
set -euo pipefail
PROFILE="${DSH_SUPERAGENT_PROFILE:-superagent}"
CHECKOUT="${DSH_SUPERAGENT_CHECKOUT:-$(pwd)}"
SESSION="dsh-superagent-$PROFILE"

cmd_start() {
  if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "already running"; exit 1
  fi
  tmux new-session -d -s "$SESSION" \
    "cd $CHECKOUT && uv run python -m superagent 2>&1 | tee -a .artifacts/$SESSION.log"
  # 等 ready（最多 60s）
  for i in $(seq 1 60); do
    if curl -sf http://127.0.0.1:${PORT:-3082}/orchestrator/api/state >/dev/null; then
      echo "ready"; exit 0
    fi
    sleep 1
  done
  echo "did not come up"; exit 1
}

cmd_status() {
  if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "not running"; exit 1
  fi
  state=$(curl -sf http://127.0.0.1:${PORT:-3082}/orchestrator/api/state || echo '{}')
  # 解析 setup/health → 退出码 0/2/3
  ...
}

cmd_stop() { tmux send-keys -t "$SESSION" C-c; ... }
cmd_restart() { cmd_stop; cmd_start; }

case "${1:-}" in
  start) cmd_start ;;
  stop) cmd_stop ;;
  restart) cmd_restart ;;
  status) cmd_status ;;
  *) echo "usage: $0 {start|stop|restart|status}"; exit 1 ;;
esac
```

退出码：0 正常 / 1 不运行或 runtime error / 2 部分 error（非 runtime）/ 3 等 setup。

## 8. hourly check cron

在另一个 Sherry profile（不是 superagent）跑：

```text
Check superagent: run `scripts/superagent --profile superagent status` in <checkout>.
Exit 0: do nothing and say nothing.
Exit 2: do not restart; tell me each part under `errors` with its detail, unless already told.
Exit 3: do not restart; tell me what `setup` says setup needs, unless already told.
If it reports manually_stopped: true, leave it stopped.
Otherwise (exit 1) run `scripts/superagent --profile superagent --respect-stop restart`,
which exits the same way: 0 means recovered, say nothing; 2 and 3 reported as above;
if it exits 1 or fails otherwise, read the newest .artifacts/superagent-*.log and the
Troubleshooting section of wiki/super-agent/deployment.md, fix what you can, and tell me.
```

`--respect-stop` 保 stop marker；只有 `stop` 写 marker，`start` 清，`restart` 不写。

## 9. 风险与回退

| 风险 | 回退 |
|---|---|
| **Sherry hooks 接缝竞态** | P-Spike 先验证承重假设（completion 重置 latch / MCP 工具 / HTTP 双向），过才开 P2 |
| **workers 不能无父 spawn**（已修正） | 架构改为 workers 是 superagent main session 子代理；`12-sherry-integration.md §4` 已改。spike 1 验证 |
| **`orchestrator_*` 工具注入 Sherry**（已修正） | 走 MCP 暴露，不侵入 `build_main_tools()`。spike 2 验证；失败退 SSE 或改工具表 |
| **Worker Session 持久性**（已决策） | MVP 选 A（每次 wake 新 subagent）；P2 评估 B（持久 session）。`06-executor.md §4` + `12-sherry-integration.md §4.2` |
| **Store 生成列无效**（已修正） | 显式列替代 JSON path 生成列。`04-store.md §2` 已改 |
| **Prompt 不是翻译是重写**（已修正） | `10-sessions-prompts.md §10` 加重写指引；工作量比初估大，P3 周期可能延长 |
| Sherry subagent 行为与 Harness Session 差异大 | P-Spike + P2 早期持续验证；差异大则调整映射 |
| GitHub CLI 行为变化 | `test_commands.py` mock CLI 输出；真实 `gh` 在 E2E 验 |
| Windows worktree | P4 在 Windows 跑一次 E2E；Git Bash 兜底；prepare 用 `bash -c` 非 `/bin/sh` |
| 面板工程量超预期 | 降级到静态 HTML MVP（Overview + Settings），P2+ 迁 Nuxt |
| LLM 成本（E2E 真调用） | E2E 标 `llm_e2e`，CI 不跑；验收时手动跑一次 |
| **Sherry 挂了 superagent 停摆**（已修正） | degraded 模式（只入队 Trigger）；`12-sherry-integration.md §10` 已改。不是"worker spawn 失败" |

## 10. 该册验收门

- 5 期里程碑各验收门可独立检查。
- P4 结束满足 README §验收标准总门。
- 风险有回退方案。
- service 脚本 + hourly check 可用。
- 文档 gate 通过。

## 11. 开工前最后清单

- [ ] **P-Spike 通过**（3 个承重假设验证：completion 重置 latch / MCP 工具 / HTTP 双向）。
- [ ] 确认 `superagent/` 包根（与 `agent/` 平级）。
- [ ] 确认旁进程部署 + HTTP loopback 双向 + MCP stdio。
- [ ] 确认 workers 是 superagent main session 子代理（非孤儿）。
- [ ] 确认 `orchestrator_*` 工具走 MCP（不侵入 `build_main_tools()`）。
- [ ] 确认 Assignment 模型选 A（每次 wake 新 subagent）。
- [ ] 确认 Sherry 侧只加 `server/trigger/http/superagent.py` + 配置（核心零改动）。
- [ ] 确认 GitHub 测试仓库 + agent 账号 + `gh` wrapper 已备。
- [ ] 确认 Feishu bot（可选，P3 才需要）。
- [ ] 确认 `pyproject.toml` 加 pydantic/aiohttp/mcp 依赖不冲突。
- [ ] 确认 `lint-imports` contract 更新（含 `superagent.mcp` 层）。
- [ ] 读一遍原型 `docs/000-008` + `07-architecture.md`（实施者必读）。
- [ ] 读一遍 Sherry `AGENTS.md` 的 subagent/taskflow/state_port/continuation 段。
- [ ] 读一遍 `REVISIONS.md` 了解初稿后的修正。
