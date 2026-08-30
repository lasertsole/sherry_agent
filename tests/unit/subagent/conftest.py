"""conftest for tests/unit/subagent/: auto-load the agent.tools.subagent module alias."""

import importlib
import importlib.util
import sys
import types as stdlib_types
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

_ROOT = Path(__file__).resolve().parents[3]

_SUBMODULE_LOADED = False

_real_init_cache: dict[str, object] = {}


def _real_init_attr(pkg: str, name: str):
    """Resolve ``name`` against the REAL package ``__init__`` of stubbed ``pkg``.

    Loads the real ``__init__.py`` once under a private name (so relative
    imports inside it keep working) and returns the requested attribute. This
    makes ``from <stub> import <name>`` behave like the real package for names
    that are plain re-exports (functions/classes) rather than same-named
    submodules — e.g. ``from context_engine import get_db`` or
    ``from pub_func import string_to_unique_int`` (which lives in
    ``string_to_int.py`` since the legacy hash modules were merged).
    """
    mod = _real_init_cache.get(pkg)
    if mod is None:
        pkg_dir = _ROOT.joinpath(*pkg.split("."))
        init_file = pkg_dir / "__init__.py"
        if not init_file.is_file():
            # e.g. ``plugins/`` is a plain namespace directory (no __init__.py).
            # Raise AttributeError — NOT FileNotFoundError — so ``hasattr(stub,
            # name)`` cleanly returns False and ``from pkg import name``
            # converts to a normal ImportError instead of an escaping
            # FileNotFoundError.
            raise AttributeError(f"stub module {pkg!r} has no attribute {name!r}")
        spec = importlib.util.spec_from_file_location(
            f"_real_init_{pkg.replace('.', '_')}",
            init_file,
            submodule_search_locations=[str(pkg_dir)],
        )
        if spec is None or spec.loader is None:
            raise AttributeError(f"stub module {pkg!r} has no attribute {name!r}")
        mod = importlib.util.module_from_spec(spec)
        # Register BEFORE exec: relative imports inside the real __init__
        # (e.g. channels/__init__.py -> `from .base import BaseChannel`)
        # look up the parent package in sys.modules.
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        _real_init_cache[pkg] = mod
    if not hasattr(mod, name):
        raise AttributeError(f"stub module {pkg!r} has no attribute {name!r}")
    return getattr(mod, name)


def _make_stub(mod_name: str) -> stdlib_types.ModuleType:
    """Create a lightweight stub that still resolves real names.

    Heavy package ``__init__`` files (``agent``, ``pub_func`` -> cv2 via
    ``media/*``, ``context_engine`` store, ...) are stubbed to keep the
    subagent import chain isolated. But ``tests/unit/subagent`` sorts
    alphabetically before ``tests/unit/test_*.py``, so this conftest would
    poison ``sys.modules`` for the whole run. To keep the other tests
    working, stubs whose real package directory exists get:

    - a ``__path__``, so ``import <stub>.<submodule>`` loads the real code;
    - a module-level ``__getattr__`` (PEP 562) that resolves missing names
      the way the real package would: first via a same-named submodule
      (``from pub_func import run_async`` -> ``pub_func/run_async.py``),
      otherwise via the real package init (``from channels import
      BaseChannel`` -> ``channels/__init__.py`` re-export).
    """
    stub = stdlib_types.ModuleType(mod_name)
    real_dir = _ROOT.joinpath(*mod_name.split("."))
    if not real_dir.is_dir():
        return stub
    stub.__path__ = [str(real_dir)]

    def _getattr(name: str, _pkg: str = mod_name):
        if name.startswith("__") and name.endswith("__"):
            # Dunders must resolve to a plain AttributeError. ``inspect.getmodule``
            # (used by torch during import) does ``hasattr(module, '__file__')``
            # over EVERY entry in sys.modules — routing that through the
            # importlib fallbacks below raised FileNotFoundError (which hasattr
            # does not catch) and crashed the torch import mid-init.
            raise AttributeError(f"module {_pkg!r} has no attribute {name!r}")
        try:
            sub = importlib.import_module(f"{_pkg}.{name}")
        except ModuleNotFoundError as exc:
            if exc.name != f"{_pkg}.{name}":
                raise  # a deeper import failed — surface the real error
            return _real_init_attr(_pkg, name)
        reexport = getattr(sub, name, None)
        return reexport if reexport is not None else sub

    stub.__getattr__ = _getattr
    return stub


def _setup_subagent_alias():
    global _SUBMODULE_LOADED
    if _SUBMODULE_LOADED:
        return

    for mod_name in [
        "agent", "agent.tools", "agent.core",
        "agent.checkpointer", "agent.middlewares",
        "pub_func", "models", "sessions", "runtime",
        "plugins", "context_engine", "channels", "skills",
    ]:
        if mod_name not in sys.modules:
            sys.modules[mod_name] = _make_stub(mod_name)

    # Light packages are NOT stubbed and load for real on demand: `config`
    # (path/num constants; chains need ENV_PATH, SRC_DIR, AUTO_SKILLS_DIR,
    # PLUGINS_PATH, ROOT_DIR, TEMP_DIR), `type` (bus dataclasses /
    # message models), `server` (empty __init__), `bus` (async queues).

    # Bind the runtime package names that real runtime submodules re-import
    # from the package (runtime/state_register.py does
    # `from runtime import Register`, middlewares do
    # `from runtime import state_register_mem`). Register MUST be bound
    # before importing state_register. `clear_all_register_sessions` stays a
    # no-op so subagent runs can't wipe session state mid-test.
    import runtime.core  # noqa: E402
    sys.modules["runtime"].Register = sys.modules["runtime.core"].Register
    import runtime.state_register  # noqa: E402
    sys.modules["runtime"].state_register_mem = sys.modules[
        "runtime.state_register"
    ].state_register_mem
    sys.modules["runtime"].clear_all_register_sessions = lambda: None

    sys.modules["agent"].tools = sys.modules["agent.tools"]
    sys.modules["agent.tools"].build_main_tools = lambda: []

    # NOTE: signature must accept positional args — server/service/messages.py
    # get_pending_interrupt() calls build_agent_config(session_id).
    sys.modules["pub_func"].build_agent_config = lambda *a, **kw: {}

    sys.modules["models"].build_main_llm = lambda: None
    sys.modules["models"].build_auxiliary_llm = lambda: None

    sys.modules["agent.checkpointer"].build_async_sqlite_checkpointer = AsyncMock(
        return_value=stdlib_types.SimpleNamespace(setup=AsyncMock())
    )
    sys.modules["agent"].checkpointer = sys.modules["agent.checkpointer"]

    for mw_name in ["IterationBudget", "ToolGuardrails", "ToolCallNormalize", "Summarization", "HeartbeatStaleness"]:
        setattr(sys.modules["agent.middlewares"], mw_name, lambda *a, **kw: None)
    sys.modules["agent"].middlewares = sys.modules["agent.middlewares"]

    sys.modules["agent.core"].StateSchema = dict

    # `agent.tools.subagent.delegate` does `from skills.loader import
    # get_skills_text, scan_skills` at module scope. The stubbed `skills` is an
    # empty module, so a real submodule can't be imported. Inject a stub
    # `skills.loader` module exposing configurable, deterministic functions so
    # the import chain resolves and tests can assert on injection behavior.
    _skills_loader = stdlib_types.ModuleType("skills.loader")

    # Scope data mirrors the real frontmatter: clawhub/skill_creator are
    # `scope: main_only`, so delegate scope-validation drops them for
    # subagent callers (replaces the old hardcoded _AUTH_SKILLS mechanism).
    _SKILL_SCOPES = {
        "web_search": "all",
        "code_interpreter": "all",
        "skill_creator": "main_only",
        "clawhub": "main_only",
    }

    def _scan_skills_stub(use_cache: bool = True) -> list[dict]:
        return [
            {"name": "web_search", "scope": "all"},
            {"name": "code_interpreter", "scope": "all"},
            {"name": "skill_creator", "scope": "main_only"},
            {"name": "clawhub", "scope": "main_only"},
        ]

    def _get_skills_text_stub(
        selected_skill_names: list[str] | None = None,
        *,
        caller_scope: str = "main",
    ) -> str:
        if not selected_skill_names:
            return ""
        names = [
            n
            for n in sorted(selected_skill_names)
            if not (caller_scope == "subagent" and _SKILL_SCOPES.get(n) == "main_only")
        ]
        return "<skills>\n" + "\n".join(f"  <skill name=\"{n}\"/>" for n in names) + "\n</skills>"

    _skills_loader.scan_skills = _scan_skills_stub
    _skills_loader.get_skills_text = _get_skills_text_stub

    # server/trigger/http/skills.py additionally imports `parse_frontmatter`
    # from skills.loader — bind the real implementation (pure text parsing
    # with only stdlib/yaml/config imports).
    _loader_spec = importlib.util.spec_from_file_location(
        "_skills_loader_real", _ROOT / "skills" / "loader.py"
    )
    _real_loader = importlib.util.module_from_spec(_loader_spec)
    _loader_spec.loader.exec_module(_real_loader)
    _skills_loader.parse_frontmatter = _real_loader.parse_frontmatter

    sys.modules["skills.loader"] = _skills_loader

    importlib.import_module("agent.tools.subagent")

    _SUBMODULE_LOADED = True


_setup_subagent_alias()
