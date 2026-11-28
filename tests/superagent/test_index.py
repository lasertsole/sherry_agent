"""Process entry, composition root and standalone boot tests."""

from __future__ import annotations

import asyncio

import pytest

from superagent.core.model import SourceRef
from superagent.deployment import Config
from superagent.index import Ports, Runtime, run
from tests.superagent.conftest import make_trigger


@pytest.fixture()
def config(tmp_path):
    """Return an isolated Config pointing at a temp state dir."""
    return Config(state_dir=str(tmp_path / "state"), sherry_backend_url="http://127.0.0.1:9")


def test_build_wires_all_parts_without_touching_external_services(config):
    runtime = Runtime.build(config)
    try:
        assert runtime.paths.database.exists()
        assert runtime.store.schema_version() == 11
        assert runtime.board.all() == {}
        assert runtime.engine.repo is not None
        assert runtime.effects is not None
        assert runtime.outbox is not None
        assert runtime.panel is not None
        assert runtime.ports.commands is not None
        assert runtime.state()["started"] is False
    finally:
        runtime.store.close()


@pytest.mark.asyncio
async def test_startup_shutdown_is_idempotent_and_releases_lock(config):
    first = Runtime.build(config)
    with pytest.raises(Exception):
        Runtime.build(config)  # second process / Store is refused by the file lock
    await first.startup()
    await first.startup()  # idempotent
    assert first.state()["started"] is True
    assert first.state()["schema_version"] == 11
    await first.shutdown()
    await first.shutdown()  # idempotent
    assert first.state()["started"] is False
    # A fresh Store can now claim the released lock.
    second = Runtime.build(config)
    second.store.close()


@pytest.mark.asyncio
async def test_receive_trigger_persists_and_pass_advances(config):
    runtime = Runtime.build(
        config,
        settings={
            "accept_mode": "observe",
            "paused": False,
            "listening": ["github"],
            "rules": {},
            "sources": {},
        },
    )
    await runtime.startup()
    try:
        trigger = make_trigger()
        assert runtime.engine.receive(trigger) == "stored"
        assert runtime.engine.repo.triggers.get(trigger.id) is not None
        await runtime.passes.run_once()
        assert runtime.passes.count == 1
        assert runtime.state()["pending_effects"] >= 0
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_run_stops_on_event(config):
    stop = asyncio.Event()
    task = asyncio.create_task(run(config, stop=stop))
    await asyncio.sleep(0)
    stop.set()
    runtime = await task
    assert runtime.state()["started"] is False


def test_ports_allow_host_override(config):
    class Scope:
        def receive(self, trigger):
            return "custom"

        def bind_thread(self, *args):
            pass

        def thread_closed(self, *args):
            pass

    runtime = Runtime.build(config, ports=Ports(scope=Scope()))
    try:
        assert runtime.engine.scope.receive(make_trigger()) == "custom"
    finally:
        runtime.store.close()


def test_prepare_uses_repository_and_trigger_source(config):
    runtime = Runtime.build(config)
    try:
        prepared = runtime.engine.prepare_ticket(make_trigger(), {})
        assert prepared["name"].startswith("github-")
        assert prepared["directory"].endswith(prepared["name"])
    finally:
        runtime.store.close()


def test_source_ref_key_is_stable():
    a = SourceRef(source="github", id="1")
    b = SourceRef(source="github", id="1")
    assert Runtime.thread_key(a) == Runtime.thread_key(b)
