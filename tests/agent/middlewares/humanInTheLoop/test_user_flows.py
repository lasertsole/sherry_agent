"""UC-01 ~ UC-16 — 用户操作模拟回归（封闭：无网络 / 无真实 LLM / 无 GGUF）。

场景清单见 tests/REGRESSION_PLAN.md §3。每个 UC 模拟一条真实用户操作路径，
组合多个组件（存储 / cron / HITL / 多模态 / 总线 / 中间件钩子），验证端到端行为。
"""

from __future__ import annotations

import asyncio
import base64
import io
import time
from types import SimpleNamespace

import pytest
from PIL import Image
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from context_engine.store import core as store_core
from context_engine.store.db import _migrate

pytestmark = [pytest.mark.module, pytest.mark.timeout(120), pytest.mark.regression]
# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _png_base64() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), color=(10, 20, 30)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


@pytest.fixture()
def store_db(tmp_path):
    """Production-shaped store on a temp file (messages + FTS + ts_ms)."""
    import sqlite3

    db = sqlite3.connect(str(tmp_path / "store.db"))
    db.row_factory = sqlite3.Row
    _migrate(db)
    with PatchStoreDb(db):
        yield db
    db.close()


class PatchStoreDb:
    """Point store/core.py's module singleton at a temp connection."""

    def __init__(self, db):
        self._db = db
        self._saved = None

    def __enter__(self):
        self._saved = store_core._db
        store_core._db = self._db
        return self._db

    def __exit__(self, *exc):
        store_core._db = self._saved


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# UC-01 chat persistence flow
# ---------------------------------------------------------------------------


class TestUC01ChatPersistence:
    def test_send_reply_reload_history(self, store_db):
        _run(store_core.add_messages("uc01", [HumanMessage(content="你好，介绍一下自己")]))
        _run(store_core.add_messages("uc01", [AIMessage(content="我是 Sherry")]))

        assert store_core.get_max_turn_num("uc01") == 2
        history = store_core.get_history_by_turn_page("uc01")
        assert len(history) == 2
        # newest turn first
        assert history[0]["content"] == "我是 Sherry"
        assert history[1]["content"] == "你好，介绍一下自己"
        # client contract: 14-char timestamp, no internal column
        assert len(history[0]["timestamp"]) == 14
        assert "ts_ms" not in history[0]

    def test_turn_numbers_increment_per_call(self, store_db):
        for i in range(3):
            _run(store_core.add_messages("uc01b", [HumanMessage(content=f"m{i}")]))
        turns = {r["turn_num"] for r in store_core.get_history_by_turn_page("uc01b")}
        assert turns == {1, 2, 3}


# ---------------------------------------------------------------------------
# UC-02 session list
# ---------------------------------------------------------------------------


class TestUC02SessionList:
    def test_order_title_and_subagent_exclusion(self, store_db):
        _run(store_core.add_messages("agent:main:subagent:xyz", [HumanMessage(content="内部")]))
        _run(store_core.add_messages("sess_early", [HumanMessage(content="早上的会话")]))
        _run(store_core.add_messages("sess_late", [AIMessage(content="占位")]))
        _run(store_core.add_messages("sess_late", [HumanMessage(content="最新的会话")]))

        sessions = store_core.get_session_ids()
        ids = [s["session_id"] for s in sessions]

        assert "agent:main:subagent:xyz" not in ids, "子代理会话必须排除"
        assert ids[0] == "sess_late", "最近活动的会话排最前"
        latest = next(s for s in sessions if s["session_id"] == "sess_late")
        assert latest["title"] == "最新的会话"
        assert len(latest["last_time"]) == 14


# ---------------------------------------------------------------------------
# UC-03 clear session
# ---------------------------------------------------------------------------


class TestUC03ClearSession:
    def test_delete_removes_history_and_listing(self, store_db):
        _run(store_core.add_messages("uc03", [HumanMessage(content="x")]))
        assert store_core.get_session_ids() != []

        deleted = store_core.delete_messages_by_session("uc03")

        assert deleted == 1
        assert store_core.get_history_by_turn_page("uc03") == []
        assert all(s["session_id"] != "uc03" for s in store_core.get_session_ids())


# ---------------------------------------------------------------------------
# UC-04 history paging
# ---------------------------------------------------------------------------


class TestUC04HistoryPaging:
    def test_five_turns_three_pages(self, store_db):
        for i in range(5):
            _run(store_core.add_messages("uc04", [HumanMessage(content=f"m{i}")]))

        p1 = store_core.get_history_by_turn_page("uc04", turn_page_size=2, turn_page_num=1)
        p2 = store_core.get_history_by_turn_page("uc04", turn_page_size=2, turn_page_num=2)
        p3 = store_core.get_history_by_turn_page("uc04", turn_page_size=2, turn_page_num=3)

        assert [r["turn_num"] for r in p1] == [5, 4]
        assert [r["turn_num"] for r in p2] == [3, 2]
        assert [r["turn_num"] for r in p3] == [1]
        # Page order newest-first with no overlap
        all_turns = [r["turn_num"] for r in (*p1, *p2, *p3)]
        assert all_turns == [5, 4, 3, 2, 1]

    def test_min_turn_num_clamps(self, store_db):
        for i in range(4):
            _run(store_core.add_messages("uc04b", [HumanMessage(content=f"m{i}")]))
        rows = store_core.get_history_by_turn_page(
            "uc04b", min_turn_num=2, turn_page_size=10, turn_page_num=1
        )
        assert {r["turn_num"] for r in rows} == {2, 3, 4}


# ---------------------------------------------------------------------------
# UC-05 full-text search flow
# ---------------------------------------------------------------------------


class TestUC05SearchFlow:
    def test_persist_then_search(self, store_db, monkeypatch):
        from context_engine import core as ce

        monkeypatch.setattr(ce, "_db", store_db)
        monkeypatch.setattr(ce, "_lock", __import__("threading").Lock())
        _run(
            store_core.add_messages(
                "uc05", [HumanMessage(content="我们讨论了 docker compose 部署")]
            )
        )
        _run(store_core.add_messages("uc05", [AIMessage(content="kubernetes 是另一回事")]))

        from context_engine import search_messages

        hits = search_messages(query="docker", session_id="uc05")
        assert len(hits) == 1
        assert hits[0]["session_id"] == "uc05"

    def test_pathological_query_bounded(self, store_db, monkeypatch):
        from context_engine import core as ce, search_messages

        monkeypatch.setattr(ce, "_db", store_db)
        monkeypatch.setattr(ce, "_lock", __import__("threading").Lock())

        query = " OR ".join(f"tok{i}" for i in range(50_000))
        start = time.perf_counter()
        results = search_messages(query=query, session_id="uc05")
        elapsed = time.perf_counter() - start

        assert isinstance(results, list)
        assert elapsed < 1.5, f"上限未生效：{elapsed:.2f}s"

    def test_special_chars_never_raise(self, store_db, monkeypatch):
        from context_engine import core as ce, search_messages

        monkeypatch.setattr(ce, "_db", store_db)
        monkeypatch.setattr(ce, "_lock", __import__("threading").Lock())

        for q in ("(((", '"unclosed', "AND OR", "a" * 500, "***"):
            assert isinstance(search_messages(query=q, session_id="uc05"), list)


# ---------------------------------------------------------------------------
# UC-06 HITL command approval
# ---------------------------------------------------------------------------


class TestUC06HitlApproval:
    @pytest.fixture()
    def pipeline(self):
        from agent.middlewares.humanInTheLoop.approval import ApprovalPipeline
        from agent.middlewares.humanInTheLoop.types import HITLConfig

        return ApprovalPipeline(HITLConfig(), lambda *a, **k: None)

    def test_hardline_command_denied(self, pipeline):
        res = pipeline.check_command("rm -rf /", "uc06")
        assert res.approved is False

    def test_normal_command_approved(self, pipeline):
        res = pipeline.check_command("ls -la", "uc06")
        assert res.approved is True

    def test_dangerous_command_escalates(self, pipeline):
        res = pipeline.check_command("DROP TABLE users", "uc06")
        assert res.approved is False  # escalated to human approval (decision None)

    def test_yolo_bypasses_dangerous_but_not_hardline(self):
        from agent.middlewares.humanInTheLoop.approval import ApprovalPipeline
        from agent.middlewares.humanInTheLoop.types import HITLConfig

        pipe = ApprovalPipeline(HITLConfig(yolo_mode=True), lambda *a, **k: None)
        assert pipe.check_command("DROP TABLE users", "uc06y").approved is True
        assert pipe.check_command("rm -rf /", "uc06y").approved is False, "hardline 先于 YOLO"

    def test_deny_rules_layer(self):
        from agent.middlewares.humanInTheLoop.approval import ApprovalPipeline
        from agent.middlewares.humanInTheLoop.types import HITLConfig

        pipe = ApprovalPipeline(HITLConfig(deny_rules=["git push*"]), lambda *a, **k: None)
        assert pipe.check_command("git push origin main", "uc06d").approved is False
        assert pipe.check_command("git status", "uc06d").approved is True


# ---------------------------------------------------------------------------
# UC-07 / UC-08 cron user operations
# ---------------------------------------------------------------------------


@pytest.fixture()
def cron_svc(tmp_path, monkeypatch):
    from skills.builtin.core.cron.scripts import base as cron_base
    from skills.builtin.core.cron.scripts.types import CronJobState

    svc = cron_base.CronService()
    svc.store_path = tmp_path / "cron_jobs.json"
    monkeypatch.setattr(cron_base, "ROOT_DIR", tmp_path)

    ran: list[str] = []

    async def _on_job(job):
        ran.append(job.id)

    svc.set_on_job(_on_job)
    svc._load_store()

    def _add(job_id: str, every_ms: int = 60_000, kind: str = "every", at_ms=None):
        from skills.builtin.core.cron.scripts.types import (
            CronJob,
            CronPayload,
            CronSchedule,
        )

        if kind == "at":
            schedule = CronSchedule(kind="at", at_ms=at_ms)
        else:
            schedule = CronSchedule(kind="every", every_ms=every_ms)
        job = CronJob(
            id=job_id,
            name=job_id,
            enabled=True,
            schedule=schedule,
            payload=CronPayload(message="tick"),
            state=CronJobState(
                next_run_at_ms=cron_base._compute_next_run(
                    schedule, cron_base._now_ms(), anchor_ms=cron_base._now_ms()
                )
                if kind == "every"
                else at_ms
            ),
        )
        svc._store.jobs.append(job)
        svc._save_store()
        return job

    return SimpleNamespace(svc=svc, ran=ran, _add=_add)


class TestUC07CronUserOps:
    def test_add_run_remove_flow(self, cron_svc):
        svc, ran = cron_svc.svc, cron_svc.ran
        job = cron_svc._add("uc07_job")

        assert asyncio.run(svc.run_job("uc07_job", force=True)) is True
        assert ran == ["uc07_job"], "手动触发立即执行回调"
        assert job.state.next_run_at_ms is not None, "every 任务触发后推进网格"

        assert svc.remove_job("uc07_job") == "removed"
        assert svc.get_job("uc07_job") is None

    def test_remove_protected_system_job_refused(self, cron_svc):
        from skills.builtin.core.cron.scripts.types import (
            CronJob,
            CronPayload,
            CronSchedule,
        )

        job = CronJob(
            id="sys001",
            name="dream",
            schedule=CronSchedule(kind="every", every_ms=60_000),
            payload=CronPayload(kind="system_event", message="x"),
        )
        cron_svc.svc._load_store()
        cron_svc.svc._store.jobs.append(job)
        cron_svc.svc._save_store()

        assert cron_svc.svc.remove_job("sys001") == "protected"

    def test_run_disabled_without_force_refused(self, cron_svc):
        job = cron_svc._add("uc07_disabled")
        job.enabled = False
        cron_svc.svc._save_store()

        assert asyncio.run(cron_svc.svc.run_job("uc07_disabled")) is False
        assert asyncio.run(cron_svc.svc.run_job("uc07_disabled", force=True)) is True


class TestUC08CronDueBatch:
    def test_on_timer_executes_all_due_jobs(self, cron_svc):
        from skills.builtin.core.cron.scripts import base as cron_base

        now = cron_base._now_ms()
        j1 = cron_svc._add("due1", every_ms=60_000)
        j2 = cron_svc._add("due2", every_ms=60_000)
        j1.state.next_run_at_ms = now - 1_000
        j2.state.next_run_at_ms = now - 1_000
        cron_svc.svc._save_store()

        asyncio.run(cron_svc.svc._on_timer())

        assert sorted(cron_svc.ran) == ["due1", "due2"]
        # Fixed-phase grid: after firing, the slot must land after now (drift-fix regression #22)
        # Note: _on_timer reloads the store — assert against the reloaded objects
        j1_after = cron_svc.svc.get_job("due1")
        j2_after = cron_svc.svc.get_job("due2")
        assert j1_after.state.next_run_at_ms > now
        assert j2_after.state.next_run_at_ms > now


# ---------------------------------------------------------------------------
# UC-09 multimodal message flow
# ---------------------------------------------------------------------------


class TestUC09MultimodalFlow:
    @pytest.fixture()
    def mm(self, tmp_path, monkeypatch):
        from agent.middlewares import multimodal_processor as mm_mod

        monkeypatch.setattr(mm_mod, "SRC_DIR", tmp_path / "src")
        return mm_mod.MultimodalProcessor(), tmp_path / "src"

    def test_image_base64_saved_and_hint_injected(self, mm):
        mw, src = mm
        b64 = _png_base64()
        msg = HumanMessage(
            content=[
                {"type": "text", "text": "看这张图"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ]
        )
        state = {"session_id": "uc09", "messages": [msg]}

        mw._before_agent_impl(state)

        # Persisted to disk (both temp + media copies)
        saved = list((src / "uc09" / "media").glob("*.png"))
        assert len(saved) == 1
        # Prompt injection
        text_block = msg.content[0]["text"]
        assert "image_to_text" in text_block and "1 image(s)" in text_block
        # Persisted path in kwargs
        assert msg.additional_kwargs["images"] == [saved[0].as_posix()]

    def test_old_image_blocks_stripped_from_history(self, mm):
        mw, src = mm
        old = HumanMessage(
            content=[
                {"type": "text", "text": "旧的"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ]
        )
        current = HumanMessage(content=[{"type": "text", "text": "新的"}])
        state = {"session_id": "uc09b", "messages": [old, current]}

        mw._before_agent_impl(state)

        assert old.content == "旧的", "历史消息的 base64 必须剥离"
        assert current.content[0]["type"] == "text"

    def test_expired_temp_files_cleaned(self, mm):
        mw, src = mm
        session_dir = src / "uc09c" / "mutil_temp"
        session_dir.mkdir(parents=True, exist_ok=True)

        now_ms = int(time.time() * 1000)
        week_ms = 7 * 24 * 60 * 60 * 1000
        (session_dir / f"{now_ms - week_ms - 1000}.png").write_bytes(b"old")
        (session_dir / f"{now_ms}.png").write_bytes(b"new")
        (session_dir / "garbage.txt").write_bytes(b"junk")

        mw._after_agent_impl({"session_id": "uc09c"})

        remaining = {p.name for p in session_dir.iterdir()}
        assert remaining == {f"{now_ms}.png"}, "过期/非时间戳文件必须删除"


# ---------------------------------------------------------------------------
# UC-10 message bus
# ---------------------------------------------------------------------------


class TestUC10MessageBus:
    def test_inbound_order_preserved(self):
        from bus.core import MessageBus
        from type.bus import InboundMessage

        bus = MessageBus(maxsize=8)

        async def flow():
            for i in range(3):
                await bus.publish_inbound(
                    InboundMessage(channel="qq", sender_id="u", chat_id="c", content=f"m{i}")
                )
            out = []
            for _ in range(3):
                out.append(await bus.consume_inbound())
            return out

        msgs = asyncio.run(flow())
        assert [m.content for m in msgs] == ["m0", "m1", "m2"]

    def test_outbound_round_trip(self):
        from bus.core import MessageBus
        from type.bus import OutboundMessage

        bus = MessageBus(maxsize=4)

        async def flow():
            msg = OutboundMessage(channel="qq", chat_id="c", content="reply")
            await bus.publish_outbound(msg)
            return await bus.consume_outbound()

        assert asyncio.run(flow()).content == "reply"


# ---------------------------------------------------------------------------
# UC-11 budget exhaustion
# ---------------------------------------------------------------------------


class TestUC11BudgetExhaustion:
    def test_terminal_message_after_budget(self):
        from agent.middlewares.iteration_budget import IterationBudget

        mw = IterationBudget(2)
        state = {"session_id": "uc11", "messages": [HumanMessage(content="x")]}
        mw.before_agent(state, None)  # mixin: reset budget

        req = SimpleNamespace(state=state)
        calls = []

        def handler(r):
            calls.append(1)
            return "MODEL_RESPONSE"

        assert mw.wrap_model_call(req, handler) == "MODEL_RESPONSE"
        assert mw.wrap_model_call(req, handler) == "MODEL_RESPONSE"

        terminal = mw.wrap_model_call(req, handler)
        assert isinstance(terminal, AIMessage), "预算耗尽必须返回终态消息"
        assert calls == [1, 1], "耗尽后 handler 不再调用"
        assert "exhausted" in terminal.content

    def test_new_turn_resets_budget(self):
        from agent.middlewares.iteration_budget import IterationBudget

        mw = IterationBudget(1)
        state = {"session_id": "uc11r", "messages": [HumanMessage(content="x")]}
        req = SimpleNamespace(state=state)
        mw.before_agent(state, None)

        assert isinstance(mw.wrap_model_call(req, lambda r: "R1"), AIMessage) is False

        mw.before_agent(state, None)  # new turn resets the budget
        assert mw.wrap_model_call(req, lambda r: "R2") == "R2"


# ---------------------------------------------------------------------------
# UC-12 guardrail escalation flow
# ---------------------------------------------------------------------------


class TestUC12GuardrailsEscalation:
    def test_exact_failure_warn_then_block(self):
        from agent.middlewares.tool_guardrails import (
            ToolCallGuardrailConfig,
            ToolGuardrails,
        )

        mw = ToolGuardrails(config=ToolCallGuardrailConfig(recovery_mode_enabled=False))
        session = {"session_id": "uc12"}
        request = SimpleNamespace(
            state=session,
            tool_call={"id": "c1", "name": "terminal", "args": {"command": "x"}},
            tool=None,
        )

        def failing_handler(r):
            return ToolMessage(content="boom", status="error", tool_call_id="c1", name="terminal")

        # Failure x1: pass through
        r1 = mw.wrap_tool_call(request, failing_handler)
        assert r1.content == "boom"
        # Failure x2: WARN (warning appended)
        r2 = mw.wrap_tool_call(request, failing_handler)
        assert "boom" in r2.content and len(r2.content) > len("boom")
        # Failures reach the block threshold: pre-check intercepts, handler no longer called
        for _ in range(3):
            mw.wrap_tool_call(request, failing_handler)
        called = []
        r6 = mw.wrap_tool_call(request, lambda r: called.append(1) or ToolMessage(content="ran"))
        assert called == [], "BLOCK 后必须预检拦截"
        assert r6.status == "error"


# ---------------------------------------------------------------------------
# UC-13 output repetition guard
# ---------------------------------------------------------------------------


class TestUC13RepetitionGuard:
    def test_char_run_warns_once_per_session(self):
        from agent.middlewares.output_repetition_guard import check_stream_repetition

        first = check_stream_repetition("uc13", "a" * 100)
        assert first is not None, "字符连跑必须触发一次性警告"

        second = check_stream_repetition("uc13", "a" * 100)
        assert second is None, "去重门：同会话至多警告一次"

    def test_benign_text_passes(self):
        from agent.middlewares.output_repetition_guard import check_stream_repetition

        assert (
            check_stream_repetition("uc13b", "这是一段完全正常的回复内容，没有任何重复模式。")
            is None
        )


# ---------------------------------------------------------------------------
# UC-14 heartbeat watchdog
# ---------------------------------------------------------------------------


class TestUC14HeartbeatKill:
    def test_stale_cycles_kill_then_timeout(self, monkeypatch):
        from agent.middlewares.heartbeat_staleness import (
            HeartbeatStaleness,
            HeartbeatTimeoutError,
        )
        from runtime import state_register_mem

        hw = HeartbeatStaleness()
        session = "uc14"

        for _ in range(8):
            hw._check_progress(session)  # no progress → stale count accumulates

        assert state_register_mem.get_state(session, "heartbeat_killed", False) is True

        req = SimpleNamespace(state={"session_id": session})
        with pytest.raises(HeartbeatTimeoutError):
            hw.wrap_model_call(req, lambda r: "SHOULD_NOT_RUN")

    def test_progress_resets_stale_counter(self, monkeypatch):
        from agent.middlewares.heartbeat_staleness import HeartbeatStaleness
        from runtime import state_register_mem

        hw = HeartbeatStaleness()
        session = "uc14b"

        hw._check_progress(session)  # baseline
        state_register_mem.set_state(session, "heartbeat_iter", 5)
        hw._check_progress(session)  # progress made
        assert state_register_mem.get_state(session, "heartbeat_stale", 0) == 0


# ---------------------------------------------------------------------------
# UC-15 same-second session ordering (#21 regression)
# ---------------------------------------------------------------------------


class TestUC15SameSecondOrdering:
    def test_rapid_sessions_stable_order(self, store_db):
        # Rapid consecutive writes (likely the same second) — ts_ms monotonicity guarantees deterministic order
        _run(store_core.add_messages("uc15_first", [HumanMessage(content="first")]))
        _run(store_core.add_messages("uc15_second", [HumanMessage(content="second")]))

        sessions = store_core.get_session_ids()
        ids = [s["session_id"] for s in sessions]
        assert ids.index("uc15_second") < ids.index("uc15_first"), "后写的会话排前面"


# ---------------------------------------------------------------------------
# UC-16 bus backpressure
# ---------------------------------------------------------------------------


class TestUC16BusBackpressure:
    def test_full_queue_delays_not_drops(self):
        from bus.core import MessageBus
        from type.bus import InboundMessage

        bus = MessageBus(maxsize=1)

        async def flow():
            first = InboundMessage(channel="qq", sender_id="u", chat_id="c", content="m0")
            await bus.publish_inbound(first)

            second = InboundMessage(channel="qq", sender_id="u", chat_id="c", content="m1")
            task = asyncio.ensure_future(bus.publish_inbound(second))
            await asyncio.sleep(0.01)
            assert bus.inbound.qsize() == 1, "队列满时发布挂起"

            got0 = await bus.consume_inbound()
            await task  # the pending publish resumes
            got1 = await bus.consume_inbound()
            return got0, got1

        m0, m1 = asyncio.run(flow())
        assert (m0.content, m1.content) == ("m0", "m1"), "背压只延迟、不丢失、不乱序"
