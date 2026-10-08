"""Explicit startup contract for the channel / subagent trigger modules.

Importing either module must not start background work; the channel
event-loop thread and the subagent registry scheduling are now explicit,
idempotent ``start()`` calls invoked by ``server.trigger.init()``. These
subprocess tests pin the isolation (import) and the idempotency (start).
"""

import os
import subprocess
import sys

import pytest

# The child process imports the whole server: measured 28-49 s on an idle box and
# several minutes under load, so the marker only has to be looser than the inner
# bound (which is the real, bounded failure) — a tighter marker fired mid-import
# and killed the whole group run.
pytestmark = [pytest.mark.integration, pytest.mark.timeout(900)]

#: Seconds a child gets to import the server before the test fails (bounded).
_CHILD_TIMEOUT_S = 600

_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)


def _run(code: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        timeout=_CHILD_TIMEOUT_S,
    )
    assert result.returncode == 0, result.stderr
    return result


def test_channel_import_is_side_effect_free_and_start_is_idempotent() -> None:
    _run(
        "from server.trigger import channels\n"
        "import server.trigger.channels.core as core\n"
        "assert core._channel_thread is None, 'import started the channel thread'\n"
        "started = []\n"
        "core._run = lambda: started.append(1)\n"
        "channels.start()\n"
        "assert core._channel_thread is not None, 'start did not create the thread'\n"
        "assert started == [1]\n"
        "channels.start()\n"
        "assert started == [1], 'start must be idempotent'\n"
    )


def test_subagent_import_does_not_schedule_and_start_consults_loop() -> None:
    _run(
        "from unittest.mock import patch\n"
        "from channels.manager import channel_manager\n"
        "calls = []\n"
        "with patch.object(\n"
        "    channel_manager,\n"
        "    'get_event_loop',\n"
        "    side_effect=lambda: (calls.append('get'), None)[1],\n"
        "):\n"
        "    import server.trigger.subagent.core as core\n"
        "    assert calls == [], 'import scheduled startup'\n"
        "    core.start()\n"
        "    assert calls == ['get'], 'start must consult the event loop'\n"
    )
