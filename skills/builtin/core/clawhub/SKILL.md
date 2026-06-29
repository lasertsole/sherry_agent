---
name: clawhub
description: Search and install agent skills from ClawHub, the public skill registry.
---

# ClawHub

Public skill registry for AI agents. Search by natural language (vector search).
homepage: https://skillhub.tencent.com
mirror: https://clawhub.ai

## When to use

Use this skill when the user asks any of:
- "find a skill for …"
- "search for skills"
- "install a skill"
- "what skills are available?"
- "update my skills"

## Search

```bash
npx --yes clawhub@latest search "web scraping" --limit 5
```

## Install

Use the Python helper to resolve the project root path automatically:

```python
from skills.builtin.core.clawhub.scripts import run_clawhub_command

result = run_clawhub_command(["install", "<slug>", "--workdir", "{{ROOT_DIR}}", "--dir", "plugins"])
```

Replace `<slug>` with the skill name from search results. This places the skill into `{{ROOT_DIR}}/skills/plugins/`, keeping third-party skills separate from built-in ones.

> **Note**: The `--dir plugins` flag ensures third-party skills go into `skills/plugins/` instead of the root `skills/`.

## Update

```python
from skills.builtin.core.clawhub.scripts import run_clawhub_command

result = run_clawhub_command(["update", "--all", "--workdir", "{{ROOT_DIR}}", "--dir", "plugins"])
```

## List installed

```python
from skills.builtin.core.clawhub.scripts import run_clawhub_command

result = run_clawhub_command(["list", "--workdir", "{{ROOT_DIR}}", "--dir", "plugins"])
```

## Notes

- Requires Node.js (`npx` comes with it).
- No API key needed for search and install.
- Login (`npx --yes clawhub@latest login`) is only required for publishing.
- `{{ROOT_DIR}}` is automatically replaced with the actual project root path by the Python helper.
- The `--dir plugins` flag is passed automatically in all documented commands to install third-party skills under `skills/plugins/` instead of the flat `skills/`.
- After install, remind the user to start a new session to load the skill.
