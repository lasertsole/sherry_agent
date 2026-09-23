"""Integration tests for the PTC execution runner (real child process + RPC)."""

from __future__ import annotations

import asyncio
import json
import os
import time

import pytest
from pydantic import BaseModel

from agent.tools.ptc import runner
from agent.tools.ptc.runner import run_ptc
from config.features import PTC

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]


def _config(**overrides) -> dict:
    cfg = dict(PTC)
    cfg.update(overrides)
    return cfg


class _ReadInput(BaseModel):
    file_path: str


class _FakeReadTool:
    name = "read_file"
    tool_call_schema = _ReadInput
    metadata: dict = {}

    def __init__(self, result=None) -> None:
        self.calls = 0
        self._result = result if result is not None else {"content": "hello"}

    async def ainvoke(self, args, config=None):
        self.calls += 1
        return self._result


def _run(code: str, cfg: dict, tools_map=None) -> dict:
    tools_map = tools_map if tools_map is not None else {"read_file": _FakeReadTool()}
    raw = asyncio.run(run_ptc(code, tools_map, "agent:executor:subagent:z", cfg))
    return json.loads(raw)


def test_end_to_end_tool_call() -> None:
    tool = _FakeReadTool()
    result = _run(
        "from sherry_tools import read_file\ndata = read_file('x.txt')\nprint(data['content'])\n",
        _config(),
        {"read_file": tool},
    )
    assert result["status"] == "ok"
    assert result["tool_calls_made"] == 1
    assert tool.calls == 1
    assert result["output"].strip() == "hello"


def test_restricted_builtins_block_open() -> None:
    result = _run("open('/etc/passwd')\n", _config())
    assert result["status"] == "error"
    assert "name 'open' is not defined" in result["error"]


def test_import_allowlist_blocks_os_but_allows_sherry_tools() -> None:
    blocked = _run("import os\nprint(os.getcwd())\n", _config())
    assert blocked["status"] == "error"
    assert "not allowed in PTC" in blocked["error"]

    allowed = _run(
        "import json\nfrom sherry_tools import read_file\nprint(json.dumps(read_file('x')))\n",
        _config(),
    )
    assert allowed["status"] == "ok"


def test_timeout_kills_child() -> None:
    started = time.time()
    result = _run("import time\ntime.sleep(30)\n", _config(ptc_timeout_seconds=1))
    elapsed = time.time() - started
    assert result["status"] == "timeout"
    assert result["exit_code"] != 0
    assert elapsed < 15


def test_stdout_truncation() -> None:
    result = _run("print('A' * 4000)\n", _config(ptc_max_stdout_bytes=40))
    assert result["status"] == "ok"
    assert result["output"].startswith("A" * 40)
    assert "truncated" in result["output"]


def test_call_budget_exceeded_terminates() -> None:
    code = (
        "from sherry_tools import read_file\nread_file('a')\nread_file('b')\nprint('unreachable')\n"
    )
    result = _run(code, _config(ptc_max_tool_calls=1))
    assert result["status"] == "budget_exceeded"
    assert result["tool_calls_made"] == 1


def test_env_scrub_removes_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_SERVICE_API_KEY", "super-secret-value")
    captured: dict = {}
    real_popen = runner.subprocess.Popen

    def _capture(*args, **kwargs):
        captured["env"] = kwargs.get("env")
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(runner.subprocess, "Popen", _capture)
    result = _run("print('ok')\n", _config())
    assert result["status"] == "ok"
    env = captured["env"]
    assert "FAKE_SERVICE_API_KEY" not in env
    assert "super-secret-value" not in set(env.values())
    assert env["PYTHONPATH"].startswith("/") or env["PYTHONPATH"]


def test_success_exit_code_is_zero() -> None:
    result = _run("print('done')\n", _config())
    assert result["status"] == "ok"
    assert result["exit_code"] == 0
    assert result["error"] == ""


def test_no_tool_import_needed_for_pure_compute() -> None:
    result = _run("print(sum(range(10)))\n", _config(), {})
    assert result["status"] == "ok"
    assert result["output"].strip() == "45"


def test_orphan_free_temp_dirs_cleaned() -> None:
    import glob
    import tempfile

    before = set(glob.glob(os.path.join(tempfile.gettempdir(), runner.PTC_TMP_PREFIX + "*")))
    _run("print('x')\n", _config())
    after = set(glob.glob(os.path.join(tempfile.gettempdir(), runner.PTC_TMP_PREFIX + "*")))
    assert after <= before
