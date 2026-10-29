"""The model-facing schema of the three migrated tools, frozen byte-for-byte.

The three write/exec tools used to inherit from ``langchain-community`` /
``langchain-experimental`` base classes (both archived). They were re-based on
plain ``BaseTool`` so the dependencies could be removed — and the bar for that
migration is that the LLM sees EXACTLY the same tool surface as before: same
names, same argument names, same descriptions, same required set. These
goldens were captured from the pre-migration build (see the migration commit)
and must not change without a deliberate, user-visible decision.
"""

from __future__ import annotations

import warnings

import pytest

from agent.tools.file_tools.write_file import build_write_file_tool
from agent.tools.python_repl import build_python_repl_tool
from agent.tools.terminal import build_terminal_tool

pytestmark = [pytest.mark.unit]

WRITE_FILE_SCHEMA = {
    "description": (
        "Write file to disk. Creating a file needs nothing else; overwriting a file "
        "that already exists requires having read it in this session first "
        "(read_file) — otherwise the call is refused. Set append=true to add to an "
        "existing file without overwriting it."
    ),
    "properties": {
        "append": {
            "default": False,
            "description": "Whether to append to an existing file.",
            "title": "Append",
            "type": "boolean",
        },
        "file_path": {"description": "name of file", "title": "File Path", "type": "string"},
        "text": {"description": "text to write to file", "title": "Text", "type": "string"},
    },
    "required": ["file_path", "text"],
    "title": "write_file",
    "type": "object",
}

TERMINAL_SCHEMA = {
    "description": "Run shell commands on this Linux machine.",
    "properties": {
        "commands": {
            "anyOf": [{"type": "string"}, {"items": {"type": "string"}, "type": "array"}],
            "description": "List of shell commands to run. Deserialized using json.loads",
            "title": "Commands",
        },
        "run_in_background": {
            "default": False,
            "description": (
                "Run the command in the background and return immediately with a task "
                "id (the process keeps running; its output is captured to a file). Use "
                "terminal_bg(action='list') to see this session's tasks, "
                "terminal_bg(action='output', task_id=...) to read one's output, and "
                "terminal_bg(action='kill', task_id=...) to stop ONE task's process tree "
                "(the current turn and the session's other tasks keep running). "
                "You are notified when it exits. Good for servers, "
                "watchers, and anything longer than the timeout."
            ),
            "title": "Run In Background",
            "type": "boolean",
        },
        "sandbox": {
            "default": True,
            "description": (
                "Sandbox toggle. When false, runs in the raw env-scrubbed environment; "
                "the main session will request human approval, while "
                "subagents/background agents are denied"
            ),
            "title": "Sandbox",
            "type": "boolean",
        },
        "timeout": {
            "anyOf": [{"type": "integer"}, {"type": "null"}],
            "default": None,
            "description": (
                "Optional max execution time in seconds. Defaults to 30s, max 600s. "
                "Use a longer value for compilation, installation, or long builds."
            ),
            "title": "Timeout",
        },
    },
    "required": ["commands"],
    "title": "terminal",
    "type": "object",
}

PYTHON_REPL_SCHEMA = {
    "description": (
        "A Python shell. Use this to execute python commands. Input should be a valid "
        "python command. If you want to see the output of a value, you should print it "
        "out with `print(...)`."
    ),
    "properties": {
        "query": {"title": "Query", "type": "string"},
        "sandbox": {"default": True, "title": "Sandbox", "type": "boolean"},
    },
    "required": ["query"],
    "title": "python_repl",
    "type": "object",
}


@pytest.mark.parametrize(
    "build,expected",
    [
        (build_write_file_tool, WRITE_FILE_SCHEMA),
        (build_terminal_tool, TERMINAL_SCHEMA),
        (build_python_repl_tool, PYTHON_REPL_SCHEMA),
    ],
    ids=["write_file", "terminal", "python_repl"],
)
def test_the_llm_facing_schema_is_byte_identical(build, expected):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        schema = build().tool_call_schema.model_json_schema()

    assert schema == expected, "the tool surface the model sees must not drift"


def test_the_three_tools_no_longer_import_archived_packages():
    """The migration's whole point: the tool modules stand on their own.

    Checked on the IMPORT graph (the docstrings still name the packages when
    explaining what the local schemas reproduce — that is provenance, not a
    dependency).
    """
    import ast

    import agent.tools.file_tools.write_file as write_file_module
    import agent.tools.python_repl as repl_module
    import agent.tools.terminal as terminal_module

    archived = ("langchain_community", "langchain_experimental")
    for module in (write_file_module, terminal_module, repl_module):
        source = module.__file__ or ""
        tree = ast.parse(open(source, encoding="utf-8").read())
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        offenders = [name for name in imported if name.startswith(archived)]
        assert offenders == [], f"{source} still imports {offenders}"
