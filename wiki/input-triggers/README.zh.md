# 输入框触发器

[English](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> 聊天客户端的一部分：`$` 选择技能，`/` 选择自定义或内置命令，`+` 按钮打开上传与选择器菜单。输入框仍是 contenteditable + 原子 chip，不引入编辑器框架。

## 触发器一览

| 触发 | 数据来源 | 选中结果 |
|---|---|---|
| `$` | `GET /skills`（active 且主 Agent 可见） | 插入原子 `$skillName` chip；`extractTextForSend` 发送 `$skillName` |
| `/` | `GET /commands?session_id=…` 的自定义命令 + 既有命令注册表 | 自定义命令插入携带展开正文的原子 chip；内置命令立即执行 |
| `@` | 既有文件 mention（拖拽、文件树“加入对话”） | 既有 `@path` chip 不变 |
| `+` | 输入框工具栏 | 图片/音频/视频上传、通用文件选择器、技能选择、命令选择 |

## `$` 技能

`use-skills-list.ts` 缓存 `GET /skills`，只保留 `active !== false` 且 `scope` 不是 `subagent_only` 的技能。`use-input-trigger.ts` 只在输入开头或空格后识别 `$`（以及中文 `¥` / `￥` 别名），再按前缀、子串和描述过滤。选中后把已输入的触发文本替换为 `skill-mention-chip`，显示 `$name`，发送文本就是 `$name`。

系统提示词包含稳定的 `## Skill References` 段：用户消息出现 `$name` 时，Agent 先用 `skill_view` 加载该技能再执行。

## `/` 命令

自定义命令是 Markdown 文件：用户范围在 `APP_HOME/commands/`，项目范围在 `<project>/.sherry/commands/`（同名时项目覆盖用户）。`server/service/custom_commands_service.py` 读取 frontmatter 的 `description` / `argument-hint` 和正文，`GET /commands?session_id=…` 返回列表。内置命令来自 `use-command-registry.ts`，与 Ctrl+K 共用同一个注册表。

带 `argument-hint` 的自定义命令会在面板里先显示参数输入框。选中后生成 `command-mention-chip`，`data-command-body` 保存把 `$ARGUMENTS` 替换后的正文；`extractTextForSend` 发送正文而不是可见的 `/name`。内置命令执行注册表 handler，不插入 chip。

## `+` 菜单与 `@` 文件 mention

`+` 按钮是隐藏触发字符 `$` / `/` 的可发现入口：各行分别打开图片/音频/视频上传、通用文件选择器、技能选择器和命令选择器。文件引用仍沿用既有的原子 `@path` chip（拖拽或文件树“加入对话”产生）。

## 自定义命令格式

```markdown
---
description: Review the current change
argument-hint: [area]
---
Review the current git diff. Focus on: $ARGUMENTS
```

命令名就是文件名（嵌套目录用 `:` 连接），`description` 必填，`$ARGUMENTS` 只做纯文本替换；不支持 shell 展开或 `!command`。

## 安全与测试

触发字符必须位于词边界，粘贴的 `<pre>` / `<code>` 内容内不触发，`contenteditable=false` 的 chip 内也不会再次触发。退格删除 query；删掉触发字符即关闭面板。测试：`client/app/composables/__tests__/use-input-trigger.test.ts`、输入框集成测试，以及 `tests/server/service/test_custom_commands_service.py`。
