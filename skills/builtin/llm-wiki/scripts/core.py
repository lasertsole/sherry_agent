"""
llm-wiki core: Wiki路径管理与初始化
所有路径均通过项目根目录动态计算，无硬编码绝对路径。
"""

import sys
import os
import json
from pathlib import Path
from loguru import logger

# 动态定位项目根目录 (skills/builtin/llm-wiki/scripts/ 向上4层)
current_file = Path(__file__).resolve()
project_root: Path = current_file.parents[4]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# ============================================================
# Wiki 目录结构定义 (JSON格式，省token且易解析)
# ============================================================
WIKI_STRUCTURE = {
    "wiki": {
        "root": ["SCHEMA.md", "index.md", "log.md"],
        "raw": {
            "description": "原始资料（只读，不可修改）",
            "articles": "网页文章/网络资料",
            "papers": "论文/研究报告",
            "transcripts": "对话记录/访谈",
            "assets": "图片/附件资源"
        },
        "entities": "人物/组织档案",
        "concepts": "概念/主题解析",
        "comparisons": "对比分析",
        "queries": "查询结果存档"
    }
}


def get_wiki_path() -> Path:
    """
    动态获取Wiki根目录路径。
    基于项目根目录下的 src/data/wiki/

    Returns:
        Path: Wiki根目录的Path对象
    """
    return project_root / "src" / "data" / "wiki"


def get_wiki_subdir(subdir: str) -> Path:
    """
    获取Wiki下指定子目录的路径。

    Args:
        subdir: 子目录名，如 "entities", "raw/articles"

    Returns:
        Path: 子目录的Path对象
    """
    return get_wiki_path() / subdir


def init_wiki() -> dict:
    """
    初始化Wiki目录结构。
    根据 WIKI_STRUCTURE 创建所有必要的目录和文件。

    Returns:
        dict: 初始化结果，包含 created_dirs, created_files, errors
    """
    result = {
        "created_dirs": [],
        "created_files": [],
        "errors": []
    }

    wiki_root = get_wiki_path()

    try:
        # 创建根目录
        wiki_root.mkdir(parents=True, exist_ok=True)
        result["created_dirs"].append(str(wiki_root))

        struct = WIKI_STRUCTURE["wiki"]

        # 创建 raw/ 下的子目录
        raw = struct.get("raw", {})
        if isinstance(raw, dict):
            for subdir_name in raw:
                if subdir_name == "description":
                    continue
                subdir_path = wiki_root / "raw" / subdir_name
                subdir_path.mkdir(parents=True, exist_ok=True)
                result["created_dirs"].append(str(subdir_path))

        # 创建其他分类目录 (entities, concepts, comparisons, queries)
        for key, value in struct.items():
            if key == "root" or key == "raw":
                continue
            if isinstance(value, str):
                dir_path = wiki_root / key
                dir_path.mkdir(parents=True, exist_ok=True)
                result["created_dirs"].append(str(dir_path))

        # 创建根目录下的文件 (SCHEMA.md, index.md, log.md)
        root_files = struct.get("root", [])
        for fname in root_files:
            fpath = wiki_root / fname
            if not fpath.exists():
                fpath.write_text("", encoding="utf-8")
                result["created_files"].append(str(fpath))

        logger.info(f"Wiki initialized at: {wiki_root}")

    except Exception as e:
        logger.error(f"Wiki initialization failed: {e}")
        result["errors"].append(str(e))

    return result


def wiki_exists() -> bool:
    """
    检查Wiki是否已初始化。

    Returns:
        bool: Wiki根目录是否存在
    """
    return get_wiki_path().exists()


def print_structure() -> str:
    """
    以JSON格式输出Wiki目录结构。
    用于在对话中展示给用户。

    Returns:
        str: 格式化的JSON字符串
    """
    return json.dumps(WIKI_STRUCTURE, ensure_ascii=False, indent=2)
