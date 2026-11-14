"""Skill creator validator: symlinked entries are refused (audit M26).

``package_skill.py`` already rejects a symlink; the validator used to skip the
entry instead, so a skill could validate cleanly and still carry a link that
points outside the skill tree.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]

_SCRIPT = (
    Path(__file__).resolve().parents[5]
    / "skills/builtin/core/skill_creator/scripts/quick_validate.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("quick_validate_for_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _skill(root: Path, name: str = "demo-skill") -> Path:
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        "description: A small demonstration skill for validator tests.\n"
        "---\n\n# Demo\n",
        encoding="utf-8",
    )
    return skill


def test_a_plain_skill_validates(tmp_path: Path) -> None:
    validate = _load()
    skill = _skill(tmp_path)

    valid, message = validate.validate_skill(skill)

    assert valid is True, message


def test_a_symlinked_child_is_refused(tmp_path: Path) -> None:
    validate = _load()
    skill = _skill(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    (skill / "leak.txt").symlink_to(outside)

    valid, message = validate.validate_skill(skill)

    assert valid is False
    assert "symlink" in message.lower()
    assert "leak.txt" in message


def test_a_symlinked_skill_md_is_refused(tmp_path: Path) -> None:
    validate = _load()
    skill = _skill(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text((skill / "SKILL.md").read_text(encoding="utf-8"), encoding="utf-8")
    (skill / "SKILL.md").unlink()
    (skill / "SKILL.md").symlink_to(outside)

    valid, message = validate.validate_skill(skill)

    assert valid is False
    assert "symlink" in message.lower()
