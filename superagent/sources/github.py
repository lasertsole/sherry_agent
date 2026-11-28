"""GitHub Source kind, plugin entry and notification polling (prototype ``src/sources/github.ts``)."""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel

from superagent.core.model import Ticket, Trigger
from superagent.infra.health import HealthBoard
from superagent.sources.github_channel import GithubChannel
from superagent.sources.github_pull import apply_pull_event
from superagent.sources.github_read import GitHubReader, GithubThreadContext
from superagent.sources.github_trigger import GithubConfig, github_trigger
from superagent.sources.host import HealthState, SourceContext
from superagent.sources.kind import FieldSpec, Issue
from superagent.sources.poll import cursor_at

KV_CURSOR = "github-cursor:notifications"


class GithubSource:
    """The GitHub SourceKind: config schema, behaviours and watch loop."""

    kind = "github"
    title = {"zh": "GitHub", "en": "GitHub"}
    description = {
        "zh": "从 GitHub issue、PR、评论与 CI 拉取工作。",
        "en": "Pull work from GitHub issues, PRs, comments and CI.",
    }
    schema: type[BaseModel] = GithubConfig
    fields = [
        FieldSpec("repository", "text", "Repository", options=("owner/name",)),
        FieldSpec("cli", "text", "Agent CLI", options=("gh",)),
        FieldSpec("agent_login", "text", "Agent login"),
        FieldSpec("observe_issues", "select", "Observe issues"),
        FieldSpec("observe_pulls", "select", "Observe pull requests"),
        FieldSpec("observe_comments", "select", "Observe comments"),
        FieldSpec("observe_ci", "select", "Observe CI"),
    ]
    health = ["github"]

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.channel = GithubChannel()
        self.poll_failures = 0

    def claims(self, board: HealthBoard) -> None:
        """Claim the health entries owned by this kind."""
        board.claim("github", "github")

    def validate(self, config: dict[str, Any]) -> list[Issue]:
        """Validate a config dict; the pydantic schema is the single source."""
        try:
            GithubConfig.model_validate(config)
        except Exception as exc:  # noqa: BLE001 — pydantic errors are surfaced as Issues
            return [Issue(path="config", message=str(exc)[:300])]
        return []

    def scope(self, meta: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        """Return the admission row for an event kind."""
        kind = str(meta.get("kind", "issue"))
        flags = {
            "issue": "observe_issues",
            "pull": "observe_pulls",
            "comment": "observe_comments",
            "review": "observe_comments",
            "check": "observe_ci",
        }
        flag = flags.get(kind, "observe_issues")
        observed = bool(config.get(flag, False))
        if not observed:
            return {"listened": False, "reason": "notObserved"}
        return {"listened": True, "observed": True, "row": f"github/{kind}"}

    async def find(self, query: str, config: dict[str, Any]) -> dict[str, Any]:
        """Match ``#12`` and GitHub URLs into thread references."""
        repo = str(config.get("repository", ""))
        threads: list[dict[str, Any]] = []
        terms = [query]
        if query.startswith("#") and query[1:].isdigit():
            threads.append({"source": "github", "id": f"{repo}#{query[1:]}"})
        elif f"github.com/{repo}/issues/" in query:
            number = query.rsplit("/", 1)[-1]
            threads.append({"source": "github", "id": f"{repo}#{number}"})
        return {"threads": threads, "terms": terms}

    @property
    def environment(self) -> GitHubSourceEnvironment:
        """Return the Source's execution-environment provider."""
        return GitHubSourceEnvironment()

    @property
    def thread_context(self) -> GithubThreadContext | None:
        """Return the thread-context reader configured for this instance."""
        parsed = (
            GithubConfig.model_validate(self.config) if self.config else GithubConfig(repository="")
        )
        return GithubThreadContext(parsed)

    async def watch(
        self,
        ctx: SourceContext,
        config: dict[str, Any],
        signal: asyncio.Event | None = None,
        *,
        started: str,
    ) -> list[Trigger]:
        """Poll GitHub notifications once and translate them into Triggers."""
        cfg = GithubConfig.model_validate(config)
        reader = GitHubReader(ctx.commands, cfg)
        cursor = self._cursor(ctx, started)
        try:
            events = await reader.notifications(cursor)
        except Exception as exc:  # noqa: BLE001 — report and retry on the next pass
            self.poll_failures += 1
            self._report(ctx, "error", f"pollFailures={self.poll_failures}: {exc}")
            return []
        self.poll_failures = 0
        self._report(ctx, "ready", "")
        triggers: list[Trigger] = []
        for event in events:
            if signal is not None and signal.is_set():
                break
            trigger = github_trigger(event, cfg)
            if trigger is not None:
                ctx.emit(trigger)
                triggers.append(trigger)
        self._save_cursor(ctx, events[-1].get("updated_at", started) if events else started)
        return triggers

    async def watch_pulls(
        self, ctx: SourceContext, config: dict[str, Any], tickets: list[Ticket]
    ) -> list[str]:
        """Reconcile PR state for all Tickets that follow one."""
        cfg = GithubConfig.model_validate(config)
        reader = GitHubReader(ctx.commands, cfg)
        ended: list[str] = []
        for ticket in tickets:
            for follow in ticket.follows:
                if "#pull-" not in follow.thread.id:
                    continue
                number = follow.thread.id.rsplit("-", 1)[-1]
                event = await reader.issue(number)
                changed, should_end = apply_pull_event(ticket, {"number": number, **event})
                if changed and ctx.tickets is not None:
                    ctx.tickets.bind_own(ticket.id, follow.thread, follow.own)
                if should_end:
                    ended.append(ticket.id)
        return ended

    def _cursor(self, ctx: SourceContext, started: str) -> str:
        getter = getattr(ctx.state, "get", None)
        raw = getter(KV_CURSOR) if callable(getter) else None
        return str(raw or cursor_at(started))

    def _save_cursor(self, ctx: SourceContext, value: str) -> None:
        setter = getattr(ctx.state, "set", None)
        if callable(setter):
            setter(KV_CURSOR, value)

    def _report(self, ctx: SourceContext, state: str, detail: str) -> None:
        ctx.report("github", HealthState(name="github", state=state, detail=detail))


class GitHubSourceEnvironment:
    """Declare and resolve GitHub-specific worker variables."""

    def variables(self) -> list[str]:
        """Return the variable names this Source provides."""
        return [
            "GITHUB_REPOSITORY",
            "GITHUB_ACCOUNT",
            "GITHUB_COMMIT_EMAIL",
            "GITHUB_NUMBER",
            "GITHUB_PR_HEAD",
        ]

    def values(self, config: dict[str, Any], trigger: Trigger | None) -> dict[str, str]:
        """Resolve values for one Ticket's worker."""
        values = {
            "GITHUB_REPOSITORY": str(config.get("repository", "")),
            "GITHUB_ACCOUNT": str(config.get("agent_login", "")),
            "GITHUB_COMMIT_EMAIL": str(config.get("commit_email", "")),
        }
        if trigger is not None:
            values["GITHUB_NUMBER"] = str(trigger.meta.get("number", ""))
            values["GITHUB_PR_HEAD"] = str(trigger.meta.get("head", ""))
        return values


_SOURCES: dict[str, GithubSource] = {}


def apply(config: dict[str, Any]) -> GithubSource:
    """Plugin entry point: build (or reuse) a GitHub Source from configuration."""
    parsed = GithubConfig.model_validate(config)
    name = parsed.repository
    if name not in _SOURCES:
        _SOURCES[name] = GithubSource(config)
    return _SOURCES[name]


async def watch(
    ctx: SourceContext,
    config: dict[str, Any],
    signal: asyncio.Event | None = None,
    *,
    started: str,
) -> list[Trigger]:
    """Plugin watch entry point for one polling pass."""
    return await apply(config).watch(ctx, config, signal, started=started)


__all__ = ["GithubSource", "GitHubSourceEnvironment", "apply", "watch"]
