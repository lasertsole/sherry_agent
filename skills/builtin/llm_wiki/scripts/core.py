"""
llm_wiki core: Wiki path management and initialization.
All paths are computed dynamically from the project root; no hardcoded absolute paths.
"""

import sys
import json
from pathlib import Path
from loguru import logger

# Dynamically locate the project root (4 levels up from skills/builtin/llm_wiki/scripts/)
current_file = Path(__file__).resolve()
project_root: Path = current_file.parents[4]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# ============================================================
# Wiki directory structure definition (JSON format: saves tokens and is easy to parse)
# ============================================================
WIKI_STRUCTURE = {
    "wiki": {
        "root": ["SCHEMA.md", "index.md", "log.md"],
        "raw": {
            "description": "原始资料（只读，不可修改）",
            "articles": "网页文章/网络资料",
            "papers": "论文/研究报告",
            "transcripts": "对话记录/访谈",
            "assets": "图片/附件资源",
        },
        "entities": "人物/组织档案",
        "concepts": "概念/主题解析",
        "comparisons": "对比分析",
        "queries": "查询结果存档",
    }
}


def get_wiki_path() -> Path:
    """
    Dynamically resolve the wiki root directory path.
    Based on src/data/wiki/ under the project root.

    Returns:
        Path: Path object for the wiki root directory
    """
    return project_root / "src" / "data" / "wiki"


def get_wiki_subdir(subdir: str) -> Path:
    """
    Get the path of a subdirectory under the wiki.

    Args:
        subdir: Subdirectory name, e.g. "entities", "raw/articles"

    Returns:
        Path: Path object for the subdirectory
    """
    return get_wiki_path() / subdir


def init_wiki() -> dict:
    """
    Initialize the wiki directory structure.
    Creates all required directories and files according to WIKI_STRUCTURE.

    Returns:
        dict: Initialization result, containing created_dirs, created_files, errors
    """
    result = {"created_dirs": [], "created_files": [], "errors": []}

    wiki_root = get_wiki_path()

    try:
        # Create the root directory
        wiki_root.mkdir(parents=True, exist_ok=True)
        result["created_dirs"].append(str(wiki_root))

        struct = WIKI_STRUCTURE["wiki"]

        # Create subdirectories under raw/
        raw = struct.get("raw", {})
        if isinstance(raw, dict):
            for subdir_name in raw:
                if subdir_name == "description":
                    continue
                subdir_path = wiki_root / "raw" / subdir_name
                subdir_path.mkdir(parents=True, exist_ok=True)
                result["created_dirs"].append(str(subdir_path))

        # Create the other category directories (entities, concepts, comparisons, queries)
        for key, value in struct.items():
            if key == "root" or key == "raw":
                continue
            if isinstance(value, str):
                dir_path = wiki_root / key
                dir_path.mkdir(parents=True, exist_ok=True)
                result["created_dirs"].append(str(dir_path))

        # Create root-level files (SCHEMA.md, index.md, log.md)
        root_files = struct.get("root", [])
        for fname in root_files:
            fpath = wiki_root / fname
            if not fpath.exists():
                fpath.write_text("", encoding="utf-8")
                result["created_files"].append(str(fpath))

        logger.debug(f"Wiki initialized at: {wiki_root}")

    except Exception as e:
        logger.error(f"Wiki initialization failed: {e}")
        result["errors"].append(str(e))

    return result


def wiki_exists() -> bool:
    """
    Check whether the wiki has been initialized.

    Returns:
        bool: Whether the wiki root directory exists
    """
    return get_wiki_path().exists()


def print_structure() -> str:
    """
    Output the wiki directory structure as JSON.
    Used to display the structure to the user in conversation.

    Returns:
        str: Formatted JSON string
    """
    return json.dumps(WIKI_STRUCTURE, ensure_ascii=False, indent=2)
