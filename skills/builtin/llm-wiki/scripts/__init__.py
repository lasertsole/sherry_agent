"""
llm-wiki scripts package.
由于技能目录名包含连字符(llm-wiki)，无法使用标准Python包导入，
所有模块间引用均使用 importlib 动态加载。
"""

import importlib.util
from pathlib import Path

_scripts_dir = Path(__file__).resolve().parent


def _load_module(name, filename):
    """动态加载同目录下的Python模块"""
    spec = importlib.util.spec_from_file_location(
        name, str(_scripts_dir / filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# 加载各模块
_core = _load_module("wiki_core", "core.py")
_search = _load_module("wiki_search", "search.py")
_ingest = _load_module("wiki_ingest", "ingest.py")

# 导出 core 中的内容
get_wiki_path = _core.get_wiki_path
get_wiki_subdir = _core.get_wiki_subdir
init_wiki = _core.init_wiki
wiki_exists = _core.wiki_exists
print_structure = _core.print_structure
WIKI_STRUCTURE = _core.WIKI_STRUCTURE

# 导出 search 中的内容
search_wiki = _search.search_wiki
lint_wiki = _search.lint_wiki

# 导出 ingest 中的内容
save_source = _ingest.save_source

__all__ = [
    "get_wiki_path", "get_wiki_subdir", "init_wiki", "wiki_exists",
    "print_structure", "WIKI_STRUCTURE",
    "search_wiki", "lint_wiki",
    "save_source",
]
