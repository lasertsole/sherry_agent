"""PathGuard — centralized path-argument screening for every tool call.

Defense-in-depth for the per-tool ``resolve_project_path()`` /
``resolve_external_path()`` pattern: a tool that forgets its own path checks
still cannot be driven to read a traversal or hard-denied path. Registered in
the main agent chain right after ``ToolCallNormalize`` (wrap order:
``IterationBudget`` → ``ToolGuardrails`` → ``PathGuard`` → tool), so a rejection
is a normal error ``ToolMessage`` that ToolGuardrails can evaluate like any
other tool failure.

Screening contract (conservative by design):

- only string values under the argument names ``file_path`` / ``path`` /
  ``directory`` / ``dir`` are considered; URL-shaped values (``scheme://``) are
  skipped so non-path semantics (e.g. a URL passed as ``path``) are never
  misread as filesystem paths;
- ``..`` traversal components (URL-decoded, backslash-normalized — the shared
  ``has_traversal_component`` predicate) are rejected;
- a value that resolves inside the session's project directory is passed through
  untouched (the root is read per call — this middleware is rebuilt every turn,
  but the session can switch directories at a turn boundary);
- a value that resolves outside that root is passed through **unless** it is
  on the hard-deny floor (the YOLO deny list / system credential files). Any
  other external path is left to the tool's own ``resolve_external_path()``
  HITL flow — intercepting it here would approve (or block) it twice, since
  the tool re-runs the same gate during execution;
- missing/unresolvable targets and unknown exception classes fall through to
  the tool, which owns its own error surface.

The middleware never rewrites arguments and never triggers an approval
interrupt itself; the only approval flow remains the one inside the tools.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections.abc import Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from loguru import logger
from typing import override

from config.path import ROOT_DIR
from pub.func.path import has_traversal_component


@dataclass(frozen=True, slots=True)
class _Rejection:
    """A screened path argument that must not reach the tool."""

    key: str
    value: str
    reason: str


_PATH_ARG_KEYS = frozenset({"file_path", "path", "directory", "dir"})

#: Arguments that carry a COMMAND STRING (terminal / python_repl). They are not
#: path arguments, so the path gate above never sees them; this separate screen
#: only rejects the hard-deny floor inside them (see ``_screen_command_paths``).
_COMMAND_ARG_KEYS = frozenset({"commands", "command", "query"})

#: System credential files that are never a legitimate project read; a HITL
#: prompt for them is at best noise. Mirrors the terminal sensitive-file set.
_SYSTEM_DENY_PATHS: tuple[str, ...] = ("/etc/passwd", "/etc/shadow", "/etc/sudoers")


def _system_deny_paths() -> tuple[Path, ...]:
    return tuple(Path(path).resolve() for path in _SYSTEM_DENY_PATHS)


def _external_target(value: str, root: Path | None = None) -> Path | None:
    """Resolve value as an external-path candidate; None when unresolvable.

    Relative values are joined onto the session root (``root``), which defaults
    to the module's ``ROOT_DIR`` for session-less callers.
    """
    try:
        target = Path(os.path.expanduser(value))
        if not target.is_absolute():
            target = (root if root is not None else ROOT_DIR) / target
        return target.resolve()
    except (OSError, RuntimeError):
        logger.debug("PathGuard: path resolution fell back", exc_info=True)
        return None


def _is_hard_denied(target: Path) -> bool:
    """Hard-deny floor: the YOLO deny list plus the system credential files."""
    if any(target == denied for denied in _system_deny_paths()):
        return True
    from agent.tools.pub_base.path_utils import _is_yolo_denied

    return _is_yolo_denied(target)


def _screen_path_arg(value: str, root: Path | None = None) -> str | None:
    """Return a rejection reason for value, or None to let the call proceed.

    ``root`` is the session's project directory; the pass/deny policy is
    unchanged (in-root passes, outside the root only the hard-deny floor
    rejects — everything else stays with the tools' HITL flow), only the
    boundary moves with the session.
    """
    if has_traversal_component(value):
        return "path traversal components are not allowed"
    from agent.tools.pub_base.path_utils import PathOutOfBoundsError, resolve_workspace_path

    try:
        resolve_workspace_path(value, root)
    except PathOutOfBoundsError:
        target = _external_target(value, root)
        if target is not None and _is_hard_denied(target):
            return "path is on the hard deny list (credentials / system files)"
    except (OSError, RuntimeError):
        logger.debug("PathGuard: path resolution fell back", exc_info=True)
        return None
    return None


#: Path-looking tokens inside a command string: absolute paths, ``~/…`` and
#: relative paths carrying a traversal component. Deliberately shallow — no
#: shell parsing, no quoting rules — because this screen is a mitigation.
_COMMAND_PATH_RE = re.compile(
    r"""(?P<path>~?/[\w.@+\-/]*|\.\.?/[\w.@+\-/]+)""",
)


#: Shell tokens that make the FOLLOWING path a write target. Deliberately a
#: short, unambiguous list — this screen is a mitigation, not a shell parser.
_WRITE_REDIRECTS = (">", ">>")
_WRITE_COMMANDS = ("tee", "truncate", "shred")
#: ``sed -i`` / ``perl -i`` edit in place: the path is the last argument.
_INPLACE_FLAG = re.compile(r"\b(?:sed|perl|gsed)\b[^|;&]*\s-i\b")


def _write_targets(text: str) -> list[str]:
    """Path-looking tokens this command WRITES to (best effort).

    Extracted from ``>`` / ``>>`` redirects and the obvious writers
    (``tee``, ``truncate``, ``shred``, ``sed -i``). Everything else is treated
    as a read for the floor check below.
    """
    targets: list[str] = []
    tokens = text.split()
    for index, token in enumerate(tokens):
        stripped = token.lstrip("0123456789")  # 2>file, 2>>file
        if stripped in _WRITE_REDIRECTS or stripped.rstrip("&") in _WRITE_REDIRECTS:
            for follow in tokens[index + 1 :]:
                if follow and not follow.startswith("&"):
                    targets.append(follow)
                    break
            continue
        if stripped in ("&>", "&>>"):
            if index + 1 < len(tokens):
                targets.append(tokens[index + 1])
            continue
        name = os.path.basename(stripped.rstrip("/")) or stripped
        if name in _WRITE_COMMANDS and index + 1 < len(tokens):
            for follow in tokens[index + 1 :]:
                if not follow.startswith("-"):
                    targets.append(follow)
                    break
    if _INPLACE_FLAG.search(text):
        # `sed -i s/a/b/ file` — the last non-option token is the edited file.
        for token in reversed(tokens):
            if token and not token.startswith("-") and not token.startswith("'") and "." in token:
                targets.append(token)
                break
    return targets


def _bare_path_tokens(text: str) -> list[str]:
    """Whitespace tokens that look like a relative path (contain ``/`` or a dot).

    Flags, URLs and shell punctuation are dropped. Deliberately crude: a token
    that is NOT a path simply resolves somewhere harmless and is allowed, while
    the ones this exists for (``.models``, ``src/data/auth.db``) resolve inside
    sherry's tree and meet the read policy.
    """
    tokens: list[str] = []
    for raw in text.split():
        token = raw.strip("\"'`()[]{}<>,;|&")
        if not token or token.startswith("-") or "://" in token or "=" in token:
            continue
        if "/" not in token and "." not in token:
            continue
        tokens.append(token)
    return tokens


def _screen_command_paths(
    command: str, root: Path | None = None, session_id: str = ""
) -> str | None:
    """Rejection reason when a command string names a denied path.

    Two hard-deny classes: the system floor (credentials) and SHERRY'S OWN
    non-whitelisted files. Read-looking paths are screened against the READ
    policy, extracted WRITE targets against the (narrower) WRITE policy — that
    is what catches both ``cat .models`` and ``echo x > agent/core.py``.

    A MITIGATION, not a barrier: base64, shell variables, ``cd`` + relative
    paths and runtime-built paths all hide a path from this scan, which is why
    the file tools' policy (``resolve_tool_path``) is the real gate and the OS
    sandbox is the backstop.

    The command is scanned first with the raw text and then with the shell
    quoting/escaping characters stripped, so ``cat '/etc/passwd'`` and
    ``cat /etc/pas\\swd`` still hit the floor.
    """
    if not command.strip():
        return None
    from agent.tools.pub_base.self_path_policy import classify_self_path

    candidates = {command}
    stripped = command.replace("\\", "").replace('"', "").replace("'", "")
    candidates.add(stripped)
    for text in candidates:
        # 1. Write targets, by TOKEN (a bare relative path like ``agent/x.py``
        #    has no leading slash, so the path regex below never sees it).
        for token in _write_targets(text):
            target = _external_target(token, root)
            if target is None:
                continue
            if _is_hard_denied(target):
                return "command writes a path on the hard deny list (credentials / system files)"
            reason = classify_self_path(target, session_id, "write")
            if reason is not None:
                return reason
        # 2. Bare path tokens (``.models``, ``src/data/auth.db``) that carry no
        #    leading slash / dot-slash and therefore miss the regex below.
        for token in _bare_path_tokens(text):
            target = _external_target(token, root)
            if target is None:
                continue
            if _is_hard_denied(target):
                return "command names a path on the hard deny list (credentials / system files)"
            reason = classify_self_path(target, session_id, "read")
            if reason is not None:
                return reason
        # 3. Path-looking tokens: the system floor first, then the read policy.
        for match in _COMMAND_PATH_RE.finditer(text):
            value = match.group("path").rstrip("\"',;|&")
            if not value or "://" in value:
                continue
            target = _external_target(value, root)
            if target is None:
                continue
            if _is_hard_denied(target):
                return "command names a path on the hard deny list (credentials / system files)"
            reason = classify_self_path(target, session_id, "read")
            if reason is not None:
                return reason
    return None


def _session_id(request: ToolCallRequest) -> str:
    """The calling session's id (empty for a state-less caller)."""
    state = getattr(request, "state", None)
    session_id = state.get("session_id") if isinstance(state, dict) else None
    return session_id if isinstance(session_id, str) else ""


def _session_project_root(request: ToolCallRequest) -> Path | None:
    """The calling session's project directory, or None when unknown.

    The session id travels in the graph state (the same ``session_id`` every
    path-aware tool injects); a state-less caller (tests, non-session graphs)
    keeps the module ``ROOT_DIR`` boundary.
    """
    state = getattr(request, "state", None)
    session_id = state.get("session_id") if isinstance(state, dict) else None
    if not isinstance(session_id, str) or not session_id:
        return None
    from agent.tools.pub_base import session_workspace_root

    return session_workspace_root(session_id)


class PathGuard(AgentMiddleware):
    """Reject tool calls whose path arguments are definitively out of bounds."""

    def _find_rejection(self, request: ToolCallRequest) -> _Rejection | None:
        args: Any = request.tool_call.get("args")
        if not isinstance(args, dict):
            return None
        root = self._session_project_root(request)
        session_id = _session_id(request)
        for key in sorted(_PATH_ARG_KEYS):
            value = args.get(key)
            if not isinstance(value, str):
                continue
            candidate = value.strip()
            if not candidate or "://" in candidate:
                continue
            reason = _screen_path_arg(candidate, root)
            if reason is not None:
                return _Rejection(key=key, value=value, reason=reason)
        # Command strings (terminal / python_repl) never carry a path argument,
        # so they get their own hard-deny-only screen.
        for key in sorted(_COMMAND_ARG_KEYS):
            value = args.get(key)
            texts = value if isinstance(value, list) else [value]
            for text in texts:
                if not isinstance(text, str):
                    continue
                reason = _screen_command_paths(text, root, session_id)
                if reason is not None:
                    return _Rejection(key=key, value=text, reason=reason)
        return None

    def _session_project_root(self, request: ToolCallRequest) -> Path | None:
        """Session root for this call (thin indirection so tests can stub it)."""
        return _session_project_root(request)

    @staticmethod
    def _blocked_message(request: ToolCallRequest, rejection: _Rejection) -> ToolMessage:
        tool_name = request.tool_call.get("name", "unknown")
        logger.warning(
            "PathGuard blocked {}.{}={!r}: {}",
            tool_name,
            rejection.key,
            rejection.value,
            rejection.reason,
        )
        return ToolMessage(
            content=(
                f"PathGuard: rejected {tool_name}({rejection.key}={rejection.value!r}): "
                f"{rejection.reason}. Use a path inside the project root; external paths "
                "go through the file tools' human-approval flow."
            ),
            tool_call_id=request.tool_call.get("id", ""),
            name=tool_name,
            status="error",
        )

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage],
    ) -> ToolMessage:
        logger.bind(middleware=type(self).__name__).debug("wrap_tool_call hook fired")
        rejection = self._find_rejection(request)
        if rejection is not None:
            return self._blocked_message(request, rejection)
        return handler(request)

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage]],
    ) -> ToolMessage:
        logger.bind(middleware=type(self).__name__).debug("awrap_tool_call hook fired")
        rejection = self._find_rejection(request)
        if rejection is not None:
            return self._blocked_message(request, rejection)
        return await handler(request)
