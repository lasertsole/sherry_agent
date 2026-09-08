"""TDD tests for audit 2.1.3 — shared ``StreamDriver`` (server/service/stream_driver.py).

Pins the turn-driving frame contract the three sites (agent WS ``_run_stream``,
``WsTurnExecutor._drive``, auto-turn ``_drive_turn``) used to duplicate:
chunk forwarding, meta capture aside, HITL-interrupt/done detection, the
stopped/error frames, and per-site knobs (done-frame fallbacks, HITL flag,
site log hooks, finish cleanup).
"""

import asyncio

import pytest

from server.service.stream_driver import StreamDriver

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class _RecordingDriver(StreamDriver):
    """Test double recording every frame + hook invocation."""

    def __init__(self, session_id="s1", *, interrupt=None, stream_error=None):
        super().__init__(session_id, websocket=object())
        self.frames: list[dict] = []
        self.interrupt_payload = interrupt
        self.stream_error = stream_error
        self.hitl_flagged = False
        self.finished = False
        self.logged_interrupt = False
        self.logged_cancelled = False
        self.logged_error: list[Exception] = []

    async def send_frame(self, payload):
        self.frames.append(payload)

    async def check_interrupt(self):
        return self.interrupt_payload

    def apply_hitl_pending(self):
        self.hitl_flagged = True

    def log_interrupt(self, interrupt):
        self.logged_interrupt = True

    def log_cancelled(self):
        self.logged_cancelled = True

    def log_error(self, exc, elapsed):
        self.logged_error.append(exc)

    async def on_finish(self):
        self.finished = True


def _chunks_source(chunks):
    async def _gen():
        for c in chunks:
            yield c

    return _gen()


def _run(coro):
    return asyncio.run(coro)


class TestChunkForwarding:
    def test_chunks_forwarded_with_event_envelope(self):
        driver = _RecordingDriver(session_id="sess-9")
        chunks = [
            {"type": "text", "content": "hello"},
            {"type": "reasoning", "content": "thinking"},
        ]

        _run(driver.drive(_chunks_source(chunks)))

        assert driver.frames[:2] == [
            {"event": "chunk", "session_id": "sess-9", "type": "text", "content": "hello"},
            {"event": "chunk", "session_id": "sess-9", "type": "reasoning", "content": "thinking"},
        ]

    def test_non_dict_chunks_skipped(self):
        driver = _RecordingDriver()

        _run(driver.drive(_chunks_source(["not-a-dict", {"type": "text", "content": "ok"}])))

        chunk_frames = [f for f in driver.frames if f["event"] == "chunk"]
        assert len(chunk_frames) == 1
        assert chunk_frames[0]["content"] == "ok"


class TestDoneAndInterrupt:
    def test_done_frame_with_meta_captured_aside(self):
        driver = _RecordingDriver()

        _run(
            driver.drive(
                _chunks_source(
                    [
                        {"type": "text", "content": "x"},
                        {
                            "type": "meta",
                            "content": "",
                            "model_name": "deepseek-v3",
                            "input_tokens": 11,
                            "output_tokens": 22,
                            "finish_reason": "",
                        },
                    ]
                )
            )
        )

        # meta never forwarded as a chunk; done carries the captured values.
        assert all(f.get("type") != "meta" for f in driver.frames)
        assert driver.frames[-1] == {
            "event": "done",
            "session_id": "s1",
            "content": "",
            "model_name": "deepseek-v3",
            "input_tokens": 11,
            "output_tokens": 22,
            "finish_reason": "",
        }

    def test_done_frame_uses_class_defaults_without_meta(self):
        driver = _RecordingDriver()

        _run(driver.drive(_chunks_source([{"type": "text", "content": "x"}])))

        assert driver.frames[-1] == {
            "event": "done",
            "session_id": "s1",
            "content": "",
            "model_name": "",
            "input_tokens": 0,
            "output_tokens": 0,
            "finish_reason": "",
        }

    def test_interrupt_sends_hitl_request_and_sets_flag(self):
        driver = _RecordingDriver(interrupt={"tool_name": "bash"})

        _run(driver.drive(_chunks_source([])))

        assert driver.frames == [
            {
                "event": "hitl_request",
                "session_id": "s1",
                "content": {"tool_name": "bash"},
            }
        ]
        assert driver.hitl_flagged is True
        assert driver.logged_interrupt is True

    def test_auto_turn_driver_keeps_null_defaults_and_no_hitl_flag(self):
        from server.service import auto_turn

        driver = auto_turn._AutoTurnStreamDriver("s1", websocket=None)
        assert driver.done_model_name is None
        assert driver.done_input_tokens is None
        assert driver.done_output_tokens is None
        # base-class no-op (auto turn never set the flag)
        driver.apply_hitl_pending()  # must not raise


class TestCancelAndError:
    def test_cancel_sends_stopped_and_reraises(self):
        driver = _RecordingDriver()

        async def _scenario():
            async def _cancelled_gen():
                yield {"type": "text", "content": "partial"}
                raise asyncio.CancelledError

            with pytest.raises(asyncio.CancelledError):
                await driver.drive(_cancelled_gen())

        _run(_scenario())

        assert driver.frames[-1] == {
            "event": "stopped",
            "session_id": "s1",
            "content": "Request cancelled",
        }
        assert driver.logged_cancelled is True
        assert driver.finished is True  # finally still runs

    def test_error_sends_error_frame_without_reraising(self):
        driver = _RecordingDriver()

        async def _scenario():
            async def _boom_gen():
                yield {"type": "text", "content": "x"}
                raise ValueError("model exploded")

            await driver.drive(_boom_gen())  # must NOT raise

        _run(_scenario())

        assert driver.frames[-1] == {
            "event": "error",
            "session_id": "s1",
            "content": "model exploded",
        }
        assert isinstance(driver.logged_error[0], ValueError)
        assert driver.finished is True

    def test_finish_runs_on_every_exit_path(self):
        driver_ok = _RecordingDriver()
        _run(driver_ok.drive(_chunks_source([])))
        assert driver_ok.finished is True


class TestSiteWiring:
    def test_turn_runner_driver_uses_turn_runner_seams(self):
        """The runner driver resolves the lazy seams (async_generate /
        get_pending_interrupt / set_hitl_pending) from turn_runner's module
        globals, so tests patching the site module keep working."""
        from server.service import turn_runner

        driver = turn_runner._WsTurnStreamDriver("s1", websocket=None)
        assert isinstance(driver, StreamDriver)

        async def _no_interrupt(session_id):
            return None

        import unittest.mock

        with unittest.mock.patch.object(turn_runner, "get_pending_interrupt", _no_interrupt):
            assert asyncio.run(driver.check_interrupt()) is None

    def test_auto_turn_driver_check_interrupt_resolves_auto_turn_global(self):
        from server.service import auto_turn

        driver = auto_turn._AutoTurnStreamDriver("s1", websocket=None)

        async def _no_interrupt(session_id):
            return None

        import unittest.mock

        with unittest.mock.patch.object(auto_turn, "get_pending_interrupt", _no_interrupt):
            assert asyncio.run(driver.check_interrupt()) is None

    def test_ws_handler_driver_finish_releases_task_slot_and_calls_on_turn_finished(
        self, monkeypatch
    ):
        from server.trigger.ws import messages as wsm

        finished: list[tuple[str, str | None]] = []

        async def _fake_on_turn_finished(session_id, claim_row_id=None):
            finished.append((session_id, claim_row_id))

        monkeypatch.setattr(wsm.turn_runner, "on_turn_finished", _fake_on_turn_finished)

        driver = wsm._AgentWsStreamDriver(
            "s1", websocket=None, claim_row_id="row-1", stream_kind="resume"
        )

        async def _scenario():
            current = asyncio.current_task()
            wsm._active_tasks["s1"] = current
            await driver.on_finish()

        asyncio.run(_scenario())

        assert finished == [("s1", "row-1")]
        assert "s1" not in wsm._active_tasks
