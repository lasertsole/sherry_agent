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
    # The execute bit travels with the checkout (group/other bits follow the
    # process umask, exactly as `git checkout` gives them)…
    assert stat.S_IMODE((ws.tree / "run.sh").stat().st_mode) & 0o100
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


# ---------------------------------------------------------------------------
# The worktree backend: baseline, auto-init, materialization, exemption
# ---------------------------------------------------------------------------


def _git(root, *args, check=False):
    """Run git in *root* and return the completed process (text mode)."""
    import subprocess

    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=check
    )


def test_the_worktree_starts_from_the_dirty_working_tree(project):
    """Uncommitted edits are the baseline: the child sees them, HEAD does not."""
    _seed(project)
    _git(project, "init", "-q")
    _git(project, "add", "-A")
    _git(project, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "seed")
    (project / "a.txt").write_text("edited but uncommitted\n", encoding="utf-8")

    ws = create_isolated_workspace(project, "agent:main:subagent:dirty")

    # The child sees the user's working-tree content, not the committed one.
    assert (ws.tree / "a.txt").read_text(encoding="utf-8") == "edited but uncommitted\n"
    manifest = json.loads((ws.meta_dir / SNAPSHOT_NAME).read_text(encoding="utf-8"))
    assert manifest["backend"] == "worktree"
    assert manifest["base_sha"]
    assert manifest["branch"].startswith("sherry/")
    # The user's stash stack is untouched by `git stash create`.
    assert _git(project, "stash", "list").stdout.strip() == ""


def test_auto_init_never_commits_secrets_and_writes_its_marker(project):
    """The deny list must land BEFORE `git add -A` — history keeps what it took."""
    _seed(project)
    (project / ".env").write_text("API_KEY=sk-real-secret\n", encoding="utf-8")
    (project / ".env.example").write_text("API_KEY=\n", encoding="utf-8")
    (project / "cert.pem").write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")

    ws = create_isolated_workspace(project, "agent:main:subagent:init")

    # The repository was created here, and the baseline commit holds the sources…
    committed = _git(project, "ls-tree", "-r", "--name-only", "HEAD").stdout.split()
    assert "a.txt" in committed
    assert ".env.example" in committed  # the intentional sample stays
    # …but no secret of any kind.
    assert ".env" not in committed
    assert "cert.pem" not in committed
    # And the child's tree does not carry them either (they were never checked out).
    manifest = json.loads((ws.meta_dir / SNAPSHOT_NAME).read_text(encoding="utf-8"))
    assert manifest["initialized_repo"] is True
    marker = project / ".sherry-isolation-repo"
    assert marker.is_file()
    assert "rm -rf" in marker.read_text(encoding="utf-8")


def test_ignored_paths_are_materialized_as_links_and_copies(project):
    """Links for shared state, copies for mutable per-run state."""
    _seed(project)
    (project / ".gitignore").write_text("src/\n.omo/\n", encoding="utf-8")
    (project / "src" / "data").mkdir(parents=True)
    (project / "src" / "data" / "state.db").write_text("db", encoding="utf-8")
    (project / ".omo").mkdir()
    (project / ".omo" / "boulder.json").write_text("{}", encoding="utf-8")
    # A project declaration adds a path of its own and retargets one default
    # (mode after the path, gitignore-glob syntax).
    (project / ".worktreeinclude").write_text("logs/\n# wti=copy\n", encoding="utf-8")
    (project / ".gitignore").write_text("src/\n.omo/\nlogs/\n", encoding="utf-8")
    (project / "logs").mkdir()
    (project / "logs" / "app.log").write_text("line\n", encoding="utf-8")

    ws = create_isolated_workspace(project, "agent:main:subagent:include")

    # src/ is linked: the same inode serves both trees (SQLite WAL stays coherent).
    assert (ws.tree / "src").is_symlink()
    assert (ws.tree / "src" / "data" / "state.db").read_text(encoding="utf-8") == "db"
    # .omo/ is copied: a mutable file two trees must not share.
    assert not (ws.tree / ".omo").is_symlink()
    assert (ws.tree / ".omo" / "boulder.json").read_text(encoding="utf-8") == "{}"
    # logs/ came from the project's own declaration, in copy mode.
    assert not (ws.tree / "logs").is_symlink()
    assert (ws.tree / "logs" / "app.log").is_file()

    manifest = json.loads((ws.meta_dir / SNAPSHOT_NAME).read_text(encoding="utf-8"))
    assert set(manifest["materialized"]) >= {"src", ".omo", "logs"}


def test_a_tracked_path_is_never_replaced_by_a_link(project):
    """The intersection rule: only git-ignored declarations are materialized."""
    _seed(project)
    (project / ".worktreeinclude").write_text("notes/\n", encoding="utf-8")
    _git(project, "init", "-q")
    _git(project, "add", "-A")
    _git(project, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "seed")

    ws = create_isolated_workspace(project, "agent:main:subagent:tracked")

    assert (ws.tree / "notes").is_dir()
    assert not (ws.tree / "notes").is_symlink()
    manifest = json.loads((ws.meta_dir / SNAPSHOT_NAME).read_text(encoding="utf-8"))
    assert "notes" not in manifest["materialized"]


def test_materialized_links_are_exempt_from_the_external_path_gate(project, monkeypatch):
    """A read through a recorded link is internal; an unrecorded link is not."""
    _seed(project)
    (project / ".gitignore").write_text("src/\n", encoding="utf-8")
    (project / "src").mkdir()
    (project / "src" / "data.db").write_text("db", encoding="utf-8")

    ws = create_isolated_workspace(project, "agent:main:subagent:exempt")
    from agent.tools.pub_base.path_utils import _is_materialized_link_path

    # The child's project root is the worktree; src/ inside it is a recorded link.
    assert _is_materialized_link_path(ws.tree / "src" / "data.db", ws.tree)
    # A file that is merely under the tree is not a "materialized" path.
    assert not _is_materialized_link_path(ws.tree / "a.txt", ws.tree)
    # Nor is anything outside the tree.
    assert not _is_materialized_link_path(project / "src" / "data.db", ws.tree)


def test_discarding_a_workspace_unregisters_the_worktree_and_branch(project):
    """A kept (conflicting) tree must not leave a stale worktree registration."""
    _seed(project)
    ws = create_isolated_workspace(project, "agent:main:subagent:discard")
    branch = json.loads((ws.meta_dir / SNAPSHOT_NAME).read_text(encoding="utf-8"))["branch"]
    assert branch in _git(project, "branch", "--list").stdout

    from agent.tools.subagent.isolation import discard_isolated_workspace

    discard_isolated_workspace(ws.meta_dir)

    assert not ws.meta_dir.exists()
    assert _git(project, "worktree", "list").stdout.count("\n") == 1  # only the main tree
    assert branch not in _git(project, "branch", "--list").stdout


# ---------------------------------------------------------------------------
# Interface changes in the merge report
#
# The per-file CAS is revision-aware and semantically blind: a rename merges
# cleanly while another child's file may still call the OLD name. The merge
# therefore snapshots the symbol surface of every merged code file, and the
# completion report tells the parent what moved.
# ---------------------------------------------------------------------------


def _write_python(path, source: str) -> None:
    path.write_text(source, encoding="utf-8")


def test_merge_report_includes_renamed_function(project):
    _write_python(project / "utils.py", "def get_user():\n    return 1\n")
    ws = create_isolated_workspace(project, "agent:main:subagent:iface-rename")

    _write_python(ws.tree / "utils.py", "def get_users():\n    return 1\n")
    report = merge_isolated_workspace(ws.meta_dir)

    assert report.applied == ["utils.py"]
    deltas = {delta.relpath: delta for delta in report.interface_changes}
    assert list(deltas) == ["utils.py"]
    delta = deltas["utils.py"]
    assert delta.language == "python"
    assert [(r.old_name, r.new_name, r.kind, r.parent) for r in delta.renamed] == [
        ("get_user", "get_users", "function", None)
    ]
    # The parent's tree holds the child's revision.
    assert (project / "utils.py").read_text(encoding="utf-8").startswith("def get_users")


def test_merge_report_includes_added_and_removed(project):
    _write_python(
        project / "helpers.py",
        'def keep():\n    return 1\n\n\ndef old_helper():\n    return "old"\n',
    )
    ws = create_isolated_workspace(project, "agent:main:subagent:iface-add-remove")

    _write_python(
        ws.tree / "helpers.py",
        'def keep():\n    return 1\n\n\ndef brand_new_function():\n    return "new"\n',
    )
    report = merge_isolated_workspace(ws.meta_dir)

    delta = report.interface_changes[0]
    assert [r.name for r in delta.removed] == ["old_helper"]
    assert [a.name for a in delta.added] == ["brand_new_function"]
    assert delta.renamed == []


def test_report_block_renders_interface_changes(project):
    _write_python(
        project / "utils.py",
        'def get_user():\n    return 1\n\n\ndef old_helper():\n    return "old"\n',
    )
    ws = create_isolated_workspace(project, "agent:main:subagent:iface-report")

    _write_python(
        ws.tree / "utils.py",
        'def get_users():\n    return 1\n\n\ndef brand_new_function():\n    return "new"\n',
    )
    report = merge_isolated_workspace(ws.meta_dir)

    from agent.tools.subagent.announce.workspace_merge import _report_block

    block = _report_block(report)

    assert "### Isolated workspace merged" in block
    assert "**Interface changes**" in block
    assert "- `utils.py` (python):" in block
    assert "renamed: `get_user` → `get_users` (function)" in block
    assert "removed: `old_helper` (function)" in block
    assert "added: `brand_new_function` (function)" in block
    # A report without interface changes must not grow an empty section.
    report.interface_changes.clear()
    assert "Interface changes" not in _report_block(report)


def test_report_block_caps_the_interface_listing(project):
    """A regenerated file must not turn the completion reply into a dump."""
    _write_python(
        project / "many.py", "".join(f"def fn_{i}():\n    return {i}\n\n\n" for i in range(25))
    )
    ws = create_isolated_workspace(project, "agent:main:subagent:iface-cap")

    _write_python(
        ws.tree / "many.py",
        "".join(f"def fn_{i}x():\n    return {i}\n\n\n" for i in range(25)),
    )
    report = merge_isolated_workspace(ws.meta_dir)

    from agent.tools.subagent.announce.workspace_merge import _report_block

    block = _report_block(report)

    assert len(report.interface_changes[0].renamed) == 25
    assert block.count("renamed: ") == 20
    assert "… and 5 more" in block


def test_unsupported_language_skipped(project):
    _seed(project)
    ws = create_isolated_workspace(project, "agent:main:subagent:iface-txt")

    (ws.tree / "a.txt").write_text("def renamed_but_not_indexed():\n", encoding="utf-8")
    report = merge_isolated_workspace(ws.meta_dir)

    assert report.applied == ["a.txt"]
    assert report.interface_changes == []


def test_syntax_error_does_not_break_merge(project):
    _write_python(project / "broken.py", "def fine():\n    return 1\n")
    ws = create_isolated_workspace(project, "agent:main:subagent:iface-syntax")

    _write_python(ws.tree / "broken.py", "def broken(:\n    return\n")
    report = merge_isolated_workspace(ws.meta_dir)

    # The merge applied the child's revision; only the note carries the error.
    assert report.applied == ["broken.py"]
    assert (project / "broken.py").read_text(encoding="utf-8").startswith("def broken(:")
    assert len(report.interface_changes) == 1
    delta = report.interface_changes[0]
    assert delta.error
    assert delta.has_changes is False
    assert "not compared" in _render_block(report)


def _render_block(report) -> str:
    from agent.tools.subagent.announce.workspace_merge import _report_block

    return _report_block(report)
