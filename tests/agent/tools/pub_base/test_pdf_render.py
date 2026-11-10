"""PDF page rendering: PyMuPDF path, pdftoppm fallback, failure mapping."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent.tools.pub_base.pdf_render as pdf_render
from agent.tools.pub_base.pdf_render import PdfRenderUnavailable, render_pdf_pages

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]


class _FakePixmap:
    width = 100
    height = 200

    def tobytes(self, fmt: str) -> bytes:
        assert fmt == "jpeg"
        return b"jpeg-page"


def _no_pymupdf(*_args):
    raise ImportError("fitz is not installed")


class _FakeDocument:
    page_count = 3

    def __init__(self) -> None:
        self.loaded: list[int] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def load_page(self, index: int):
        self.loaded.append(index)
        return SimpleNamespace(get_pixmap=lambda **kwargs: _FakePixmap())


def test_pymupdf_renders_the_requested_range(tmp_path, monkeypatch):
    document = _FakeDocument()
    monkeypatch.setitem(sys.modules, "fitz", SimpleNamespace(open=lambda _path: document))

    pages = render_pdf_pages(tmp_path / "in.pdf", first_page=2, last_page=3, dpi=100)

    assert [page.number for page in pages] == [2, 3]
    assert document.loaded == [1, 2]
    assert pages[0].data == b"jpeg-page"
    assert (pages[0].width, pages[0].height) == (100, 200)


def test_pymupdf_failure_is_reported_as_unavailable(tmp_path, monkeypatch):
    def _open(_path):
        raise RuntimeError("password required")

    monkeypatch.setitem(sys.modules, "fitz", SimpleNamespace(open=_open))

    with pytest.raises(PdfRenderUnavailable, match="password required"):
        render_pdf_pages(tmp_path / "locked.pdf")


def test_pdftoppm_fallback_collects_the_rendered_files(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_render, "_render_with_pymupdf", _no_pymupdf)
    monkeypatch.setattr(pdf_render.shutil, "which", lambda _name: "/usr/bin/pdftoppm")

    def _run(argv, **_kwargs):
        prefix = Path(argv[-1])
        prefix.with_name("page-1.jpg").write_bytes(b"one")
        prefix.with_name("page-2.jpg").write_bytes(b"two")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(pdf_render.subprocess, "run", _run)

    pages = render_pdf_pages(tmp_path / "in.pdf", first_page=1, last_page=2)

    assert [page.data for page in pages] == [b"one", b"two"]


def test_missing_renderer_reports_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_render, "_render_with_pymupdf", _no_pymupdf)
    monkeypatch.setattr(pdf_render.shutil, "which", lambda _name: None)

    with pytest.raises(PdfRenderUnavailable, match="neither is installed"):
        render_pdf_pages(tmp_path / "in.pdf")


def test_pdftoppm_timeout_reports_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_render, "_render_with_pymupdf", _no_pymupdf)
    monkeypatch.setattr(pdf_render.shutil, "which", lambda _name: "/usr/bin/pdftoppm")

    def _run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 1))

    monkeypatch.setattr(pdf_render.subprocess, "run", _run)

    with pytest.raises(PdfRenderUnavailable, match="timed out"):
        render_pdf_pages(tmp_path / "in.pdf", timeout_s=5)
