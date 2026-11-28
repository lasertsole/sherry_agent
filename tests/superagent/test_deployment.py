"""Deployment config and setup probing tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from superagent.deployment import (
    Config,
    Machine,
    execution_root,
    resolve_deployment,
    resolve_machine,
)
from superagent.infra.paths import state_paths
from superagent.setup import (
    SETUP_CODES,
    SetupIssue,
    SetupProbe,
    SetupResult,
    cli_path,
    probe,
    worker_bin_script,
)


def test_config_from_env_reads_all_overrides(monkeypatch):
    monkeypatch.setenv("DSH_SUPERAGENT_STATE_DIR", "/tmp/sa-state")
    monkeypatch.setenv("DSH_SUPERAGENT_SHERRY_URL", "http://example:1234")
    monkeypatch.setenv("DSH_SUPERAGENT_PORT", "4000")
    monkeypatch.setenv("DSH_SUPERAGENT_SECRET", "sekret")
    monkeypatch.setenv("DSH_SUPERAGENT_MCP_TRANSPORT", "sse")
    config = Config.from_env()
    assert config.state_dir == "/tmp/sa-state"
    assert config.sherry_backend_url == "http://example:1234"
    assert config.port == 4000
    assert config.shared_secret == "sekret"
    assert config.mcp_transport == "sse"


def test_config_defaults_are_safe(monkeypatch):
    for key in (
        "DSH_SUPERAGENT_STATE_DIR",
        "DSH_SUPERAGENT_SHERRY_URL",
        "DSH_SUPERAGENT_PORT",
        "DSH_SUPERAGENT_SECRET",
        "DSH_SUPERAGENT_MCP_TRANSPORT",
    ):
        monkeypatch.delenv(key, raising=False)
    config = Config.from_env()
    assert config.port == 3082
    assert config.shared_secret == ""
    assert config.mcp_transport == "stdio"


def test_resolve_deployment_and_machine():
    config, machine = resolve_deployment(Config(state_dir="x"))
    assert config.state_dir == "x"
    assert isinstance(machine, Machine)
    assert machine.posix is (resolve_machine().system != "Windows")


def test_execution_root_uses_state_dir():
    config = Config(state_dir="/tmp/state")
    assert execution_root(config) == Path("/tmp/state/executions")


def test_setup_result_exit_codes():
    result = SetupResult()
    assert result.ok and result.exit_code == 0
    result.add("missingCli", "gh")
    assert not result.ok and result.exit_code == 3
    assert result.as_dict()["issues"][0]["code"] == "missingCli"


def test_setup_issue_rejects_unknown_code():
    with pytest.raises(ValueError):
        SetupIssue(code="bogus")
    for code in SETUP_CODES:
        assert SetupIssue(code=code).code == code


def test_probe_all_codes(tmp_path):
    paths = state_paths(tmp_path / "state")
    config = Config(state_dir=str(paths.root))
    machine = Machine(system="Linux", shell="/bin/sh")
    facts = SetupProbe(
        repo_exists=False,
        cli_paths={"gh": ""},
        credentials=(),
        unmanaged=("worker-bin/rogue",),
    )
    result = probe(config, machine, paths, facts=facts)
    codes = {issue.code for issue in result.issues}
    assert codes == {"missingRepo", "missingCli", "badCredentials", "unmanagedFiles"}
    assert result.exit_code == 3


def test_probe_ok_when_all_facts_present(tmp_path):
    paths = state_paths(tmp_path / "state")
    config = Config(state_dir=str(paths.root))
    machine = Machine(system="Linux", shell="/bin/sh")
    facts = SetupProbe(
        repo_exists=True,
        cli_paths={"gh": "/usr/bin/gh"},
        credentials=("GH_TOKEN",),
        unmanaged=(),
    )
    result = probe(config, machine, paths, facts=facts)
    assert result.ok and result.exit_code == 0


def test_cli_path_and_worker_bin_script(tmp_path):
    assert isinstance(cli_path("definitely-not-a-real-cli-xyz"), str)
    paths = state_paths(tmp_path / "state")
    assert worker_bin_script(paths, "gh") == paths.worker_bin / "gh"
