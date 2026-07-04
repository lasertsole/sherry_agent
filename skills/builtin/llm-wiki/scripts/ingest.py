"""
llm-wiki ingest: 原始资料导入
"""

import sys
import hashlib
import importlib.util
from pathlib import Path
from datetime import date
from loguru import logger

# 动态加载 core 模块
_scripts_dir = Path(__file__).resolve().parent
_core_spec = importlib.util.spec_from_file_location(
    "wiki_core", str(_scripts_dir / "core.py"))
_core = importlib.util.module_from_spec(_core_spec)
_core_spec.loader.exec_module(_core)
get_wiki_path = _core.get_wiki_path


def save_source(content: str, category: str = "articles", filename: str = None) -> dict:
    """
    保存原始资料到 raw/ 目录。

    Args:
        content: 资料内容（文本）
        category: 分类，可选 articles / papers / transcripts / assets
        filename: 文件名（不含路径），如不指定则自动生成

    Returns:
        dict: 保存结果，包含 file_path, sha256, success
    """
    wiki_root = get_wiki_path()
    raw_dir = wiki_root / "raw" / category
    raw_dir.mkdir(parents=True, exist_ok=True)

    if not filename:
        # 用内容前20字 + 日期生成文件名
        prefix = content[:20].strip().replace(" ", "-").replace("\n", "")
        today = date.today().isoformat()
        filename = f"{prefix}-{today}.md"

    file_path = raw_dir / filename

    # 计算sha256
    sha256_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    # 写入文件（带frontmatter）
    frontmatter = f"""---
source_url: manual
ingested: {date.today().isoformat()}
sha256: {sha256_hash}
---

"""
    full_content = frontmatter + content

    try:
        file_path.write_text(full_content, encoding="utf-8")
        logger.info(f"Source saved: {file_path}")
        return {
            "file_path": str(file_path),
            "sha256": sha256_hash,
            "success": True
        }
    except Exception as e:
        logger.error(f"Failed to save source: {e}")
        return {
            "file_path": str(file_path),
            "error": str(e),
            "success": False
        }
