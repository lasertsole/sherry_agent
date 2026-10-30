"""Unit tests for the wrong-language-labels gate.

The gate exists because the docs are authored from the Chinese spec: a UI label
(模型配置, 继续生成) can slip into an English sentence, and Simplified-Chinese
text can slip into a Japanese or Korean page, while every other gate passes (the
parity gate compares structure, the link gate resolves targets). These tests
prove it fails those leaks in PROSE, stays silent for everything that is not
prose — code spans, fenced blocks, link targets (a link may point at a
Chinese-named file) — and that it never scans the Chinese source pages.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

_GATE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_doc_language.py"


@pytest.fixture(scope="module")
def gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_doc_language", _GATE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _scan(gate: ModuleType, text: str, lang: str = "en") -> list[object]:
    return gate.scan_text(Path("README.md"), text, lang)


class TestEnglishProseFindings:
    def test_a_chinese_label_in_an_english_sentence_fails(self, gate: ModuleType) -> None:
        findings = _scan(gate, "Use System Config then the 模型配置 tab in the UI.\n")

        assert len(findings) == 1
        assert findings[0].line == 1
        assert findings[0].run == "模型配置"

    def test_each_run_on_each_line_is_reported(self, gate: ModuleType) -> None:
        findings = _scan(gate, "line one\n配置 and 预算 here\nline three\n")

        assert [(f.line, f.run) for f in findings] == [(2, "配置"), (2, "预算")]

    def test_ascii_prose_is_clean(self, gate: ModuleType) -> None:
        assert _scan(gate, "The Model Config panel edits `.models`.\n") == []


class TestJapaneseKoreanProseFindings:
    def test_simplified_only_characters_in_a_japanese_page_fail(self, gate: ModuleType) -> None:
        # 这个/们 are Simplified-only; a Japanese page never uses them.
        findings = _scan(gate, "この機能は这个です。\n", "ja")

        assert [f.run for f in findings] == ["这个"]
        assert "Simplified Chinese" in findings[0].detail
        assert "ja" in findings[0].detail

    def test_simplified_only_characters_in_a_korean_page_fail(self, gate: ModuleType) -> None:
        findings = _scan(gate, "설정 화면에서 设置을 누릅니다.\n", "ko")

        assert [f.run for f in findings] == ["设置"]

    def test_kanji_shared_with_japanese_is_clean(self, gate: ModuleType) -> None:
        # 設定 / 画面 / 実行 / 保存 are Japanese shinjitai, not Simplified-only.
        assert _scan(gate, "設定画面で実行して保存します。\n", "ja") == []

    def test_shared_simplified_forms_are_clean(self, gate: ModuleType) -> None:
        # 体/国/数/条/后/万/号 are absent from the list on purpose: they are
        # ordinary Japanese words (体, 国, 数, 条, 皇后, 万歳, 番号).
        assert _scan(gate, "体と国の数、条項、皇后、万歳、番号。\n", "ja") == []

    def test_a_label_built_only_from_shared_characters_is_not_detectable(
        self, gate: ModuleType
    ) -> None:
        # The documented limit: 模型配置 is all shared characters, so a ja/ko page
        # carrying it slips through. Mixed-language prose is what this catches.
        assert _scan(gate, "模型配置 パネル\n", "ja") == []

    def test_korean_prose_is_clean(self, gate: ModuleType) -> None:
        assert _scan(gate, "모델 설정 패널에서 실행합니다.\n", "ko") == []


class TestNonProseIsIgnored:
    @pytest.mark.parametrize("lang", ["en", "ja", "ko"])
    def test_inline_code_spans_are_code_not_prose(self, gate: ModuleType, lang: str) -> None:
        assert _scan(gate, "The path `docs/配置/README.md` is a path.\n", lang) == []

    @pytest.mark.parametrize("lang", ["en", "ja", "ko"])
    def test_fenced_blocks_are_skipped_including_their_fences(
        self, gate: ModuleType, lang: str
    ) -> None:
        text = "Prose before\n```bash\n# 配置 the model\necho 预算\n```\nProse after\n"

        assert _scan(gate, text, lang) == []

    def test_a_tilde_fence_is_skipped_too(self, gate: ModuleType) -> None:
        assert _scan(gate, "~~~\n配置\n~~~\n") == []

    @pytest.mark.parametrize("lang", ["en", "ja", "ko"])
    def test_a_link_target_may_point_at_a_chinese_named_file(
        self, gate: ModuleType, lang: str
    ) -> None:
        text = "See [the plan](../../TODO/计划_前端_白板.md) for details.\n"

        assert _scan(gate, text, lang) == []

    def test_a_chinese_link_label_still_fails(self, gate: ModuleType) -> None:
        text = "See [配置指南](guide.md) for details.\n"

        findings = _scan(gate, text)

        assert [f.run for f in findings] == ["配置指南"]

    def test_an_html_comment_is_skipped(self, gate: ModuleType) -> None:
        assert _scan(gate, "Prose\n<!-- 配置 -->\nAfter\n") == []


class TestLanguageNameAllowlist:
    def test_the_language_switcher_line_passes(self, gate: ModuleType) -> None:
        text = "English · 中文 · 한국어 · 日本語 · Wiki\n"

        assert _scan(gate, text) == []

    def test_an_english_sentence_naming_the_languages_passes(self, gate: ModuleType) -> None:
        assert _scan(gate, "The UI ships English, 简体中文, 日本語 and 한국어.\n") == []


class TestSimplifiedOnlyList:
    def test_the_shared_list_loads(self, gate: ModuleType) -> None:
        chars = gate.load_simplified_only()

        assert "时" in chars
        assert "这" in chars
        # Japanese-usable simplified forms stay out (documented in the file).
        for char in ("体", "国", "数", "装", "没", "据", "后", "万"):
            assert char not in chars

    def test_a_missing_list_raises(self, gate: ModuleType, tmp_path: Path) -> None:
        path = tmp_path / "empty.txt"
        path.write_text("# only a comment\n", encoding="utf-8")

        with pytest.raises(RuntimeError):
            gate.load_simplified_only(path)


class TestFileSelection:
    def _write(self, root: Path, name: str, body: str) -> None:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def test_each_document_is_scanned_in_its_own_language(
        self, gate: ModuleType, tmp_path: Path
    ) -> None:
        self._write(tmp_path, "README.md", "# English\nNo CJK here.\n")
        self._write(tmp_path, "README.zh.md", "# 中文\n全部中文。\n")
        self._write(tmp_path, "README.ja.md", "# 日本語\nすべて日本語です。\n")
        self._write(tmp_path, "README.ko.md", "# 한국어\n모두 한국어입니다.\n")
        self._write(tmp_path, "wiki/guide/README.md", "# Guide\nAll English.\n")
        self._write(tmp_path, "wiki/guide/README.zh.md", "# 指南\n全部中文。\n")
        self._write(tmp_path, "wiki/guide/README.ja.md", "# ガイド\nすべて日本語。\n")
        self._write(tmp_path, "wiki/guide/README.ko.md", "# 가이드\n모두 한국어.\n")
        # A NESTED English page is scanned and must be clean.
        self._write(tmp_path, "wiki/guide/deep/README.md", "# Deep\nAll English.\n")
        # AGENTS.md is not a scanned document (it quotes the labels on purpose).
        self._write(tmp_path, "AGENTS.md", "# Rules\n继续生成 means continue.\n")

        assert gate.main(["--root", str(tmp_path)]) == 0

    def test_the_chinese_source_pages_are_never_scanned(
        self, gate: ModuleType, tmp_path: Path
    ) -> None:
        self._write(tmp_path, "README.md", "# English\n")
        self._write(tmp_path, "README.zh.md", "# 中文\nThe 模型配置 panel, Model Config.\n")

        assert gate.main(["--root", str(tmp_path)]) == 0

    def test_a_nested_english_page_is_scanned(self, gate: ModuleType, tmp_path: Path) -> None:
        self._write(tmp_path, "README.md", "# English\n")
        self._write(tmp_path, "wiki/guide/deep/README.md", "# Deep\nThe 系统配置 panel.\n")

        assert gate.main(["--root", str(tmp_path)]) == 1

    def test_a_japanese_page_carrying_simplified_chinese_fails(
        self, gate: ModuleType, tmp_path: Path
    ) -> None:
        self._write(tmp_path, "README.md", "# English\n")
        self._write(tmp_path, "wiki/guide/README.ja.md", "# ガイド\nここで这个を使います。\n")

        assert gate.main(["--root", str(tmp_path)]) == 1

    def test_the_summary_counts_every_language(
        self, gate: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._write(tmp_path, "README.md", "# English\n")
        self._write(tmp_path, "README.ja.md", "# 日本語\n")
        self._write(tmp_path, "README.ko.md", "# 한국어\n")

        assert gate.main(["--root", str(tmp_path)]) == 0
        output = capsys.readouterr().out
        assert "3 file(s) scanned" in output
        assert "1 en" in output and "1 ja" in output and "1 ko" in output

    def test_the_failure_message_names_the_file_and_run(
        self, gate: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._write(tmp_path, "README.md", "# English\nThe 系统配置 panel.\n")

        assert gate.main(["--root", str(tmp_path)]) == 1
        output = capsys.readouterr().out
        assert "README.md:2" in output
        assert "系统配置" in output
        assert "VERDICT: FAIL" in output
