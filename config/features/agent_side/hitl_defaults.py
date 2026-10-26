"""Human-in-the-loop approval defaults."""

from typing import TypedDict


class HitlDefaultsConfig(TypedDict):
    """Human-in-the-loop approval defaults."""

    yolo_deny_paths: list[str]
    first_call_confirmation_enabled: bool
    first_call_confirmation_tools: tuple[str, ...]


HITL_DEFAULTS: HitlDefaultsConfig = {
    # File-change gate, per access mode (the two behaviours share this handler):
    #
    # * "Auto edit" / auto_edit (the default) NEVER asks for a file change — that
    #   is what the mode means: the agent edits, and only dangerous or
    #   uncertain calls raise a card. `first_call_confirmation_enabled` is the
    #   optional ask-once reminder on top of that (off by default; turning it
    #   on makes the first use of a listed tool ask once per session);
    # * "Confirm every change" / confirm_all asks on EVERY change, ignoring both this flag
    #   and any remembered confirmation.
    #
    # The listed tools are the mutation surfaces no other gate intercepts:
    # `terminal` / `python_repl` keep their own command and sandbox-bypass
    # gates, memory/skill writes keep the write gate, and an external path
    # keeps its own approval card. Only interactive turns are gated: a turn
    # with no operator in scope (cron, heartbeat, subagent carrier) follows
    # its existing policy, since nobody is present to answer.
    "first_call_confirmation_enabled": False,
    "first_call_confirmation_tools": ("write_file", "patch_file"),
    # Security floor for external path access: these paths stay denied even
    # when YOLO is on, the session allowlist matches, or a subagent inherits
    # its parent's authorization. `~` is expanded at check time; a trailing
    # separator means "this directory and everything under it", no separator
    # means exact match. Users extend the list via `yolo_deny_paths` in
    # sherry.jsonc (merged after these defaults, duplicates dropped).
    "yolo_deny_paths": [
        "~/.ssh/",
        "~/.aws/",
        "~/.gnupg/",
        "~/.config/gcloud/",
        "~/.env",
        "~/.gitconfig",
        "~/.npmrc",
        "~/.pypirc",
    ],
}
