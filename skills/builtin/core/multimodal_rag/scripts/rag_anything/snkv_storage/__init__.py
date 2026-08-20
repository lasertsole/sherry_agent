"""SNKV storage backends vendored into the rag_anything package.

These are LightRAG storage implementations backed by the ``snkv``
embedded key-value store. They are vendored from the ``lightrag-snkv``
reference, adapted to:
- Import the co-located modules via relative imports (this package).
- Not depend on the ``bench.llm_env`` helper from the upstream repo.
- Match the abstract-method contract of the installed ``lightrag-hku``
  1.5.2 ``DocStatusStorage`` (3 methods added by the v1.5.2 interface).

Path bootstrap
--------------
The other rag_anything modules (``base.py``) and the LightRAG lazy loader
call this package through the short, absolute module name
``rag_anything.snkv_storage.*``. That only resolves when the ``scripts/``
directory (the parent of the ``rag_anything`` package) is on ``sys.path``.

The parent package can be reached two ways at runtime:
1. As a script ``python rag_index.py`` -> ``scripts/`` is already on path.
2. Via the full dotted path ``skills.builtin.core.multimodal_rag.scripts.
   rag_anything`` -> only the repo root is on path, NOT ``scripts/``.

To support BOTH cases this module ensures the ``scripts/`` directory is on
``sys.path`` before any submodule that uses the short ``rag_anything`` name
is imported.  This is idempotent and cheap (a set membership check).
"""
from __future__ import annotations

import os
import sys

# Parent of the `rag_anything` package == the `scripts/` directory.
# __file__ -> .../scripts/rag_anything/snkv_storage/__init__.py
# os.path.dirname x3 -> .../scripts
_parent_pkg_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _parent_pkg_root not in sys.path:
    sys.path.insert(0, _parent_pkg_root)

from .register import register, register_with_lightrag
from .snkv_doc_status_impl import SNKVDocStatusStorage
from .snkv_graph_impl import SNKVGraphStorage
from .snkv_kv_impl import SNKVKVStorage
from .snkv_vector_impl import SNKVVectorStorage

__all__ = [
    "SNKVKVStorage",
    "SNKVVectorStorage",
    "SNKVGraphStorage",
    "SNKVDocStatusStorage",
    "register",
    "register_with_lightrag",
]
