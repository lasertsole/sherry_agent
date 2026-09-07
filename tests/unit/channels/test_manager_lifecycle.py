"""TDD tests for audit issue #16 — ChannelManager lifecycle.

Audit findings being pinned by these tests:

* ``start_service()`` called ``self._event_loop.run_forever()``, permanently
  blocking the calling thread instead of returning after scheduling.
* The module-level ``channel_manager = ChannelManager()`` singleton loaded
  config, discovered channel plugins and created an event loop at IMPORT
  time — an import side effect for every consumer of the package.

Contract after the fix:

1. ``import channels`` / ``import channels.manager`` instantiate nothing;
   the singleton materializes lazily via ``get_channel_manager()`` (module
   and package both expose it through PEP 562 ``__getattr__``).
2. ``start_service()`` schedules the dispatcher/consumer/channel tasks on
   the event loop and RETURNS — it never drives the loop itself. The caller
   owns ``run_forever()``.
3. ``start_service()`` is idempotent and a no-op without channels.
"""

import asyncio
import os
import signal
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

# Stub plugin discovery only during the import of channels.manager (restore
# afterwards): the pre-fix module builds its singleton at import, which would
# attempt plugin dep installation. Leaking the stub breaks other suites that
# need the real registry.
_real_registry = sys.modules.get("channels.registry")
_registry_stub = types.ModuleType("channels.registry")
_registry_stub.discover_all = lambda: {}
sys.modules["channels.registry"] = _registry_stub
try:
    from channels.manager import ChannelManager  # noqa: E402
finally:
    if _real_registry is not None:
        sys.modules["channels.registry"] = _real_registry
    else:
        sys.modules.pop("channels.registry", None)

REPO_ROOT = Path(__file__).resolve().parents[3]

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

# Stubbed in the subprocesses before importing channels: plugin discovery is
# irrelevant to the lifecycle contract and would try to install plugin deps.
_STUB_REGISTRY = (
    "import sys, types\n"
    "reg = types.ModuleType('channels.registry')\n"
    "reg.discover_all = lambda: {}\n"
    "sys.modules['channels.registry'] = reg\n"
)


def _run_py(code: str, timeout_s: float = 45.0) -> subprocess.CompletedProcess:
    """Run python -c in its own process group; SIGKILL the group on hang.

    A hung child (import side effects) must fail the test, not block CI.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, err = proc.communicate()
        raise AssertionError(
            f"subprocess hung for {timeout_s}s (import side effect?)\nstdout={out}\nstderr={err}"
        ) from None
    return subprocess.CompletedProcess(code, proc.returncode, out, err)


class FakeBus:
    async def consume_inbound(self) -> None:
        await asyncio.Event().wait()  # park forever

    async def consume_outbound(self) -> None:
        await asyncio.Event().wait()  # park forever


class FakeChannel:
    name = "fake"
    started = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        pass


class FakeLoop:
    """Duck-typed loop: records scheduling; run_forever inside start_service is the bug."""

    def __init__(self) -> None:
        self.scheduled: list[Any] = []
        self.run_forever_calls = 0

    def is_running(self) -> bool:
        return False

    def create_task(self, coro):
        self.scheduled.append(coro)
        return coro

    def run_forever(self) -> None:
        self.run_forever_calls += 1

    def close_scheduled(self) -> None:
        for coro in self.scheduled:
            coro.close()
        self.scheduled.clear()


def make_manager(event_loop) -> ChannelManager:
    """Build a ChannelManager without touching config files or plugin discovery."""
    manager = object.__new__(ChannelManager)
    manager._bus = FakeBus()
    manager._channels = {"fake": FakeChannel()}
    manager._config = {"fake": {}}
    manager._event_loop = event_loop
    manager._dispatch_task = None
    manager._inbound_consumer = None
    manager._outbound_consumer = None
    manager._started = False
    return manager


@pytest.fixture
def fake_loop():
    loop = FakeLoop()
    yield loop
    loop.close_scheduled()


class TestLazySingleton:
    def test_import_channels_does_not_load_manager(self):
        """``import channels`` must not import channels.manager or build the singleton."""
        code = (
            _STUB_REGISTRY + "import channels\n"
            "assert 'channels.manager' not in sys.modules, (\n"
            "    'importing channels eagerly loaded channels.manager'\n"
            ")\n"
            "from channels import BaseChannel\n"
            "assert BaseChannel is not None\n"
            "print('PKG_LAZY_OK')\n"
        )
        result = _run_py(code)
        assert result.returncode == 0, (
            f"import side effect:\nstdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "PKG_LAZY_OK" in result.stdout

    def test_import_manager_does_not_instantiate_singleton(self):
        """``import channels.manager`` must leave the singleton unset."""
        code = (
            _STUB_REGISTRY + "import channels.manager as m\n"
            "assert m._channel_manager is None, (\n"
            "    f'import instantiated the singleton: {m._channel_manager!r}'\n"
            ")\n"
            "first = m.get_channel_manager()\n"
            "second = m.get_channel_manager()\n"
            "assert first is second, 'get_channel_manager must be a singleton'\n"
            "assert m.channel_manager is first, 'module attr must expose the singleton'\n"
            "print('LAZY_OK')\n"
        )
        result = _run_py(code)
        assert result.returncode == 0, (
            f"singleton not lazy:\nstdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "LAZY_OK" in result.stdout


class TestStartService:
    def test_schedules_tasks_and_returns_without_running_loop(self, fake_loop):
        manager = make_manager(fake_loop)

        manager.start_service()  # must RETURN, not block on run_forever

        assert fake_loop.run_forever_calls == 0, (
            "start_service must never call run_forever (caller owns the loop)"
        )
        assert len(fake_loop.scheduled) == 4, (
            f"expected dispatcher + 2 consumers + 1 channel start, got {len(fake_loop.scheduled)}"
        )
        assert manager._dispatch_task is not None

    def test_start_service_is_idempotent(self, fake_loop):
        manager = make_manager(fake_loop)

        manager.start_service()
        first_count = len(fake_loop.scheduled)
        manager.start_service()

        assert len(fake_loop.scheduled) == first_count, "second start_service re-scheduled tasks"
        assert fake_loop.run_forever_calls == 0

    def test_no_channels_is_a_noop(self, fake_loop):
        manager = make_manager(fake_loop)
        manager._channels = {}

        manager.start_service()

        assert len(fake_loop.scheduled) == 0
        assert fake_loop.run_forever_calls == 0
        assert manager._dispatch_task is None

    def test_scheduled_tasks_run_when_caller_drives_loop(self):
        """After start_service returns, the caller runs the loop and tasks execute."""
        real_loop = asyncio.new_event_loop()
        try:
            manager = make_manager(real_loop)
            fake_channel = manager._channels["fake"]

            manager.start_service()
            assert not fake_channel.started, "start_service must not execute tasks itself"

            real_loop.run_until_complete(asyncio.sleep(0.05))
            assert fake_channel.started, "channel start task must run once the loop runs"
        finally:
            for task in asyncio.all_tasks(real_loop):
                task.cancel()
            real_loop.run_until_complete(
                asyncio.gather(*asyncio.all_tasks(real_loop), return_exceptions=True)
            )
            real_loop.close()
