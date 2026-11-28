"""GitHub channel send (prototype ``src/sources/github-channel.ts``).

A hidden comment marker makes retries idempotent.  404/410 means the
conversation is gone (``gone``); every other failure is retried by the Outbox.
"""

from __future__ import annotations

from typing import Any

from superagent.core.model import ChannelAddress, Receipt
from superagent.infra.commands import Commands
from superagent.sources.github_trigger import GithubConfig
from superagent.sources.kind import SendResult

GONE_CODES = (404, 410)


def marker(receipt_id: str) -> str:
    """Return the hidden idempotency marker for a Receipt."""
    return f"<!-- dsh-superagent: {receipt_id} -->"


class GithubChannel:
    """Post comments and review replies through the agent CLI."""

    def __init__(self) -> None:
        self._sent: dict[str, str] = {}

    def allowed(self, addr: ChannelAddress, config: GithubConfig) -> bool:
        """Return whether the address still belongs to the configured repo."""
        repo = addr.address.get("repo")
        return not repo or repo == config.repository

    async def send(self, commands: Commands, message: Receipt, config: GithubConfig) -> SendResult:
        """Send one reply; return ``sent`` or ``gone``, otherwise raise."""
        if message.id in self._sent:
            return SendResult(status="sent", message_id=self._sent[message.id])
        number = str(message.channel.address.get("number", ""))
        repo = str(message.channel.address.get("repo", config.repository))
        kind = str(message.channel.address.get("kind", "issue"))
        text = f"{marker(message.id)}\n{message.text}"
        if kind == "review":
            argv = [
                config.cli,
                "api",
                f"repos/{repo}/pulls/{number}/comments/{message.channel.address.get('comment_id', '')}/replies",
                "-f",
                f"body={text}",
            ]
        else:
            argv = [
                config.cli,
                "issue",
                "comment",
                number,
                "--repo",
                repo,
                "--body",
                text,
            ]
        result = await commands.run(argv)
        if result.code in GONE_CODES:
            return SendResult(status="gone")
        if not result.ok:
            raise RuntimeError(f"github send failed with code {result.code}: {result.stderr[:200]}")
        parsed = result.json()
        comment_id = str(parsed.get("id", "")) if isinstance(parsed, dict) else ""
        self._sent[message.id] = comment_id
        return SendResult(status="sent", message_id=comment_id)

    def link(self, addr: ChannelAddress) -> dict[str, str]:
        """Return a human-facing link for the channel."""
        repo = str(addr.address.get("repo", ""))
        number = str(addr.address.get("number", ""))
        return {
            "url": f"https://github.com/{repo}/issues/{number}" if repo and number else "",
            "label": f"{repo}#{number}" if repo and number else addr.thread,
        }

    def replay(self, comment_bodies: list[str]) -> None:
        """Learn markers from existing comments so a restart stays idempotent."""
        for body in comment_bodies:
            needle = "<!-- dsh-superagent: "
            if needle in body:
                receipt_id = body.split(needle, 1)[1].split(" -->", 1)[0]
                self._sent.setdefault(receipt_id, "")


def channel_from_config(config: Any) -> GithubChannel:
    """Return a fresh channel for a config (kept for plugin symmetry)."""
    del config
    return GithubChannel()


__all__ = ["GONE_CODES", "GithubChannel", "channel_from_config", "marker"]
