"""Unit tests for pub_func/message/tool_result_ttl.py (Task 4).

Covers the TTL registry (record/evict-cap), expiry selection, in-place
str truncation, multimodal list-content truncation, the compression-budget
loop, and the tool-call/tool-result PAIRING INVARIANT (the highest-risk
regression gate for Tasks 5-7).

Why the invariants matter: ToolCallNormalize (before_model) runs
sanitize_tool_use_result_pairing BEFORE the compression pipeline, and that
sanitizer DROPS ToolMessages whose content is empty. Any empty placeholder
or message removal here would produce broken tool pairing hitting the
provider on the SAME call. Therefore: in-place content mutation only,
placeholders must be non-empty, message list length/order/identity unchanged.

Style follows tests/unit/test_pub_func_message_tools.py: real langchain_core
messages, class grouping, plain asserts. Windows-safe: ASCII only.
"""

# pyright: reportUnknownParameterType=false
# pyright: reportArgumentType=false
# pyright: reportAny=false
# pyright: reportUnknownVariableType=false
# pyright: reportUnknownMemberType=false
# pyright: reportCallIssue=false

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from pub_func.message import tool_result_ttl
from pub_func.message.estimate_msg_tokens import estimate_msg_tokens
from pub_func.message.tool_result_ttl import (
    NON_TEXT_BLOCK_PLACEHOLDER,
    TTL_PLACEHOLDER,
    record_first_seen,
    select_expired,
    truncate_expired,
    truncate_to_budget,
)


def _ai_with_calls(*tc_ids: str) -> AIMessage:
    return AIMessage(
        content="calling tools",
        tool_calls=[
            {"name": "bash", "args": {"cmd": "ls"}, "id": tc_id} for tc_id in tc_ids
        ],
    )


def _tool(tc_id: str, content) -> ToolMessage:
    return ToolMessage(content=content, tool_call_id=tc_id, name="bash")


def _big_str(n: int, fill: str = "A") -> str:
    return fill * n


def _multimodal_blocks(text_n: int = 20000, b64_n: int = 8000) -> list:
    return [
        {"type": "text", "text": "T" * text_n},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64," + "Z" * b64_n},
        },
    ]


# ======================================================================
# TestRecordFirstSeen
# ======================================================================


class TestRecordFirstSeen:
    def test_records_new_tool_call_ids(self):
        registry: dict = {}
        msgs = [
            HumanMessage(content="hi"),
            _ai_with_calls("tc1", "tc2"),
            _tool("tc1", "r1"),
            _tool("tc2", "r2"),
        ]
        record_first_seen(registry, msgs, now=1000.0)
        assert registry == {"tc1": 1000.0, "tc2": 1000.0}

    def test_does_not_overwrite_existing_entries(self):
        registry = {"tc1": 100.0}
        msgs = [_ai_with_calls("tc1"), _tool("tc1", "r1")]
        record_first_seen(registry, msgs, now=999.0)
        assert registry["tc1"] == 100.0

    def test_ignores_messages_without_tool_results(self):
        registry: dict = {}
        msgs = [
            SystemMessage(content="sys"),
            HumanMessage(content="q"),
            AIMessage(content="no tool calls here"),
            _ai_with_calls("tcX"),  # tool call without its result yet
        ]
        record_first_seen(registry, msgs, now=1.0)
        assert registry == {}

    def test_evicts_oldest_by_first_seen_value(self, monkeypatch):
        monkeypatch.setattr(tool_result_ttl, "TTL_REGISTRY_MAX_ENTRIES", 2)
        registry = {"old": 1.0, "mid": 2.0}
        msgs = [_ai_with_calls("new"), _tool("new", "r")]
        record_first_seen(registry, msgs, now=3.0)
        # cap=2, one new id -> oldest-by-value entry evicted before write
        assert "old" not in registry
        assert registry == {"mid": 2.0, "new": 3.0}

    def test_registry_size_never_exceeds_cap(self, monkeypatch):
        monkeypatch.setattr(tool_result_ttl, "TTL_REGISTRY_MAX_ENTRIES", 3)
        registry: dict = {}
        for i in range(7):
            tc = f"tc{i}"
            msgs = [_ai_with_calls(tc), _tool(tc, "r")]
            record_first_seen(registry, msgs, now=float(i))
            assert len(registry) <= 3
        assert set(registry) == {"tc4", "tc5", "tc6"}

    def test_cap_constant_imported_from_config(self):
        from config.num import TTL_REGISTRY_MAX_ENTRIES

        assert tool_result_ttl.TTL_REGISTRY_MAX_ENTRIES == TTL_REGISTRY_MAX_ENTRIES
        assert tool_result_ttl.TTL_REGISTRY_MAX_ENTRIES == 512


# ======================================================================
# TestSelectExpired
# ======================================================================


class TestSelectExpired:
    def test_returns_expired_indices(self):
        now = 1000.0
        registry = {"tc1": now - 301.0, "tc2": now - 10.0}
        msgs = [
            _ai_with_calls("tc1", "tc2"),
            _tool("tc1", _big_str(2000)),
            _tool("tc2", _big_str(2000)),
        ]
        assert select_expired(registry, msgs, ttl_seconds=300.0, now=now) == [1]

    def test_fresh_messages_not_selected(self):
        now = 1000.0
        registry = {"tc1": now - 10.0}
        msgs = [_ai_with_calls("tc1"), _tool("tc1", _big_str(2000))]
        assert select_expired(registry, msgs, ttl_seconds=300.0, now=now) == []

    def test_boundary_equal_ttl_is_expired(self):
        # now - first_seen == ttl_seconds qualifies (>= semantics)
        now = 1000.0
        registry = {"tc1": now - 300.0}
        msgs = [_ai_with_calls("tc1"), _tool("tc1", _big_str(2000))]
        assert select_expired(registry, msgs, ttl_seconds=300.0, now=now) == [1]

    def test_unregistered_ids_not_selected(self):
        now = 1000.0
        registry: dict = {}  # tc1 never recorded (e.g. registry was reset)
        msgs = [_ai_with_calls("tc1"), _tool("tc1", _big_str(2000))]
        assert select_expired(registry, msgs, ttl_seconds=300.0, now=now) == []

    def test_only_tool_messages_selected(self):
        now = 1000.0
        registry = {"tc1": now - 400.0}
        msgs = [
            HumanMessage(content="plain huge human message " + _big_str(5000)),
            _ai_with_calls("tc1"),
            _tool("tc1", _big_str(5000)),
        ]
        assert select_expired(registry, msgs, ttl_seconds=300.0, now=now) == [2]


# ======================================================================
# TestTruncateExpiredStr
# ======================================================================


class TestTruncateExpiredStr:
    def _content(self) -> str:
        return "H" * 300 + "M" * 20000 + "T" * 300

    def test_head_tail_and_placeholder(self):
        now = 1000.0
        content = self._content()
        msgs = [_ai_with_calls("tc1"), _tool("tc1", content)]
        head = content[: int(len(content) * 0.3)]
        tail = content[-int(len(content) * 0.3):]

        freed = truncate_expired(msgs, [1], budget_tokens=10**9)

        new_content = msgs[1].content
        assert isinstance(new_content, str)
        assert len(new_content) < len(content)
        assert new_content.startswith(head)
        assert new_content.endswith(tail)
        assert TTL_PLACEHOLDER in new_content
        assert freed > 0

    def test_in_place_no_removal_no_reorder(self):
        now = 1000.0
        msgs = [
            SystemMessage(content="sys"),
            HumanMessage(content="q"),
            _ai_with_calls("tc1"),
            _tool("tc1", self._content()),
            HumanMessage(content="next"),
        ]
        before = list(msgs)
        truncate_expired(msgs, [3], budget_tokens=10**9)
        assert len(msgs) == len(before)
        assert all(a is b for a, b in zip(msgs, before))  # same objects, same slots
        assert msgs[3].tool_call_id == "tc1"

    def test_freed_tokens_positive_and_estimated(self):
        now = 1000.0
        msgs = [_ai_with_calls("tc1"), _tool("tc1", self._content())]
        est_before = estimate_msg_tokens(msgs[1])
        freed = truncate_expired(msgs, [1], budget_tokens=10**9)
        est_after = estimate_msg_tokens(msgs[1])
        assert freed == est_before - est_after
        assert freed > 0

    def test_tail_shrunk_when_over_budget_head_kept(self):
        now = 1000.0
        content = self._content()
        msgs = [_ai_with_calls("tc1"), _tool("tc1", content)]
        head = content[: int(len(content) * 0.3)]

        truncate_expired(msgs, [1], budget_tokens=10)  # tiny budget

        new_content = msgs[1].content
        assert new_content.startswith(head)  # head preserved
        assert TTL_PLACEHOLDER in new_content
        # tail fully collapsed under budget pressure
        assert new_content == head + TTL_PLACEHOLDER

    def test_small_content_left_untouched(self):
        # Truncating would GROW the content (placeholder overhead) -> no-op.
        now = 1000.0
        msgs = [_ai_with_calls("tc1"), _tool("tc1", "short result")]
        freed = truncate_expired(msgs, [1], budget_tokens=10**9)
        assert msgs[1].content == "short result"
        assert freed == 0

    def test_out_of_range_and_non_tool_indices_ignored(self):
        now = 1000.0
        msgs = [_ai_with_calls("tc1"), _tool("tc1", self._content())]
        original = msgs[0].content
        freed = truncate_expired(msgs, [-5, 0, 99], budget_tokens=100)
        assert freed == 0
        assert msgs[0].content == original  # AIMessage never touched


# ======================================================================
# TestTruncateExpiredMultimodal
# ======================================================================


class TestTruncateExpiredMultimodal:
    def test_text_block_truncated_image_block_replaced(self):
        now = 1000.0
        blocks = _multimodal_blocks()
        msgs = [_ai_with_calls("tc1"), _tool("tc1", blocks)]

        freed = truncate_expired(msgs, [1], budget_tokens=10**9)

        content = msgs[1].content
        assert isinstance(content, list)
        assert len(content) == 2  # structure never dropped
        first, second = content
        assert first["type"] == "text"
        assert TTL_PLACEHOLDER in first["text"]
        assert len(first["text"]) < 20000
        assert second == {"type": "text", "text": NON_TEXT_BLOCK_PLACEHOLDER}
        assert freed > 0

    def test_list_structure_preserved_not_str(self):
        now = 1000.0
        blocks = _multimodal_blocks()
        msgs = [_ai_with_calls("tc1"), _tool("tc1", blocks)]
        truncate_expired(msgs, [1], budget_tokens=10)
        assert isinstance(msgs[1].content, list)  # never flattened to str
        assert len(msgs[1].content) == 2

    def test_text_block_shrunk_in_place(self):
        now = 1000.0
        blocks = _multimodal_blocks()
        original_text = blocks[0]["text"]
        msgs = [_ai_with_calls("tc1"), _tool("tc1", blocks)]
        truncate_expired(msgs, [1], budget_tokens=10**9)
        # text block mutated in place (message object kept), text shortened
        content = msgs[1].content
        assert isinstance(content, list)
        assert content[0]["type"] == "text"
        assert len(content[0]["text"]) < len(original_text)
        assert TTL_PLACEHOLDER in content[0]["text"]

    def test_multimodal_budget_shrink_keeps_head(self):
        now = 1000.0
        original_text = _multimodal_blocks()[0]["text"]
        head = original_text[: int(len(original_text) * 0.3)]
        msgs = [_ai_with_calls("tc1"), _tool("tc1", _multimodal_blocks())]
        truncate_expired(msgs, [1], budget_tokens=10)
        first_text = msgs[1].content[0]["text"]
        assert first_text.startswith(head)
        assert TTL_PLACEHOLDER in first_text

    def test_non_text_dict_and_non_dict_blocks_replaced(self):
        now = 1000.0
        blocks = [
            {"type": "text", "text": "T" * 20000},
            "not-even-a-dict",  # malformed block
        ]
        msgs = [_ai_with_calls("tc1"), _tool("tc1", blocks)]
        truncate_expired(msgs, [1], budget_tokens=10**9)
        content = msgs[1].content
        assert len(content) == 2
        assert content[1] == {"type": "text", "text": NON_TEXT_BLOCK_PLACEHOLDER}


# ======================================================================
# TestTruncateToBudget
# ======================================================================


class TestTruncateToBudget:
    def _transcript(self) -> list:
        # three big ToolMessages of decreasing size at idx 3, 5, 7
        return [
            SystemMessage(content="sys"),
            HumanMessage(content="q1"),
            _ai_with_calls("tc1"),
            _tool("tc1", "A" * 40000),  # largest
            HumanMessage(content="q2"),
            _ai_with_calls("tc2"),
            _tool("tc2", "B" * 20000),  # middle
            HumanMessage(content="q3"),
            _ai_with_calls("tc3"),
            _tool("tc3", "C" * 10000),  # smallest
        ]

    def _candidates(self, msgs) -> list:
        return [(i, estimate_msg_tokens(m)) for i, m in enumerate(msgs) if isinstance(m, ToolMessage)]

    def test_largest_first_until_budget_met(self):
        msgs = self._transcript()
        candidates = self._candidates(msgs)

        freed = truncate_to_budget(msgs, candidates, budget_tokens=5400)

        # The largest candidate is truncated first (budget pressure shrinks
        # its tail), and its freed estimate alone satisfies the budget, so
        # the smaller candidates must remain untouched. Smallest-first
        # ordering would instead leave idx3 for last and truncate all three.
        assert TTL_PLACEHOLDER in msgs[3].content
        assert msgs[6].content == "B" * 20000
        assert msgs[9].content == "C" * 10000
        assert freed >= 5400

    def test_exhausts_candidates_when_budget_huge(self):
        msgs = self._transcript()
        candidates = self._candidates(msgs)
        truncate_to_budget(msgs, candidates, budget_tokens=10**9)
        for idx in (3, 6, 9):
            assert TTL_PLACEHOLDER in msgs[idx].content

    def test_in_place_and_placeholder_guarantees(self):
        msgs = self._transcript()
        snapshot = list(msgs)
        candidates = self._candidates(msgs)
        truncate_to_budget(msgs, candidates, budget_tokens=10**9)
        assert len(msgs) == len(snapshot)
        assert all(a is b for a, b in zip(msgs, snapshot))
        for idx in (3, 6, 9):
            assert msgs[idx].tool_call_id == snapshot[idx].tool_call_id
            assert isinstance(msgs[idx].content, str)
            assert msgs[idx].content  # non-empty -> pairing sanitizer keeps it

    def test_empty_candidates_or_zero_budget_noop(self):
        msgs = self._transcript()
        original = [m.content for m in msgs]
        assert truncate_to_budget(msgs, [], budget_tokens=1000) == 0
        assert truncate_to_budget(msgs, self._candidates(msgs), budget_tokens=0) == 0
        assert [m.content for m in msgs] == original

    def test_out_of_range_indices_skipped(self):
        msgs = self._transcript()
        original = msgs[9].content
        freed = truncate_to_budget(msgs, [(99, 99999)], budget_tokens=1000)
        assert freed == 0
        assert msgs[9].content == original


# ======================================================================
# TestPairingInvariant (regression gate for Tasks 5-7)
# ======================================================================


class TestPairingInvariant:
    def test_every_tool_call_keeps_its_tool_message_after_full_truncation(self):
        now = 1000.0
        msgs = [
            SystemMessage(content="sys"),
            HumanMessage(content="q1"),
            _ai_with_calls("tc1", "tc2"),
            _tool("tc1", _multimodal_blocks()),
            _tool("tc2", _big_str(40000)),
            AIMessage(content="done"),
            HumanMessage(content="q2"),
            _ai_with_calls("tc3"),
            _tool("tc3", _big_str(30000)),
        ]
        registry: dict = {}
        record_first_seen(registry, msgs, now=now - 400.0)

        expired = select_expired(registry, msgs, ttl_seconds=300.0, now=now)
        assert len(expired) == 3
        snapshot = list(msgs)
        freed = truncate_expired(msgs, expired, budget_tokens=1000)
        candidates = [(i, estimate_msg_tokens(m)) for i, m in enumerate(msgs) if isinstance(m, ToolMessage)]
        freed += truncate_to_budget(msgs, candidates, budget_tokens=1000)

        # 1. message list untouched: same length, same objects, same slots
        assert len(msgs) == len(snapshot)
        assert all(a is b for a, b in zip(msgs, snapshot))

        # 2. every AI tool_call_id still has its ToolMessage present
        ai_call_ids = set()
        for msg in msgs:
            for tc in getattr(msg, "tool_calls", None) or []:
                ai_call_ids.add(tc["id"])
        result_ids = {
            m.tool_call_id for m in msgs if isinstance(m, ToolMessage)
        }
        assert ai_call_ids == {"tc1", "tc2", "tc3"}
        assert result_ids == ai_call_ids  # nothing dropped, nothing missing

        # 3. every ToolMessage content non-empty (sanitizer drops empties)
        for m in msgs:
            if isinstance(m, ToolMessage):
                assert m.content  # truthy for str AND non-empty list
                if isinstance(m.content, str):
                    assert TTL_PLACEHOLDER in m.content

        # 4. truncation actually freed tokens
        assert freed > 0
