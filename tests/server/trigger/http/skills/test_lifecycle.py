"""Upload / toggle endpoint tests (``server/trigger/http/skills/lifecycle.py``).

These are the two endpoints that accept third-party code and mutate the skill
state file, and they had no coverage. Locked here:

- upload: name resolution (form > frontmatter > filename), size cap, name
  validation, path confinement, and the security-scan gate running BEFORE
  anything reaches ``PLUGIN_SKILLS_DIR`` (staged scan in a temp dir, rejected
  verdicts leave no skill dir and no state entry);
- the scan policy wired end-to-end: DO_NOT_INSTALL and "enabled but
  unavailable" block the upload, while ``SKILL_SCANNER_ENABLED=0`` (explicit
  operator opt-out) allows it;
- CAUTION is advisory: the upload lands and the warnings ride the response;
- toggle: state update + snapshot rebuild, 404 for unknown skills, 400 for
  invalid names / non-boolean ``active``;
- both endpoints persist the state file through the atomic writer.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.service.skill_scan_model import ScanFinding, ScanResult, ScanStatus, Severity
from server.trigger.http.skills import _shared
from server.trigger.http.skills import lifecycle

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

SKILL_MD = "---\nname: demo\ndescription: demo skill\n---\n\nbody\n"


class _FakeUploadRequest:
    def __init__(self, files: dict[str, object] | None = None, form: dict[str, str] | None = None):
        self.files = files or {}
        self.form_data = form or {}


class _FakeJsonRequest:
    def __init__(self, payload: object):
        self._payload = payload

    def json(self):
        return self._payload


def _safe_scan(_path: Path) -> ScanResult:
    return ScanResult(status=ScanStatus.SCANNED, risk_score=0, risk_recommendation="SAFE")


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "plugins_skills"
    root.mkdir()
    state_file = tmp_path / "skills_state.json"
    monkeypatch.setattr(lifecycle, "PLUGIN_SKILLS_DIR", root)
    monkeypatch.setattr(_shared, "SKILLS_STATE_FILE", state_file)
    rebuilds: list[int] = []
    monkeypatch.setattr(lifecycle, "_rebuild_snapshot", lambda: rebuilds.append(1))
    monkeypatch.setattr(lifecycle, "scan_skill", _safe_scan)
    return SimpleNamespace(root=root, state_file=state_file, rebuilds=rebuilds)


def _upload(request) -> tuple[int, dict]:
    response = asyncio.run(lifecycle.upload_skill_handler(request))
    return int(response.status_code), json.loads(response.description)


def _toggle(request) -> tuple[int, dict]:
    response = asyncio.run(lifecycle.toggle_skill_handler(request))
    return int(response.status_code), json.loads(response.description)


def _state(state_file: Path) -> dict:
    return json.loads(state_file.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


def test_upload_writes_skill_and_inactive_state(env):
    status, body = _upload(
        _FakeUploadRequest(files={"SKILL.md": SKILL_MD.encode()}, form={"name": "demo"})
    )

    assert status == 200
    assert body == {"success": True, "warnings": []}
    assert (env.root / "demo" / "SKILL.md").read_text(encoding="utf-8") == SKILL_MD
    assert _state(env.state_file) == {"demo": {"active": False}}
    assert env.rebuilds == [1], "a successful upload rebuilds the snapshot once"


def test_upload_name_falls_back_to_frontmatter(env):
    status, _ = _upload(_FakeUploadRequest(files={"SKILL.md": SKILL_MD.encode()}))

    assert status == 200
    assert (env.root / "demo" / "SKILL.md").is_file()


def test_upload_missing_file_part_is_400(env):
    status, body = _upload(_FakeUploadRequest(files={}, form={"name": "demo"}))

    assert status == 400
    assert body["success"] is False
    assert "Missing file part" in body["message"]
    assert list(env.root.iterdir()) == []


def test_upload_oversize_content_is_400(env):
    status, body = _upload(
        _FakeUploadRequest(files={"SKILL.md": b"x" * 200_000}, form={"name": "demo"})
    )

    assert status == 400
    assert "characters (limit" in body["message"]
    assert list(env.root.iterdir()) == []


def test_upload_traversal_name_is_rejected_and_writes_nothing(env, tmp_path):
    status, body = _upload(
        _FakeUploadRequest(files={"SKILL.md": SKILL_MD.encode()}, form={"name": "../../evil"})
    )

    assert status == 400
    assert "Invalid skill name" in body["message"]
    assert list(env.root.iterdir()) == []
    assert not (tmp_path.parent / "evil").exists()
    assert not env.state_file.exists()


def test_upload_do_not_install_verdict_blocks_before_any_write(env, monkeypatch):
    staging_dirs: list[Path] = []

    def _hostile_scan(path: Path) -> ScanResult:
        staging_dirs.append(path)
        return ScanResult(
            status=ScanStatus.SCANNED,
            risk_score=95,
            risk_recommendation="DO_NOT_INSTALL",
            findings=[ScanFinding(title="exfiltrates ~/.ssh", severity=Severity.HIGH)],
        )

    monkeypatch.setattr(lifecycle, "scan_skill", _hostile_scan)
    status, body = _upload(
        _FakeUploadRequest(files={"SKILL.md": SKILL_MD.encode()}, form={"name": "demo"})
    )

    assert status == 400
    assert "DO_NOT_INSTALL" in body["message"]
    assert list(env.root.iterdir()) == [], "nothing may reach PLUGIN_SKILLS_DIR"
    assert not env.state_file.exists()
    assert env.rebuilds == []
    assert staging_dirs and not staging_dirs[0].is_relative_to(env.root), (
        "the scan must run on the staged copy, outside the skills dir"
    )


def test_upload_unavailable_scanner_with_enabled_flag_fails_closed(env, monkeypatch):
    monkeypatch.setattr(
        lifecycle,
        "scan_skill",
        lambda path: ScanResult(status=ScanStatus.UNAVAILABLE, disabled_by_config=False),
    )
    status, body = _upload(
        _FakeUploadRequest(files={"SKILL.md": SKILL_MD.encode()}, form={"name": "demo"})
    )

    assert status == 400
    assert "scanner is enabled but could not run" in body["message"]
    assert list(env.root.iterdir()) == []


def test_upload_unavailable_scanner_disabled_by_config_allows(env, monkeypatch):
    monkeypatch.setattr(
        lifecycle,
        "scan_skill",
        lambda path: ScanResult(status=ScanStatus.UNAVAILABLE, disabled_by_config=True),
    )
    status, body = _upload(
        _FakeUploadRequest(files={"SKILL.md": SKILL_MD.encode()}, form={"name": "demo"})
    )

    assert status == 200
    assert body["success"] is True
    assert (env.root / "demo" / "SKILL.md").is_file()


def test_upload_caution_lands_with_advisory_warnings(env, monkeypatch):
    def _caution_scan(_path: Path) -> ScanResult:
        return ScanResult(
            status=ScanStatus.SCANNED,
            risk_score=40,
            risk_recommendation="CAUTION",
            findings=[ScanFinding(title="reads environment variables", severity=Severity.MEDIUM)],
        )

    monkeypatch.setattr(lifecycle, "scan_skill", _caution_scan)
    status, body = _upload(
        _FakeUploadRequest(files={"SKILL.md": SKILL_MD.encode()}, form={"name": "demo"})
    )

    assert status == 200
    assert body["success"] is True
    assert len(body["warnings"]) == 1
    assert "CAUTION" in body["warnings"][0]
    assert "reads environment variables" in body["warnings"][0]
    assert (env.root / "demo" / "SKILL.md").is_file()


# ---------------------------------------------------------------------------
# Toggle
# ---------------------------------------------------------------------------


def _seed_skill(env, name: str = "demo") -> Path:
    skill_dir = env.root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    return skill_dir


def test_toggle_updates_state_and_rebuilds_snapshot(env):
    _seed_skill(env)

    status, body = _toggle(_FakeJsonRequest({"name": "demo", "active": True}))

    assert status == 200
    assert body == {"success": True}
    assert _state(env.state_file) == {"demo": {"active": True}}
    assert env.rebuilds == [1]


def test_toggle_unknown_skill_is_404(env):
    status, body = _toggle(_FakeJsonRequest({"name": "missing", "active": True}))

    assert status == 404
    assert "not found" in body["message"]
    assert not env.state_file.exists()
    assert env.rebuilds == []


def test_toggle_traversal_name_is_400(env):
    status, body = _toggle(_FakeJsonRequest({"name": "../../evil", "active": True}))

    assert status == 400
    assert "Invalid skill name" in body["message"]
    assert not env.state_file.exists()


def test_toggle_non_boolean_active_is_400(env):
    _seed_skill(env)

    status, body = _toggle(_FakeJsonRequest({"name": "demo", "active": "yes"}))

    assert status == 400
    assert "must be boolean" in body["message"]
    assert not env.state_file.exists()


def test_toggle_invalid_json_body_is_400(env):
    status, body = _toggle(_FakeJsonRequest(42))

    assert status == 400
    assert body["message"] == "Invalid JSON body"


# ---------------------------------------------------------------------------
# State-file persistence
# ---------------------------------------------------------------------------


def test_state_writes_go_through_the_atomic_writer(env, monkeypatch):
    """Both endpoints persist the state file through ``atomic_write_text``.

    The helper's own temp-file + ``os.replace`` semantics are covered by
    ``tests/server/test_atomic_io.py``; this pins the endpoint wiring so a
    partial write can never truncate the skill state.
    """
    _seed_skill(env)
    calls: list[tuple[object, str]] = []

    def _spy(path, text, **kwargs):
        calls.append((path, text))

    monkeypatch.setattr(_shared, "atomic_write_text", _spy)

    status, _ = _toggle(_FakeJsonRequest({"name": "demo", "active": True}))

    assert status == 200
    assert len(calls) == 1
    written_path, written_text = calls[0]
    assert Path(written_path) == env.state_file
    assert json.loads(written_text) == {"demo": {"active": True}}
