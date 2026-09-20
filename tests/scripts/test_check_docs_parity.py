"""Unit tests for the four-language README parity gate.

The gate has two layers: a structural signature (heading levels, fences, table
rows) and second-order semantic metrics (link-target set, per-section body
length ratio). These tests build disposable four-language README groups, prove
the semantic metrics fail on the drift they exist to catch — a dropped link, an
emptied translation section, a deleted section — and prove a correct group
passes. They also pin the exemption contract: ``ALLOWLIST`` must stay empty
(add an entry only alongside an explicit edit here), short or placeholder
reasons must be rejected, and every allowlisted hit must be re-printed in the
summary so CI logs cannot bury it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

_GATE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_docs_parity.py"

_TITLES = {"en": "Gated Group", "zh": "受控组", "ja": "ゲート対象", "ko": "게이트 그룹"}
_FILES = {"en": "README.md", "zh": "README.zh.md", "ja": "README.ja.md", "ko": "README.ko.md"}
_DESTS = {
    "en": "../guide/README.md",
    "zh": "../guide/README.zh.md",
    "ja": "../guide/README.ja.md",
    "ko": "../guide/README.ko.md",
}
_ONE_BODIES = {
    "en": (
        "The first section explains the contract with enough words that an empty translation"
        " becomes detectable, then points at the guide and the local anchor."
    ),
    "zh": "第一节用足够多的字数说明契约，使空译文可以被检测出来，然后指向指南与本地锚点。",
    "ja": "最初のセクションでは、空の翻訳を検出できるように十分な長さの言葉で契約を説明し、ガイドとローカルアンカーを指し示します。",
    "ko": "첫 번째 섹션은 빈 번역을 감지할 수 있을 만큼 충분한 길이의 문장으로 계약을 설명하고 가이드와 로컬 앵커를 가리킵니다.",
}
_TWO_BODIES = {
    "en": (
        "The second section restates the contract in plain prose so the ratio check always"
        " has a second sample to compare."
    ),
    "zh": "第二节用平实的文字重申契约，使长度比检查始终有第二个可比样本。",
    "ja": "2つ目のセクションでは、比率チェックが常に2つのサンプルを比較できるよう、契約を平易な文章で繰り返します。",
    "ko": "두 번째 섹션은 비율 검사가 항상 두 개의 샘플을 비교할 수 있도록 계약을 평이한 문장으로 다시 설명합니다.",
}


@pytest.fixture(scope="module")
def parity() -> ModuleType:
    """Load the gate from its file path (``scripts/`` is not a package)."""
    spec = importlib.util.spec_from_file_location("check_docs_parity", _GATE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves string annotations through sys.modules[cls.__module__],
    # so the module must be registered before exec_module runs.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _render(
    lang: str,
    *,
    one_body: str | None = None,
    link_dest: str | None = None,
    with_section_two: bool = True,
) -> str:
    """Render one language variant; defaults already satisfy every gate."""
    one = _ONE_BODIES[lang] if one_body is None else one_body
    dest = _DESTS[lang] if link_dest is None else link_dest
    text = (
        f"# {_TITLES[lang]}\n\n"
        "Intro paragraph.\n\n"
        "## Section One\n\n"
        f"{one}\n\n"
        f"See the [guide]({dest}) and the [local anchor](#section-two).\n"
    )
    if with_section_two:
        text += f"\n## Section Two\n\n{_TWO_BODIES[lang]}\n"
    return text


def _empty_section_one(lang: str) -> str:
    """A variant whose first section keeps its heading but lost its whole body."""
    return (
        f"# {_TITLES[lang]}\n\n"
        "Intro paragraph.\n\n"
        "## Section One\n\n"
        "## Section Two\n\n"
        f"{_TWO_BODIES[lang]}\n"
    )


def _write_group(root: Path, overrides: dict[str, str] | None = None) -> None:
    """Write all four README variants under ``root``."""
    overrides = overrides or {}
    for lang, filename in _FILES.items():
        (root / filename).write_text(overrides.get(lang, _render(lang)), encoding="utf-8")


def test_valid_group_passes_and_localized_links_normalize(
    parity: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A correct group passes; README.zh/ja/ko.md links normalize to README.md."""
    _write_group(tmp_path)

    assert parity.main(tmp_path) == 0
    out = capsys.readouterr().out
    assert "VERDICT: PASS" in out
    assert "links=" in out


def test_missing_translated_section_fails(
    parity: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Given a translation that deleted a whole section, the gate fails."""
    _write_group(tmp_path, {"ko": _render("ko", with_section_two=False)})

    assert parity.main(tmp_path) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "headings" in out
    assert "VERDICT: FAIL" in out


def test_missing_link_fails(
    parity: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Given a translation that dropped a link, the gate names the exact target."""
    _write_group(tmp_path, {"zh": _render("zh").replace("[guide](../guide/README.zh.md)", "guide")})

    assert parity.main(tmp_path) == 1
    out = capsys.readouterr().out
    assert "README.zh.md: links missing: ['../guide/README.md']" in out
    assert "VERDICT: FAIL" in out


def test_empty_translation_section_fails(
    parity: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Given a section whose translation body is empty, the ratio gate fails."""
    _write_group(tmp_path, {"ja": _empty_section_one("ja")})

    assert parity.main(tmp_path) == 1
    out = capsys.readouterr().out
    assert "body ratio" in out
    assert "README.ja.md" in out
    assert "VERDICT: FAIL" in out


def test_allowlist_is_empty(parity: ModuleType) -> None:
    """The exemption list is pinned empty: adding one requires editing this test."""
    assert parity.ALLOWLIST == {}


def test_short_allowlist_reason_fails(
    parity: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An exemption reason must be substantive, not a token."""
    _write_group(tmp_path)
    monkeypatch.setattr(parity, "ALLOWLIST", {".": "skip"})

    assert parity.main(tmp_path) == 1
    out = capsys.readouterr().out
    assert "allowlist contract violated" in out
    assert "below the 40-char minimum" in out


def test_placeholder_allowlist_reason_fails(
    parity: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A long reason that merely opens with placeholder text is still rejected."""
    _write_group(tmp_path)
    monkeypatch.setattr(
        parity,
        "ALLOWLIST",
        {".": "TODO: replace this placeholder exemption once upstream documentation stabilizes"},
    )

    assert parity.main(tmp_path) == 1
    out = capsys.readouterr().out
    assert "placeholder text" in out


def test_stale_allowlist_entry_fails(
    parity: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An exemption that matches no discovered group is dead weight and fails."""
    _write_group(tmp_path)
    monkeypatch.setattr(
        parity, "ALLOWLIST", {"ghost/group": "deliberate language-specific divergence, reviewed"}
    )

    assert parity.main(tmp_path) == 1
    out = capsys.readouterr().out
    assert "stale exemption" in out


def test_allowlisted_hit_is_skipped_and_reprinted(
    parity: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A valid exemption skips the group and is echoed, with its reason, in the summary."""
    _write_group(tmp_path, {"ja": _empty_section_one("ja")})
    reason = "Japanese keeps this section as a stub because upstream data is authored in en only"
    monkeypatch.setattr(parity, "ALLOWLIST", {".": reason})

    assert parity.main(tmp_path) == 0
    out = capsys.readouterr().out
    assert f"SKIP . (allowlisted): {reason}" in out
    assert "allowlisted: 1" in out
    assert f"  - .: {reason}" in out
    assert "VERDICT: PASS" in out
