"""Conversation rewind: hidden ranges, the read filter, the service gates, fencing.

The contract: a rewind hides a message-id range from every reader while the rows
stay in SQLite (append-only, R15); a message written after the rewind is visible
again immediately; the generation counter and the rewind instant are what stop
asynchronous work created before the cut from landing after it; and the service
refuses while a turn is in flight — fencing cannot stop an already-running tool
call, so the turn boundary is the only safe place for the cut.
"""

import time

import pytest

from runtime.session.conversation_branch import (
    apply_rewind,
    branch_generation,
    clear_branch,
    filter_visible,
    is_visible,
    read_branch,
    rewound_at,
)

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

SESSION = "s-rewind-unit"


@pytest.fixture(autouse=True)
def _clean_branch():
    clear_branch(SESSION)
    yield
    clear_branch(SESSION)


def test_a_rewind_hides_its_range_and_leaves_the_rows_alone():
    apply_rewind(SESSION, cut_after_message_id=10, tip_message_id=14)

    branch = read_branch(SESSION)
    assert branch.hidden_ranges == ((10, 14),)
    assert is_visible(branch, 10) is True  # the cut message itself stays
    assert is_visible(branch, 11) is False
    assert is_visible(branch, 15) is True  # written after the rewind


def test_a_second_rewind_unions_the_ranges():
    apply_rewind(SESSION, cut_after_message_id=10, tip_message_id=14)
    second = apply_rewind(SESSION, cut_after_message_id=15, tip_message_id=20)

    assert second.hidden_ranges == ((10, 14), (15, 20))
    assert second.branch_generation == 2
    assert is_visible(second, 12) is False
    assert is_visible(second, 18) is False
    assert is_visible(second, 21) is True


def test_overlapping_ranges_merge():
    apply_rewind(SESSION, cut_after_message_id=10, tip_message_id=14)
    merged = apply_rewind(SESSION, cut_after_message_id=12, tip_message_id=18)

    assert merged.hidden_ranges == ((10, 18),)


def test_the_filter_drops_only_hidden_ids():
    apply_rewind(SESSION, cut_after_message_id=10, tip_message_id=14)
    rows = [{"id": i} for i in (8, 10, 11, 14, 15, 16)]

    kept = filter_visible(SESSION, rows)

    assert [row["id"] for row in kept] == [8, 10, 15, 16]


def test_without_a_rewind_the_filter_is_the_identity():
    rows = [{"id": 1}, {"id": 2}]

    assert filter_visible(SESSION, rows) == rows


def test_the_generation_and_the_instant_move_together():
    assert branch_generation(SESSION) == 0
    assert rewound_at(SESSION) == 0.0

    before = time.time()
    apply_rewind(SESSION, cut_after_message_id=1, tip_message_id=2)

    assert branch_generation(SESSION) == 1
    assert rewound_at(SESSION) >= before


def test_a_rewind_to_the_tip_records_no_range_but_bumps_the_generation():
    """Nothing visible changed, yet the fence must still invalidate old work."""
    branch = apply_rewind(SESSION, cut_after_message_id=9, tip_message_id=9)

    assert branch.hidden_ranges == ()
    assert branch.branch_generation == 1


def test_the_state_survives_a_round_trip_through_json():
    apply_rewind(SESSION, cut_after_message_id=3, tip_message_id=7)

    from runtime.session.conversation_branch import ConversationBranch

    branch = read_branch(SESSION)
    restored = ConversationBranch.from_state(branch.as_state())

    assert restored == branch


def test_corrupt_state_reads_as_no_rewind():
    from runtime import state_register_db
    from runtime.session.state_register import state_register_mem
    from runtime.session.state_keys import StateKey

    state_register_mem.set_state(SESSION, StateKey.CONVERSATION_BRANCH, "{not json")
    assert read_branch(SESSION).hidden_ranges == ()

    state_register_mem.set_state(
        SESSION, StateKey.CONVERSATION_BRANCH, '{"hidden_ranges": [[5, 9]]}'
    )
    assert read_branch(SESSION).hidden_ranges == ((5, 9),)
    state_register_db.delete_state(SESSION, StateKey.CONVERSATION_BRANCH)
