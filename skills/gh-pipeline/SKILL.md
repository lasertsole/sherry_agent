---
name: gh-pipeline
description: 主会话编排 GitHub issue 修复管线,从远端仓库解析到 issue 拉取、worker 分支派发、PR 创建的双路径协议,gh CLI 与 REST v3 互为降级
scope: main_only
---

# gh-pipeline:GitHub issue 修复管线编排协议

本技能只由主会话使用(orchestrator),编排从 GitHub issue 到 PR 的完整修复管线。子代理不可见(scope: main_only)。

与 openclaw 的差异:本移植以 gh CLI 为主路径,REST v3 作为文档化兜底,双路径互为降级;token 全程只经环境变量进入内存,绝不落盘。

## 1. 适用与前置

- 适用:主会话拿到一批 GitHub issue(按编号、标签或里程碑圈定),需走远端拉取、worker 修复、PR 创建全流程。
- 环境准备 Path A(gh CLI 已装或可装):`winget install GitHub.cli`,随后 `gh auth login` 完成鉴权,再以 `gh auth status` 确认登录态。
- 环境准备 Path B(gh 不可用):走 REST v3 兜底;鉴权经 `GH_TOKEN` 环境变量注入,匿名访问仅限公开仓。

## 2. Phase 1:仓库身份解析

- 执行 `git remote get-url origin` 取远端 URL。
- 两种规范形态共用同一条正则解析出 <owner> 与 <repo>:
  - `https://github.com/<owner>/<repo>.git`
  - `git@github.com:<owner>/<repo>.git`
- 解析结果非 GitHub 远端时直接终止管线并报告用户。

## 3. Phase 2:issue 拉取(双路径)

gh 路径:

```text
gh issue list --repo <owner>/<repo> --label <label> --limit <N> --state open --json number,title,body
```

- 按里程碑圈定时追加 `--milestone <number>`,可与 --label 组合。
- issues 端点同时返回 PR 条目:过滤掉 pull_request 字段非空的条目,只保留真 issue。

REST 兜底路径:

```text
GET /repos/{owner}/{repo}/issues?labels=<label>&per_page=<N>&state=open
```

- 匿名限流 60 req/h(按 IP 计),小批量够用;分页经 per_page 翻页,同样过滤 pull_request 非空条目。

## 4. Phase 3:worker 派发

每 issue 一次 sessions_spawn,spawn prompt 按下列模板填充:

```text
你是 gh-pipeline worker,负责修复一个 issue。
输入(issue 三要素,主会话派发时填充):
- issue 编号:<number>
- issue 标题:<title>
- issue 正文:<body>
分支:从 main 切出 gh-pipeline/<issue-number>,所有提交只落该分支,禁止推送共享远端。
工具边界:
- tool_allow:read,write,patch,python_repl,terminal
- tool_deny:subagent/spawn 类工具与任何直接建 PR 的写远端操作
回报:完成后经 announce 回流主会话,必含分支名、变更摘要、自验命令及输出。
```

- worker 结果经 announce 管线回流;回报缺自验命令输出的条目视为未完成,不进入 Phase 4(门禁)。

## 5. Phase 4:PR 创建(双路径)

gh 路径(每 issue 一条):

```text
gh pr create --repo <owner>/<repo> --head gh-pipeline/<issue-number> --base main
```

REST 兜底:POST /repos/{owner}/{repo}/pulls,必须携带 token(经 GH_TOKEN 注入 Authorization 头),body 含 title/head/base 三字段。

- PR 描述引用 issue 编号与变更摘要;head 必须是 Phase 3 产出的 gh-pipeline/<issue-number> 分支。

## 6. 执行参数

- `--dry-run`:零远程写,只解析仓库身份、列出待拉 issue 与待建 PR 计划,不 spawn、不建分支、不建 PR。
- `--yes`:跳过确认;默认主会话在 Phase 3 派发前展示批次计划等待用户确认,加 `--yes` 直接执行。

## 7. 降级表

| 场景 | 执行路径 |
| --- | --- |
| 公开仓,小批量 | REST v3 匿名即可:60 req/h 配额够拉取小批 issue;建 PR 仍需 gh 或 token |
| 私有仓 | gh CLI 或 token 必选,匿名 REST 对私有仓一律无效 |
| 无 gh 且无 token | 只出分支:worker 产出 gh-pipeline/<issue-number> 分支并回报,PR 手工由用户创建 |

## 8. 风险与回滚

- token 安全:token 只经环境变量进入内存,绝不落盘,不写入台账、日志或 spawn prompt。
- 禁止把 token 拼进命令行参数或经 echo 打印,回报与日志中一律脱敏。
- 限流处理:REST 返回 403/429 时整批暂停,不重试,确认配额恢复后续跑;连续两次限流则中止本轮。
- REST v3 端点与参数以 2026-08 查询的 GitHub REST API 文档为准,接口变更时先核对再执行。
- 回滚:删除 skills/gh-pipeline 目录即卸载本协议;已产出的 gh-pipeline/<issue-number> 分支经 git branch -D 删除。
