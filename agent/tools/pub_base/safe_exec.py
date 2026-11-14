"""Static guard for model-authored Python executed under restricted builtins.

The restricted-builtins sets in ``python_repl`` and ``agent/tools/ptc`` remove
``open`` / ``__import__``, but Python's object model still reaches the
filesystem and the process API through introspection::

    ().__class__.__base__.__subclasses__()   # -> subprocess.Popen, os._wrap_close, ...

That escape is the reason 2026-11-09 audit item H4 exists. The OS sandbox is
the real barrier; this module is the cheap layer in front of it that refuses
the escape SHAPES before anything runs (a sandbox may be unavailable and
``python_repl``/PTC execute model-written code, so defense in depth is the
point).

Rules (AST-level, applied to the user's source text only — never the wrapper):

- any dunder ATTRIBUTE (``x.__class__``, ``x.__subclasses__``,
  ``x.__globals__``, …) is refused: ordinary task code has no use for one;
- the global names ``__builtins__`` / ``__loader__`` / ``__spec__`` are refused;
- a LITERAL ``str.format`` / ``str.format_map`` format string whose replacement
  field reaches a DUNDER attribute (``"{0.__class__}".format(x)``) is refused
  too. The AST never sees inside a format string, so the ordinary attribute rule
  cannot catch that shape. A field's non-dunder attribute/item access is
  equivalent to writing ``x.attr`` / ``x[key]`` directly (already allowed), and
  ``str.format`` materialises a repr — never the object — so a non-literal format
  string is deliberately accepted: it can only ever produce a string, and
  ``repr`` is an allowed builtin anyway.

Syntax errors are reported too, so the caller can surface one clear message
instead of a child-process traceback.
"""

from __future__ import annotations

import ast
import re
import string
from collections.abc import Iterator

#: Global names that expose the interpreter's own namespace machinery.
_FORBIDDEN_NAMES = frozenset({"__builtins__", "__loader__", "__spec__"})

#: String methods that interpolate a format template (both resolve ``{field}``
#: with the same attribute/item syntax).
_FORMAT_METHODS = frozenset({"format", "format_map"})


def _is_dunder(name: str) -> bool:
    return len(name) > 4 and name.startswith("__") and name.endswith("__")


def _field_reaches_a_dunder(field_name: str) -> bool:
    """True when a format replacement field accesses a dunder attribute.

    Field grammar: ``key ("." attribute | "[" index "]")*``. The first segment
    names the argument/index; every later segment is an accessor, so a dunder
    there is introspection (``0.__class__`` / ``0[__class__]``). A dunder that is
    itself the argument keyword (``"{__class__}".format(__class__=x)``) is just a
    mapping key and stays allowed.
    """
    parts = re.split(r"[.\[]", field_name)
    return any(_is_dunder(part.rstrip("]")) for part in parts[1:])


def _format_field_escapes(format_string: str) -> Iterator[str]:
    """Yield every replacement field that reaches a dunder attribute.

    ``string.Formatter`` parses the replacement-field grammar (including the
    ``!conversion`` and ``:spec`` suffixes); a malformed template raises
    ``ValueError`` and yields nothing — formatting it would fail at run time,
    which is not an escape. A nested spec (``"{0:{1}}"``) is parsed too.
    """
    try:
        parsed = list(string.Formatter().parse(format_string))
    except ValueError:
        return
    for _literal, field_name, format_spec, _conversion in parsed:
        if field_name and _field_reaches_a_dunder(field_name):
            yield field_name
        if format_spec and "{" in format_spec:
            yield from _format_field_escapes(format_spec)


def _literal_format_template(node: ast.Call) -> str | None:
    """The literal template of a ``str.format`` / ``str.format_map`` call.

    Covers both shapes: ``"{0}".format(x)`` (the receiver is the constant) and
    ``str.format("{0}", x)`` (the unbound method takes the template first).
    """
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr not in _FORMAT_METHODS:
        return None
    receiver = func.value
    if isinstance(receiver, ast.Constant) and isinstance(receiver.value, str):
        return receiver.value
    if isinstance(receiver, ast.Name) and receiver.id == "str" and node.args:
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return None


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
        if isinstance(node, ast.Call):
            template = _literal_format_template(node)
            if template is not None:
                for field_name in _format_field_escapes(template):
                    raise ValueError(
                        f"{label} refused: str.format field '{field_name}' reaches a "
                        f"dunder attribute, which restricted code does not allow "
                        f"(line {node.lineno})"
                    )
        if isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES:
            raise ValueError(
                f"{label} refused: name '{node.id}' is not available in restricted code "
                f"(line {node.lineno})"
            )


__all__ = ["validate_restricted_code"]
