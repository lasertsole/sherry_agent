# 14 非目标、研究原型警告、未来工作

## 1. 不复刻的部分

### 1.1 DeepSeek Harness 协议本身

原型是 Harness 插件，依赖 Harness 的 Session API、preset、inbox、plugin lifecycle、Plugin Manager。Python 版**不复刻 Harness 协议**——用 Sherry 的 subagent + hooks 替代（见 `12-sherry-integration.md`）。具体不复刻：

- Harness Session 的 wire 协议、event shape、preset 注册机制。
- `cordis.patch.yml` 的 plugin insert 机制（Python 版用 `superagent.jsonc` + 直接进程入口）。
- Harness Plugin Manager 的 Deployment 页（Python 版面板自带 Deployment tab）。
- `dsh-superagent-worker`/`dsh-superagent-router` preset 作为 Harness 一等公民（Python 版用 Sherry subagent 的 functional_role + tool 集）。

### 1.2 React/TSX 面板

原型 `src/client/` 是 React + TSX 嵌入 Harness sidebar。Python 版**不引入 React 工具链**——用 Sherry Nuxt 4 + XUI 组件（见 `11-panel-http.md §2`）。降级方案：静态 HTML + vanilla JS MVP。

### 1.3 GitHub Enterprise Server

原型明确不支持（`006-deployment.md`："GitHub Enterprise Server is not supported"）。Python 版同样不支持——GitHub 源走 `gh` CLI 的 github.com 行为。

### 1.4 跨 Source 身份映射

原型明确拒绝（`001-domain.md §Rejected concepts`："Principal or Membership as an entity, or mapping identities across Sources"）。一个人在多 Source 的账号映射是 **prompt 文本**（team `identities` skill），不是域概念。Python 版同样。

### 1.5 跨天元目标树

原型没有（Ticket 是 per-repository、per-Ticket 的，无"本周要完成的三件事"元目标）。Python 版同样不做——留作未来工作（见 §3）。

### 1.6 存储清理（第一版）

原型明确："Nothing deletes old records yet, so these grow without bound: the `trigger`, `disposition` and `decision` rows; ended Tickets and their Threads; the outbox's receipts; and in `kv`, every saved settings revision and prompt version..."。Python 版第一版**同样不删**——P2 再加保留策略（见 §3）。

### 1.7 自动 reviewer 识别

原型：automated reviewer 用普通账号算"人"，靠 team prompt 文本识别（`004-prompts.md`）。Python 版同样不做自动识别。

## 2. 研究原型警告（继承自原型 README）

原型 README 开篇：

> Research prototype for reference only, not an official DeepSeek product. This code grew out of our team's day-to-day development work, with much of the engineering done by coding agents. Treat it as research code, not production-ready software. It may still contain bugs, and its coding style and engineering practices may be inconsistent. Review and adapt it before using it in your own environment.

> We have adapted it for publication, so it is not identical to our internal version or the implementation described in *Exploring Super-agent Design: Towards a More Autonomous Agent System*. Some designs discussed in the article are directions for future development and are not implemented in this prototype.

Python 版**继承全部警告**：

- **研究代码，非生产就绪**。Python 复刻品同样可能含 bug、风格不一致。
- **可能与论文描述不一致**。论文里某些设计是 future direction，原型未实现，Python 版也不实现。
- **先 review 再用**。落地后必须在自己的环境 review + adapt。
- **agent 可犯错**。superagent 在 agent 账号下评论/开 PR/消息人，每条消息占同事注意力，operator 负责（000 §Responsibility）。
- **start narrow**：短允许列表 + proactive 仅记录；扩宽前看"agent 会做的事"对不对。
- **不产 volume**：批量 PR/广播/忙活给团队的成本高于价值。
- **own what it says**：读 agent 以你名义发的，错则在 prompt 修。

### 2.1 Python 版额外风险

- **Sherry 集成是新增**：原型用 Harness（成熟），Python 版用 Sherry subagent（复杂系统）。hooks 接缝是新代码，可能有竞态/状态不一致。E2E 测试是必须的。
- **旁进程部署新**：原型同进程，Python 版旁进程。进程间通信失败模式要覆盖（`12-sherry-integration.md §10.3`）。
- **Windows**：Sherry 跑 win32（AGENTS.md environment），superagent 的 worker shell 需要 POSIX（git worktree）。prepare 命令用 Git Bash。Windows 路径/quoting 要测。
- **不强制 LLM 质量**：superagent 编排决定"持续在对的地方用智力"，智力天花板仍由 Sherry 的模型决定。差模型 + superagent = 持续跑偏。

## 3. 未来工作（P2+）

### 3.1 存储清理

- `trigger`/`disposition`/`decision`/ended Ticket/Threads/outbox receipts 的保留 + 清理。
- 按 age + 活跃度（原型 facts overflow 的 least-active 思路）。
- 备份 + 归档（不直接删）。

### 3.2 跨天元目标树

- "本周要完成的三件事"级别的元目标，驱动 Ticket 优先级。
- 元目标持久化 + 主动唤醒（不依赖 Source Trigger）。
- 这是真正 L7→L8 的台阶（见之前讨论）。

### 3.3 多仓库

- 原型一 repo。Python 版可扩多 repo（每 repo 一 Source instance）。
- 跨 repo 的 Ticket 关联（同 feature 涉及多 repo）。

### 3.4 更多 Source

- webhook 模板已就绪（`05-sources.md §7`）。
- Slack/Discord/邮件/Jira 等。
- CI 系统（Jenkins/CI 通用 webhook）。

### 3.5 部署隔离

- 原型 `007 §Open questions`：agent profile 独立 pin 在自己的 Harness 安装上，升级 Harness 别处不影响。
- Python 版等价：superagent 进程的 Python 环境/依赖独立 pin，Sherry 升级不影响。

### 3.6 信任模型细化

- 原型 `007 §Open questions`："which Trigger fields are trusted, such as the Source's actor and Thread membership, and which are not, such as the body and quotes."
- Python 版第一版照原型（actor/Thread 信，body/quotes 不信）；P2 细化。

### 3.7 Sherry effects 二层整合

- superagent 自己有 Effects + restart replay。但与 Sherry 的 `runtime/effects/` 二层整合可双保险（superagent 进程重启时 Sherry 那边也保活）。
- P2 评估是否启用。

### 3.8 面板高级搜索

- regex、跨 Session 搜索、tool args/results 搜索（Sherry `use-conversation-find.ts` 拒绝的那些）。
- full-text `<mark>` 高亮。

## 4. 与 Sherry 现有功能的关系

| Sherry 现有 | superagent 复刻后 |
|---|---|
| `max_continuation_turns=15` | 不删，但 worker 跑满报 waiting → Ticket idle → Trigger 唤醒新 Assignment。续跑 cap 变成"单 session 上限"而非"整体上限"。 |
| HITL per-action | 保留（worker 内仍 HITL），但 superagent 的 per-ticket 准入前移 + attention 机制升级。 |
| TaskFlow | 复用，Ticket 映射 flow，finish gate 复用。 |
| subagent system | 复用，Executor = subagent run。 |
| evidence ledger + 回归门 | 复用，作为 ready Report 前置。 |
| `runtime/hooks.py` | 扩展（加 SuperAgentHooks）。 |
| `state_port.py` | 复用（read/truncate/heal）。 |
| `trajectory_events.py` | 复用（read_trajectory）。 |
| Sherry 主后端重启周期 | 不变（superagent 旁进程独立重启）。 |
| Sherry 桌面打包 | 不影响（superagent 是独立进程，桌面打包可选包含）。 |

## 5. 该册验收门

- 非目标清单（§1）逐项在代码里 enforce（如：无 Harness import、无 React、无 GHE 路径）。
- 研究原型警告（§2）写入 `wiki/super-agent/README.md` 头部 + `superagent/__init__.py` docstring。
- Python 版额外风险（§2.1）写入 `wiki/super-agent/` 的部署文档。
- 未来工作（§3）记在 `wiki/super-agent/roadmap.md`，不阻塞第一版验收。
- 与 Sherry 现有功能关系（§4）在 `wiki/super-agent/integration.md` 说明。
