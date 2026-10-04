"""Isolated subagent workspaces: snapshot, merge-back CAS, locked serialization.

An isolated child works in a private copy of the project directory; on
completion the copy is merged back file by file under a per-parent-root lock.
The contract pinned here:

* the copy is faithful (files, modes, and empty directories), never follows
  symlinks out of the tree, and skips the heavy caches;
* a child change lands only when the parent still holds the snapshot revision —
  a parent that moved is a CONFLICT, and the parent's file is left untouched;
* new files are created into empty space only; deletions require the parent to
  match; symlinks are never merged through;
* two merges of one parent root serialize on the flock, and a conflicting merge
  keeps the tree while consuming the manifest (no double merge);
* a clean merge removes the whole workspace.
"""

import json
import os
import stat
import threading

import pytest

from agent.tools.subagent.isolation import (
    create_isolated_workspace,
    isolated_workspace_meta,
    merge_isolated_workspace,
)
from agent.tools.subagent.isolation.tree import SNAPSHOT_NAME

pytestmark = [pytest.mark.unit, pytest.mark.timeout(120)]


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A parent project tree plus scratch workspace/lock directories."""
    root = tmp_path / "project"
    root.mkdir()
    workspaces = tmp_path / "isolated"
    locks = tmp_path / "locks"
    monkeypatch.setenv("SHERRY_ISOLATED_WORKSPACES_DIR", str(workspaces))
    monkeypatch.setenv("SHERRY_FILE_LOCKS_DIR", str(locks))
    return root


def _seed(root):
    """A small project: two files and a nested one."""
    (root / "a.txt").write_text("a-v1\n", encoding="utf-8")
    (root / "notes").mkdir()
    (root / "notes" / "n.txt").write_text("n-v1\n", encoding="utf-8")


def test_the_copy_is_faithful_and_the_manifest_records_it(project):
    _seed(project)
    (project / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (project / "run.sh").chmod(0o755)
    (project / "node_modules").mkdir()
    (project / "node_modules" / "junk.js").write_text("junk", encoding="utf-8")

    ws = create_isolated_workspace(project, "agent:main:subagent:one")

    assert ws.tree.name == "tree"
    assert (ws.tree / "a.txt").read_text(encoding="utf-8") == "a-v1\n"
    assert (ws.tree / "notes" / "n.txt").is_file()
    # The execute bit travels with the copy…
    assert stat.S_IMODE((ws.tree / "run.sh").stat().st_mode) == 0o755
    # …and the heavy caches do not.
    assert not (ws.tree / "node_modules").exists()

    manifest = json.loads((ws.meta_dir / SNAPSHOT_NAME).read_text(encoding="utf-8"))
    assert set(manifest["revisions"]) == {"a.txt", "notes/n.txt", "run.sh"}
    assert manifest["parent_root"] == str(project.resolve())
    # The child's cwd is what identifies an isolated run later.
    assert isolated_workspace_meta(ws.tree) == ws.meta_dir
    assert isolated_workspace_meta(project) is None


def test_a_clean_merge_applies_every_change_and_removes_the_workspace(project):
    _seed(project)
    ws = create_isolated_workspace(project, "agent:main:subagent:two")

    (ws.tree / "a.txt").write_text("a-v2 — child edit\n", encoding="utf-8")
    (ws.tree / "fresh.txt").write_text("created by the child\n", encoding="utf-8")
    (ws.tree / "notes" / "n.txt").unlink()

    report = merge_isolated_workspace(ws.meta_dir)

    assert report.is_clean
    assert report.applied == ["a.txt"]
    assert report.created == ["fresh.txt"]
    assert report.deleted == ["notes/n.txt"]
    assert (project / "a.txt").read_text(encoding="utf-8") == "a-v2 — child edit\n"
    assert (project / "fresh.txt").read_text(encoding="utf-8") == "created by the child\n"
    assert not (project / "notes" / "n.txt").exists()
    # A clean merge leaves nothing behind.
    assert not ws.meta_dir.exists()
    assert isolated_workspace_meta(ws.tree) is None


def test_a_parent_edit_since_the_snapshot_is_a_conflict(project):
    _seed(project)
    ws = create_isolated_workspace(project, "agent:main:subagent:three")

    (ws.tree / "a.txt").write_text("child version\n", encoding="utf-8")
    (ws.tree / "notes" / "n.txt").write_text("child n\n", encoding="utf-8")
    # The parent moves a.txt after the snapshot (a different size, so the
    # revision moves even within the same millisecond).
    (project / "a.txt").write_text("parent moved on, unmerged\n", encoding="utf-8")

    report = merge_isolated_workspace(ws.meta_dir)

    assert not report.is_clean
    assert report.applied == ["notes/n.txt"]  # the untouched-in-parent file lands
    assert [path for path, _ in report.conflicts] == ["a.txt"]
    assert (project / "a.txt").read_text(encoding="utf-8") == "parent moved on, unmerged\n"
    # A conflicting merge keeps the tree for inspection but consumes the
    # manifest, so re-running the announce flow cannot merge it twice.
    assert ws.tree.exists()
    assert (ws.tree / "a.txt").read_text(encoding="utf-8") == "child version\n"
    assert isolated_workspace_meta(ws.tree) is None

    # …so a second merge attempt finds no manifest at all.
    with pytest.raises(FileNotFoundError):
        merge_isolated_workspace(ws.meta_dir)


def test_a_new_file_never_clobbers_one_the_parent_created(project):
    _seed(project)
    ws = create_isolated_workspace(project, "agent:main:subagent:four")

    (ws.tree / "shared-new.txt").write_text("child\n", encoding="utf-8")
    (project / "shared-new.txt").write_text("parent\n", encoding="utf-8")

    report = merge_isolated_workspace(ws.meta_dir)

    assert not report.is_clean
    assert [path for path, _ in report.conflicts] == ["shared-new.txt"]
    assert (project / "shared-new.txt").read_text(encoding="utf-8") == "parent\n"


def test_a_deletion_never_drops_a_file_the_parent_changed(project):
    _seed(project)
    ws = create_isolated_workspace(project, "agent:main:subagent:five")

    (ws.tree / "a.txt").unlink()
    (ws.tree / "notes" / "n.txt").unlink()
    (project / "a.txt").write_text("parent rewrote this after the snapshot\n", encoding="utf-8")

    report = merge_isolated_workspace(ws.meta_dir)

    assert report.deleted == ["notes/n.txt"]
    assert [path for path, _ in report.conflicts] == ["a.txt"]
    assert (project / "a.txt").is_file()


def test_symlinks_are_copied_but_never_merged_through(project):
    _seed(project)
    outside = project.parent / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    try:
        os.symlink(outside, project / "link.txt")
    except OSError:
        pytest.skip("cannot create symlink on this platform")

    ws = create_isolated_workspace(project, "agent:main:subagent:six")
    assert (ws.tree / "link.txt").is_symlink()  # copied as a link, not followed
    os.unlink(ws.tree / "link.txt")
    os.symlink(project.parent / "elsewhere.txt", ws.tree / "link.txt")

    report = merge_isolated_workspace(ws.meta_dir)

    assert report.skipped == ["link.txt"]
    assert (project / "link.txt").is_symlink()
    assert os.readlink(project / "link.txt") == str(outside)


def test_an_isolated_run_can_replace_a_file_it_created_in_the_copy(project):
    """The child's own new file is writable again in a second merge pass."""
    _seed(project)
    ws = create_isolated_workspace(project, "agent:main:subagent:seven")

    (ws.tree / "new.txt").write_text("first\n", encoding="utf-8")
    first = merge_isolated_workspace(ws.meta_dir)
    assert first.created == ["new.txt"]

    # A second isolated run sees the merged file as part of the snapshot.
    ws2 = create_isolated_workspace(project, "agent:main:subagent:eight")
    (ws2.tree / "new.txt").write_text("second\n", encoding="utf-8")
    second = merge_isolated_workspace(ws2.meta_dir)

    assert second.applied == ["new.txt"]
    assert (project / "new.txt").read_text(encoding="utf-8") == "second\n"


def test_binary_files_merge_byte_exactly(project):
    """The copy and the merge are byte-level: resource files survive both."""
    _seed(project)
    image = project / "logo.png"
    original = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
    image.write_bytes(original)

    ws = create_isolated_workspace(project, "agent:main:subagent:binary")
    assert (ws.tree / "logo.png").read_bytes() == original

    replacement = b"\x89PNG\r\n\x1a\n" + b"\xff" * 40
    (ws.tree / "logo.png").write_bytes(replacement)
    report = merge_isolated_workspace(ws.meta_dir)

    assert report.applied == ["logo.png"]
    assert image.read_bytes() == replacement


def test_files_the_child_never_touched_are_left_alone(project):
    """A parent edit is only a conflict for a file the child actually changed."""
    _seed(project)
    ws = create_isolated_workspace(project, "agent:main:subagent:nine")

    (ws.tree / "added.txt").write_text("child's own file\n", encoding="utf-8")
    # The parent rewrites a file the child never opened.
    (project / "a.txt").write_text("parent moved on, unmerged\n", encoding="utf-8")

    report = merge_isolated_workspace(ws.meta_dir)

    assert report.is_clean
    assert report.created == ["added.txt"]
    assert report.applied == []
    assert (project / "a.txt").read_text(encoding="utf-8") == "parent moved on, unmerged\n"


def test_merges_of_one_parent_root_serialize(project, monkeypatch):
    """Two children finishing at once must not interleave their applies."""
    _seed(project)
    first = create_isolated_workspace(project, "agent:main:subagent:ten")
    second = create_isolated_workspace(project, "agent:main:subagent:eleven")
    (first.tree / "a.txt").write_text("from the first\n", encoding="utf-8")
    (second.tree / "notes" / "n.txt").write_text("from the second\n", encoding="utf-8")

    from agent.tools.subagent.isolation import merge as merge_mod

    order: list[str] = []
    real_apply = merge_mod._apply_bytes

    def recording_apply(source, target, expected_revision):
        order.append(f"enter {target.name}")
        try:
            real_apply(source, target, expected_revision)
        finally:
            order.append(f"exit {target.name}")

    monkeypatch.setattr(merge_mod, "_apply_bytes", recording_apply)

    threads = [
        threading.Thread(target=merge_isolated_workspace, args=(ws.meta_dir,))
        for ws in (first, second)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    # No interleaving: every enter is followed by its own exit.
    for index in range(0, len(order), 2):
        assert order[index].startswith("enter")
        assert order[index + 1].replace("exit", "enter", 1) == order[index]
    assert (project / "a.txt").read_text(encoding="utf-8") == "from the first\n"
    assert (project / "notes" / "n.txt").read_text(encoding="utf-8") == "from the second\n"
