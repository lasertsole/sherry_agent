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


# --- Blind spot B: heading reorder under an unchanged level sequence ---------

_MARKER_BODY = {
    "en": (
        "This section carries enough English prose for the body-length ratio check to compare a"
        " real sample against its translation without tripping the near-empty rule."
    ),
    "zh": "本节包含足够长度的中文正文，使长度比检查能够将真实样本与其译文进行比较，而不会触发近空规则。",
    "ja": "このセクションには、長さ比チェックが実サンプルと翻訳を比較できるだけの十分な長さの日本語本文が含まれています。",
    "ko": "이 섹션에는 길이 비율 검사가 실제 샘플과 번역을 비교할 수 있을 만큼 충분히 긴 한국어 본문이 포함되어 있습니다.",
}


def _render_marker_sections(lang: str, headings: list[str]) -> str:
    """Four-language fixture whose headings are the supplied Markdown lines."""
    text = f"# {_TITLES[lang]}\n\nIntro paragraph.\n"
    for heading in headings:
        text += f"\n{heading}\n\n{_MARKER_BODY[lang]}\n"
    return text


def test_reordered_marked_headings_fail_with_first_divergence(
    parity: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Given swapped marker-bearing headings with identical levels, the gate fails."""
    _write_group(
        tmp_path,
        {
            "en": _render_marker_sections("en", ["## `alpha`", "## `beta`", "## `gamma`"]),
            "zh": _render_marker_sections("zh", ["## `beta`", "## `alpha`", "## `gamma`"]),
            "ja": _render_marker_sections("ja", ["## `alpha`", "## `beta`", "## `gamma`"]),
            "ko": _render_marker_sections("ko", ["## `alpha`", "## `beta`", "## `gamma`"]),
        },
    )

    assert parity.main(tmp_path) == 1
    out = capsys.readouterr().out
    assert "heading markers diverge at position 0" in out
    assert "reference code:alpha vs translation code:beta" in out
    assert "VERDICT: FAIL" in out


def test_consistent_marker_order_including_markerless_and_link_headings_passes(
    parity: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A consistent order — marker-less and link-target headings included — passes."""
    _write_group(
        tmp_path,
        {
            lang: _render_marker_sections(
                lang,
                ["## `alpha`", "## Overview", f"## [Guide]({_DESTS[lang]})", "## `gamma`"],
            )
            for lang in _FILES
        },
    )

    assert parity.main(tmp_path) == 0
    out = capsys.readouterr().out
    assert "VERDICT: PASS" in out
    assert "section pairing:" in out
    assert "pairing note:" in out


# --- Blind spot A: long but stale translation (missing invariant tokens) -----

_TOKEN_EN = (
    "The persistence layer reads `TOOL_CALL_TIMEOUT_MINUTES` and hashes paths with `sha256[:16]`"
    " before writing them under `agent/tools/todolist/registry/`, which is why the section is"
    " long enough to defeat a near-empty check on its own."
)
_TOKEN_KEPT = {
    "zh": (
        "持久化层读取 `TOOL_CALL_TIMEOUT_MINUTES`，并用 `sha256[:16]` 对路径做哈希，再写入"
        " `agent/tools/todolist/registry/` 目录，因此这一段正文足够长，不会触发近空检查。"
    ),
    "ja": (
        "永続化レイヤーは `TOOL_CALL_TIMEOUT_MINUTES` を読み、`sha256[:16]` でパスをハッシュして"
        " `agent/tools/todolist/registry/` の下に書き込みます。この本文は近空チェックを回避できる"
        " 十分な長さです。"
    ),
    "ko": (
        "영속화 계층은 `TOOL_CALL_TIMEOUT_MINUTES`를 읽고 `sha256[:16]`로 경로를 해시한 뒤"
        " `agent/tools/todolist/registry/` 아래에 기록합니다. 이 본문은 근접 빈 섹션 검사를 피할"
        " 만큼 충분히 깁니다."
    ),
}
_TOKEN_DROPPED = (
    "这段译文在讲同一件事，但故意一个代码标识符或路径都没有保留下来，同时正文长度仍然足够长，"
    "因此旧的近空长度比指标完全看不出这里有内容缺失。"
)


def _render_token_section(lang: str, body: str) -> str:
    return f"# {_TITLES[lang]}\n\nIntro paragraph.\n\n## Section One\n\n{body}\n"


def test_long_but_stale_translation_missing_tokens_fails(
    parity: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Given a long translation that dropped all of EN's code spans/paths, the gate fails."""
    overrides = {
        "en": _render_token_section("en", _TOKEN_EN),
        "zh": _render_token_section("zh", _TOKEN_DROPPED),
        "ja": _render_token_section("ja", _TOKEN_KEPT["ja"]),
        "ko": _render_token_section("ko", _TOKEN_KEPT["ko"]),
    }
    _write_group(tmp_path, overrides)

    assert parity.main(tmp_path) == 1
    out = capsys.readouterr().out
    assert "missing invariant tokens" in out
    assert "`TOOL_CALL_TIMEOUT_MINUTES`" in out
    assert "`sha256[:16]`" in out
    assert "`agent/tools/todolist/registry/`" in out
    assert "VERDICT: FAIL" in out


def test_long_translation_retaining_tokens_passes(
    parity: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A long translation that keeps the reference tokens passes the invariant-token gate."""
    _write_group(
        tmp_path,
        {
            "en": _render_token_section("en", _TOKEN_EN),
            **{lang: _render_token_section(lang, _TOKEN_KEPT[lang]) for lang in ("zh", "ja", "ko")},
        },
    )

    assert parity.main(tmp_path) == 0
    out = capsys.readouterr().out
    assert "VERDICT: PASS" in out
