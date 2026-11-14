"""Legacy stale events match a command by token, never by substring.

Older rows carry only ``file_path`` (no ``command``). The original rule was
``file_path in command``, so a write to ``a.py`` stale-d a verification of
``bla.py``. The read-side derivation now reuses the ledger's token-aware
matcher; these tests pin that and the new-row path-token rule next to it.
"""

from __future__ import annotations

import pytest

from agent.tools.pub_base.evidence_ledger import derive_evidence_state

pytestmark = [pytest.mark.unit]


def _records(command: str, file_path: str, modified_files: list[str] | None = None):
    return [
        {"event": None, "command": command, "modified_files": modified_files or []},
        {"event": "stale", "file_path": file_path},
    ]


def test_a_substring_is_not_a_stale_match():
    broken, stale = derive_evidence_state(_records("python bla.py", "a.py"), 0)

    assert (broken, stale) == (False, False)


def test_a_command_token_matches_the_path():
    broken, stale = derive_evidence_state(_records("pytest src/a.py", "src/a.py"), 0)

    assert (broken, stale) == (False, True)


def test_a_basename_token_matches_a_nested_path():
    broken, stale = derive_evidence_state(_records("pytest a.py", "src/a.py"), 0)

    assert (broken, stale) == (False, True)


def test_a_new_row_keeps_the_exact_modified_files_set():
    broken, stale = derive_evidence_state(
        _records("true", "src/a.py", modified_files=["src/a.py"]), 0
    )

    assert (broken, stale) == (False, True)


def test_an_unrelated_new_row_is_not_stale():
    broken, stale = derive_evidence_state(
        _records("true", "src/b.py", modified_files=["src/a.py"]), 0
    )

    assert (broken, stale) == (False, False)
