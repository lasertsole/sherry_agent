"""The read-before-write license: ``write_file`` refuses to clobber unseen content.

ZCode's Write/Edit require the file to have been read in the session first;
Sherry's ``write_file`` was a blind overwrite that could silently destroy an
edit it never saw. The license (``agent/tools/pub_base/read_state.py``) comes
from a COMPLETE ``read_file`` or from this session's own previous write, and is
advanced by this session's own append/patch. Pinned here:

* an existing file this session never read is REFUSED, content untouched;
* a complete read licenses the overwrite;
* a partial read (offset > 1, or truncated) licenses nothing;
* a change landing between the read and the write is refused, not clobbered;
* creating a file licenses the next overwrite of it;
* append/patch move an existing license forward but never invent one;
* licenses are per session and per resolved path.
"""

import json
import os
import time

import pytest

from agent.tools.file_tools.patch_file import build_patch_file_tool
from agent.tools.file_tools.read_file import build_read_file_tool
from agent.tools.file_tools.write_file import build_write_file_tool
from agent.tools.pub_base import path_utils
from agent.tools.pub_base import read_state

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

SESSION = "s-read-before-write"
OTHER_SESSION = "s-read-before-write-2"


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


def _write(session: str, path: str, text: str, **kwargs) -> str:
    return build_write_file_tool()._core(path, text, session_id=session, **kwargs)


def _read(session: str, path: str, **kwargs) -> dict:
    return json.loads(build_read_file_tool()._core(path, session_id=session, **kwargs))


def _patch(session: str, path: str, old: str, new: str) -> dict:
    return json.loads(
        build_patch_file_tool()._core(path, old_string=old, new_string=new, session_id=session)
    )


def test_overwriting_an_unread_file_is_refused(project):
    target = project / "config.json"
    target.write_text('{"keep": true}', encoding="utf-8")

    out = json.loads(_write(SESSION, "config.json", '{"replaced": true}'))

    assert "has not been read" in out["error"]
    assert "read_file" in out["hint"]
    assert target.read_text(encoding="utf-8") == '{"keep": true}'


def test_a_complete_read_licenses_the_overwrite(project):
    target = project / "config.json"
    target.write_text('{"keep": true}', encoding="utf-8")

    _read(SESSION, "config.json")
    out = _write(SESSION, "config.json", '{"replaced": true}')

    assert "successfully" in out
    assert target.read_text(encoding="utf-8") == '{"replaced": true}'


def test_a_partial_read_licenses_nothing(project):
    target = project / "big.txt"
    target.write_text("\n".join(f"line-{i}" for i in range(1, 11)), encoding="utf-8")

    # Wrong window…
    page = _read(SESSION, "big.txt", offset=2)
    assert page["total_lines"] == 10
    # …and truncated first page: neither is the whole file.
    first_page = _read(SESSION, "big.txt", limit=3)
    assert first_page["truncated"] is True

    out = json.loads(_write(SESSION, "big.txt", "replaced"))
    assert "has not been read" in out["error"]
    assert target.read_text(encoding="utf-8").startswith("line-1")


def test_a_change_between_read_and_write_is_refused(project):
    target = project / "notes.txt"
    target.write_text("original", encoding="utf-8")

    _read(SESSION, "notes.txt")
    # Another writer lands after the read (different size, so the revision
    # moves even within the same millisecond).
    target.write_text("someone else's much longer content", encoding="utf-8")

    out = json.loads(_write(SESSION, "notes.txt", "mine"))

    assert "changed on disk" in out["error"]
    assert "Re-read" in out["hint"]
    assert target.read_text(encoding="utf-8") == "someone else's much longer content"


def test_creating_a_file_licenses_the_next_overwrite(project):
    assert "successfully" in _write(SESSION, "fresh.txt", "v1")
    # No read in between: this session authored v1, so it may replace it.
    assert "successfully" in _write(SESSION, "fresh.txt", "v2")

    assert (project / "fresh.txt").read_text(encoding="utf-8") == "v2"


def test_a_create_raced_by_another_writer_is_refused(project, monkeypatch):
    """The window between "the file is absent" and the replace is closed too."""
    from agent.tools.pub_base import atomic_write as atomic_write_mod

    target = project / "race.txt"
    real_revision = atomic_write_mod.file_revision

    def racing(path):
        # Another writer creates the file after this session's "it does not
        # exist" check but before the replace.
        target.write_text("created by someone else", encoding="utf-8")
        return real_revision(path)

    monkeypatch.setattr(atomic_write_mod, "file_revision", racing)

    out = json.loads(_write(SESSION, "race.txt", "mine"))

    assert "changed on disk" in out["error"]
    assert target.read_text(encoding="utf-8") == "created by someone else"


def test_patch_advances_a_license_but_never_invents_one(project):
    target = project / "patchable.txt"
    target.write_text("alpha beta\n", encoding="utf-8")

    # Patching without a read works (the tool reads for itself) but does not
    # license an overwrite of content the session has never seen.
    assert _patch(SESSION, "patchable.txt", "beta", "gamma")["success"] is True
    refused = json.loads(_write(SESSION, "patchable.txt", "replaced"))
    assert "has not been read" in refused["error"]

    # With a read first, the session's own patch advances the license instead of
    # making it stale: the overwrite is allowed.
    _read(SESSION, "patchable.txt")
    assert _patch(SESSION, "patchable.txt", "gamma", "delta")["success"] is True
    assert "successfully" in _write(SESSION, "patchable.txt", "replaced")
    assert target.read_text(encoding="utf-8") == "replaced"


def test_append_advances_an_existing_license(project):
    target = project / "log.txt"
    target.write_text("first\n", encoding="utf-8")

    _read(SESSION, "log.txt")
    assert "successfully" in _write(SESSION, "log.txt", "second\n", append=True)
    # The session knows the file it appended to: the overwrite is licensed.
    assert "successfully" in _write(SESSION, "log.txt", "replaced\n")

    assert target.read_text(encoding="utf-8") == "replaced\n"


def test_append_alone_licenses_nothing(project):
    target = project / "unseen.txt"
    target.write_text("content\n", encoding="utf-8")

    assert "successfully" in _write(SESSION, "unseen.txt", "more\n", append=True)

    out = json.loads(_write(SESSION, "unseen.txt", "replaced\n"))
    assert "has not been read" in out["error"]


def test_licenses_are_per_session(project):
    target = project / "shared.txt"
    target.write_text("content\n", encoding="utf-8")

    _read(SESSION, "shared.txt")

    out = json.loads(_write(OTHER_SESSION, "shared.txt", "replaced"))
    assert "has not been read" in out["error"]
    assert target.read_text(encoding="utf-8") == "content\n"

    # …and the reading session still holds its license.
    assert "successfully" in _write(SESSION, "shared.txt", "replaced")


def test_a_read_licenses_only_the_file_it_read(project):
    (project / "a.txt").write_text("A\n", encoding="utf-8")
    (project / "b.txt").write_text("B\n", encoding="utf-8")

    _read(SESSION, "a.txt")

    assert "successfully" in _write(SESSION, "a.txt", "A2")
    out = json.loads(_write(SESSION, "b.txt", "B2"))
    assert "has not been read" in out["error"]


def test_a_touch_between_read_and_write_is_refused(project):
    target = project / "touched.txt"
    target.write_text("content\n", encoding="utf-8")

    _read(SESSION, "touched.txt")
    # Same bytes, a moved mtime: the license is a revision, so a bare touch by
    # anyone else refuses the write. Pinned deliberately — refusing (and
    # re-reading) is cheaper than hashing the target on every overwrite, and
    # unlike patch_file's read-time fingerprint there is no content-hash
    # exemption here.
    future = time.time() + 5
    os.utime(target, (future, future))

    out = json.loads(_write(SESSION, "touched.txt", "replaced"))

    assert "changed on disk" in out["error"]
    assert target.read_text(encoding="utf-8") == "content\n"


def test_a_restart_forgets_the_licenses(project):
    target = project / "notes.txt"
    target.write_text("content\n", encoding="utf-8")
    _read(SESSION, "notes.txt")

    # The registry is process-local, so a restart (or any eviction) drops the
    # licenses: the next overwrite asks for a read again — one extra read,
    # never a lost edit.
    read_state.forget_all()

    out = json.loads(_write(SESSION, "notes.txt", "replaced"))
    assert "has not been read" in out["error"]
    assert target.read_text(encoding="utf-8") == "content\n"


def test_the_license_registry_evicts_the_oldest(monkeypatch, project):
    monkeypatch.setattr(read_state, "_MAX_LICENSES", 2)
    first, second, third = (project / name for name in ("a", "b", "c"))

    read_state.note_read(SESSION, first, "mtime:1:size:1")
    read_state.note_read(SESSION, second, "mtime:2:size:2")
    read_state.note_read(SESSION, third, "mtime:3:size:3")

    # Eviction drops protection, never grants it: the evicted file needs a
    # fresh read before it can be overwritten again.
    assert read_state.licensed_revision(SESSION, first) is None
    assert read_state.licensed_revision(SESSION, second) == "mtime:2:size:2"
    assert read_state.licensed_revision(SESSION, third) == "mtime:3:size:3"
