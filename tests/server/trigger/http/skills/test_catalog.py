"""Path-traversal guard for the skill-catalog read endpoint.

``GET /skills/*skill_path`` used to join the URL wildcard onto ``ROOT_DIR``
raw, so ``GET /skills/../../.env`` read arbitrary files. The handler now
resolves and confines to ``ROOT_DIR``; traversal attempts must answer with
the same 404 as a missing file so the endpoint cannot probe the filesystem.
"""

import asyncio
import json
from pathlib import Path

import pytest

from server.trigger.http.skills import catalog

pytestmark = [pytest.mark.unit]


class _FakeRequest:
    pass


def _make_tree(root: Path) -> None:
    skill_dir = root / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: demo skill\n---\n\nbody\n", encoding="utf-8"
    )
    (root / ".env").write_text("MAIN_LLM_API_KEY=secret\n", encoding="utf-8")


def _call(skill_path: str, root: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[int, dict]:
    """Invoke the handler and normalize Robyn's wrapping into ``(status, body)``.

    Both handler returns — the plain success dict and the
    ``(body, headers, 404)`` tuple — come back as a Robyn ``Response``: the
    body is JSON text in ``.description`` and the status in ``.status_code``.
    """
    monkeypatch.setattr("config.ROOT_DIR", root)
    response = asyncio.run(catalog.read_skill_handler(_FakeRequest(), {"skill_path": skill_path}))
    return int(response.status_code), json.loads(response.description)


def test_legit_skill_file_is_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _make_tree(tmp_path)

    status, payload = _call("skills/demo/SKILL.md", tmp_path, monkeypatch)

    assert status == 200
    assert payload["name"] == "demo"
    assert "body" in payload["content"]


def test_traversal_to_env_returns_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _make_tree(tmp_path)

    status, payload = _call("../../.env", tmp_path, monkeypatch)

    assert status == 404
    assert payload == {"error": "Skill file not found"}


def test_inner_traversal_resolving_inside_root_is_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The guard confines after resolving:
    a ``..`` spelling that resolves back inside ROOT_DIR behaves exactly like
    the equivalent plain path — only escapes above ROOT_DIR get the 404.
    """
    _make_tree(tmp_path)
    other = tmp_path / "skills" / "other"
    other.mkdir()
    (other / "notes.md").write_text("in-root payload", encoding="utf-8")

    status, payload = _call("skills/demo/../../skills/other/notes.md", tmp_path, monkeypatch)

    assert status == 200
    assert payload["content"] == "in-root payload"


def test_absolute_escape_returns_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _make_tree(tmp_path)

    outside = tmp_path.parent / "outside.md"
    outside.write_text("stolen", encoding="utf-8")

    status, payload = _call(f"../{outside.name}", tmp_path, monkeypatch)

    assert status == 404
    assert payload == {"error": "Skill file not found"}
