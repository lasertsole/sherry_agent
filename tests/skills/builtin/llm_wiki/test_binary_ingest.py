"""Binary/multimodal llm_wiki ingest (office stdlib readers + dispatch)."""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").exists())
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skills.builtin.llm_wiki.scripts import graph, ingest  # noqa: E402

pytestmark = [pytest.mark.unit]

SEMANTICS_JSON = (
    '{"entities": [{"name": "Apple", "type": "org"}, '
    '{"name": "Galaxy", "type": "product"}], '
    '"relations": [{"src": "Apple", "dst": "Galaxy", "type": "makes", '
    '"confidence": "EXTRACTED", "evidence": "Apple makes the Galaxy."}]}'
)


def _docx_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>Hello</w:t></w:r><w:r><w:t> world</w:t></w:r></w:p>"
            "<w:p><w:r><w:t>Second paragraph</w:t></w:r></w:p></w:body></w:document>",
        )
    return buffer.getvalue()


def _xlsx_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "xl/sharedStrings.xml",
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            "<si><t>Alpha</t></si><si><t>Beta</t></si></sst>",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetData><row><c t="s"><v>0</v></c><c><v>42</v></c></row>'
            '<row><c t="s"><v>1</v></c></row></sheetData></worksheet>',
        )
    return buffer.getvalue()


def test_docx_reader_extracts_paragraph_text():
    assert ingest._extract_docx(_docx_bytes()) == "Hello world\nSecond paragraph"


def test_xlsx_reader_resolves_shared_strings_and_numbers():
    assert ingest._extract_xlsx(_xlsx_bytes()) == "Alpha\t42\nBeta"


def test_save_binary_source_writes_markdown_and_refreshes_the_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    async def fake_semantics(text: str) -> str:
        assert "Apple makes the Galaxy" in text
        return SEMANTICS_JSON

    monkeypatch.setattr(
        ingest, "_extract_text", lambda data, kind: ("Apple makes the Galaxy.", None)
    )
    monkeypatch.setattr(ingest, "_semantics_async", fake_semantics)

    result = ingest.save_binary_source(
        b"binary",
        "docx",
        source_url="https://example.test/a",
        filename="paper.docx",
        wiki_root=tmp_path,
    )

    assert result["success"] is True
    assert result["category"] == "articles"
    written = Path(result["file_path"])
    assert written == tmp_path / "raw" / "articles" / "paper.md"
    text = written.read_text(encoding="utf-8")
    assert "content_type: docx" in text
    assert "extracted_relations" in text
    assert "Apple makes the Galaxy." in text
    assert (tmp_path / ".wiki_graph.db").is_file()
    stats = graph.graph_stats(wiki_root=tmp_path)
    assert stats["nodes"] >= 2
    assert stats["edges"] >= 1


def test_save_binary_source_routes_images_to_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    async def fake_semantics(text: str) -> str:
        return '{"entities": [], "relations": []}'

    monkeypatch.setattr(ingest, "_extract_image", lambda data, suffix: "A photo of an apple.")
    monkeypatch.setattr(ingest, "_semantics_async", fake_semantics)

    result = ingest.save_binary_source(b"\x89PNG", "png", filename="photo.png", wiki_root=tmp_path)

    assert result["success"] is True
    assert result["category"] == "assets"
    assert (tmp_path / "raw" / "assets" / "photo.md").is_file()


def test_save_binary_source_fails_cleanly(tmp_path: Path):
    assert ingest.save_binary_source(b"", "pdf", wiki_root=tmp_path)["success"] is False
    unsupported = ingest.save_binary_source(b"x", "tiff", wiki_root=tmp_path)
    assert unsupported["success"] is False
    assert "unsupported content_type" in unsupported["error"]


def test_semantics_failure_is_fail_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def boom(text: str) -> str:
        raise RuntimeError("provider down")

    monkeypatch.setattr(ingest, "_extract_text", lambda data, kind: ("text", None))
    monkeypatch.setattr(ingest, "_run_async", boom)

    result = ingest.save_binary_source(b"x", "txt", filename="note.txt", wiki_root=tmp_path)
    assert result["success"] is True
    assert result["entities"] == []
    assert result["relations"] == []
