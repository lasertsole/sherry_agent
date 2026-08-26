"""conftest for tests/unit/future_subagent/: auto-load the agent.tools.subagent module alias."""

import sys
import types as stdlib_types
from unittest.mock import MagicMock, AsyncMock


def _setup_subagent_alias():
    if "future_subagent" in sys.modules:
        return

    for mod_name in [
        "agent", "agent.tools", "agent.core", "agent.codeact",
        "agent.checkpointer", "agent.middlewares",
        "bus", "bus.core", "type", "type.bus", "type.message",
        "pub_func", "models", "sessions", "runtime", "config",
        "plugins", "context_engine", "channels", "server", "skills",
    ]:
        if mod_name not in sys.modules:
            sys.modules[mod_name] = stdlib_types.ModuleType(mod_name)

    # Point the stubbed `agent.tools` at the real package directory so that
    # `agent.tools.subagent` resolves to the REAL library (not a stub), while
    # avoiding the heavy `agent/__init__.py` / `agent/tools/__init__.py` imports.
    from pathlib import Path
    sys.modules["agent.tools"].__path__ = [str(Path(__file__).resolve().parents[3] / "agent" / "tools")]

    # Give the stubbed `config` module the attributes that agent.tools.subagent's
    # import chain needs (e.g. `agent/tools/subagent/spawn/attachments.py` does
    # `from config import ROOT_DIR, TEMP_DIR`). Without these, importing
    # agent.tools.subagent under pytest fails because the stub is an empty module.
    cfg = sys.modules["config"]
    if not hasattr(cfg, "ROOT_DIR"):
        cfg.ROOT_DIR = Path(__file__).resolve().parents[3]
    if not hasattr(cfg, "TEMP_DIR"):
        cfg.TEMP_DIR = cfg.ROOT_DIR / "temp"

    sys.modules["agent"].tools = sys.modules["agent.tools"]
    sys.modules["agent.tools"].build_main_tools = lambda: []

    sys.modules["bus"].core = sys.modules["bus.core"]
    sys.modules["bus.core"].MessageBus = MagicMock

    sys.modules["pub_func"].build_agent_config = lambda **kw: {}

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

    if not hasattr(sys.modules["type.bus"], "InboundMessage"):
        from pydantic import BaseModel
        class _InboundMessage(BaseModel):
            channel: str = ""
            sender_id: str = ""
            chat_id: str = ""
            content: str = ""
            session_id: str = ""
            metadata: dict = {}
        sys.modules["type.bus"].InboundMessage = _InboundMessage
    sys.modules["type"].bus = sys.modules["type.bus"]

    if not hasattr(sys.modules["runtime"], "clear_all_register_sessions"):
        sys.modules["runtime"].clear_all_register_sessions = lambda: None

    # `agent.tools.subagent.delegate` does `from skills.loader import
    # get_skills_text, scan_skills` at module scope. The stubbed `skills` is an
    # empty module, so a real submodule can't be imported. Inject a stub
    # `skills.loader` module exposing configurable, deterministic functions so
    # the import chain resolves and tests can assert on injection behavior.
    _skills_loader = stdlib_types.ModuleType("skills.loader")

    def _scan_skills_stub(use_cache: bool = True) -> list[dict]:
        return [
            {"name": "web_search"},
            {"name": "code_interpreter"},
            {"name": "skill_creator"},
            {"name": "clawhub"},
        ]

    def _get_skills_text_stub(
        selected_skill_names: list[str] | None = None,
        *,
        exclude_auth_skills: bool = False,
    ) -> str:
        if not selected_skill_names:
            return ""
        names = sorted(selected_skill_names)
        if exclude_auth_skills:
            names = [n for n in names if n not in ("clawhub", "skill_creator")]
        return "<skills>\n" + "\n".join(f"  <skill name=\"{n}\"/>" for n in names) + "\n</skills>"

    _skills_loader.scan_skills = _scan_skills_stub
    _skills_loader.get_skills_text = _get_skills_text_stub
    sys.modules["skills.loader"] = _skills_loader

    import importlib
    fs = importlib.import_module("agent.tools.subagent")
    sys.modules["future_subagent"] = fs


_setup_subagent_alias()
