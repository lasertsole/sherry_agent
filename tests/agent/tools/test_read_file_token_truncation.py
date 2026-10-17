"""Token-budgeted ``read_file`` pagination, the unchanged re-read stub.

``read_file`` used to cut by LINE COUNT, so 2000 lines of English (~20 000
tokens) and 2000 lines of Chinese (~50 000 tokens) cost wildly different
context. The window is now bounded by a token budget (CJK-aware, the same
ratios ``pub.func.message.token_truncation`` uses) AND the caller's line
``limit`` — the stricter wins — with truncation always at a line boundary.
Pinned here:

* the same budget spends ~the same tokens on ASCII and CJK text (fewer CJK
  lines come back, same context cost);
* the cut is a whole line — numbering stays continuous, no half line;
* one line longer than the whole budget is clipped inside itself and counts as
  truncated (no write license may ride that read);
* ``limit`` stays a hard cap and comes from the registry (not hardcoded);
* a budget of 0 restores the historical line-count window;
* a re-read of an unchanged file the session READ is answered with a pointer,
  while a ranged read, a changed file, a truncated first read, and a file the
  session only WROTE all return content.
"""

from __future__ import annotations

import json
import re

import pytest

from agent.tools.file_tools.read_file import (
    _READ_DEFAULT_LIMIT,
    _READ_MAX_LIMIT,
    ReadFileInput,
    build_read_file_tool,
)
from agent.tools.file_tools.write_file import build_write_file_tool
from agent.tools.pub_base import path_utils, read_state
from config.features import TOOLS_TIMEOUTS

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

SESSION = "s-token-window"
OTHER_SESSION = "s-token-window-2"
BUDGET = TOOLS_TIMEOUTS["read_file_token_budget"]
_LINE_TOKEN_CAP = TOOLS_TIMEOUTS["read_file_max_line_tokens"]


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A session root at tmp_path, with the process license registry emptied."""
    root = tmp_path.resolve()
    monkeypatch.setattr(path_utils, "ROOT_DIR", root)
    monkeypatch.setenv("SHERRY_PROJECT_DIR", str(root))

    from runtime import state_register_mem

    monkeypatch.setattr(state_register_mem, "_states", {})
    state_register_mem.clear_session(SESSION)
    state_register_mem.clear_session(OTHER_SESSION)
    read_state.forget_all()
    yield root
    read_state.forget_all()


def _read(session: str, path: str, **kwargs) -> dict:
    return json.loads(build_read_file_tool()._core(path, session_id=session, **kwargs))


def _numbered_lines(content: str) -> list[str]:
    return content.split("\n")


def _first_number(content: str) -> int:
    return int(_numbered_lines(content)[0].split("|", 1)[0])


def _last_number(content: str) -> int:
    return int(_numbered_lines(content)[-1].split("|", 1)[0])


# ---------------------------------------------------------------------------
# The token window
# ---------------------------------------------------------------------------


class TestTokenWindow:
    def test_a_long_ascii_file_is_cut_by_the_budget_with_a_continue_hint(self, project):
        target = project / "code.py"
        target.write_text("\n".join(f"value_{i} = compute({i})" for i in range(4000)), "utf-8")

        out = _read(SESSION, "code.py")

        assert out["truncated"] is True
        assert out["total_lines"] == 4000
        assert "Use offset=" in out["hint"]
        assert "tokens of the" in out["hint"]

    @staticmethod
    def _tokens_spent(out: dict) -> int:
        return int(re.search(r"~([0-9]+) tokens of the", out["hint"]).group(1))

    def test_the_same_budget_means_the_same_token_cost_in_both_languages(self, project):
        ascii_file = project / "ascii.txt"
        cjk_file = project / "cjk.txt"
        # 40 ASCII chars/line vs 20 CJK chars/line: EQUAL token cost per line
        # (4 chars/token vs 2), so the budget buys the same number of lines and
        # the ASCII page carries twice the characters.
        ascii_file.write_text("\n".join("x" * 40 for _ in range(2000)), "utf-8")
        cjk_file.write_text("\n".join("中" * 20 for _ in range(2000)), "utf-8")

        ascii_out = _read(SESSION, "ascii.txt")
        cjk_out = _read(SESSION, "cjk.txt")

        assert ascii_out["truncated"] and cjk_out["truncated"]
        assert len(_numbered_lines(ascii_out["content"])) == len(
            _numbered_lines(cjk_out["content"])
        )
        assert abs(self._tokens_spent(ascii_out) - self._tokens_spent(cjk_out)) <= 1
        assert len(cjk_out["content"]) < len(ascii_out["content"])

    def test_equal_length_lines_buy_fewer_cjk_lines(self, project):
        # Same characters per line, so a CJK line costs ~2x the tokens: the
        # budget must cut the CJK page at about half the lines.
        ascii_file = project / "a.txt"
        cjk_file = project / "z.txt"
        ascii_file.write_text("\n".join("x" * 40 for _ in range(2000)), "utf-8")
        cjk_file.write_text("\n".join("中" * 40 for _ in range(2000)), "utf-8")

        ascii_lines = len(_numbered_lines(_read(SESSION, "a.txt")["content"]))
        cjk_lines = len(_numbered_lines(_read(SESSION, "z.txt")["content"]))

        assert cjk_lines < ascii_lines, (
            f"CJK lines cost ~2x, so fewer fit the same budget "
            f"(ascii={ascii_lines}, cjk={cjk_lines})"
        )

    def test_the_cut_is_a_whole_line_and_numbering_is_continuous(self, project):
        target = project / "wide.txt"
        target.write_text("\n".join(f"line-{i}-" + "y" * 60 for i in range(1, 501)), "utf-8")

        out = _read(SESSION, "wide.txt")
        lines = _numbered_lines(out["content"])

        assert out["truncated"] is True
        assert _first_number(out["content"]) == 1
        last = _last_number(out["content"])
        # Continuous, complete lines: every line carries its own number and the
        # next number is absent (it belongs to the truncated remainder).
        assert [int(line.split("|", 1)[0]) for line in lines] == list(range(1, last + 1))
        assert f"{last + 1}|" not in out["content"]

    def test_limit_is_still_a_hard_cap(self, project):
        target = project / "small.txt"
        target.write_text("\n".join(f"row {i}" for i in range(1, 101)), "utf-8")

        out = _read(SESSION, "small.txt", limit=10)

        assert len(_numbered_lines(out["content"])) == 10
        assert _last_number(out["content"]) == 10
        assert out["truncated"] is True

    def test_a_zero_budget_restores_the_line_count_window(self, project, monkeypatch):
        target = project / "many.txt"
        target.write_text("\n".join(f"row {i}" for i in range(1, 3001)), "utf-8")
        monkeypatch.setattr("agent.tools.file_tools.read_file._TOKEN_BUDGET", 0)

        out = _read(SESSION, "many.txt")

        assert len(_numbered_lines(out["content"])) == _READ_DEFAULT_LIMIT
        assert "tokens of the" not in out["hint"]

    def test_a_single_over_budget_line_is_clipped_and_counts_as_truncated(self, project):
        target = project / "min.js"
        target.write_text("x" * 500_000, "utf-8")

        out = _read(SESSION, "min.js")

        assert out["truncated"] is True, "a clipped line is NOT a complete read"
        assert "chars clipped" in out["content"]
        assert len(out["content"]) < BUDGET * 4 * 2, "the response must stay bounded"
        assert len(_numbered_lines(out["content"])) == 1
        assert read_state.licensed(SESSION, target) is None, "no license off a clipped read"

    def test_a_giant_line_after_the_first_is_clipped_too(self, project):
        # The hole this pins: the per-line cap used to fire only when the giant
        # line was the page's FIRST line, so a minified blob on line 2 rode the
        # response whole and pushed it past every bound.
        target = project / "data.json"
        target.write_text("header line\n" + "x" * 500_000 + "\nfooter line\n", "utf-8")

        out = _read(SESSION, "data.json")

        assert out["truncated"] is True
        assert "chars clipped" in out["content"], "line 2 was clipped"
        assert len(out["content"]) < BUDGET * 4 * 2, (
            f"the page must stay bounded whatever line the blob sits on "
            f"(got {len(out['content'])} chars)"
        )
        assert "header line" in out["content"], "the page starts at the requested line"
        assert "footer line" not in out["content"], "the walk stopped at the blob"
        assert read_state.licensed(SESSION, target) is None

    def test_the_clipped_hint_names_the_tools_that_can_read_the_rest(self, project):
        # read_file pages by LINE, so the remainder of a clipped line has no
        # read_file address; the hint must say which tools reach it.
        target = project / "min.js"
        target.write_text("x" * 500_000, "utf-8")

        hint = _read(SESSION, "min.js")["hint"]

        assert "search_files" in hint
        assert "terminal" in hint and "python_repl" in hint
        assert "cannot page INSIDE a line" in hint

    def test_the_per_line_cap_holds_even_with_the_budget_switched_off(self, project, monkeypatch):
        # The page budget is switchable (0 = the historical line-count window);
        # the single-line ceiling is NOT: one giant line must never be able to
        # blow the context whatever the operator set the budget to.
        target = project / "min.js"
        target.write_text("x" * 500_000, "utf-8")
        monkeypatch.setattr("agent.tools.file_tools.read_file._TOKEN_BUDGET", 0)

        out = _read(SESSION, "min.js")

        assert out["truncated"] is True
        assert "chars clipped" in out["content"]
        assert len(out["content"]) < _LINE_TOKEN_CAP * 4 * 2
        assert read_state.licensed(SESSION, target) is None

    def test_a_normal_page_is_untouched_by_the_per_line_cap(self, project):
        # Lines well under the ceiling must come back whole — the cap is a
        # ceiling, not a re-wrap.
        target = project / "code.py"
        target.write_text("\n".join(f"value_{i} = compute({i})" for i in range(1, 51)), "utf-8")

        out = _read(SESSION, "code.py")

        assert "clipped" not in out["content"]
        assert out["truncated"] is False
        assert _last_number(out["content"]) == 50


class TestWindowConfiguration:
    def test_the_schema_reads_the_registry(self):
        field = ReadFileInput.model_fields["limit"]

        assert field.default == TOOLS_TIMEOUTS["file_tools_read_default_limit"]
        assert field.default == _READ_DEFAULT_LIMIT
        assert TOOLS_TIMEOUTS["file_tools_read_max_limit"] == _READ_MAX_LIMIT

    def test_a_limit_above_the_maximum_is_clamped(self, project):
        target = project / "big.txt"
        target.write_text("\n".join(f"row {i}" for i in range(1, 5001)), "utf-8")

        out = _read(SESSION, "big.txt", limit=99_999)

        assert len(_numbered_lines(out["content"])) <= _READ_MAX_LIMIT


# ---------------------------------------------------------------------------
# The unchanged re-read stub
# ---------------------------------------------------------------------------


class TestUnchangedStub:
    def test_a_complete_read_licenses_and_the_next_read_is_a_pointer(self, project):
        target = project / "notes.txt"
        target.write_text("alpha\nbeta\n", "utf-8")

        first = _read(SESSION, "notes.txt")
        second = _read(SESSION, "notes.txt")

        assert "1|alpha" in first["content"]
        assert second.get("unchanged") is True
        assert "already in this" in second["content"]
        assert second["total_lines"] == 2

    def test_a_ranged_read_always_returns_content(self, project):
        target = project / "notes.txt"
        target.write_text("alpha\nbeta\n", "utf-8")
        _read(SESSION, "notes.txt")

        out = _read(SESSION, "notes.txt", offset=2)

        assert out.get("unchanged") is None
        assert out["content"] == "2|beta"

    def test_a_changed_file_returns_content(self, project):
        target = project / "notes.txt"
        target.write_text("alpha\n", "utf-8")
        _read(SESSION, "notes.txt")
        target.write_text("alpha\nbeta\n", "utf-8")

        out = _read(SESSION, "notes.txt")

        assert out.get("unchanged") is None
        assert "2|beta" in out["content"]

    def test_a_truncated_first_read_never_stubs(self, project):
        target = project / "big.txt"
        target.write_text("\n".join(f"row {i}" for i in range(1, 2001)), "utf-8")

        first = _read(SESSION, "big.txt")
        assert first["truncated"] is True

        second = _read(SESSION, "big.txt")

        assert second.get("unchanged") is None, "a partial read licenses nothing, so no stub"
        assert "1|row 1" in second["content"]

    def test_a_file_the_session_only_wrote_is_still_returned(self, project):
        build_write_file_tool()._core("own.txt", "written by me\n", session_id=SESSION)

        out = _read(SESSION, "own.txt")

        assert out.get("unchanged") is None
        assert "written by me" in out["content"]

    def test_the_stub_is_per_session(self, project):
        target = project / "notes.txt"
        target.write_text("alpha\n", "utf-8")
        _read(SESSION, "notes.txt")

        out = _read(OTHER_SESSION, "notes.txt")

        assert out.get("unchanged") is None
        assert "1|alpha" in out["content"]
