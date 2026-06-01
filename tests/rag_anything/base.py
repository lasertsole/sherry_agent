"""
Tests backward-compatible re-export of rag.rag_anything.base.
All implementation lives under rag/rag_anything/base.py.
"""

import os
import sys

_project_root = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# Set environment variables before any raganything imports
os.environ.setdefault("MAX_SOURCE_IDS_PER_ENTITY", "50")
os.environ.setdefault("MAX_SOURCE_IDS_PER_RELATION", "50")
os.environ.setdefault("SOURCE_IDS_LIMIT_METHOD", "FIFO")
os.environ.setdefault("RELATED_CHUNK_NUMBER", "5")

from rag.rag_anything.base import (  # noqa: E402, F401
    get_lightrag,
    add_rag,
    retrieve_rag,
    delete_rag,
)
