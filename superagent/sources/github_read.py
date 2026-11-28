"""GitHub reads: notifications, CI snapshots, thread context (prototype ``src/sources/github-read.ts``)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from superagent.core.model import Trigger
from superagent.infra.commands import Commands
from superagent.sources.github_trigger import GithubConfig, _number, _repo
from superagent.sources.kind import ThreadContext


class GitHubReader:
    """Read GitHub state exclusively through the agent's ``gh`` wrapper."""

    def __init__(self, commands: Commands, config: GithubConfig) -> None:
        self.commands = commands
        self.config = config

    async def notifications(self, since: str | None = None) -> list[dict[str, Any]]:
        """Return notifications newer than ``since`` as event dictionaries."""
        argv = [self.config.cli, "api", "notifications"]
        if since:
            argv += ["-f", f"since={since}"]
        result = await self.commands.run(argv)
        if not result.ok:
            raise RuntimeError(f"gh notifications failed: {result.stderr.strip()[:200]}")
        parsed = result.json()
        if not isinstance(parsed, list):
            return []
        events: list[dict[str, Any]] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            subject = item.get("subject") or {}
            detail = item.get("repository") or {}
            events.append(
                {
                    "id": item.get("id", ""),
                    "updated_at": item.get("updated_at", ""),
                    "type": str(subject.get("type", "")).lower().replace("issue", "issue"),
                    "title": subject.get("title", ""),
                    "url": subject.get("url", ""),
                    "repository": detail.get("full_name", self.config.repository),
                }
            )
        return events

    async def issue(self, number: str | int, *, repo: str | None = None) -> dict[str, Any]:
        """Read one Issue or Pull Request."""
        result = await self.commands.run(
            [self.config.cli, "api", f"repos/{repo or self.config.repository}/issues/{number}"]
        )
        if not result.ok:
            raise RuntimeError(f"gh issue read failed: {result.stderr.strip()[:200]}")
        parsed = result.json()
        return dict(parsed) if isinstance(parsed, dict) else {}

    async def comments(self, number: str | int, *, repo: str | None = None) -> list[dict[str, Any]]:
        """Read the comments of one Issue or Pull Request."""
        result = await self.commands.run(
            [
                self.config.cli,
                "api",
                f"repos/{repo or self.config.repository}/issues/{number}/comments",
                "--paginate",
            ]
        )
        if not result.ok:
            raise RuntimeError(f"gh comments read failed: {result.stderr.strip()[:200]}")
        parsed = result.json()
        return (
            [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []
        )

    async def ci_snapshot(self, number: str | int, *, repo: str | None = None) -> str:
        """Return a stable fingerprint of the checks on a Pull Request."""
        result = await self.commands.run(
            [
                self.config.cli,
                "pr",
                "checks",
                str(number),
                "--json",
                "name,state,conclusion,startedAt,completedAt",
                "--repo",
                repo or self.config.repository,
            ]
        )
        if not result.ok:
            raise RuntimeError(f"gh pr checks failed: {result.stderr.strip()[:200]}")
        try:
            rows = json.loads(result.stdout)
        except ValueError as exc:
            raise RuntimeError("gh pr checks returned invalid JSON") from exc
        return json.dumps(rows, sort_keys=True, separators=(",", ":"))

    def repository_path(self) -> Path:
        """Return the local checkout path configured for this repository."""
        return Path(self.config.repository_path or self.config.repository.split("/")[-1])


class GithubThreadContext:
    """Bounded thread-context reader for GitHub (prototype ``githubThreadContext``)."""

    def __init__(self, config: GithubConfig) -> None:
        self.config = config

    def message_id(self, trigger: Trigger) -> str:
        """Return the Source message id carried by a Trigger."""
        return str(trigger.meta.get("message_id", trigger.id))

    async def read(
        self,
        commands: Commands,
        trigger: Trigger,
        part: str,
        cursor: str | None,
    ) -> ThreadContext:
        """Read one page of comments around a Trigger."""
        number = _number(trigger.meta)
        repo = _repo(trigger.meta, self.config)
        comments = await GitHubReader(commands, self.config).comments(number, repo=repo)
        messages = [
            {
                "id": str(item.get("id", "")),
                "frm": str((item.get("user") or {}).get("login", "")),
                "at": str(item.get("created_at", "")),
                "text": str(item.get("body", "")),
                "url": str(item.get("html_url", "")),
            }
            for item in comments
        ]
        return ThreadContext(messages=messages, body=str(trigger.text))


__all__ = ["GitHubReader", "GithubThreadContext"]
