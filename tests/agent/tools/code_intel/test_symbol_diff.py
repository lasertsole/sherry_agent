"""The interface comparator behind the merge report's symbol-level section.

An isolated merge's per-file CAS is revision-aware and semantically blind: a
rename merges cleanly while a second child's file still calls the old name.
``diff_file_interface`` answers the missing question — which symbols a change
added, removed or renamed — from two byte revisions, without touching disk.

The rules pinned here: exact-identity diff first, then greedy rename pairing
inside one ``(kind, parent)`` group within a Levenshtein threshold; a creation
or deletion reads as all-added / all-removed; and everything that cannot be
compared (unsupported suffix, syntax error, oversized revision) is reported in
``error`` instead of raising — the caller is a merge.
"""

import pytest

from agent.tools.code_intel.symbol_diff import (
    SymbolChange,
    diff_file_interface,
    language_for_extension,
)

pytestmark = [pytest.mark.unit]


def _python(source: str) -> bytes:
    return source.encode("utf-8")


def _names(changes: list[SymbolChange]) -> set[str]:
    return {change.name for change in changes}


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_rename_detection():
    old = _python("def get_user():\n    return 1\n")
    new = _python("def get_users():\n    return 1\n")

    delta = diff_file_interface(old, new, "python", "utils.py")

    assert delta is not None
    assert [(r.old_name, r.new_name, r.kind, r.parent) for r in delta.renamed] == [
        ("get_user", "get_users", "function", None)
    ]
    assert delta.added == [] and delta.removed == []
    assert delta.has_changes


def test_add_function():
    old = _python("def foo():\n    return 1\n")
    new = _python("def foo():\n    return 1\n\n\ndef bar():\n    return 2\n")

    delta = diff_file_interface(old, new, "python", "utils.py")

    assert delta is not None
    assert _names(delta.added) == {"bar"}
    assert delta.removed == [] and delta.renamed == []


def test_remove_function():
    old = _python("def foo():\n    return 1\n\n\ndef old_helper():\n    return 2\n")
    new = _python("def foo():\n    return 1\n")

    delta = diff_file_interface(old, new, "python", "utils.py")

    assert delta is not None
    assert _names(delta.removed) == {"old_helper"}
    assert delta.added == [] and delta.renamed == []


def test_method_attributed_to_class():
    old = _python("class Api:\n    def get(self):\n        return 1\n")
    new = _python(
        "class Api:\n    def get(self):\n        return 1\n\n    def post(self):\n        return 2\n"
    )

    delta = diff_file_interface(old, new, "python", "api.py")

    assert delta is not None
    assert [(a.name, a.kind, a.parent) for a in delta.added] == [("post", "method", "Api")]


def test_no_rename_when_distance_exceeds_threshold():
    old = _python("def foo():\n    return 1\n")
    new = _python("def completely_different():\n    return 1\n")

    delta = diff_file_interface(old, new, "python", "utils.py")

    assert delta is not None
    assert delta.renamed == []
    assert _names(delta.removed) == {"foo"}
    assert _names(delta.added) == {"completely_different"}


def test_a_three_edit_rename_reports_removed_and_added():
    """``foo`` → ``bar`` is distance 3: heuristic pairs stay conservative.

    The trade-off is deliberate and fail-safe — the report still names the OLD
    symbol under ``removed``, which is what the parent needs to check other
    children's call sites.
    """
    old = _python("def foo():\n    return 1\n")
    new = _python("def bar():\n    return 1\n")

    delta = diff_file_interface(old, new, "python", "utils.py")

    assert delta is not None
    assert delta.renamed == []
    assert _names(delta.removed) == {"foo"}
    assert _names(delta.added) == {"bar"}


def test_created_file_all_added():
    new = _python("def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n")

    delta = diff_file_interface(None, new, "python", "fresh.py")

    assert delta is not None
    assert _names(delta.added) == {"alpha", "beta"}
    assert delta.removed == [] and delta.renamed == []


def test_deleted_file_all_removed():
    old = _python("def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n")

    delta = diff_file_interface(old, None, "python", "gone.py")

    assert delta is not None
    assert _names(delta.removed) == {"alpha", "beta"}
    assert delta.added == [] and delta.renamed == []


def test_multiple_renames_greedy_match():
    old = _python(
        "def get_user():\n    return 1\n\n\ndef get_order():\n    return 2\n",
    )
    new = _python(
        "def get_users():\n    return 1\n\n\ndef get_orders():\n    return 2\n",
    )

    delta = diff_file_interface(old, new, "python", "api.py")

    assert delta is not None
    pairs = {(r.old_name, r.new_name) for r in delta.renamed}
    assert pairs == {("get_user", "get_users"), ("get_order", "get_orders")}
    assert delta.added == [] and delta.removed == []


def test_a_method_never_pairs_with_a_free_function():
    """Same neighbourhood, different home: the group is part of the identity."""
    old = _python("class Api:\n    def sync(self):\n        return 1\n")
    new = _python("def syncd():\n    return 1\n")

    delta = diff_file_interface(old, new, "python", "api.py")

    assert delta is not None
    assert delta.renamed == []
    assert {(r.name, r.parent) for r in delta.removed} == {("Api", None), ("sync", "Api")}
    assert [(a.name, a.kind) for a in delta.added] == [("syncd", "function")]


# ---------------------------------------------------------------------------
# Failure modes report, never raise
# ---------------------------------------------------------------------------


def test_unsupported_language_returns_none():
    assert language_for_extension(".txt") is None
    assert language_for_extension(".md") is None
    assert language_for_extension(".PY") == "python"


def test_syntax_error_sets_error_field():
    delta = diff_file_interface(
        _python("def broken(:\n"), _python("def broken():\n"), "python", "x.py"
    )

    assert delta is not None
    assert delta.error
    assert delta.has_changes is False


def test_large_file_sets_error_field():
    old = _python("def foo():\n    return 1\n")
    new = _python("def bar():\n    return 1\n")

    delta = diff_file_interface(old, new, "python", "big.py", max_file_bytes=5)

    assert delta is not None
    assert delta.error and "size cap" in delta.error
    assert delta.has_changes is False


def test_both_revisions_absent_is_nothing_to_compare():
    assert diff_file_interface(None, None, "python", "x.py") is None
