"""Static guard for model-authored Python executed under restricted builtins.

The restricted-builtins sets in ``python_repl`` and ``agent/tools/ptc`` remove
``open`` / ``__import__``, but Python's object model still reaches the
filesystem and the process API through introspection::

    ().__class__.__base__.__subclasses__()   # -> subprocess.Popen, os._wrap_close, ...

That escape is the reason 2026-11-09 audit item H4 exists. The OS sandbox is
the real barrier; this module is the cheap layer in front of it that refuses
the escape SHAPES before anything runs (a sandbox may be unavailable — H3 — and
``python_repl``/PTC execute model-written code, so defense in depth is the
point).

Rules (AST-level, applied to the user's source text only — never the wrapper):

- any dunder ATTRIBUTE (``x.__class__``, ``x.__subclasses__``,
  ``x.__globals__``, …) is refused: ordinary task code has no use for one;
- the global names ``__builtins__`` / ``__loader__`` / ``__spec__`` are refused.

Syntax errors are reported too, so the caller can surface one clear message
instead of a child-process traceback.
"""

from __future__ import annotations

import ast

#: Global names that expose the interpreter's own namespace machinery.
_FORBIDDEN_NAMES = frozenset({"__builtins__", "__loader__", "__spec__"})


def _is_dunder(name: str) -> bool:
    return len(name) > 4 and name.startswith("__") and name.endswith("__")


def validate_restricted_code(source: str, *, label: str = "code") -> None:
    """Refuse introspection escapes in ``source``.

    Raises:
        ValueError: with a model-readable explanation naming the offending
            line and construct. Callers translate it into their own tool error.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ValueError(f"{label} refused: syntax error: {exc.msg} (line {exc.lineno})") from exc

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and _is_dunder(node.attr):
            raise ValueError(
                f"{label} refused: attribute access '{node.attr}' is not allowed in "
                f"restricted code (line {node.lineno})"
            )
        if isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES:
            raise ValueError(
                f"{label} refused: name '{node.id}' is not available in restricted code "
                f"(line {node.lineno})"
            )


__all__ = ["validate_restricted_code"]
