"""GitHub event → Trigger translation (prototype ``src/sources/github-trigger.ts``).

The ``updated_at`` suffix makes an edit a new event; the agent's own messages
never produce a Trigger.  CI events have no sender, so their actor id is empty
and they fall through the unlisted/unaddressed admission cell.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from superagent.core.ids import key
from superagent.core.model import Actor, AddressKind, ChannelAddress, SourceRef, Trigger

KINDS = frozenset({"issue", "pull", "comment", "review", "check"})


class GithubConfig(BaseModel):
    """Configuration for one GitHub Source instance."""

    model_config = ConfigDict(extra="forbid")

    repository: str
    cli: str = "gh"
    agent_login: str = ""
    observe_issues: bool = False
    observe_pulls: bool = False
    observe_comments: bool = False
    observe_ci: bool = False
    repository_path: str = ""
    max_workers: int = Field(default=1, ge=1)


def is_bot(login: str, *, apps: tuple[str, ...] = ()) -> bool:
    """Return whether a GitHub login belongs to a bot."""
    return login.endswith("[bot]") or login in apps


def _repo(event: dict[str, Any], config: GithubConfig) -> str:
    value = event.get("repository") or event.get("repo_full_name")
    if isinstance(value, dict):
        value = value.get("full_name")
    return str(value or config.repository)


def _number(event: dict[str, Any]) -> str:
    for holder in (event, event.get("issue"), event.get("pull_request")):
        if isinstance(holder, dict) and holder.get("number") is not None:
            return str(holder["number"])
    return str(event.get("number", ""))


def thread_ref(event: dict[str, Any], config: GithubConfig) -> SourceRef:
    """Return the canonical Thread for a GitHub event."""
    return SourceRef(source="github", id=f"{_repo(event, config)}#{_number(event)}")


def _head_ref(event: dict[str, Any]) -> str:
    pull = event.get("pull_request")
    if not isinstance(pull, dict):
        return ""
    head = pull.get("head")
    return str(head.get("ref", "")) if isinstance(head, dict) else ""


def _kind(event: dict[str, Any]) -> str:
    raw = str(event.get("type", event.get("kind", ""))).lower()
    if raw in KINDS:
        return raw
    if event.get("check_suite") or event.get("check_run"):
        return "check"
    if event.get("pull_request") is not None:
        return "pull"
    if event.get("comment") is not None or "comments" in str(event.get("url", "")):
        return "comment"
    return "issue"


def github_trigger(
    event: dict[str, Any], config: GithubConfig, *, apps: tuple[str, ...] = ()
) -> Trigger | None:
    """Translate one GitHub notification into a Trigger, or None if filtered."""
    kind = _kind(event)
    flag = {
        "issue": config.observe_issues,
        "pull": config.observe_pulls,
        "comment": config.observe_comments,
        "review": config.observe_comments,
        "check": config.observe_ci,
    }[kind]
    if not flag:
        return None

    raw_id = str(event.get("id", ""))
    updated = str(event.get("updated_at", event.get("at", "")))
    if not raw_id:
        return None

    user = event.get("user") or event.get("sender") or {}
    if not isinstance(user, dict):
        user = {}
    login = str(user.get("login", ""))
    bot = is_bot(login, apps=apps) or bool(user.get("type") == "Bot")
    if login and config.agent_login and login == config.agent_login:
        return None

    body = str(event.get("body", event.get("text", "")))
    addressed = (
        AddressKind.MENTION if config.agent_login and f"@{config.agent_login}" in body else None
    )
    thread = thread_ref(event, config)
    repo = _repo(event, config)
    number = _number(event)
    url = str(event.get("html_url", event.get("url", ""))) or None
    return Trigger(
        id=key("github", f"{kind}:{raw_id}:{updated}"),
        source="github",
        threads=[thread],
        actor=Actor(source="github", id="" if kind == "check" else login),
        human=not bot and bool(login) and kind != "check",
        addressed=addressed,
        at=updated,
        text=body,
        channel=ChannelAddress(
            source="github",
            thread=thread.id,
            address={"repo": repo, "number": number, "kind": kind},
        ),
        url=url,
        meta={
            "kind": kind,
            "repo": repo,
            "number": number,
            "head": _head_ref(event),
            "bot": bot,
        },
    )


__all__ = ["GithubConfig", "github_trigger", "is_bot", "thread_ref"]
