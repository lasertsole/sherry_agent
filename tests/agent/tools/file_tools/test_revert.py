"""Stage 2 acceptance: the revert tool's plan/apply contract.

The rule under test is the batch rule: one file that moved since its snapshot
refuses the WHOLE revert with every reason reported and the disk untouched, and
a refusal in a git work tree additionally reports whether a three-way merge is
even feasible. A clean revert restores each path to its EARLIEST snapshot and
consumes the rows (so a second revert finds nothing — there is no redo).
"""

import asyncio
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from agent.tools.file_tools import snapshot as snapshot_mod
from agent.tools.file_tools.read_file import build_read_file_tool
from agent.tools.file_tools.revert import build_file_changes_revert_tool, revert_file_changes
from agent.tools.file_tools.write_file import build_write_file_tool
from agent.tools.pub_base import path_utils

pytestmark = [pytest.mark.unit, pytest.mark.timeout(120)]

SESSION = "s-revert"


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(path_utils, "ROOT_DIR", root)
    monkeypatch.setenv("SHERRY_PROJECT_DIR", str(root))

    from runtime import state_register_mem

    monkeypatch.setattr(state_register_mem, "_states", {})
    state_register_mem.clear_session(SESSION)
    yield root


def _read(path: str) -> dict:
    return json.loads(build_read_file_tool()._core(path, session_id=SESSION))


def _write(path: str, text: str, call: str, **kwargs) -> str:
    return build_write_file_tool()._core(
        path, text, session_id=SESSION, tool_call_id=call, **kwargs
    )


def _revert(**kwargs) -> dict:
    return json.loads(build_file_changes_revert_tool()._run(session_id=SESSION, **kwargs))


def test_a_single_write_reverts_to_the_old_content(project):
    target = project / "notes.txt"
    target.write_text("original\n", encoding="utf-8")
    _read("notes.txt")
    _write("notes.txt", "changed\n", "call-1")

    result = _revert()

    assert result["success"] is True
    assert target.read_text(encoding="utf-8") == "original\n"
    assert [f["detail"] for f in result["files"]] == ["restored"]


def test_a_path_written_twice_reverts_to_the_earliest_snapshot(project):
    """Undo means "the state before this turn's first write", not the last one."""
    target = project / "loop.txt"
    target.write_text("v0\n", encoding="utf-8")
    _read("loop.txt")
    _write("loop.txt", "v1\n", "call-a")
    _write("loop.txt", "v2\n", "call-b")
    _write("loop.txt", "v3\n", "call-c")

    assert _revert()["success"] is True

    assert target.read_text(encoding="utf-8") == "v0\n"


def test_a_created_file_is_deleted_not_emptied(project):
    _write("fresh.txt", "hello\n", "call-new")

    result = _revert()

    assert result["success"] is True
    assert not (project / "fresh.txt").exists()


def test_an_external_edit_refuses_the_whole_batch(project):
    safe = project / "safe.txt"
    safe.write_text("safe-v0\n", encoding="utf-8")
    touched = project / "touched.txt"
    touched.write_text("touched-v0\n", encoding="utf-8")
    _read("safe.txt")
    _read("touched.txt")
    _write("safe.txt", "safe-v1\n", "call-safe")
    _write("touched.txt", "touched-v1\n", "call-touched")

    # The user edits one of the two files after the agent wrote it.
    touched.write_text("the user's own edit, longer\n", encoding="utf-8")

    result = _revert()

    assert result["success"] is False
    assert "nothing was changed" in result["error"]
    reasons = {f["path"]: f.get("reason") for f in result["files"]}
    assert reasons[str(touched)] == "external_modified"
    # The safe file is reported safe but NOT applied — the batch rule.
    assert reasons[str(safe)] is None
    assert safe.read_text(encoding="utf-8") == "safe-v1\n"
    assert touched.read_text(encoding="utf-8") == "the user's own edit, longer\n"


def test_a_file_deleted_by_the_user_is_refused_not_recreated(project):
    target = project / "gone.txt"
    target.write_text("v0\n", encoding="utf-8")
    _read("gone.txt")
    _write("gone.txt", "v1\n", "call-x")
    target.unlink()

    result = _revert()

    assert result["success"] is False
    assert {f["path"]: f.get("reason") for f in result["files"]}[str(target)] == "missing"
    assert not target.exists()


def test_a_touched_but_unchanged_file_reverts_through_the_hash_channel(project):
    """mtime moved, bytes identical: the same exemption patch_file gives."""
    target = project / "touched.txt"
    target.write_text("v0\n", encoding="utf-8")
    _read("touched.txt")
    _write("touched.txt", "v1\n", "call-t")

    import os

    future = target.stat().st_mtime + 5
    os.utime(target, (future, future))

    result = _revert()

    assert result["success"] is True
    assert target.read_text(encoding="utf-8") == "v0\n"


def test_a_missing_blob_reports_snapshot_expired(project):
    target = project / "expired.txt"
    target.write_text("v0\n", encoding="utf-8")
    _read("expired.txt")
    _write("expired.txt", "v1\n", "call-e")

    rows = snapshot_mod.rows_for_session(SESSION)
    blob = snapshot_mod._blob_path(SESSION, rows[0].blob_sha256)
    assert blob is not None
    blob.unlink()

    result = _revert()

    assert result["success"] is False
    assert {f["path"]: f.get("reason") for f in result["files"]}[str(target)] == "snapshot_expired"
    assert target.read_text(encoding="utf-8") == "v1\n"


def test_dry_run_touches_nothing(project):
    target = project / "dry.txt"
    target.write_text("v0\n", encoding="utf-8")
    _read("dry.txt")
    _write("dry.txt", "v1\n", "call-d")

    result = _revert(dry_run=True)

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["files"][0]["action"] == "restore"
    assert target.read_text(encoding="utf-8") == "v1\n"
    # The rows survive a dry run: the real revert still works afterwards.
    assert _revert()["success"] is True
    assert target.read_text(encoding="utf-8") == "v0\n"


def test_reverts_are_one_shot(project):
    target = project / "once.txt"
    target.write_text("v0\n", encoding="utf-8")
    _read("once.txt")
    _write("once.txt", "v1\n", "call-o")

    assert _revert()["success"] is True

    second = _revert()
    assert second["success"] is False
    assert "no reversible changes" in second["error"]


def test_a_scoped_revert_only_touches_the_named_path(project):
    first = project / "a.txt"
    second = project / "b.txt"
    first.write_text("a0\n", encoding="utf-8")
    second.write_text("b0\n", encoding="utf-8")
    _read("a.txt")
    _read("b.txt")
    _write("a.txt", "a1\n", "call-a")
    _write("b.txt", "b1\n", "call-b")

    result = _revert(paths=["a.txt"])

    assert result["success"] is True
    assert first.read_text(encoding="utf-8") == "a0\n"
    assert second.read_text(encoding="utf-8") == "b1\n"


def test_to_tool_call_id_reverts_up_to_that_call(project):
    target = project / "seq.txt"
    target.write_text("v0\n", encoding="utf-8")
    _read("seq.txt")
    _write("seq.txt", "v1\n", "call-1")
    _write("seq.txt", "v2\n", "call-2")

    result = _revert(to_tool_call_id="call-1")

    assert result["success"] is True
    assert target.read_text(encoding="utf-8") == "v0\n"
    # call-2's row is untouched: only the consumed scope is removed.
    remaining = snapshot_mod.rows_for_session(SESSION)
    assert [r.tool_call_id for r in remaining] == ["call-2"]


def test_a_refusal_in_a_git_work_tree_reports_merge_feasibility(project):
    repo = project
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    target = repo / "code.py"
    target.write_text("alpha = 1\nbeta = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "code.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)

    _read("code.py")
    _write("code.py", "alpha = 1\nbeta = 3\n", "call-git")
    # A later external edit that does NOT overlap the snapshot's change.
    target.write_text("alpha = 1\ngamma = 9\nbeta = 3\n", encoding="utf-8")

    status_before = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
    ).stdout
    result = _revert()
    status_after = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
    ).stdout

    assert result["success"] is False
    assert result["merge3way"] is not None
    assert result["merge3way"]["available"] is True
    assert "git merge-file -p" in result["merge3way"]["command"]
    # Read-only: the probe leaves the work tree exactly as it found it.
    assert status_before == status_after
    assert target.read_text(encoding="utf-8") == "alpha = 1\ngamma = 9\nbeta = 3\n"


def test_a_refusal_outside_a_git_work_tree_has_no_merge_block(project):
    target = project / "plain.txt"
    target.write_text("v0\n", encoding="utf-8")
    _read("plain.txt")
    _write("plain.txt", "v1\n", "call-p")
    target.write_text("user's edit, longer\n", encoding="utf-8")

    result = _revert()

    assert result["success"] is False
    assert result["merge3way"] is None


def test_the_tool_returns_json_for_the_model(project):
    target = project / "model.txt"
    target.write_text("v0\n", encoding="utf-8")
    _read("model.txt")
    _write("model.txt", "v1\n", "call-m")

    raw = build_file_changes_revert_tool()._run(session_id=SESSION)

    assert json.loads(raw)["success"] is True


def test_a_binary_blob_round_trips(project):
    """Restore is byte-level: a file the text tools refuse still reverts."""
    target = project / "keep.bin"
    target.write_bytes(b"\x00\x01\x02old")
    rows_before = snapshot_mod.rows_for_session(SESSION)
    assert rows_before == []  # sanity: nothing captured yet

    # Force a capture the way the tools do, then a change, then revert.
    capture = snapshot_mod.capture_pre_write(target, project)
    target.write_bytes(b"\x00\x01\x02new")
    snapshot_mod.finalize_capture(
        capture, session_id=SESSION, tool_call_id="call-bin", written=target.read_bytes()
    )

    result = revert_file_changes(SESSION)

    assert result["success"] is True
    assert target.read_bytes() == b"\x00\x01\x02old"


def test_digest_of_restored_content_matches_the_blob(project):
    target = project / "digest.txt"
    payload = "正文 content\n"
    target.write_text(payload, encoding="utf-8")
    _read("digest.txt")
    _write("digest.txt", "replaced\n", "call-d2")

    assert _revert()["success"] is True

    assert (
        hashlib.sha256(target.read_bytes()).hexdigest()
        == hashlib.sha256(payload.encode("utf-8")).hexdigest()
    )
    assert Path(target).read_text(encoding="utf-8") == payload


def test_the_revert_notice_names_the_files_and_is_an_ai_message(monkeypatch):
    """The conversation is never cut: the agent is TOLD what was undone."""
    from langchain_core.messages import AIMessage

    from agent.tools.file_tools import revert as revert_mod

    captured: dict = {}

    class _Graph:
        async def aupdate_state(self, config, values):  # noqa: ANN001
            captured["config"] = config
            captured["values"] = values

    async def _built_agent() -> _Graph:
        return _Graph()

    monkeypatch.setattr("agent.core.built_agent", _built_agent)

    ok = asyncio.run(revert_mod.append_revert_notice("s-notice", ["/proj/a.txt", "/proj/b.txt"]))

    assert ok is True
    message = captured["values"]["messages"][0]
    assert isinstance(message, AIMessage)
    assert message.content.startswith("<revert>user had revert editing")
    assert "/proj/a.txt" in message.content
    assert "/proj/b.txt" in message.content
    assert message.content.endswith("</revert>")


def test_the_notice_is_skipped_without_files():
    from agent.tools.file_tools.revert import append_revert_notice

    assert asyncio.run(append_revert_notice("s", [])) is False
    assert asyncio.run(append_revert_notice("", ["/a"])) is False


def test_the_notice_never_raises(monkeypatch):
    from agent.tools.file_tools import revert as revert_mod

    async def _boom() -> None:
        raise RuntimeError("graph unavailable")

    monkeypatch.setattr("agent.core.built_agent", _boom)

    assert asyncio.run(revert_mod.append_revert_notice("s", ["/a"])) is False


def test_a_successful_revert_reports_the_paths_it_undid(project):
    target = project / "undone.txt"
    target.write_text("v0\n", encoding="utf-8")
    _read("undone.txt")
    _write("undone.txt", "v1\n", "call-u")

    result = _revert()

    assert result["success"] is True
    assert result["reverted_paths"] == [str(target)]
