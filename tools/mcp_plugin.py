from __future__ import annotations

import sys
import json
from pathlib import Path
from loguru import logger
from pub_func import run_async
from langchain_core.tools import BaseTool

_HERE_DIR = Path(__file__).resolve().parent  # tools/
_PROJECT_ROOT = _HERE_DIR.parent             # EMA_AI_agent/
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config import PLUGINS_PATH

def _load_config() -> dict:
    """Load MCP server configs from JSON, resolve special tokens."""
    config_path = PLUGINS_PATH / "mcp_server/config.json"

    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("{}", encoding="utf-8")
        return {}

    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)

    # -- Resolve placeholders ---
    # "$sys.executable"  → 当前虚拟环境的 Python 解释器路径
    # "$here/"          → JSON 文件所在目录（plugins/mcp_server）
    here_str = f"{PLUGINS_PATH.as_posix()}/mcp_server/"

    for name, server in config.items():
        if server.get("command") == "$sys.executable":
            server["command"] = sys.executable

        args = server.get("args")
        if args:
            server["args"] = [
                a.replace("$here/", here_str) if isinstance(a, str) else a
                for a in args
            ]

    return config


def build_mcp_tools(session_id: str | None = None) -> list[BaseTool]:
    from langchain_mcp_adapters.client import MultiServerMCPClient
    servers = _load_config()
    client = MultiServerMCPClient(servers)
    tools: list[BaseTool] = run_async(client.get_tools())

    return tools