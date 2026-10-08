"""code_wiki package — convenient re-exports of the public API from scripts/core.py."""

from .scripts.core import (
    get_wiki_path,
    get_repo_wiki_path,
    get_modules_dir,
    get_diagrams_dir,
    init_repo_wiki,
    list_modules,
    repo_wiki_exists,
    read_doc,
    write_doc,
    get_structure,
    print_structure,
)

__all__ = [
    "get_wiki_path",
    "get_repo_wiki_path",
    "get_modules_dir",
    "get_diagrams_dir",
    "init_repo_wiki",
    "list_modules",
    "repo_wiki_exists",
    "read_doc",
    "write_doc",
    "get_structure",
    "print_structure",
]
