"""Vector semantic search over MesMemory (SESSION plan P2-5).

Embeddings are generated lazily for messages that do not have one yet, stored
in the ``message_embeddings`` table, and searched by cosine similarity. The
embedding backend is the project's configured embed model (``models``),
resolved lazily so importing this package never loads model weights.
"""

from .search import semantic_search
from .store import load_all_embeddings, save_embeddings

__all__ = ["semantic_search", "save_embeddings", "load_all_embeddings"]
