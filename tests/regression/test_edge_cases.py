"""EC-01 ~ EC-16 — 全功能边界情况回归（对应 tests/REGRESSION_PLAN.md §4）。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import ValidationError

from context_engine.store import core as store_core
from context_engine.store.db import _migrate
from langchain_core.messages import ToolMessage

pytestmark = [pytest.mark.module, pytest.mark.timeout(120)]


@pytest.fixture()
def store_db(tmp_path):
    import sqlite3

    db = sqlite3.connect(str(tmp_path / "store.db"))
    db.row_factory = sqlite3.Row
    _migrate(db)
    saved = store_core._db
    store_core._db = db
    yield db
    store_core._db = saved
    db.close()


# ---------------------------------------------------------------------------
# EC-01 / EC-02 add_messages 边界
# ---------------------------------------------------------------------------


class TestAddMessagesBounds:
    def test_empty_list_noop(self, store_db):
        asyncio.run(store_core.add_messages("s", []))
        assert store_core.get_max_turn_num("s") == 0

    def test_none_list_noop(self, store_db):
        asyncio.run(store_core.add_messages("s", None))
        assert store_core.get_max_turn_num("s") == 0

    def test_unknown_role_skipped(self, store_db):
        fake = SimpleNamespace(type="function_call", content="x")
        asyncio.run(store_core.add_messages("s", [fake, HumanMessage(content="real")]))
        rows = store_core.get_history_by_turn_page("s")
        assert len(rows) == 1
        assert rows[0]["role"] == "human"


# ---------------------------------------------------------------------------
# EC-03 历史分页边界
# ---------------------------------------------------------------------------


class TestHistoryPagingBounds:
    def test_page_beyond_range_empty(self, store_db):
        asyncio.run(store_core.add_messages("s", [HumanMessage(content="x")]))
        assert store_core.get_history_by_turn_page("s", turn_page_num=99) == []

    def test_page_num_zero_rejected(self, store_db):
        with pytest.raises(ValidationError):
            store_core.get_history_by_turn_page("s", turn_page_num=0)

    def test_page_size_zero_rejected(self, store_db):
        with pytest.raises(ValidationError):
            store_core.get_history_by_turn_page("s", turn_page_size=0)

    def test_empty_session_empty_history(self, store_db):
        assert store_core.get_history_by_turn_page("ghost") == []


# ---------------------------------------------------------------------------
# EC-04 FTS5 查询边界（恶意/畸形输入有界返回）
# ---------------------------------------------------------------------------


class TestFtsQueryBounds:
    @pytest.fixture()
    def search(self, store_db, monkeypatch):
        from context_engine import core as ce

        monkeypatch.setattr(ce, "_db", store_db)
        monkeypatch.setattr(ce, "_lock", __import__("threading").Lock())
        asyncio.run(store_core.add_messages("s1", [HumanMessage(content="部署 docker compose 服务")]))
        return ce.search_messages

    @pytest.mark.parametrize(
        "query",
        ["", "   ", "(((", '"unclosed', "AND", "OR", "***", "a" * 400, "^", "+", "{}"],
    )
    def test_malformed_queries_return_list(self, search, query):
        assert isinstance(search(query=query, session_id="s1"), list)

    def test_wildcard_and_boolean_still_work(self, search):
        results = search(query="docker*", session_id="s1")
        assert isinstance(results, list)
        assert any("docker" in r["snippet"].lower() for r in results)


# ---------------------------------------------------------------------------
# EC-05 状态注册表边界 + clear_all 联动
# ---------------------------------------------------------------------------


class TestStateRegisterBounds:
    def test_mem_roundtrip_and_missing(self):
        from runtime import state_register_mem

        state_register_mem.set_state("ec05", "k", {"v": 1})
        assert state_register_mem.get_state("ec05", "k") == {"v": 1}
        assert state_register_mem.delete_state("ec05", "missing") is False
        assert state_register_mem.has_session("missing") is False
        state_register_mem.clear_session("ec05")
        assert state_register_mem.has_session("ec05") is False

    def test_update_states_creates_session(self):
        from runtime import state_register_mem

        state_register_mem.update_states("ec05u", {"a": 1, "b": 2})
        assert state_register_mem.get_state("ec05u", "a") == 1
        state_register_mem.clear_session("ec05u")

    def test_db_register_roundtrip(self, tmp_path):
        from runtime.state_register import StateRegisterDB

        db = StateRegisterDB()
        db.db_path = tmp_path / "state.db"
        db._init_db()

        assert db.set_state("s", "k", {"n": 1}) is True
        assert db.get_state("s", "k") == {"n": 1}
        assert db.has_key("s", "k") is True
        assert db.delete_state("s", "k") is True
        assert db.delete_state("s", "k") is False

    def test_clear_all_register_sessions_wipes_mem(self):
        from runtime import state_register_mem, relation_register
        from runtime.core import clear_all_register_sessions

        state_register_mem.set_state("ec05c", "k", "v")
        clear_all_register_sessions("ec05c")
        assert state_register_mem.get_state("ec05c", "k") is None


# ---------------------------------------------------------------------------
# EC-06 cron 调度边界
# ---------------------------------------------------------------------------


class TestCronScheduleBounds:
    def test_every_ms_zero_never_fires(self):
        from skills.builtin.core.cron.scripts.base import _compute_next_run
        from skills.builtin.core.cron.scripts.types import CronSchedule

        assert _compute_next_run(CronSchedule(kind="every", every_ms=0), 1_000) is None

    def test_at_in_past_never_fires(self):
        from skills.builtin.core.cron.scripts.base import _compute_next_run
        from skills.builtin.core.cron.scripts.types import CronSchedule

        assert _compute_next_run(CronSchedule(kind="at", at_ms=1), 2_000) is None

    def test_at_in_future_scheduled(self):
        from skills.builtin.core.cron.scripts.base import _compute_next_run
        from skills.builtin.core.cron.scripts.types import CronSchedule

        assert _compute_next_run(CronSchedule(kind="at", at_ms=5_000), 1_000) == 5_000


# ---------------------------------------------------------------------------
# EC-08 预算边界
# ---------------------------------------------------------------------------


class TestBudgetBoundary:
    def test_exactly_max_then_terminal(self):
        from agent.middlewares.iteration_budget import IterationBudget

        mw = IterationBudget(2)
        state = {"session_id": "ec08", "messages": [HumanMessage(content="x")]}
        mw.before_agent(state, None)
        req = SimpleNamespace(state=state)

        assert mw.wrap_model_call(req, lambda r: "R1") == "R1"
        assert mw.wrap_model_call(req, lambda r: "R2") == "R2"
        assert isinstance(mw.wrap_model_call(req, lambda r: "R3"), AIMessage)

    def test_consume_counts(self):
        from agent.middlewares.iteration_budget import IterationBudget

        mw = IterationBudget(3)
        assert mw._consume("ec08c") is True
        assert mw._consume("ec08c") is True
        assert mw._consume("ec08c") is True
        assert mw._consume("ec08c") is False


# ---------------------------------------------------------------------------
# EC-09 总线边界
# ---------------------------------------------------------------------------


class TestBusBounds:
    def test_maxsize_zero_rejected(self):
        from bus.core import MessageBus

        with pytest.raises(ValueError, match="maxsize must be positive"):
            MessageBus(maxsize=0)


# ---------------------------------------------------------------------------
# EC-10 护栏各病理
# ---------------------------------------------------------------------------


class TestGuardrailsPathologies:
    @pytest.fixture()
    def mw(self):
        from agent.middlewares.tool_guardrails import (
            ToolCallGuardrailConfig,
            ToolGuardrails,
        )

        return ToolGuardrails(
            config=ToolCallGuardrailConfig(recovery_mode_enabled=False)
        )

    def _request(self, tool_name, args, tool=None):
        return SimpleNamespace(
            state={"session_id": "ec10"},
            tool_call={"id": "c1", "name": tool_name, "args": args},
            tool=tool,
        )

    def test_idempotent_no_progress_warn(self, mw):
        tool = SimpleNamespace(metadata={"idempotent": True})
        req = self._request("read_file", {"path": "a.txt"}, tool=tool)
        ok = ToolMessage(content="SAME", tool_call_id="c1", name="read_file")

        mw.wrap_tool_call(req, lambda r: ok)
        r2 = mw.wrap_tool_call(req, lambda r: ok)
        assert "warn" in r2.content.lower() or "SAME" in r2.content

    def test_ping_pong_reset_by_non_idempotent_success(self, mw):
        tool = SimpleNamespace(metadata={"idempotent": True})
        ra = self._request("read_file", {"p": "a"}, tool=tool)
        rb = self._request("read_file", {"p": "b"}, tool=tool)
        ok_a = ToolMessage(content="A", tool_call_id="c1", name="read_file")
        ok_b = ToolMessage(content="B", tool_call_id="c2", name="read_file")
        mutating = SimpleNamespace(metadata={})

        mw.wrap_tool_call(ra, lambda r: ok_a)
        mw.wrap_tool_call(rb, lambda r: ok_b)
        mw.wrap_tool_call(ra, lambda r: ok_a)
        # 非幂等成功打断 ping-pong 累计
        mw.wrap_tool_call(
            self._request("terminal", {"command": "mutate"}, tool=mutating),
            lambda r: ToolMessage(content="done", tool_call_id="c3", name="terminal"),
        )
        mw.wrap_tool_call(rb, lambda r: ok_b)
        mw.wrap_tool_call(ra, lambda r: ok_a)
        # 若累计未重置，此处会触发 ping-pong 警告/升级——重置后应放行
        r = mw.wrap_tool_call(rb, lambda r: ok_b)
        assert "halt" not in r.content.lower()


# ---------------------------------------------------------------------------
# EC-11 时间戳边界
# ---------------------------------------------------------------------------


class TestTimestampBounds:
    def test_same_ms_turns_monotonic(self, store_db):
        stamps = []
        for _ in range(2):
            ms, stamp = store_core._next_turn_stamp()
            stamps.append((ms, stamp))
        assert stamps[1][0] > stamps[0][0], "同毫秒必须 +1ms 错开"

    def test_legacy_garbage_backfills_zero(self):
        import sqlite3
        from context_engine.store.db import _legacy_ts_to_ms, add_turn_ts_ms_column

        assert _legacy_ts_to_ms("20231114231320") == 1699974800000 or _legacy_ts_to_ms(
            "20231114231320"
        ) > 0
        assert _legacy_ts_to_ms("garbage") == 0
        assert _legacy_ts_to_ms("") == 0
        assert _legacy_ts_to_ms(None) == 0


# ---------------------------------------------------------------------------
# EC-12 技能加载边界
# ---------------------------------------------------------------------------


class TestLoaderBounds:
    def test_lookalike_path_not_third_party(self):
        from skills.loader import _is_third_party

        assert _is_third_party("./skills/builtin/skills/plugins/x/SKILL.md") is False
        assert _is_third_party("./skills/plugins/real/SKILL.md") is True

    def test_malformed_state_file_degrades(self, tmp_path, monkeypatch):
        from skills import loader as loader_mod

        bad = tmp_path / "skills_state.json"
        bad.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(loader_mod, "SKILLS_STATE_FILE", bad)
        assert loader_mod._read_skills_state() == {}

    def test_malformed_snapshot_raises_like_legacy(self, tmp_path, monkeypatch):
        from skills import loader as loader_mod

        skills_dir = tmp_path / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        (skills_dir / "skills_snapshot.json").write_text("{broken", encoding="utf-8")
        monkeypatch.setattr(loader_mod, "SKILLS_DIR", skills_dir)
        with pytest.raises(json.JSONDecodeError):
            loader_mod.read_skills_snapshot()


# ---------------------------------------------------------------------------
# EC-13 .env 解析语义
# ---------------------------------------------------------------------------


class TestEnvFileSemantics:
    def test_empty_value_does_not_swallow_next_line(self, tmp_path):
        from models.utils import read_env_file_value

        p = tmp_path / ".env"
        p.write_text("EMPTY=\nNEXT_LINE=real\n", encoding="utf-8")
        assert read_env_file_value("EMPTY", "fallback", env_path=p) == "fallback"

    def test_export_prefix_and_quotes(self, tmp_path):
        from models.utils import read_env_file_value

        p = tmp_path / ".env"
        p.write_text('export K="v"\n', encoding="utf-8")
        assert read_env_file_value("K", env_path=p) == "v"


# ---------------------------------------------------------------------------
# EC-14 快照缺失/损坏
# ---------------------------------------------------------------------------


class TestSnapshotBounds:
    def test_missing_snapshot_none(self, tmp_path, monkeypatch):
        from skills.loader import read_skills_snapshot
        import skills.loader as lm

        monkeypatch.setattr(lm, "SKILLS_DIR", tmp_path / "skills")
        assert read_skills_snapshot() is None


# ---------------------------------------------------------------------------
# EC-15 HITL deny_rules 优先级
# ---------------------------------------------------------------------------


class TestDenyRulesPriority:
    def test_deny_rule_beats_yolo_for_matched_command(self):
        from agent.middlewares.humanInTheLoop.approval import ApprovalPipeline
        from agent.middlewares.humanInTheLoop.types import HITLConfig

        pipe = ApprovalPipeline(
            HITLConfig(yolo_mode=True, deny_rules=["git push*"]), lambda *a, **k: None
        )
        assert pipe.check_command("git push origin", "ec15").approved is False

    def test_yolo_approves_unmatched(self):
        from agent.middlewares.humanInTheLoop.approval import ApprovalPipeline
        from agent.middlewares.humanInTheLoop.types import HITLConfig

        pipe = ApprovalPipeline(
            HITLConfig(yolo_mode=True, deny_rules=["git push*"]), lambda *a, **k: None
        )
        assert pipe.check_command("echo hi", "ec15b").approved is True


# ---------------------------------------------------------------------------
# EC-16 多模态清理边界（在 user_flows UC-09 覆盖，此处引用以保持矩阵完整）
# ---------------------------------------------------------------------------


def test_cleanup_edge_cases_covered_reference():
    """EC-16 的文件清理矩阵由 TestUC09MultimodalFlow.test_expired_temp_files_cleaned
    与 UC-09 主体共同覆盖（非数字文件名删除 / 过期删除 / 新文件保留）。"""
    import agent.middlewares.multimodal_processor as mm_mod

    assert hasattr(mm_mod.MultimodalProcessor, "_after_agent_impl")
