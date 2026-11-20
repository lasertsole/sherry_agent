"""patch_file multi-file patch mode: preflight, commit, CAS and integration.

The single-file str_replace mode is pinned by the existing file-tool suites;
this file covers the ``patch=`` branch plus the regression that the old call
shape is untouched.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from agent.tools.file_tools import patch_apply
from agent.tools.file_tools.patch_file import PatchFileInput, build_patch_file_tool
from agent.tools.pub_base import path_utils

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

SESSION = "s-patch-mode"


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    monkeypatch.setattr(path_utils, "ROOT_DIR", root)
    monkeypatch.setenv("SHERRY_PROJECT_DIR", str(root))
    from runtime import state_register_mem

    state_register_mem.clear_session(SESSION)
    state_register_mem.set_state(SESSION, "caller_scope", "subagent")
    return root


def _run(patch_text: str) -> dict:
    tool = build_patch_file_tool()
    out = tool._core(None, None, None, session_id=SESSION, patch=patch_text)
    return json.loads(out)


def _patch(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def test_add_file(workspace):
    result = _run(
        _patch("*** Begin Patch", "*** Add File: src/new.txt", "+hello", "+world", "*** End Patch")
    )
    assert result["success"] is True
    assert result["applied"] == 1
    assert (workspace / "src/new.txt").read_text(encoding="utf-8") == "hello\nworld\n"


def test_add_python_file_is_formatted(workspace):
    import importlib.util

    result = _run(
        _patch(
            "*** Begin Patch",
            "*** Add File: tool.py",
            "+def f( ):",
            "+    return  1",
            "*** End Patch",
        )
    )
    assert result["success"] is True
    text = (workspace / "tool.py").read_text(encoding="utf-8")
    if importlib.util.find_spec("autopep8") is not None:
        assert "def f():" in text and "return 1" in text
    else:
        # Formatting is advisory: without autopep8 the bytes stay as patched.
        assert "def f( ):" in text


def test_update_file_with_hunk(workspace):
    target = workspace / "a.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    result = _run(
        _patch(
            "*** Begin Patch",
            "*** Update File: a.txt",
            "@@",
            " alpha",
            "-beta",
            "+BETA",
            " gamma",
            "*** End Patch",
        )
    )
    assert result["success"] is True
    assert target.read_text(encoding="utf-8") == "alpha\nBETA\ngamma\n"
    assert result["operations"][0]["diff"]


def test_multi_file_add_update_delete_move(workspace):
    (workspace / "keep.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    (workspace / "gone.txt").write_text("delete me\n", encoding="utf-8")
    (workspace / "move.txt").write_text("move me\n", encoding="utf-8")

    result = _run(
        _patch(
            "*** Begin Patch",
            "*** Add File: added.txt",
            "+added",
            "*** Update File: keep.txt",
            "@@",
            "-beta",
            "+BETA",
            "*** Delete File: gone.txt",
            "*** Update File: move.txt",
            "*** Move to: moved.txt",
            "@@",
            "-move me",
            "+moved",
            "*** End Patch",
        )
    )

    assert result["success"] is True
    assert result["applied"] == 4
    assert (workspace / "added.txt").read_text(encoding="utf-8") == "added\n"
    assert (workspace / "keep.txt").read_text(encoding="utf-8") == "alpha\nBETA\n"
    assert not (workspace / "gone.txt").exists()
    assert not (workspace / "move.txt").exists()
    assert (workspace / "moved.txt").read_text(encoding="utf-8") == "moved\n"


def test_move_without_hunks_preserves_content(workspace):
    (workspace / "old.txt").write_text("same\n", encoding="utf-8")
    result = _run(
        _patch(
            "*** Begin Patch",
            "*** Update File: old.txt",
            "*** Move to: new.txt",
            "*** End Patch",
        )
    )
    assert result["success"] is True
    assert (workspace / "new.txt").read_text(encoding="utf-8") == "same\n"
    assert not (workspace / "old.txt").exists()


def test_add_then_update_the_same_file(workspace):
    result = _run(
        _patch(
            "*** Begin Patch",
            "*** Add File: fresh.txt",
            "+one",
            "*** Update File: fresh.txt",
            "@@",
            "-one",
            "+two",
            "*** End Patch",
        )
    )
    assert result["success"] is True
    assert (workspace / "fresh.txt").read_text(encoding="utf-8") == "two\n"


def test_delete_then_add_the_same_path(workspace):
    (workspace / "x.txt").write_text("old\n", encoding="utf-8")
    result = _run(
        _patch(
            "*** Begin Patch",
            "*** Delete File: x.txt",
            "*** Add File: x.txt",
            "+new",
            "*** End Patch",
        )
    )
    assert result["success"] is True
    assert (workspace / "x.txt").read_text(encoding="utf-8") == "new\n"


def test_a_syntax_error_changes_nothing(workspace):
    (workspace / "a.txt").write_text("keep\n", encoding="utf-8")
    result = _run("*** Begin Patch\n*** Add File: b.txt\n+new\n")
    assert "error" in result and result["changed"] is False
    assert not (workspace / "b.txt").exists()
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "keep\n"


def test_a_hunk_mismatch_changes_nothing_and_suggests_a_section(workspace):
    target = workspace / "a.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    result = _run(
        _patch(
            "*** Begin Patch",
            "*** Add File: b.txt",
            "+would have been written",
            "*** Update File: a.txt",
            "@@",
            "-béta",
            "+BETA",
            "*** End Patch",
        )
    )
    assert "error" in result and result["changed"] is False
    assert not (workspace / "b.txt").exists()
    assert target.read_text(encoding="utf-8") == "alpha\nbeta\ngamma\n"
    refusal = "\n".join(result["rejected"])
    assert "hunk mismatch" in refusal
    assert "Did you mean" in refusal


@pytest.mark.parametrize(
    ("setup", "patch", "needle"),
    [
        (
            None,
            _patch(
                "*** Begin Patch",
                "*** Update File: missing.txt",
                "@@",
                "-a",
                "+b",
                "*** End Patch",
            ),
            "file not found",
        ),
        (
            "existing",
            _patch("*** Begin Patch", "*** Add File: a.txt", "+x", "*** End Patch"),
            "already exists",
        ),
        (
            "existing",
            _patch(
                "*** Begin Patch",
                "*** Update File: a.txt",
                "*** Move to: a.txt",
                "@@",
                "-a",
                "+b",
                "*** End Patch",
            ),
            "destination",
        ),
        (
            "binary",
            _patch(
                "*** Begin Patch",
                "*** Update File: b.bin",
                "@@",
                "-a",
                "+b",
                "*** End Patch",
            ),
            "binary",
        ),
        (
            "existing",
            _patch(
                "*** Begin Patch",
                "*** Update File: a.txt",
                "*** Move to: taken.txt",
                "@@",
                "-a",
                "+b",
                "*** End Patch",
            ),
            "destination",
        ),
        (
            "existing",
            _patch("*** Begin Patch", "*** Add File: ../escape.txt", "+x", "*** End Patch"),
            "syntax error",
        ),
        (
            "existing",
            _patch("*** Begin Patch", "*** Add File: /tmp/escape.txt", "+x", "*** End Patch"),
            "syntax error",
        ),
    ],
)
def test_refusals_leave_every_file_untouched(workspace, setup, patch, needle):
    if setup == "existing":
        (workspace / "a.txt").write_text("a\n", encoding="utf-8")
        (workspace / "taken.txt").write_text("taken\n", encoding="utf-8")
    elif setup == "binary":
        (workspace / "b.bin").write_bytes(b"\x80\x81\x82")
    before = sorted(
        (str(path.relative_to(workspace)), path.read_bytes())
        for path in workspace.rglob("*")
        if path.is_file() and ".git" not in path.parts
    )
    result = _run(patch)
    assert "error" in result, result
    text = json.dumps(result, ensure_ascii=False)
    assert needle in text
    after = sorted(
        (str(path.relative_to(workspace)), path.read_bytes())
        for path in workspace.rglob("*")
        if path.is_file() and ".git" not in path.parts
    )
    assert after == before


def test_a_commit_failure_stops_without_rollback(workspace, monkeypatch):
    (workspace / "a.txt").write_text("a\n", encoding="utf-8")

    real_commit_update = patch_apply._commit_update

    def _boom(resolved, session_id):
        if resolved.operation.path == "b.txt":
            raise OSError("simulated commit failure")
        return real_commit_update(resolved, session_id)

    monkeypatch.setattr(patch_apply, "_commit_update", _boom)
    patch = _patch(
        "*** Begin Patch",
        "*** Add File: a-new.txt",
        "+one",
        "*** Update File: b.txt",
        "@@",
        "-b",
        "+B",
        "*** Add File: c-new.txt",
        "+three",
        "*** End Patch",
    )
    (workspace / "b.txt").write_text("b\n", encoding="utf-8")

    result = _run(patch)

    assert result["success"] is False
    assert result["applied"] == 1
    statuses = [operation["status"] for operation in result["operations"]]
    assert statuses == ["completed", "failed", "skipped"]
    assert (workspace / "a-new.txt").exists()
    assert (workspace / "b.txt").read_text(encoding="utf-8") == "b\n"
    assert not (workspace / "c-new.txt").exists()


def test_utf16_content_keeps_its_encoding(workspace):
    target = workspace / "u16.txt"
    target.write_bytes("old\n".encode("utf-16"))
    result = _run(
        _patch(
            "*** Begin Patch",
            "*** Update File: u16.txt",
            "@@",
            "-old",
            "+new",
            "*** End Patch",
        )
    )
    assert result["success"] is True
    assert target.read_bytes().startswith(b"\xff\xfe")
    assert target.read_text(encoding="utf-16") == "new\n"


def test_str_replace_mode_is_unchanged(workspace):
    target = workspace / "x.txt"
    target.write_text("a\n", encoding="utf-8")
    tool = build_patch_file_tool()

    out = json.loads(tool._core("x.txt", "a", "b", session_id=SESSION))

    assert out["success"] is True
    assert target.read_text(encoding="utf-8") == "b\n"


def test_patch_wins_over_single_file_arguments(workspace):
    tool = build_patch_file_tool()
    out = json.loads(
        tool._core(
            "ignored.txt",
            "nothing",
            "nothing",
            session_id=SESSION,
            patch=_patch("*** Begin Patch", "*** Add File: only.txt", "+x", "*** End Patch"),
        )
    )
    assert out["success"] is True
    assert (workspace / "only.txt").exists()
    assert not (workspace / "ignored.txt").exists()


def test_the_async_path_runs_the_same_patch(workspace):
    tool = build_patch_file_tool()
    out = asyncio.run(
        tool._arun(
            None,
            None,
            None,
            session_id=SESSION,
            patch=_patch("*** Begin Patch", "*** Add File: async.txt", "+x", "*** End Patch"),
        )
    )
    assert json.loads(out)["success"] is True
    assert (workspace / "async.txt").exists()


def test_the_schema_accepts_either_mode_but_not_neither():
    assert PatchFileInput(patch="*** Begin Patch\n*** End Patch\n").patch
    assert PatchFileInput(file_path="a", old_string="x", new_string="y").file_path == "a"
    with pytest.raises(ValueError):
        PatchFileInput(file_path="a")


@pytest.mark.skipif(os.name != "posix", reason="symlink fixture is POSIX-only")
def test_a_symlink_target_is_refused(workspace):
    outside = workspace.parent / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    try:
        os.symlink(outside, workspace / "link.txt")
    except OSError:
        pytest.skip("cannot create symlink on this platform")
    result = _run(
        _patch(
            "*** Begin Patch",
            "*** Update File: link.txt",
            "@@",
            "-outside",
            "+inside",
            "*** End Patch",
        )
    )
    assert "error" in result
    assert outside.read_text(encoding="utf-8") == "outside\n"


def test_patch_mode_marks_each_touched_path_stale(workspace, monkeypatch):
    (workspace / "b.txt").write_text("b\n", encoding="utf-8")
    (workspace / "c.txt").write_text("c\n", encoding="utf-8")
    marked: list[str] = []
    monkeypatch.setattr(
        patch_apply, "mark_evidence_stale", lambda path, session: marked.append(path)
    )

    result = _run(
        _patch(
            "*** Begin Patch",
            "*** Add File: a.txt",
            "+a",
            "*** Update File: b.txt",
            "@@",
            "-b",
            "+B",
            "*** Update File: c.txt",
            "*** Move to: d.txt",
            "*** End Patch",
        )
    )

    assert result["success"] is True
    assert {"a.txt", "b.txt", "c.txt", "d.txt"} <= set(marked)


def test_one_shadow_checkpoint_per_patch(workspace, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        patch_apply, "ensure_checkpoint", lambda session_id, reason="": calls.append(reason)
    )
    result = _run(
        _patch(
            "*** Begin Patch",
            "*** Add File: one.txt",
            "+1",
            "*** Add File: two.txt",
            "+2",
            "*** End Patch",
        )
    )
    assert result["success"] is True
    assert len(calls) == 1


def test_lsp_sync_runs_for_each_written_file(workspace, monkeypatch):
    (workspace / "b.txt").write_text("b\n", encoding="utf-8")
    synced: list[str] = []
    monkeypatch.setattr(
        patch_apply, "_sync", lambda path, content, session_id: synced.append(path.name)
    )
    result = _run(
        _patch(
            "*** Begin Patch",
            "*** Add File: a.txt",
            "+a",
            "*** Update File: b.txt",
            "@@",
            "-b",
            "+B",
            "*** End Patch",
        )
    )
    assert result["success"] is True
    assert set(synced) == {"a.txt", "b.txt"}


def test_the_langchain_invoke_path_accepts_a_patch_only_call(workspace):
    tool = build_patch_file_tool()
    out = asyncio.run(
        tool.ainvoke(
            {
                "patch": _patch(
                    "*** Begin Patch", "*** Add File: invoked.txt", "+x", "*** End Patch"
                ),
                "session_id": SESSION,
            }
        )
    )
    assert json.loads(out)["success"] is True
    assert (workspace / "invoked.txt").read_text(encoding="utf-8") == "x\n"
