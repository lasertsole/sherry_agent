"""
code_wiki core: Wiki 路径管理与文档操作
所有路径均通过项目根目录动态计算，无硬编码绝对路径。
"""

import sys
import json
from pathlib import Path
from loguru import logger

# 动态定位项目根目录 (skills/builtin/code_wiki/scripts/ 向上4层)
current_file = Path(__file__).resolve()
project_root: Path = current_file.parents[4]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))


WIKI_SUBDIR = "wiki"  # 放在 src/data/wiki/ 下


def get_wiki_path() -> Path:
    """
    动态获取 code_wiki 输出根目录路径。
    基于项目根目录下的 src/data/wiki/

    Returns:
        Path: wiki 根目录的 Path 对象
    """
    return project_root / "src" / "data" / WIKI_SUBDIR


def get_repo_wiki_path(repo_name: str) -> Path:
    """
    获取指定仓库的 wiki 文档目录。

    Args:
        repo_name: 仓库名称

    Returns:
        Path: 该仓库 wiki 目录的 Path 对象
    """
    return get_wiki_path() / repo_name


def get_modules_dir(repo_name: str) -> Path:
    """获取指定仓库的 modules/ 子目录。"""
    return get_repo_wiki_path(repo_name) / "modules"


def get_diagrams_dir(repo_name: str) -> Path:
    """获取指定仓库的 diagrams/ 子目录。"""
    return get_repo_wiki_path(repo_name) / "diagrams"


def init_repo_wiki(repo_name: str) -> dict:
    """
    初始化某个仓库的 wiki 文档目录结构。

    Args:
        repo_name: 仓库名称

    Returns:
        dict: 创建结果，包含 created_dirs, created_files, errors
    """
    result = {"created_dirs": [], "created_files": [], "errors": []}

    try:
        repo_dir = get_repo_wiki_path(repo_name)
        modules_dir = repo_dir / "modules"
        diagrams_dir = repo_dir / "diagrams"

        repo_dir.mkdir(parents=True, exist_ok=True)
        result["created_dirs"].append(str(repo_dir))

        modules_dir.mkdir(exist_ok=True)
        result["created_dirs"].append(str(modules_dir))

        diagrams_dir.mkdir(exist_ok=True)
        result["created_dirs"].append(str(diagrams_dir))

        logger.debug(f"code_wiki initialized for '{repo_name}' at: {repo_dir}")

    except Exception as e:
        logger.error(f"code_wiki init failed for '{repo_name}': {e}")
        result["errors"].append(str(e))

    return result


def list_modules(repo_name: str) -> list[dict]:
    """
    列出指定仓库已生成的模块文档。

    Args:
        repo_name: 仓库名称

    Returns:
        list[dict]: 模块文档列表，每项包含 name 和 path
    """
    modules_dir = get_modules_dir(repo_name)
    if not modules_dir.exists():
        return []

    results = []
    for f in sorted(modules_dir.iterdir()):
        if f.suffix == ".md":
            results.append({"name": f.stem, "path": str(f)})
    return results


def repo_wiki_exists(repo_name: str) -> bool:
    """检查指定仓库的 wiki 文档是否已初始化。"""
    return get_repo_wiki_path(repo_name).exists()


def read_doc(repo_name: str, filename: str) -> str | None:
    """
    读取指定仓库 wiki 中的某个文档。

    Args:
        repo_name: 仓库名称
        filename: 文件名 (如 "README.md", "architecture.md", "modules/core.md")

    Returns:
        str | None: 文件内容，不存在则返回 None
    """
    path = get_repo_wiki_path(repo_name) / filename
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def write_doc(repo_name: str, filename: str, content: str) -> Path:
    """
    写入文档到指定仓库 wiki。

    Args:
        repo_name: 仓库名称
        filename: 文件名 (如 "README.md", "modules/core.md")
        content: 文件内容

    Returns:
        Path: 写入文件的路径
    """
    path = get_repo_wiki_path(repo_name) / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    logger.debug(f"Written: {path}")
    return path


def get_structure() -> dict:
    """
    返回 code_wiki 文档目录结构定义。

    Returns:
        dict: 目录结构
    """
    return {
        "wiki_root": str(get_wiki_path()),
        "structure": {
            "<repo_name>/": {
                "README.md": "项目概览 + 模块地图",
                "architecture.md": "系统架构 + Mermaid 流程图",
                "getting-started.md": "搭建、首次运行、工作流",
                "modules/": {"<module>.md": "每个模块的深入分析"},
                "diagrams/": {
                    "class-diagram.md": "Mermaid class 图",
                    "sequences.md": "Mermaid 时序图",
                },
            }
        },
    }


def print_structure() -> str:
    """以 JSON 格式输出目录结构。"""
    return json.dumps(get_structure(), ensure_ascii=False, indent=2)
