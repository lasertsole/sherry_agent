"""Every shipped built-in skill carries the same trigger/exclusion signals.

The nudge/curator path already requires ``when_to_use``; the shipped built-ins
must not be the exception, or the model has to infer triggers from the
description alone. This also pins the code-wiki scan guard: its own output tree
must never be scanned (a rerun would document the previous wiki and nest it).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pub.func.estimate_tokens import estimate_text_tokens
from skills.loader import parse_frontmatter

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

BUILTIN = Path(__file__).resolve().parents[2] / "skills" / "builtin"
WHEN_TO_USE_MAX_TOKENS = 50
WHEN_NOT_TO_USE_MAX_TOKENS = 50


def _skill_files() -> list[Path]:
    return sorted(BUILTIN.rglob("SKILL.md"))


def test_every_builtin_skill_has_trigger_and_exclusion_fields() -> None:
    files = _skill_files()
    assert files, "no built-in skills found"

    missing: list[str] = []
    oversized: list[str] = []
    for path in files:
        meta = parse_frontmatter(path.read_text(encoding="utf-8"))
        for field, limit in (
            ("when_to_use", WHEN_TO_USE_MAX_TOKENS),
            ("when_not_to_use", WHEN_NOT_TO_USE_MAX_TOKENS),
        ):
            value = str(meta.get(field, "") or "").strip()
            if not value:
                missing.append(f"{path}: {field}")
            elif estimate_text_tokens(value) > limit:
                oversized.append(f"{path}: {field}")
        assert meta.get("name"), f"{path}: missing name"
        assert meta.get("description"), f"{path}: missing description"

    assert missing == [], f"built-in skills missing trigger fields: {missing}"
    assert oversized == [], (
        f"built-in trigger fields over {WHEN_TO_USE_MAX_TOKENS} tokens: {oversized}"
    )


def test_code_wiki_prunes_its_output_tree_from_every_scan() -> None:
    text = (BUILTIN / "code_wiki" / "SKILL.md").read_text(encoding="utf-8")

    assert "src/data/wiki" in text
    assert "src/data/wikis" not in text
    assert "-not -path './src/data/wiki*'" in text
    assert "-not -path './wiki*'" in text
    assert "-not -path './graphify-out*'" in text
    assert "Scanning the output tree" in text
