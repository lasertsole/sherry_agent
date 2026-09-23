"""Generate ``sherry_tools.py`` for the PTC child process.

The generated stub is a normal importable module that exposes one synchronous
wrapper per tool the child is allowed to call. Each wrapper forwards to the
parent over the newline-delimited JSON RPC socket seeded by
:class:`~agent.tools.ptc.rpc_server.PtcRpcServer`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolStub:
    """A single generated wrapper.

    ``params`` holds ``(name, default_source)`` pairs; ``default_source`` is a
    valid Python literal (e.g. ``"1"``, ``"'content'"``, ``"None"``) or
    ``None`` when the parameter is required.
    """

    name: str
    params: tuple[tuple[str, str | None], ...]


def _default_source(default: Any) -> str:
    """Render a pydantic field default as a Python source literal."""
    if default is None:
        return "None"
    if isinstance(default, (str, int, float, bool, list, dict, tuple, set, frozenset)):
        return repr(default)
    # Unsupported default shape (e.g. a callable) — omit it, making the
    # parameter effectively required in the stub; the real tool re-validates.
    return "None"


def tool_specs_from_base_tools(tools: Any) -> list[ToolStub]:
    """Derive :class:`ToolStub` specs from live BaseTool instances.

    Uses ``tool_call_schema`` (not ``args_schema``) so injected arguments such
    as ``session_id`` are excluded — they are supplied by the RPC dispatcher,
    never by the model-authored script.
    """
    specs: list[ToolStub] = []
    for tool in tools:
        name = getattr(tool, "name", None)
        if not name:
            continue
        schema = getattr(tool, "tool_call_schema", None)
        fields = getattr(schema, "model_fields", None) or {}
        params: list[tuple[str, str | None]] = []
        for field_name, field in fields.items():
            if field.is_required():
                params.append((field_name, None))
            else:
                params.append((field_name, _default_source(field.default)))
        specs.append(ToolStub(name=str(name), params=tuple(params)))
    return specs


def _render_wrapper(stub: ToolStub) -> str:
    rendered_params = []
    call_args = []
    for param_name, default in stub.params:
        rendered_params.append(param_name if default is None else f"{param_name}={default}")
        call_args.append(f'"{param_name}": {param_name}')
    signature = ", ".join(rendered_params)
    args_dict = "{" + ", ".join(call_args) + "}"
    return f'def {stub.name}({signature}):\n    return _rpc_call("{stub.name}", {args_dict})\n'


_STUB_TEMPLATE = '''"""Auto-generated PTC tool proxy — do not edit.

The parent process (Sherry) generated this module for one `execute_code`
invocation. It forwards every tool call to the parent over a localhost TCP
RPC socket. The socket lives only for the duration of the script.
"""

import json
import socket
import shlex
import time

_RPC_HOST = "__RPC_HOST__"
_RPC_PORT = __RPC_PORT__


def _rpc_call(tool_name, args):
    """Send one RPC request and block for its response."""
    payload = json.dumps({"tool": tool_name, "args": args}, ensure_ascii=False) + "\\n"
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect((_RPC_HOST, _RPC_PORT))
        sock.sendall(payload.encode("utf-8"))
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\\n" in chunk:
                break
        raw = b"".join(chunks).split(b"\\n", 1)[0]
    finally:
        sock.close()
    if not raw:
        raise RuntimeError("PTC RPC connection closed before a response arrived")
    message = json.loads(raw.decode("utf-8"))
    if not message.get("ok"):
        raise RuntimeError(message.get("error", "PTC tool call failed"))
    return message["result"]


# === Tool wrappers (one per allowed tool) ===
__WRAPPERS__

# === Built-in helpers (no tool call; run locally) ===
def json_parse(text):
    """`json.loads` with strict=False (tolerates control characters)."""
    return json.loads(text, strict=False)


def shell_quote(value):
    """Shell-quote a value for safe interpolation into a command string."""
    return shlex.quote(value)


def retry(fn, max_attempts=3, delay=2):
    """Call `fn` up to `max_attempts` times with exponential backoff."""
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception:
            if attempt == max_attempts - 1:
                raise
            time.sleep(delay * (2 ** attempt))
'''


def generate_stub(host: str, port: int, stubs: list[ToolStub]) -> str:
    """Render the ``sherry_tools.py`` source for ``stubs``."""
    wrappers = "\n".join(_render_wrapper(stub) for stub in stubs)
    if wrappers:
        wrappers += "\n"
    else:
        wrappers = "# (no tools available)\n"
    return (
        _STUB_TEMPLATE.replace("__RPC_HOST__", host)
        .replace("__RPC_PORT__", str(port))
        .replace("__WRAPPERS__", wrappers)
    )
