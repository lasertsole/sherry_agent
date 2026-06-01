"""
Tests backward-compatible re-export of rag.rag_anything.core.
All implementation lives under rag/rag_anything/core.py.
"""

import os
import sys

_project_root = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from rag.rag_anything.core import (  # noqa: E402, F401
    get_rag_anything,
    FallbackTxtParser,
    _vision_model_func,
)
