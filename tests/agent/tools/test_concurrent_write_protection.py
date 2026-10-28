"""Concurrent-write protection and symbol-break detection (P0 batch).

The audit's finding: same-file writes were well protected (locks + write
license + revision CAS + merge CAS), but the SYMBOL world was not — a renamed
or reshaped function merged with zero conflicts and no report line, and the
shell wrote files with no ledger row at all. These cases pin the four P0 fixes:

* a formatter may only be replayed in a report-only form (it rewrites files
  outside the whole license/CAS stack);
* a shell command's writes are accounted from the shadow-git diff;
* the interface diff sees parameter/return shape changes and module-level
  variables;
* the merge report names the files that still import a changed symbol, and an
  optional merge-time replay can produce a verdict on the spot.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.tools.code_intel.symbol_diff import diff_file_interface

pytestmark = [pytest.mark.unit, pytest.mark.timeout(120)]


class TestFormatterReplayIsReportOnly:
    """A recorded `black foo.py` must not be replayed as a file rewrite."""

    @pytest.mark.parametrize(
        ("command", "allowed"),
        [
            ("black foo.py", False),
            ("black --check foo.py", True),
            ("black --diff foo.py", True),
            ("prettier --write src/", False),
            ("prettier src/", False),
            ("prettier --check src/", True),
            ("ruff format foo.py", False),
            ("ruff format --check foo.py", True),
            ("ruff check foo.py", True),
            ("npx prettier --write x", False),
            ("npx prettier --check x", True),
            ("pnpm exec black foo.py", False),
            # Non-formatters are unaffected.
            ("pytest -q", True),
            ("eslint src/", True),
            ("tsc --noEmit", True),
        ],
    )
    def test_the_formatter_policy(self, command: str, allowed: bool) -> None:
        from agent.tools.pub_base.regression_gate import _safe_argv

        assert (_safe_argv(command) is not None) is allowed


class TestShellWritesAreAccounted:
    """`sed -i` / `open(...,'w')` leave a ledger row like `write_file` does."""

    @pytest.fixture()
    def project(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        import agent.tools.pub_base.evidence_ledger as ledger_mod
        import agent.tools.pub_base.path_utils as path_utils

        root = tmp_path / "proj"
        root.mkdir()
        (root / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
        monkeypatch.setattr(path_utils, "session_workspace_root", lambda _sid: root)
        monkeypatch.setattr(ledger_mod, "session_workspace_root", lambda _sid: root)
        monkeypatch.setattr(
            ledger_mod.EvidenceLedger, "LEDGER_PATH", str(root / "src" / "ledger.db")
        )
        return root

    def test_a_shell_write_reaches_the_changed_paths(self, project: Path) -> None:
        from agent.tools.pub_base.evidence_ledger import EvidenceLedger
        from agent.tools.pub_base.shadow_git import diff_changed_paths, ensure_checkpoint
        from agent.tools.terminal import _record_shell_writes

        ensure_checkpoint("sess-1", reason="before_write:terminal:sed")
        (project / "a.py").write_text("VALUE = 2\n", encoding="utf-8")

        assert diff_changed_paths(project) == ["a.py"]
        assert EvidenceLedger.changed_paths("sess-1") == []

        _record_shell_writes("sess-1")

        assert EvidenceLedger.changed_paths("sess-1") == ["a.py"]

    def test_an_unchanged_tree_marks_nothing(self, project: Path) -> None:
        from agent.tools.pub_base.evidence_ledger import EvidenceLedger
        from agent.tools.pub_base.shadow_git import ensure_checkpoint
        from agent.tools.terminal import _record_shell_writes

        ensure_checkpoint("sess-2", reason="before_write:terminal:true")
        _record_shell_writes("sess-2")

        assert EvidenceLedger.changed_paths("sess-2") == []

    def test_accounting_never_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import agent.tools.pub_base.shadow_git as shadow_git

        def _boom(_path: Path, **_kwargs):
            raise RuntimeError("git exploded")

        monkeypatch.setattr(shadow_git, "diff_changed_paths", _boom)

        from agent.tools.terminal import _record_shell_writes

        _record_shell_writes("sess-3")  # must not raise


class TestSignatureSurface:
    """Same name, different call shape: the case a rename hunt never finds."""

    def test_a_parameter_change_is_reported(self) -> None:
        old = b"def foo(a, b):\n    return a\n"
        new = b"def foo(a, b, *, timeout=1):\n    return a\n"

        delta = diff_file_interface(old, new, "python", "utils.py")

        assert delta is not None and delta.has_changes
        assert [change.name for change in delta.changed] == ["foo"]
        assert delta.changed[0].old_signature == "(a, b)"
        assert "timeout" in (delta.changed[0].new_signature or "")
        assert not delta.renamed and not delta.added and not delta.removed

    def test_a_return_annotation_appearing_is_reported(self) -> None:
        old = b"def foo(a):\n    return a\n"
        new = b"def foo(a) -> int:\n    return a\n"

        delta = diff_file_interface(old, new, "python", "utils.py")

        assert [change.name for change in delta.changed] == ["foo"]

    def test_a_pure_reformat_is_not_reported(self) -> None:
        old = b"def foo(a,  b):\n  return a\n"
        new = b"def foo(a, b):\n    return a\n"

        delta = diff_file_interface(old, new, "python", "utils.py")

        assert delta is not None and delta.changed == []

    def test_a_module_variable_rename_shows_as_removed_plus_added(self) -> None:
        old = b"MAX_N = 3\ndef foo():\n    return MAX_N\n"
        new = b"MAX_COUNT = 3\ndef foo():\n    return MAX_COUNT\n"

        delta = diff_file_interface(old, new, "python", "utils.py")

        assert {symbol.name for symbol in delta.removed} == {"MAX_N"}
        assert {symbol.name for symbol in delta.added} == {"MAX_COUNT"}

    def test_function_locals_never_enter_the_surface(self) -> None:
        old = b"def foo():\n    LOCAL = 1\n    return LOCAL\n"
        new = b"def foo():\n    LOCAL = 2\n    return LOCAL\n"

        delta = diff_file_interface(old, new, "python", "utils.py")

        assert delta is not None and not delta.has_changes

    def test_the_code_index_does_not_gain_variables(self) -> None:
        """The extractor's variable surface is opt-in — the index must not list it."""
        from agent.tools.code_intel.extract import extract_symbols

        source = b"MAX_N = 3\ndef foo():\n    return MAX_N\n"

        indexed = extract_symbols(source, "python")
        diffed = extract_symbols(source, "python", include_module_variables=True)

        assert "variable" not in {symbol.kind for symbol in indexed.symbols}
        assert "variable" in {symbol.kind for symbol in diffed.symbols}


class TestReverseImportIndex:
    """Name the callers instead of leaving the hunt to the model."""

    @staticmethod
    def _index(tmp_path: Path, rows: list[tuple[str, str]]) -> Path:
        import sqlite3

        db = tmp_path / "index.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE import_edges (id INTEGER PRIMARY KEY, importer_id INTEGER, "
            "imported_name TEXT, imported_id INTEGER, file_path TEXT, line INTEGER, "
            "resolved INTEGER)"
        )
        conn.executemany(
            "INSERT INTO import_edges(importer_id, imported_name, file_path, line, resolved) "
            "VALUES (0, ?, ?, 1, 0)",
            [(name, path) for name, path in rows],
        )
        conn.commit()
        conn.close()
        return db

    def test_importers_are_returned_deduped(self, tmp_path: Path) -> None:
        from agent.tools.code_intel.indexer import importers_of_names

        db = self._index(tmp_path, [("foo", "b.py"), ("foo", "c.py"), ("bar", "d.py")])

        assert importers_of_names(db, {"foo"}) == ["b.py", "c.py"]
        assert importers_of_names(db, {"foo", "bar"}) == ["b.py", "c.py", "d.py"]

    def test_a_missing_index_answers_empty_and_never_builds(self, tmp_path: Path) -> None:
        from agent.tools.code_intel.indexer import importers_of_names

        assert importers_of_names(tmp_path / "absent.db", {"foo"}) == []
        assert not (tmp_path / "absent.db").exists(), "the lookup must not create an index"

    def test_the_cap_bounds_the_answer(self, tmp_path: Path) -> None:
        from agent.tools.code_intel.indexer import importers_of_names

        db = self._index(tmp_path, [("foo", f"m{i}.py") for i in range(10)])

        assert len(importers_of_names(db, {"foo"}, cap=3)) == 3


class TestMergeReportNamesImporters:
    """The announcement carries both the callers and (optionally) a verdict."""

    def test_the_report_renders_the_importer_line(self) -> None:
        from agent.tools.subagent.announce.workspace_merge import _interface_lines
        from agent.tools.subagent.isolation.merge import MergeReport

        report = MergeReport(
            parent_root=Path("/proj"),
            applied=["utils.py"],
            interface_changes=[
                diff_file_interface(
                    b"def foo(a):\n    pass\n", b"def foo(a, b):\n    pass\n", "python", "utils.py"
                )
            ],
            affected_importers=["b.py", "c.py"],
            merge_reverify=["failed: pytest -q"],
        )

        text = "\n".join(_interface_lines(report))

        assert "changed:" in text and "foo" in text
        assert "affected importers" in text and "b.py" in text
        assert "re-verify failed: pytest -q" in text

    def test_the_merge_verdict_is_opt_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio

        from config.features import EVIDENCE_REGRESSION_GATE
        from agent.tools.subagent.announce import workspace_merge
        from agent.tools.subagent.isolation.merge import MergeReport

        calls: list[tuple] = []

        async def _spy(*args, **kwargs):
            calls.append(args)
            return []

        monkeypatch.setattr("agent.tools.pub_base.regression_gate.reverify_affected_paths", _spy)
        monkeypatch.setitem(EVIDENCE_REGRESSION_GATE, "merge_reverify_enabled", False)
        report = MergeReport(parent_root=Path("/proj"), affected_importers=["b.py"])
        run = _stub_run()

        asyncio.run(workspace_merge._reverify_after_merge(run, report))
        assert calls == [], "disabled by default: no subprocess during an announce"

        monkeypatch.setitem(EVIDENCE_REGRESSION_GATE, "merge_reverify_enabled", True)
        asyncio.run(workspace_merge._reverify_after_merge(run, report))
        assert len(calls) == 1, "enabling it replays the affected commands"


def _stub_run():
    """A run record with just the spawner/requester fields the merge reads."""
    from agent.tools.subagent.types.registry import SubagentRunRecord

    return SubagentRunRecord(
        run_id="run-merge-1",
        child_session_key="agent:main:subagent:child",
        requester_session_key="agent:main:session:owner",
        spawned_by="agent:main:session:owner",
        task="probe",
    )
