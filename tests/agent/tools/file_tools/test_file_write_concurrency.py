"""File-tool write concurrency: per-path serialization + the two CAS layers.

What this pins:

* two writers of one path serialize in-process, so a read-modify-write cycle can
  never lose the other's edit;
* a file changed between the tool's read and its write is REFUSED with an
  actionable error instead of being overwritten (layer 1: fingerprint, layer 2:
  the atomic write's ``expected_revision``);
* a reader never observes a half-written file — old content or new content only.

The tools are driven through their real ``_run`` entry points; only the lock
directory is redirected (``SHERRY_FILE_LOCKS_DIR``) so nothing lands in the repo.
"""

import json
import threading

import pytest

from agent.tools.file_tools import patch_file as patch_file_mod
from agent.tools.file_tools.patch_file import build_patch_file_tool
from agent.tools.file_tools.write_file import build_write_file_tool
from agent.tools.pub_base import path_utils
from agent.tools.pub_base.path_lock import active_path_locks

pytestmark = [pytest.mark.unit, pytest.mark.timeout(120)]

SESSION = "s-concurrency"


@pytest.fixture
def root(tmp_path, monkeypatch):
    """A scratch project root plus a scratch lock directory."""
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(path_utils, "ROOT_DIR", project)
    monkeypatch.setenv("SHERRY_PROJECT_DIR", str(project))
    monkeypatch.setenv("SHERRY_FILE_LOCKS_DIR", str(tmp_path / "locks"))
    return project


def _lines(count: int) -> str:
    return "".join(f"line-{i:03d}\n" for i in range(count))


def test_concurrent_patches_of_different_regions_all_land(root):
    """Eight threads patch eight disjoint lines: nothing may be lost."""
    tool = build_patch_file_tool()
    target = root / "wide.txt"
    target.write_text(_lines(40), encoding="utf-8")

    errors: list[str] = []

    def patch(index: int) -> None:
        result = tool._run(
            file_path=str(target),
            old_string=f"line-{index:03d}",
            new_string=f"patched-{index:03d}",
            session_id=SESSION,
        )
        if '"success": true' not in result:
            errors.append(result)

    threads = [threading.Thread(target=patch, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors
    content = target.read_text(encoding="utf-8")
    for index in range(8):
        assert f"patched-{index:03d}\n" in content
    # Untouched lines are intact — no torn or lost region.
    assert content.count("\n") == 40


def test_concurrent_patches_of_the_same_line_never_both_report_success(root):
    """Two writers on one line: exactly one wins, the other is told, not ignored."""
    tool = build_patch_file_tool()
    target = root / "contested.txt"
    target.write_text("only-line\n", encoding="utf-8")

    results: list[str] = []
    barrier = threading.Barrier(2)

    def patch(text: str) -> None:
        barrier.wait()
        results.append(
            tool._run(
                file_path=str(target),
                old_string="only-line",
                new_string=text,
                session_id=SESSION,
            )
        )

    threads = [threading.Thread(target=patch, args=(text,)) for text in ("first", "second")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    successes = [r for r in results if '"success": true' in r]
    failures = [r for r in results if '"success": true' not in r]
    assert len(successes) == 1, results
    assert len(failures) == 1, results
    # The loser learns WHY (no silent no-op): the old text was already replaced.
    assert "not found" in failures[0].lower() or "match" in failures[0].lower()
    assert target.read_text(encoding="utf-8") in {"first\n", "second\n"}


def test_patch_refuses_a_file_changed_between_read_and_write(root, monkeypatch):
    """Layer 1: an external writer landing mid-cycle is detected, not overwritten."""
    tool = build_patch_file_tool()
    target = root / "raced.txt"
    target.write_text("original\n", encoding="utf-8")

    real_replace = patch_file_mod.fuzzy_find_and_replace

    def racing(content, old_string, new_string, replace_all):
        # Another process (not taking our lock) rewrites the file while the tool
        # is still deciding — then the tool continues with what it had read.
        target.write_text("interloper\n", encoding="utf-8")
        return real_replace(content, old_string, new_string, replace_all)

    monkeypatch.setattr(patch_file_mod, "fuzzy_find_and_replace", racing)

    result = tool._run(
        file_path=str(target), old_string="original", new_string="mine", session_id=SESSION
    )

    payload = json.loads(result)
    assert "changed on disk" in payload["error"]
    assert "Re-read" in payload["hint"]
    assert target.read_text(encoding="utf-8") == "interloper\n"


def test_write_reports_a_held_cross_process_lock(root, monkeypatch):
    """A busy lock surfaces as an actionable error, never as a silent overwrite."""
    from agent.tools.pub_base import file_lock

    tool = build_write_file_tool()
    target = root / "busy.txt"
    target.write_text("old", encoding="utf-8")

    import contextlib

    @contextlib.contextmanager
    def busy(target_path, timeout_s=10.0):
        raise file_lock.FileBusyError(target_path, timeout_s)
        yield  # pragma: no cover

    monkeypatch.setattr(patch_file_mod, "file_write_lock", busy)
    from agent.tools.file_tools import write_file as write_file_mod

    monkeypatch.setattr(write_file_mod, "file_write_lock", busy)

    result = tool._run(file_path=str(target), text="new", session_id=SESSION)

    payload = json.loads(result)
    assert "locked by another process" in payload["error"]
    assert target.read_text(encoding="utf-8") == "old"


def test_readers_never_see_a_half_written_file(root):
    """Atomicity: every concurrent read is one of the two full versions."""
    writer = build_write_file_tool()
    target = root / "atomic.txt"
    old_content = "A" * 200_000
    new_content = "B" * 200_000
    target.write_text(old_content, encoding="utf-8")

    stop = threading.Event()
    torn: list[int] = []

    def read_loop() -> None:
        while not stop.is_set():
            try:
                content = target.read_text(encoding="utf-8")
            except FileNotFoundError:  # pragma: no cover - replace is atomic
                torn.append(-1)
                return
            if content not in (old_content, new_content):
                torn.append(len(content))
                return

    reader = threading.Thread(target=read_loop)
    reader.start()
    try:
        for _ in range(30):
            writer._run(file_path=str(target), text=new_content, session_id=SESSION)
            writer._run(file_path=str(target), text=old_content, session_id=SESSION)
    finally:
        stop.set()
        reader.join()

    assert not torn, f"torn reads: {torn[:5]}"


def test_path_lock_registry_drains_after_use(root):
    """No leak: the in-process registry returns to empty once writers finish."""
    tool = build_write_file_tool()
    for index in range(5):
        tool._run(file_path=str(root / f"f{index}.txt"), text="x", session_id=SESSION)

    assert active_path_locks() == 0


def test_appending_python_rewrites_once_and_atomically(root):
    """The .py append flow formats the whole file in ONE atomic write."""
    tool = build_write_file_tool()
    target = root / "appended.py"
    target.write_text("def a():\n    return 1\n", encoding="utf-8")

    result = tool._run(
        file_path=str(target), text="\ndef b():\n    return 2\n", append=True, session_id=SESSION
    )

    assert "successfully" in result
    content = target.read_text(encoding="utf-8")
    assert "def a():" in content and "def b():" in content
    # No torn remnant of the old append-then-rewrite pair.
    assert not list(root.glob(".sherry-tmp-*"))


def test_write_keeps_the_executable_bit(root):
    import stat as stat_mod

    tool = build_write_file_tool()
    target = root / "run.sh"
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    target.chmod(0o755)

    tool._run(file_path=str(target), text="#!/bin/sh\necho hi\n", session_id=SESSION)

    assert stat_mod.S_IMODE(target.stat().st_mode) == 0o755
