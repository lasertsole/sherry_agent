# Static interface for the graph_rag package. The runtime __init__.py resolves
# get_lightrag / get_rag_anything lazily via module __getattr__ (PEP 562) so
# importing the package never touches the model stack — dynamic exports are
# invisible to type checkers and IDEs, hence this stub.

from .base import LightRAG
from .core import RAGAnything

async def get_lightrag() -> LightRAG: ...
async def get_rag_anything(parser: str = ..., parse_method: str = ...) -> RAGAnything: ...
def ensure_mineru_models(source: str = ..., download_vlm: bool = ...) -> dict: ...
