"""
llm-wiki search: Wiki搜索与健康检查
"""

import sys
import os
import re
import glob
import importlib.util
from pathlib import Path
from collections import defaultdict
from loguru import logger

# 动态加载 core 模块
_scripts_dir = Path(__file__).resolve().parent
_core_spec = importlib.util.spec_from_file_location(
    "wiki_core", str(_scripts_dir / "core.py"))
_core = importlib.util.module_from_spec(_core_spec)
_core_spec.loader.exec_module(_core)
get_wiki_path = _core.get_wiki_path
WIKI_STRUCTURE = _core.WIKI_STRUCTURE


def search_wiki(keyword: str) -> list:
    """
    在Wiki中按关键词搜索所有.md文件。

    Args:
        keyword: 搜索关键词

    Returns:
        list: 匹配结果列表，每项为 {"file": 相对路径, "matches": 匹配行数}
    """
    wiki_root = get_wiki_path()
    results = []

    if not wiki_root.exists():
        logger.warning(f"Wiki directory does not exist: {wiki_root}")
        return results

    pattern = os.path.join(str(wiki_root), "**", "*.md")
    for fpath in glob.glob(pattern, recursive=True):
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
            if keyword.lower() in content.lower():
                rel_path = os.path.relpath(fpath, str(wiki_root))
                match_count = content.lower().count(keyword.lower())
                results.append({
                    "file": rel_path,
                    "matches": match_count
                })
        except Exception as e:
            logger.warning(f"Failed to read {fpath}: {e}")

    results.sort(key=lambda x: x["matches"], reverse=True)
    return results


def lint_wiki() -> dict:
    """
    对Wiki进行健康检查。

    Returns:
        dict: 检查结果，包含各类问题
    """
    wiki_root = get_wiki_path()
    report = {
        "orphan_pages": [],
        "broken_links": [],
        "index_completeness": {"missing_from_index": [], "extra_in_index": []},
        "frontmatter_issues": [],
        "large_pages": [],
        "total_pages": 0,
        "errors": []
    }

    if not wiki_root.exists():
        report["errors"].append("Wiki directory does not exist")
        return report

    # 扫描所有.md文件
    all_md_files = []
    pattern = os.path.join(str(wiki_root), "**", "*.md")
    for fpath in glob.glob(pattern, recursive=True):
        rel_path = os.path.relpath(fpath, str(wiki_root))
        # 排除根目录的SCHEMA/index/log
        if rel_path in ("SCHEMA.md", "index.md", "log.md"):
            continue
        all_md_files.append(rel_path)

    report["total_pages"] = len(all_md_files)

    # 构建wikilink引用图
    inbound_links = defaultdict(set)
    outbound_links = defaultdict(set)

    for fpath in glob.glob(pattern, recursive=True):
        rel_path = os.path.relpath(fpath, str(wiki_root))
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()

            # 提取 [[wikilinks]]
            links = re.findall(r'\[\[([^\]]+)\]\]', content)
            for link in links:
                target = link.split("|")[0].strip()
                outbound_links[rel_path].add(target)
                inbound_links[target].add(rel_path)

            # 检查frontmatter（跳过raw/目录，raw使用自己的frontmatter格式）
            if not rel_path.startswith("raw") and content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    fm = parts[1]
                    required = ["title", "created", "updated", "type", "tags"]
                    missing = [f for f in required if f not in fm]
                    if missing:
                        report["frontmatter_issues"].append({
                            "file": rel_path,
                            "missing_fields": missing
                        })

            # 检查文件大小
            lines = content.split("\n")
            if len(lines) > 200:
                report["large_pages"].append({
                    "file": rel_path,
                    "lines": len(lines)
                })

        except Exception as e:
            report["errors"].append(f"Failed to process {rel_path}: {e}")

    # 孤儿页面：没有入链的页面（跳过raw/目录）
    for page in all_md_files:
        if page.startswith("raw"):
            continue
        page_name = Path(page).stem
        if page_name not in inbound_links:
            report["orphan_pages"].append(page)

    # 断链：指向不存在的页面
    all_page_names = set()
    for p in all_md_files:
        all_page_names.add(Path(p).stem)
    all_page_names.update(["SCHEMA", "index", "log"])

    for src, targets in outbound_links.items():
        for t in targets:
            if t not in all_page_names:
                report["broken_links"].append({
                    "source": src,
                    "target": t
                })

    return report
