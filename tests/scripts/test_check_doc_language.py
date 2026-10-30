"""Unit tests for the no-CJK-in-English-docs gate.

The gate exists because the docs are authored from the Chinese spec: a UI label
(模型配置, 继续生成) can slip into an English sentence while every other gate
passes (the parity gate compares structure, the link gate resolves targets).
These tests prove it fails that leak in PROSE and stays silent for everything
that is not prose — code spans, fenced blocks, link targets (a link may point
at a Chinese-named file) — and that it never scans the localized documents.
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


def _scan(gate: ModuleType, text: str) -> list[object]:
    return gate.scan_text(Path("README.md"), text)


class TestProseFindings:
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


class TestNonProseIsIgnored:
    def test_inline_code_spans_are_code_not_prose(self, gate: ModuleType) -> None:
        assert _scan(gate, "The path `docs/配置/README.md` is a path.\n") == []

    def test_fenced_blocks_are_skipped_including_their_fences(self, gate: ModuleType) -> None:
        text = "Prose before\n```bash\n# 配置 the model\necho 预算\n```\nProse after\n"

        assert _scan(gate, text) == []

    def test_a_tilde_fence_is_skipped_too(self, gate: ModuleType) -> None:
        assert _scan(gate, "~~~\n配置\n~~~\n") == []

    def test_a_link_target_may_point_at_a_chinese_named_file(self, gate: ModuleType) -> None:
        text = "See [the plan](../../TODO/计划_前端_白板.md) for details.\n"

        assert _scan(gate, text) == []

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


class TestFileSelection:
    def _write(self, root: Path, name: str, body: str) -> None:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def test_only_the_english_documents_are_scanned(self, gate: ModuleType, tmp_path: Path) -> None:
        self._write(tmp_path, "README.md", "# English\nNo CJK here.\n")
        # Every localized sibling is Chinese by design: never scanned.
        for suffix in ("zh", "ja", "ko"):
            self._write(tmp_path, f"README.{suffix}.md", "# 配置\n全部中文。\n")
        self._write(tmp_path, "wiki/guide/README.md", "# Guide\nAll English.\n")
        self._write(tmp_path, "wiki/guide/README.zh.md", "# 指南\n全部中文。\n")
        # A NESTED English page is scanned and must be clean.
        self._write(tmp_path, "wiki/guide/deep/README.md", "# Deep\nAll English.\n")
        # AGENTS.md is not a scanned document (it quotes the labels on purpose).
        self._write(tmp_path, "AGENTS.md", "# Rules\n继续生成 means continue.\n")

        assert gate.main(["--root", str(tmp_path)]) == 0

    def test_a_nested_english_page_is_scanned(self, gate: ModuleType, tmp_path: Path) -> None:
        self._write(tmp_path, "README.md", "# English\n")
        self._write(tmp_path, "wiki/guide/deep/README.md", "# Deep\nThe 系统配置 panel.\n")

        assert gate.main(["--root", str(tmp_path)]) == 1

    def test_the_failure_message_names_the_file_and_run(
        self, gate: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._write(tmp_path, "README.md", "# English\nThe 系统配置 panel.\n")

        assert gate.main(["--root", str(tmp_path)]) == 1
        output = capsys.readouterr().out
        assert "README.md:2" in output
        assert "系统配置" in output
        assert "VERDICT: FAIL" in output
