"""rag_anything package — LightRAG-backed multimodal RAG.

The submodules reference the sibling package ``snkv_storage`` through the
short, absolute module name ``rag_anything.snkv_storage`` (e.g. ``base.py``
and the LightRAG lazy ``STORAGES`` loader).  That only resolves when the
parent directory of this package (``scripts/``) is on ``sys.path``.

This package may be reached two ways at runtime:
1. As a script ``python rag_index.py`` -> ``scripts/`` is already on path.
2. Via the full dotted path ``skills.builtin.core.multimodal_rag.scripts.
   rag_anything`` -> only the repo root is on path, NOT ``scripts/``.

To support BOTH cases we ensure ``scripts/`` is on ``sys.path`` here, before
any submodule (notably ``base``) executes a short ``rag_anything`` import.
The setup is idempotent and cheap (a set membership check).
"""
from __future__ import annotations

import os
import sys

# Parent of this package == the `scripts/` directory.
# __file__ -> .../scripts/rag_anything/__init__.py -> dirname x1 = .../scripts
_scripts_dir = os.path.dirname(os.path.abspath(__file__))
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from .base import get_lightrag
from .core import get_rag_anything
from .ensure_mineru_models import ensure_mineru_models

__all__ = [
    "get_lightrag",
    "get_rag_anything",
    "ensure_mineru_models"
]
