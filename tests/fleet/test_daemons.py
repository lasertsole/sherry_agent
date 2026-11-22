"""Fleet-owned shared model daemons."""

from __future__ import annotations

from pathlib import Path

import pytest

from fleet.config import IngressConfig
from fleet.daemons import FleetDaemons
from fleet import daemons as daemons_mod

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


def _config(tmp_path: Path) -> IngressConfig:
    return IngressConfig(
        host="127.0.0.1",
        port=8081,
        db_path=tmp_path / "fleet.db",
        jwt_secret="x" * 40,
        static_dir=None,
        fleet_root=tmp_path / "cells",
    )


def test_specs_use_the_shared_daemon_home(tmp_path: Path):
    fleet = FleetDaemons(_config(tmp_path))

    embed = fleet.family("embed")
    vision = fleet.family("vision")

    assert embed.pid_path == tmp_path / "cells" / "daemons" / ".embed_daemon.pid"
    assert vision.pid_path == tmp_path / "cells" / "daemons" / ".vision_daemon.pid"
    assert embed.port != vision.port
    with pytest.raises(ValueError, match="unknown daemon family"):
        fleet.family("nope")


def test_start_passes_the_host_home_and_shared_pid_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fleet = FleetDaemons(_config(tmp_path))
    captured: dict = {}

    states = iter([False, True])
    monkeypatch.setattr(daemons_mod.daemon_base, "is_ready", lambda _spec: next(states, True))
    monkeypatch.setattr(
        daemons_mod.daemon_base,
        "spawn",
        lambda _spec, **kwargs: captured.update(kwargs) or True,
    )
    monkeypatch.setattr(daemons_mod.daemon_base, "wait_until_ready", lambda _spec: True)
    monkeypatch.setattr(daemons_mod.daemon_base, "recorded_pid", lambda _spec: 4242)

    result = fleet.start(["embed"])

    assert captured["extra_env"]["SHERRY_DAEMON_HOME"] == str(tmp_path / "cells" / "daemons")
    assert captured["extra_env"]["SHERRY_HOME"]
    assert result[0]["family"] == "embed"
    assert result[0]["pid"] == 4242
    assert result[0]["ready"] is True


def test_stop_uses_the_same_specs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    fleet = FleetDaemons(_config(tmp_path))
    killed: list[str] = []
    monkeypatch.setattr(daemons_mod.daemon_base, "kill", lambda spec: killed.append(spec.name))

    fleet.stop(["embed", "reranker"])

    assert killed == ["embedding", "reranker"]


def test_local_chat_daemons_need_their_weight_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fleet = FleetDaemons(_config(tmp_path))
    monkeypatch.delenv("SHERRY_FLEET_AUXILIARY_MODEL_PATH", raising=False)
    monkeypatch.delenv("SHERRY_FLEET_VISION_MODEL_PATH", raising=False)
    monkeypatch.delenv("SHERRY_FLEET_VISION_MMPROJ_PATH", raising=False)

    auxiliary = fleet.start(["auxiliary"])[0]
    vision = fleet.start(["vision"])[0]

    assert auxiliary["ready"] is False
    assert "SHERRY_FLEET_AUXILIARY_MODEL_PATH" in auxiliary["error"]
    assert vision["ready"] is False
    assert "SHERRY_FLEET_VISION_MODEL_PATH" in vision["error"]


def test_local_chat_daemons_pass_port_and_weights(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    fleet = FleetDaemons(_config(tmp_path))
    captured: dict = {}
    monkeypatch.setenv("SHERRY_FLEET_VISION_MODEL_PATH", "/models/vision.gguf")
    monkeypatch.setenv("SHERRY_FLEET_VISION_MMPROJ_PATH", "/models/mmproj.gguf")
    states = iter([False, True])
    monkeypatch.setattr(daemons_mod.daemon_base, "is_ready", lambda _spec: next(states, True))
    monkeypatch.setattr(
        daemons_mod.daemon_base,
        "spawn",
        lambda _spec, **kwargs: captured.update(kwargs) or True,
    )
    monkeypatch.setattr(daemons_mod.daemon_base, "wait_until_ready", lambda _spec: True)
    monkeypatch.setattr(daemons_mod.daemon_base, "recorded_pid", lambda _spec: 4242)

    result = fleet.start(["vision"])[0]

    env = captured["extra_env"]
    assert env["SHERRY_LOCAL_LLM_DAEMON_PORT"] == str(fleet.family("vision").port)
    assert env["SHERRY_LOCAL_LLM_MODEL_PATH"] == "/models/vision.gguf"
    assert env["SHERRY_LOCAL_LLM_MMPROJ_PATH"] == "/models/mmproj.gguf"
    assert result["ready"] is True
