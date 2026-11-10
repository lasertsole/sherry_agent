# Composer Input Triggers

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> Part of the chat client: `$` selects a skill, `/` selects a custom or built-in command, and the `+` button opens the upload + picker menu. The composer stays a plain contenteditable with atomic chips; no editor framework is involved.

## Trigger map

| Trigger | Source | Selection result |
|---|---|---|
| `$` | `GET /skills` (active, main-visible skills) | Insert an atomic `$skillName` chip; `extractTextForSend` emits `$skillName` |
| `/` | Custom commands from `GET /commands?session_id=…`, plus the existing command registry | Custom command inserts an atomic chip carrying its expanded prompt body; a built-in executes immediately |
| `@` | Existing file-mention flow (drag, tree "Add to chat") | The existing `@path` chip is unchanged |
| `+` | Composer toolbar | Image / audio / video upload, generic file picker, skill picker, command picker |

## `$` skills

`use-skills-list.ts` caches `GET /skills` and keeps only `active !== false` skills whose `scope` is not `subagent_only`. `use-input-trigger.ts` detects `$` (and the Chinese `¥` / `￥` aliases) only at the start of the input or after whitespace, then filters by prefix, substring and description. Selecting a row replaces the typed token with a `skill-mention-chip`; the visible label is `$name`, and the sent text is exactly `$name`.

The system prompt carries a stable `## Skill References` block: when a user message contains `$name`, the agent loads that skill with `skill_view` before doing the work.

## `/` commands

Custom commands are Markdown files under `APP_HOME/commands/` (user scope) and `<project>/.sherry/commands/` (workspace scope; workspace wins on a duplicate name). `server/service/custom_commands_service.py` reads the frontmatter `description` / `argument-hint` and the body; `GET /commands?session_id=…` exposes them. Built-in commands come from `use-command-registry.ts`, the same registry as Ctrl+K.

A custom command with an `argument-hint` opens a small argument field in the panel. The selected command becomes a `command-mention-chip` whose `data-command-body` carries the body with `$ARGUMENTS` replaced; `extractTextForSend` sends that body, never the visible `/name`. A built-in command executes its registry handler and inserts nothing.

## `+` menu and `@` file mentions

The `+` button is the discoverable entry for the otherwise hidden `$` and `/` triggers. Its rows open image / audio / video uploads, a generic OS file picker, the skill picker and the command picker. File references remain the existing atomic `@path` chips produced by drag-and-drop or the tree's **Add to chat** action.

## Custom command format

```markdown
---
description: Review the current change
argument-hint: [area]
---
Review the current git diff. Focus on: $ARGUMENTS
```

The command name is the file name (nested directories join with `:`), descriptions are required, and `$ARGUMENTS` is a plain text substitution. No shell expansion or `!command` syntax is supported.

## Safety and tests

Triggers require a word boundary, never fire inside pasted `<pre>` / `<code>` content, and a chip inside a `contenteditable=false` atom cannot trigger again. Backspace removes the typed query; deleting the trigger character closes the panel. Tests: `client/app/composables/__tests__/use-input-trigger.test.ts`, the composer integration suite, and `tests/server/service/test_custom_commands_service.py`.
