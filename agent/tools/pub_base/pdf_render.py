"""PDF page rendering for the non-native-PDF fallback.

Native PDF blocks (Anthropic ``document`` / OpenAI-compatible ``file``) are the
primary path and need no renderer. A model that supports images but not PDFs
gets pages here: PyMuPDF when installed, otherwise the system ``pdftoppm``.
Neither is a hard dependency — when both are missing the caller reports
``pdf_render_unavailable`` instead of silently dropping the document.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

__all__ = ["PdfPage", "PdfRenderUnavailable", "render_pdf_pages"]


class PdfRenderUnavailableError(RuntimeError):
    """No PDF renderer is installed (PyMuPDF or poppler's pdftoppm)."""


#: Readable alias; N818 prefers an ``Error`` suffix on exception classes.
PdfRenderUnavailable = PdfRenderUnavailableError


@dataclass(frozen=True)
class PdfPage:
    number: int
    data: bytes
    width: int
    height: int


def _render_with_pymupdf(
    path: Path, first_page: int, last_page: int | None, dpi: int
) -> list[PdfPage]:
    import fitz  # type: ignore[import-not-found]  # optional dependency

    try:
        pages: list[PdfPage] = []
        with fitz.open(str(path)) as document:
            total = document.page_count
            start = max(1, first_page)
            end = min(total, last_page) if last_page is not None else total
            for number in range(start, end + 1):
                page = document.load_page(number - 1)
                pixmap = page.get_pixmap(dpi=dpi, alpha=False)
                pages.append(
                    PdfPage(
                        number=number,
                        data=pixmap.tobytes("jpeg"),
                        width=pixmap.width,
                        height=pixmap.height,
                    )
                )
        return pages
    except Exception as exc:  # noqa: BLE001 — a corrupt/encrypted PDF is a render failure
        raise PdfRenderUnavailable(f"PyMuPDF failed: {exc}") from exc


def _render_with_pdftoppm(
    path: Path, first_page: int, last_page: int | None, dpi: int, timeout_s: float
) -> list[PdfPage]:
    binary = shutil.which("pdftoppm")
    if not binary:
        raise PdfRenderUnavailable(
            "PDF rendering needs PyMuPDF or poppler's pdftoppm; neither is installed"
        )
    with tempfile.TemporaryDirectory(prefix="sherry-pdf-") as tmpdir:
        prefix = Path(tmpdir) / "page"
        argv = [binary, "-jpeg", "-r", str(dpi), "-f", str(max(1, first_page))]
        if last_page is not None:
            argv += ["-l", str(max(1, last_page))]
        argv += [str(path), str(prefix)]
        try:
            subprocess.run(
                argv,
                check=True,
                capture_output=True,
                timeout=timeout_s,
                text=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise PdfRenderUnavailable(f"PDF rendering timed out after {timeout_s}s") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or b"").decode("utf-8", "replace")[:300]
            raise PdfRenderUnavailable(f"pdftoppm failed: {detail}") from exc
        files = sorted(Path(tmpdir).glob("page-*.jpg"))
        pages = [
            PdfPage(number=index, data=file.read_bytes(), width=0, height=0)
            for index, file in enumerate(files, start=max(1, first_page))
        ]
    return pages


def render_pdf_pages(
    path: str | Path,
    *,
    first_page: int = 1,
    last_page: int | None = None,
    dpi: int = 100,
    timeout_s: float = 120.0,
) -> list[PdfPage]:
    """Render the requested page range to JPEG bytes.

    Raises :class:`PdfRenderUnavailable` when neither backend exists (or the
    external renderer fails/times out).
    """
    source = Path(path)
    try:
        return _render_with_pymupdf(source, first_page, last_page, dpi)
    except ImportError:
        return _render_with_pdftoppm(source, first_page, last_page, dpi, timeout_s)
