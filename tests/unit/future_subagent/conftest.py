"""conftest for tests/unit/future_subagent/: auto-load the future_subagent module alias."""

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

    import importlib
    fs = importlib.import_module("future_subagent")
    sys.modules["future_subagent"] = fs


_setup_subagent_alias()
