"""Unit tests for the PTC stub generator."""

from __future__ import annotations

import pytest

from agent.tools.file_tools import (
    build_patch_file_tool,
    build_read_file_tool,
    build_search_files_tool,
    build_write_file_tool,
)
from agent.tools.ptc.stub_generator import (
    ToolStub,
    generate_stub,
    tool_specs_from_base_tools,
)
from agent.tools.terminal import build_terminal_tool
from agent.tools.web_search import build_web_search_tool

pytestmark = [pytest.mark.unit]

_REAL_TOOL_BUILDERS = (
    build_read_file_tool,
    build_write_file_tool,
    build_patch_file_tool,
    build_search_files_tool,
    build_terminal_tool,
    build_web_search_tool,
)


def _real_tools() -> list:
    return [builder() for builder in _REAL_TOOL_BUILDERS]


def test_generate_stub_embeds_endpoint_and_helpers() -> None:
    source = generate_stub("127.0.0.1", 4321, [ToolStub("read_file", (("file_path", None),))])
    compile(source, "sherry_tools.py", "exec")
    assert '_RPC_HOST = "127.0.0.1"' in source
    assert "_RPC_PORT = 4321" in source
    assert "def _rpc_call(" in source
    assert "def json_parse(" in source
    assert "def shell_quote(" in source
    assert "def retry(" in source


def test_specs_match_real_tool_signatures_and_exclude_injected_state() -> None:
    specs = {spec.name: spec for spec in tool_specs_from_base_tools(_real_tools())}
    assert set(specs) == {
        "read_file",
        "write_file",
        "patch_file",
        "search_files",
        "terminal",
        "web_search",
    }
    assert specs["read_file"].params == (
        ("file_path", None),
        ("offset", "1"),
        ("limit", "500"),
    )
    assert specs["write_file"].params == (
        ("file_path", None),
        ("text", None),
        ("append", "False"),
    )
    assert specs["search_files"].params == (
        ("pattern", None),
        ("target", "'content'"),
        ("path", "'.'"),
        ("file_glob", "None"),
        ("limit", "50"),
        ("offset", "0"),
        ("context", "0"),
    )
    assert specs["terminal"].params == (
        ("commands", None),
        ("sandbox", "True"),
    )
    # session_id is InjectedState — never surfaced in a stub signature.
    for spec in specs.values():
        assert all(name != "session_id" for name, _ in spec.params)


def test_wrappers_forward_to_rpc_call() -> None:
    specs = tool_specs_from_base_tools(_real_tools())
    source = generate_stub("127.0.0.1", 9999, specs)
    namespace: dict = {}
    exec(compile(source, "sherry_tools.py", "exec"), namespace)  # noqa: S102 - generated stub under test

    recorded: list[tuple[str, dict]] = []

    def _record(tool_name, args):
        recorded.append((tool_name, args))
        return {"ok": True}

    namespace["_rpc_call"] = _record
    namespace["read_file"]("f.txt", 2, 3)
    namespace["search_files"]("needle")
    assert recorded[0] == ("read_file", {"file_path": "f.txt", "offset": 2, "limit": 3})
    assert recorded[1] == (
        "search_files",
        {
            "pattern": "needle",
            "target": "content",
            "path": ".",
            "file_glob": None,
            "limit": 50,
            "offset": 0,
            "context": 0,
        },
    )


def test_helpers_run_locally() -> None:
    source = generate_stub("127.0.0.1", 1, [])
    namespace: dict = {}
    exec(compile(source, "sherry_tools.py", "exec"), namespace)  # noqa: S102
    assert namespace["json_parse"]('{"a": 1}') == {"a": 1}
    assert namespace["shell_quote"]("a b") == "'a b'"
    assert "def retry(" in source


def test_no_tools_renders_placeholder() -> None:
    source = generate_stub("127.0.0.1", 1, [])
    compile(source, "sherry_tools.py", "exec")
    assert "(no tools available)" in source
