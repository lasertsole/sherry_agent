"""
llm_wiki ingest: raw source ingestion
"""

import hashlib
import importlib.util
from pathlib import Path
from datetime import date
from loguru import logger

# Dynamically load the core module
_scripts_dir = Path(__file__).resolve().parent
_core_spec = importlib.util.spec_from_file_location("wiki_core", str(_scripts_dir / "core.py"))
_core = importlib.util.module_from_spec(_core_spec)
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
        logger.error(f"Failed to save source: {e}")
        return {"file_path": str(file_path), "error": str(e), "success": False}
