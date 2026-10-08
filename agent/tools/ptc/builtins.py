"""Restricted builtins for the PTC child process.

The child process executes model-authored Python. To keep that code from
touching the filesystem directly, the wrapper script replaces the module
``__builtins__`` with :data:`RESTRICTED_BUILTIN_NAMES` before ``exec``-ing the
user script. The dangerous builtins (``open``, ``exec``, ``eval``,
``compile``, ``__import__``, ``globals``, ``locals``, ``input``,
``breakpoint``) are deliberately absent.

Imports are not removed wholesale — the import bytecode resolves
``__import__`` from the module builtins, so an entirely absent
``__import__`` would break ``from sherry_tools import read_file`` as well.
Instead the wrapper installs a guarded ``__import__`` that only permits
:data:`ALLOWED_IMPORT_MODULES` (and submodules thereof). This is the minimum
needed for PTC to function; ``import os`` / ``import subprocess`` raise
``ImportError``.
"""

import builtins

# Names exposed to the child script. Mirrors python_repl.py's restricted set
# so the two execution surfaces stay consistent. Intentionally excludes:
# open, exec, eval, compile, __import__, globals, locals, input, breakpoint,
# exit, quit, help, memoryview, object, super, classmethod, staticmethod.
RESTRICTED_BUILTIN_NAMES: tuple[str, ...] = (
    "True",
    "False",
    "None",
    "int",
    "float",
    "str",
    "bool",
    "list",
    "dict",
    "tuple",
    "set",
    "len",
    "range",
    "enumerate",
    "zip",
    "map",
    "filter",
    "reversed",
    "sorted",
    "any",
    "all",
    "sum",
    "min",
    "max",
    "abs",
    "round",
    "pow",
    "print",
    "type",
    "isinstance",
    "hasattr",
    "getattr",
    "dir",
    "vars",
    "id",
    "repr",
    "Exception",
    "ValueError",
    "TypeError",
    "KeyError",
    "IndexError",
    "AttributeError",
    "RuntimeError",
    "ZeroDivisionError",
)

# Modules the guarded ``__import__`` allows in the child. ``sherry_tools`` is
# the generated RPC proxy; the rest are pure-computation stdlib modules.
ALLOWED_IMPORT_MODULES: frozenset[str] = frozenset(
    {
        "sherry_tools",
        "json",
        "re",
        "math",
        "time",
        "csv",
        "datetime",
        "collections",
        "itertools",
        "functools",
        "statistics",
        "string",
        "textwrap",
        "decimal",
        "random",
        "fractions",
        "heapq",
        "bisect",
    }
)

# Concrete {name: object} mapping for tests and introspection. Built from the
# live ``builtins`` module; only names that actually exist are included.
RESTRICTED_BUILTINS: dict[str, object] = {
    name: getattr(builtins, name) for name in RESTRICTED_BUILTIN_NAMES
}


def render_restricted_builtins() -> str:
    """Render the restricted builtins as a Python dict literal.

    Used by the generated child wrapper, which runs with the real builtins so
    each bare name resolves. Rendering ``repr()`` of the callables would emit
    unusable ``<built-in function len>`` tokens, hence the name-to-name form.
    """
    entries = ", ".join(f'"{name}": {name}' for name in RESTRICTED_BUILTIN_NAMES)
    return "{" + entries + "}"


def render_allowed_modules() -> str:
    """Render the import allowlist as a Python frozenset literal."""
    entries = ", ".join(f'"{name}"' for name in sorted(ALLOWED_IMPORT_MODULES))
    return "frozenset({" + entries + "})"
