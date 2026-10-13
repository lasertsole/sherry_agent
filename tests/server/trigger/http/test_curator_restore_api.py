"""Module tests for the curator restore HTTP entry points.

Covers ``POST /curator/restore`` and ``GET /curator/archived`` in
``server/trigger/http/curator.py``. No real server is started: the registered
Robyn handlers are invoked directly with a fabricated request exposing only
the attribute they read (``json()``) — the same pattern as
``tests/server/test_cron_api.py``. The restore route runs against a real
isolated archive tree, so the wiring (name validation -> curator-layer
restore -> HTTP envelope) is covered end to end.
"""

import asyncio
import json

import pytest
from typing import Any
from dataclasses import dataclass

from context_engine.curator import constants as curator_constants
from context_engine.curator import usage as curator_usage
from server.trigger.http import curator as curator_api


pytestmark = [pytest.mark.module]


class _FakeRequest:
    """Minimal request stand-in: the handlers only call ``request.json()``."""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("invalid json")
        return self._payload


@dataclass(frozen=True)
class _Tree:
    auto: Any
    archive: Any
    usage_dir: Any


@pytest.fixture
def curator_tree(tmp_path, monkeypatch):
    skills_root = tmp_path / "skills"
    auto = skills_root / "auto"
    usage_dir = auto / ".usage"
    archive = skills_root / ".archive"
    usage_dir.mkdir(parents=True)

    monkeypatch.setattr(curator_constants, "AUTO_SKILLS_DIR", auto)
    monkeypatch.setattr(curator_constants, "ARCHIVE_DIR", archive)
    monkeypatch.setattr(curator_usage, "USAGE_DIR", usage_dir)
    return _Tree(auto=auto, archive=archive, usage_dir=usage_dir)


def _call(handler, payload):
    return asyncio.run(handler(_FakeRequest(payload)))


def _payload(response) -> dict:
    return json.loads(response.description)


def _make_skill(tree: _Tree, name: str) -> None:
    skill_dir = tree.auto / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill\n---\n\nBody of {name}.\n",
        encoding="utf-8",
    )


class TestRestoreEndpoint:
    def test_restores_an_archived_skill(self, curator_tree):
        _make_skill(curator_tree, "docker")
        assert curator_usage.archive_skill("docker")[0] is True

        response = _call(curator_api.restore_archived_skill_handler, {"name": "docker"})

        assert response.status_code == 200
        payload = _payload(response)
        assert payload["success"] is True
        assert payload["name"] == "docker"
        assert "restored" in payload["message"]
        assert (curator_tree.auto / "docker" / "SKILL.md").is_file()
        assert curator_usage.load_record("docker")["state"] == curator_constants.STATE_ACTIVE

    def test_unknown_skill_returns_400(self, curator_tree):
        curator_tree.archive.mkdir(parents=True)

        response = _call(curator_api.restore_archived_skill_handler, {"name": "ghost"})

        assert response.status_code == 400
        payload = _payload(response)
        assert payload["success"] is False
        assert "not found in archive" in payload["message"]

    def test_bundled_shadow_returns_400(self, curator_tree):
        (curator_tree.archive / "docker").mkdir(parents=True)
        (curator_tree.auto / ".bundled_manifest").write_text("docker:deadbeef\n", encoding="utf-8")

        response = _call(curator_api.restore_archived_skill_handler, {"name": "docker"})

        assert response.status_code == 400
        assert "bundled or hub-installed" in _payload(response)["message"]
        assert (curator_tree.archive / "docker").is_dir()

    def test_missing_name_returns_400(self, curator_tree):
        response = _call(curator_api.restore_archived_skill_handler, {"name": "   "})

        assert response.status_code == 400
        assert "Missing or invalid 'name'" in _payload(response)["message"]

    def test_invalid_json_body_returns_400(self, curator_tree):
        response = _call(curator_api.restore_archived_skill_handler, None)

        assert response.status_code == 400
        assert _payload(response)["message"] == "Invalid JSON body"

    def test_separator_name_is_rejected_before_restore(self, curator_tree):
        (curator_tree.archive / "docker").mkdir(parents=True)

        response = _call(curator_api.restore_archived_skill_handler, {"name": "../docker"})

        assert response.status_code == 400
        assert "Invalid skill name" in _payload(response)["message"]
        assert (curator_tree.archive / "docker").is_dir()

    def test_overlong_name_is_rejected(self, curator_tree):
        response = _call(curator_api.restore_archived_skill_handler, {"name": "a" * 65})

        assert response.status_code == 400
        assert "exceeds" in _payload(response)["message"]


class TestArchivedListingEndpoint:
    def test_lists_archive_directory_names(self, curator_tree):
        curator_tree.archive.mkdir(parents=True)
        (curator_tree.archive / "docker").mkdir()
        (curator_tree.archive / "docker-20260101120000").mkdir()
        (curator_tree.archive / "notes.txt").write_text("x", encoding="utf-8")

        response = _call(curator_api.list_archived_skills_handler, None)

        assert response.status_code == 200
        payload = _payload(response)
        assert payload["success"] is True
        assert payload["archived"] == ["docker", "docker-20260101120000"]
        assert payload["count"] == 2

    def test_missing_archive_root_lists_empty(self, curator_tree):
        response = _call(curator_api.list_archived_skills_handler, None)

        assert response.status_code == 200
        assert _payload(response) == {"success": True, "archived": [], "count": 0}
