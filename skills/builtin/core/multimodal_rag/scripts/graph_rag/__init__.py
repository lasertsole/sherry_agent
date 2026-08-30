"""graph_rag package — LightRAG-backed multimodal RAG.

The vendored LightRAG (``graph_rag.vendored_lightrag``) is **SNKV-only**.
The four SNKV storage backends are NATIVE modules of the vendored LightRAG,
co-located in ``vendored_lightrag.kg.snkv_*_impl`` and registered directly
in ``kg/__init__.py`` — no runtime ``register()`` injection is involved.
The vendored LightRAG is fundamentally reachable only through the short,
absolute module name ``graph_rag.vendored_lightrag.*`` (its ~80 files use
short absolute imports internally).  That only resolves when the parent
directory of this package (``scripts/``) is on ``sys.path``.

This package may be reached two ways at runtime:
1. As a script ``python rag_index.py`` -> ``scripts/`` is already on path.
2. Via the full dotted path ``skills.builtin.core.multimodal_rag.scripts.
   graph_rag`` -> only the repo root is on path, NOT ``scripts/``.

To support BOTH cases we ensure ``scripts/`` is on ``sys.path`` here, before
any submodule (notably ``base``) executes a short ``graph_rag`` import.
The setup is idempotent and cheap (a set membership check).
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys

# Parent of this package == the `scripts/` directory.
# __file__ -> .../scripts/graph_rag/__init__.py -> dirname x1 = .../scripts/graph_rag,
# dirname x2 = .../scripts (the directory that contains this package, so that the
# short, absolute module name `graph_rag...` resolves on sys.path).
_scripts_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)


def _alias_vendored_lightrag() -> None:
    """Expose the vendored LightRAG under the bare ``lightrag`` import names.

    The installed ``raganything`` (in site-packages) resolves bare
    ``from lightrag import LightRAG`` / ``from lightrag.utils import ...`` /
    ``from lightrag.kg.shared_storage import ...`` at module-import time.  We run
    exclusively against the vendored LightRAG, so we alias every bare name that
    raganything imports onto the corresponding vendored module via ``sys.modules``.
    This lets ``lightrag-hku`` be dropped from the dependency graph entirely.

    The raganything import surface (verified against raganything 1.3.1):
      - ``from lightrag import LightRAG, QueryParam``
      - ``from lightrag.lightrag import LightRAG``
      - ``from lightrag.utils import logger, compute_mdhash_id,
        always_get_an_event_loop, get_env_value``
      - ``from lightrag.operate import extract_entities, merge_nodes_and_edges``
      - ``from lightrag.kg.shared_storage import get_namespace_data,
        get_pipeline_status_lock``

    Must run BEFORE ``.base`` / ``.core`` are imported (they pull in
    ``raganything`` transitively).
    """
    from .vendored_lightrag import (
        lightrag as _lightrag_mod,
        operate as _operate_mod,
        utils as _utils_mod,
    )
    from .vendored_lightrag.kg import shared_storage as _shared_storage_mod
    import graph_rag.vendored_lightrag as _vendored

    # Submodule aliases: key = bare import name, value = vendored module object.
    _aliases = {
        "lightrag": _vendored,
        "lightrag.lightrag": _lightrag_mod,
        "lightrag.utils": _utils_mod,
        "lightrag.operate": _operate_mod,
        "lightrag.kg": _vendored.kg,
        "lightrag.kg.shared_storage": _shared_storage_mod,
    }
    for bare_name, vendored_mod in _aliases.items():
        sys.modules.setdefault(bare_name, vendored_mod)


_alias_vendored_lightrag()


# --- Vendored RAG-Anything ---
#
# ``raganything`` itself is now VENDORED as ``graph_rag.vendored_raganything``
# (the package was copied out of ``.venv`` site-packages so that the
# ``raganything`` dependency can be dropped from ``pyproject.toml``/``uv.lock``).
# Its ~19 files use short, bare absolute imports internally (e.g. ``from
# raganything.config import RAGAnythingConfig``, ``from raganything.parser
# import Parser``).  For those to keep resolving to the vendored copy — instead
# of an installed PIP package — we load the vendored package directly under the
# bare ``raganything`` identity.
def _alias_vendored_raganything() -> None:
    """Expose the vendored RAG-Anything under the bare ``raganything`` name.

    The vendored ``__init__.py`` does ``from .raganything import RAGAnything``
    and the vendored submodules import each other with bare ``from
    raganything.X import ...`` statements.  For all of those to resolve to the
    vendored copy (and nowhere else), we build the package object from the
    vendored ``__init__.py`` via ``importlib.util.spec_from_file_location`` and
    register it in ``sys.modules`` under the bare ``raganything`` name BEFORE
    executing it.  That mirrors exactly how the original PIP package resolved
    from site-packages — except ``raganything.__path__`` now points at the
    vendored directory.

    We only force-load the modules the original ``__init__.py`` loads eagerly
    (``raganything``, ``config``, ``parser``).  Feature-heavy modules
    (``enhanced_markdown``, ``omml_extractor``, ``modalprocessors``,
    ``processor``, ...) are left LAZY: the original package never imported them
    at package-import time either, and some (``enhanced_markdown``) hard-import
    WeasyPrint, which is not always installed.  They resolve on demand through
    ``__path__`` whenever a feature is actually used.

    Must run BEFORE ``.core`` is imported (``core.py`` does
    ``from raganything import RAGAnything, ...``).
    """
    _this_dir = os.path.dirname(os.path.abspath(__file__))
    _vendor_dir = os.path.join(_this_dir, "vendored_raganything")
    _init_path = os.path.join(_vendor_dir, "__init__.py")

    if "raganything" in sys.modules:
        # Already aliased (e.g. re-import in the same interpreter). Do nothing.
        return

    _spec = importlib.util.spec_from_file_location(
        "raganything", _init_path, submodule_search_locations=[_vendor_dir]
    )
    _pkg = importlib.util.module_from_spec(_spec)
    # Register BEFORE exec_module so the vendored __init__ and all bare inner
    # ``raganything.X`` imports resolve to this same package (never to PIP).
    sys.modules["raganything"] = _pkg
    _spec.loader.exec_module(_pkg)

    # Eagerly force-load the modules the original package exposes at import time
    # plus ``parser`` (needed directly by ``core.py``).
    for _sub in ("raganything", "config", "parser"):
        importlib.import_module(f"raganything.{_sub}")


_alias_vendored_raganything()

from .base import get_lightrag
from .core import get_rag_anything
from .ensure_mineru_models import ensure_mineru_models

__all__ = ["get_lightrag", "get_rag_anything", "ensure_mineru_models"]
