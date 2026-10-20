"""
llm_wiki ingest: raw source ingestion
"""

import sys
import hashlib
import importlib.util
from pathlib import Path
from datetime import date
from loguru import logger

# Dynamically load the core module
_scripts_dir = Path(__file__).resolve().parent
_core_spec = importlib.util.spec_from_file_location("wiki_core", str(_scripts_dir / "core.py"))
_core = importlib.util.module_from_spec(_core_spec)
sys.modules.setdefault("wiki_core", _core)
_core_spec.loader.exec_module(_core)
get_wiki_path = _core.get_wiki_path


def save_source(content: str, category: str = "articles", filename: str = None) -> dict:
    """
    Save a raw source document into the raw/ directory.

    Args:
        content: Source content (text)
        category: Category, one of articles / papers / transcripts / assets
        filename: File name (without path); auto-generated if not specified

    Returns:
        dict: Save result, containing file_path, sha256, success
    """
    wiki_root = get_wiki_path()
    raw_dir = wiki_root / "raw" / category
    raw_dir.mkdir(parents=True, exist_ok=True)

    if not filename:
        # Generate the filename from the first 20 chars of content + date
        prefix = content[:20].strip().replace(" ", "-").replace("\n", "")
        today = date.today().isoformat()
        filename = f"{prefix}-{today}.md"

    file_path = raw_dir / filename

    # Compute sha256
    sha256_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    # Write the file (with frontmatter)
    frontmatter = f"""---
source_url: manual
ingested: {date.today().isoformat()}
sha256: {sha256_hash}
---

"""
    full_content = frontmatter + content

    try:
        file_path.write_text(full_content, encoding="utf-8")
        logger.debug(f"Source saved: {file_path}")
        return {"file_path": str(file_path), "sha256": sha256_hash, "success": True}
    except Exception as e:
        logger.exception(f"Failed to save source: {e}")
        return {"file_path": str(file_path), "error": str(e), "success": False}


# ============================================================
# Binary / multimodal ingest (SHERRY-PATCH, additive to save_source)
# ============================================================

_CONTENT_SUFFIX = {
    "pdf": ".pdf",
    "image": ".png",
    "png": ".png",
    "jpg": ".jpg",
    "jpeg": ".jpg",
    "webp": ".webp",
    "gif": ".gif",
    "audio": ".wav",
    "mp3": ".mp3",
    "wav": ".wav",
    "m4a": ".m4a",
    "video": ".mp4",
    "mp4": ".mp4",
    "mov": ".mov",
    "docx": ".docx",
    "xlsx": ".xlsx",
    "markdown": ".md",
    "md": ".md",
    "text": ".txt",
    "txt": ".txt",
}

_CATEGORY_BY_TYPE = {
    "pdf": "papers",
    "image": "assets",
    "png": "assets",
    "jpg": "assets",
    "jpeg": "assets",
    "webp": "assets",
    "gif": "assets",
    "audio": "transcripts",
    "mp3": "transcripts",
    "wav": "transcripts",
    "m4a": "transcripts",
    "video": "transcripts",
    "mp4": "transcripts",
    "mov": "transcripts",
}


def _write_temp(data: bytes, suffix: str) -> Path:
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        return Path(tmp.name)


def _extract_pdf(data: bytes) -> str:
    path = _write_temp(data, ".pdf")
    try:
        try:
            from pypdf import PdfReader  # pyright: ignore[reportMissingImports]

            reader = PdfReader(str(path))
            return "\n".join(page.extract_text() or "" for page in reader.pages).strip()
        except ImportError:
            pass
        import shutil
        import subprocess

        if shutil.which("pdftotext"):
            result = subprocess.run(  # noqa: S603 - fixed binary, list argv
                ["pdftotext", str(path), "-"],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        from skills.builtin.core.multimodal_rag.scripts.graph_rag.core import (
            ensure_mineru_models,
        )
        from skills.builtin.core.multimodal_rag.scripts.graph_rag.vendored_raganything.parser import (
            MineruParser,
        )

        ensure_mineru_models()
        blocks = MineruParser().parse_pdf(str(path), backend="pipeline")
        return "\n".join(
            str(block.get("text", "")).strip()
            for block in blocks
            if isinstance(block, dict) and block.get("text")
        ).strip()
    finally:
        path.unlink(missing_ok=True)


def _extract_image(data: bytes, suffix: str) -> str:
    path = _write_temp(data, suffix)
    try:
        from skills.builtin.core.image_to_text.scripts import itt

        return itt(
            str(path),
            "Describe the image content in as much detail as possible, including any "
            "text, entities, and relationships visible.",
        )
    finally:
        path.unlink(missing_ok=True)


def _extract_audio(data: bytes, suffix: str) -> str:
    path = _write_temp(data, suffix)
    try:
        from skills.builtin.core.speech_to_text.scripts import stt

        return stt(str(path))
    finally:
        path.unlink(missing_ok=True)


def _extract_video(data: bytes, suffix: str) -> str:
    path = _write_temp(data, suffix)
    try:
        from skills.builtin.core.video_text_to_text.scripts import vtt

        return vtt(str(path), "Summarize what happens in the video.")
    finally:
        path.unlink(missing_ok=True)


def _extract_docx(data: bytes) -> str:
    """DOCX is a ZIP of XML; read word/document.xml without python-docx."""
    import io
    import zipfile
    from xml.etree import ElementTree

    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        with archive.open("word/document.xml") as handle:
            root = ElementTree.fromstring(handle.read())
    paragraphs: list[str] = []
    for paragraph in root.iter(f"{namespace}p"):
        text = "".join(node.text or "" for node in paragraph.iter(f"{namespace}t"))
        if text.strip():
            paragraphs.append(text.strip())
    return "\n".join(paragraphs)


def _extract_xlsx(data: bytes) -> str:
    """XLSX is a ZIP of XML; read shared strings + sheet cells without openpyxl."""
    import io
    import zipfile
    from xml.etree import ElementTree

    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.iter(f"{ns}si"):
                shared.append("".join(node.text or "" for node in item.iter(f"{ns}t")))
        lines: list[str] = []
        sheets = sorted(name for name in archive.namelist() if name.startswith("xl/worksheets/"))
        for sheet in sheets:
            root = ElementTree.fromstring(archive.read(sheet))
            for row in root.iter(f"{ns}row"):
                cells: list[str] = []
                for cell in row.iter(f"{ns}c"):
                    value_node = cell.find(f"{ns}v")
                    if value_node is None or value_node.text is None:
                        continue
                    value = value_node.text
                    if cell.get("t") == "s":
                        index = int(value)
                        value = shared[index] if 0 <= index < len(shared) else value
                    cells.append(value)
                if cells:
                    lines.append("\t".join(cells))
    return "\n".join(lines)


def _extract_text(content_bytes: bytes, content_type: str) -> tuple[str, str | None]:
    """Dispatch one binary/text payload to its extractor."""
    kind = content_type.lower().lstrip(".")
    suffix = _CONTENT_SUFFIX.get(kind)
    if suffix is None:
        return "", f"unsupported content_type: {content_type!r}"
    if kind == "pdf":
        text = _extract_pdf(content_bytes)
    elif kind in {"image", "png", "jpg", "jpeg", "webp", "gif"}:
        text = _extract_image(content_bytes, suffix)
    elif kind in {"audio", "mp3", "wav", "m4a"}:
        text = _extract_audio(content_bytes, suffix)
    elif kind in {"video", "mp4", "mov"}:
        text = _extract_video(content_bytes, suffix)
    elif kind == "docx":
        text = _extract_docx(content_bytes)
    elif kind == "xlsx":
        text = _extract_xlsx(content_bytes)
    else:
        text = content_bytes.decode("utf-8", errors="replace")
    text = (text or "").strip()
    if text.startswith(("[Error]", "[error]")) or not text:
        return "", text or "extractor returned no text"
    return text, None


def _run_async(coro):
    """Run a coroutine from a sync skill call (thread fallback when a loop exists)."""
    import asyncio
    import threading

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    result: dict = {}

    def _runner() -> None:
        result["value"] = asyncio.run(coro)

    thread = threading.Thread(target=_runner)
    thread.start()
    thread.join()
    return result.get("value")


async def _semantics_async(text: str) -> str:
    from langchain_core.messages import HumanMessage, SystemMessage
    from models import build_auxiliary_llm

    llm = build_auxiliary_llm()
    response = await llm.ainvoke(
        [
            SystemMessage(
                content=(
                    "You extract a knowledge graph from documents. Return JSON only, with "
                    'this shape: {"entities": [{"name": str, "type": str}], '
                    '"relations": [{"src": str, "dst": str, "type": str, '
                    '"confidence": "EXTRACTED"|"INFERRED", "evidence": str}]}. '
                    "EXTRACTED means the document states it directly; INFERRED means you "
                    "derived it. Keep entity names in the document's own language."
                )
            ),
            HumanMessage(content=text[:12000]),
        ]
    )
    return str(response.content)


def _parse_semantics(raw: str) -> dict:
    import json
    import re

    cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", raw or "", flags=re.MULTILINE)
    try:
        payload = json.loads(cleaned)
    except Exception:  # noqa: BLE001 - semantic extraction is fail-open
        return {"entities": [], "relations": []}
    if not isinstance(payload, dict):
        return {"entities": [], "relations": []}
    entities = [item for item in payload.get("entities") or [] if isinstance(item, dict)]
    relations = [item for item in payload.get("relations") or [] if isinstance(item, dict)]
    return {"entities": entities, "relations": relations}


def _frontmatter_block(fields: dict) -> str:
    try:
        import yaml

        body = yaml.safe_dump(fields, allow_unicode=True, sort_keys=False).strip()
    except ImportError:
        import json

        body = "\n".join(
            f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in fields.items()
        )
    return f"---\n{body}\n---\n\n"


def save_binary_source(
    content_bytes: bytes,
    content_type: str,
    source_url: str = "",
    filename: str = None,
    wiki_root=None,
) -> dict:
    """Ingest a binary source (PDF/image/audio/video/docx/xlsx/markdown).

    Extracts text through the skill's own extractors, runs one LLM semantic pass
    to pull entities/relations (fail-open), writes a markdown source with the
    optional ``extracted_entities`` / ``extracted_relations`` frontmatter, then
    refreshes the explicit graph. ``save_source`` is untouched.
    """
    if not content_bytes:
        return {"success": False, "error": "empty payload"}
    try:
        text, error = _extract_text(content_bytes, content_type)
    except Exception as exc:  # noqa: BLE001 - a missing extractor dependency is fail-open
        return {"success": False, "error": str(exc), "content_type": content_type}
    if error is not None:
        return {"success": False, "error": error, "content_type": content_type}

    semantics = {"entities": [], "relations": []}
    coro = _semantics_async(text)
    try:
        raw = _run_async(coro)
        semantics = _parse_semantics(raw) if raw else semantics
    except Exception as exc:  # noqa: BLE001 - semantics are additive
        close = getattr(coro, "close", None)
        if close is not None:
            close()
        logger.warning(f"llm_wiki: semantic extraction failed: {exc}")

    kind = content_type.lower().lstrip(".")
    category = _CATEGORY_BY_TYPE.get(kind, "articles")
    root = Path(wiki_root) if wiki_root else get_wiki_path()
    raw_dir = root / "raw" / category
    raw_dir.mkdir(parents=True, exist_ok=True)

    if filename:
        stem = Path(filename).stem
    else:
        prefix = text[:20].strip().replace(" ", "-").replace("\n", "") or "source"
        stem = prefix
    target = raw_dir / f"{stem}.md"

    sha256_hash = hashlib.sha256(content_bytes).hexdigest()
    fields = {
        "source_url": source_url or "manual",
        "ingested": date.today().isoformat(),
        "sha256": sha256_hash,
        "content_type": kind,
        "extracted_entities": [
            entity.get("name") for entity in semantics["entities"] if entity.get("name")
        ],
        "extracted_relations": semantics["relations"],
    }
    try:
        target.write_text(_frontmatter_block(fields) + text, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - report, never raise from a skill script
        logger.exception(f"llm_wiki: failed to write {target}: {exc}")
        return {"success": False, "error": str(exc), "file_path": str(target)}

    graph_result = None
    try:
        from skills.builtin.llm_wiki.scripts.graph import build_wiki_graph

        graph_result = build_wiki_graph(wiki_root=root)
    except Exception as exc:  # noqa: BLE001 - graph refresh is best-effort
        logger.warning(f"llm_wiki: graph refresh failed: {exc}")

    return {
        "success": True,
        "file_path": str(target),
        "sha256": sha256_hash,
        "content_type": kind,
        "category": category,
        "entities": semantics["entities"],
        "relations": semantics["relations"],
        "graph": graph_result,
    }
