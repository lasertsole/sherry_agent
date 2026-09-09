"""
code_wiki core: Wiki path management and document operations.
All paths are computed dynamically from the project root; no hardcoded absolute paths.
"""

import sys
import json
from pathlib import Path
from loguru import logger

# Dynamically locate the project root (4 levels up from skills/builtin/code_wiki/scripts/)
current_file = Path(__file__).resolve()
project_root: Path = current_file.parents[4]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))


WIKI_SUBDIR = "wiki"  # placed under src/data/wiki/


def get_wiki_path() -> Path:
    """
    Dynamically resolve the code_wiki output root directory.
    Based on src/data/wiki/ under the project root.

    Returns:
        Path: Path object for the wiki root directory
    """
    return project_root / "src" / "data" / WIKI_SUBDIR


def get_repo_wiki_path(repo_name: str) -> Path:
    """
    Get the wiki documentation directory for the given repository.

    Args:
        repo_name: Repository name

    Returns:
        Path: Path object for that repository's wiki directory
    """
    return get_wiki_path() / repo_name


def get_modules_dir(repo_name: str) -> Path:
    """Get the modules/ subdirectory for the given repository."""
    return get_repo_wiki_path(repo_name) / "modules"


def get_diagrams_dir(repo_name: str) -> Path:
    """Get the diagrams/ subdirectory for the given repository."""
    return get_repo_wiki_path(repo_name) / "diagrams"


def init_repo_wiki(repo_name: str) -> dict:
    """
    Initialize the wiki documentation directory structure for a repository.

    Args:
        repo_name: Repository name

    Returns:
        dict: Creation result, containing created_dirs, created_files, errors
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
    List the generated module documents for the given repository.

    Args:
        repo_name: Repository name

    Returns:
        list[dict]: List of module documents, each with name and path
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
    """Check whether the wiki docs for the given repository have been initialized."""
    return get_repo_wiki_path(repo_name).exists()


def read_doc(repo_name: str, filename: str) -> str | None:
    """
    Read a document from the given repository's wiki.

    Args:
        repo_name: Repository name
        filename: File name (e.g. "README.md", "architecture.md", "modules/core.md")

    Returns:
        str | None: File content, or None if the file does not exist
    """
    path = get_repo_wiki_path(repo_name) / filename
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def write_doc(repo_name: str, filename: str, content: str) -> Path:
    """
    Write a document into the given repository's wiki.

    Args:
        repo_name: Repository name
        filename: File name (e.g. "README.md", "modules/core.md")
        content: File content

    Returns:
        Path: Path of the written file
    """
    path = get_repo_wiki_path(repo_name) / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    logger.debug(f"Written: {path}")
    return path


def get_structure() -> dict:
    """
    Return the code_wiki documentation directory structure definition.

    Returns:
        dict: Directory structure
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
    """Output the directory structure as JSON."""
    return json.dumps(get_structure(), ensure_ascii=False, indent=2)
