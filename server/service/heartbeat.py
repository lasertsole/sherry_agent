import json
from typing import Any
from pathlib import Path
from loguru import logger
from config import PLUGINS_PATH
from config.path import HEARTBEAT_PATH
from models import build_main_llm
from type.bus import OutboundMessage
from langchain.agents import create_agent
from workspace import CORE_SYSTEM_FILE_NAMES
from workspace.file_sync import ensure_workspace_system_files
from channels import BaseChannel, channel_manager
from langgraph.graph.state import CompiledStateGraph
from runtime import relation_register
from workspace.prompt_builder import build_system_prompt
from langchain_core.messages import SystemMessage, BaseMessage, HumanMessage
from agent.tools import build_python_repl_tool, build_read_file_tool, build_write_file_tool
from skills.builtin.core.heartbeat.scripts import move_task_to_completed, list_active_tasks

tools = [build_python_repl_tool(), build_read_file_tool(), build_write_file_tool()]

# The browser WebSocket client connects as `session_id=default`
# (client/app/composables/ws.ts), so pushes target that session so the UI
# refreshes live when a heartbeat execution completes.
HEARTBEAT_WS_SESSION_ID: str = "default"


async def push_heartbeat_updated() -> None:
    """Push a `heartbeat:updated` event over WebSocket so the browser UI can
    refresh the heartbeat file (especially `## Completed`) live.

    The push is best-effort: if no browser is connected, or the file read
    fails, we simply skip without breaking the execution flow.
    """
    try:
        websocket = relation_register.get_websocket_by_session_id(HEARTBEAT_WS_SESSION_ID)
        if websocket is None:
            return
        content: dict[str, str] = read_heartbeat_file()
        res: dict[str, Any] = {"event": "heartbeat:updated", "content": content}
        await websocket.send_text(json.dumps(res))
    except Exception as e:
        logger.warning("Failed to push heartbeat:updated: {}", e)


async def push_heartbeat_notification(result_content: str) -> None:
    """Push a `notification` WS event after a heartbeat task completes so the
    browser's notification bell/dialog is updated live.

    Mirrors the subagent notification payload shape
    (server/trigger/subagent/core.py): `{"event": "notification", "content": ...}`.
    The content is prefixed with `heartbeat:` so the client can attribute the
    source. Best-effort: failures are logged and never break the flow.
    """
    try:
        websocket = relation_register.get_websocket_by_session_id(HEARTBEAT_WS_SESSION_ID)
        if websocket is None:
            return
        res: dict[str, Any] = {"event": "notification", "content": f"heartbeat: {result_content}"}
        await websocket.send_text(json.dumps(res))
    except Exception as e:
        logger.warning("Failed to push heartbeat notification: {}", e)


async def process_heartbeat_task(task: str) -> str:
    try:
        # Lazy-ensure the core persona files exist before building the prompt.
        ensure_workspace_system_files()

        # Get graph-memory system prompt
        main_llm = build_main_llm()  # Create a fresh LLM instance for the current event loop

        agent: CompiledStateGraph = create_agent(
            model=main_llm,
            tools=tools,
        )

        messages: list[BaseMessage] = [
            SystemMessage(content=build_system_prompt(selected_file_names=CORE_SYSTEM_FILE_NAMES)),
            HumanMessage(content=task),
        ]
        result: dict[str, Any] = agent.invoke(input={"messages": messages})
        res_messages = result["messages"]

        agent_res: str = res_messages[-1].content

        # Closed loop: once the heartbeat task has been executed, move any active
        # task line matching this run's `task` (or, as a fallback, every active
        # task line) into `## Completed`, then push a WS event so the browser's
        # Completed section updates live. `move_task_to_completed` is a substring
        # match, so passing the full active line matches itself.
        await _mark_executed_tasks_completed(task)
        await push_heartbeat_updated()
        # Notify the bell: a heartbeat task just completed.
        await push_heartbeat_notification(agent_res)

        return agent_res
    except Exception as e:
        logger.exception(e)
        return f"Error occurred: {e}"


async def _mark_executed_tasks_completed(task: str) -> None:
    """Move executed heartbeat task line(s) from Active Tasks to Completed.

    Defensive: if the exact task cannot be matched (task text may have drifted
    from the stored line), fall back to moving all remaining active task lines so
    the list does not accumulate stale entries. Any failure is logged and ignored
    so it never breaks the notification flow.
    """
    try:
        did_match: bool = False
        try:
            move_task_to_completed(task)
            did_match = True
        except ValueError as e:
            logger.info("No exact match for task '{}', moving all active tasks: {}", task, e)

        if not did_match:
            for line in list_active_tasks():
                try:
                    move_task_to_completed(line)
                except ValueError as e:
                    logger.warning("Failed to move active task '{}': {}", line, e)
    except Exception as e:
        logger.warning("Failed to mark heartbeat tasks completed: {}", e)


async def process_heartbeat_notify(agent_res: str) -> None:
    channels_json: Path = PLUGINS_PATH / "channels/config.json"
    res: dict[str, str] = {}

    if channels_json.exists():
        channels_configs: dict[str, Any] = json.loads(channels_json.read_text())
        for name, config in channels_configs.items():
            if not config.get("heartbeat", False):
                continue
            # The `receiver` (default chat_id) now lives in each plugin's own
            # config file (plugins/channels/<name>/config.json), not the root block.
            plugin_cfg: Path = PLUGINS_PATH / "channels" / name / "config.json"
            receiver: str = ""
            if plugin_cfg.exists():
                try:
                    receiver = json.loads(plugin_cfg.read_text()).get("receiver", "")
                except Exception as e:
                    logger.warning("Failed to read {}: {}", plugin_cfg, e)
            elif config.get("receiver", False):
                # Fallback: still honor a receiver defined in the root block.
                receiver = config["receiver"]
            if receiver:
                res[name] = receiver

    for name, receiver in res.items():
        channel: BaseChannel = channel_manager.get_channel(name)
        if channel:
            await channel.send(OutboundMessage(channel=name, chat_id=receiver, content=agent_res))


# The heartbeat file lives directly at workspace/HEARTBEAT.md (NOT under memory/).
# It holds pending tasks for the heartbeat scheduled service and can legitimately
# grow larger than the memory files, so we allow a generous content cap.
# However the budget below applies ONLY to the task text bodies: the three
# structural headings (`# Heartbeat Tasks`, `## Active Tasks`, `## Completed`)
# do NOT count toward it (see heartbeat_content_length).
HEARTBEAT_FILE_NAME: str = "HEARTBEAT.md"
HEARTBEAT_MAX_CONTENT_LENGTH: int = 2000


def heartbeat_content_length(content: str) -> int:
    """Count only the task text in HEARTBEAT.md.

    Excluded (structural markdown, not user task content):
      - the H1 title `# Heartbeat Tasks`
      - the H2 section headings `## Active Tasks` / `## Completed`
      - blank lines
      - the `- ` list-item marker prefixes

    Mirrors the client-side counter in HeartbeatDialog.vue (totalLength).
    """
    total = 0
    for line in content.splitlines():
        stripped = line.strip()
        # Skip blank lines, the H1 title, and the two H2 section headings.
        if not stripped or stripped.startswith("#"):
            continue
        # Task list item: count only the text after the `- ` marker.
        if stripped.startswith("- "):
            total += len(stripped[2:].strip())
        else:
            # Any other non-empty content line (e.g. wrapped text) also counts.
            total += len(stripped)
    return total


def read_heartbeat_file() -> dict[str, str]:
    """Read the heartbeat file (workspace/HEARTBEAT.md)."""
    if HEARTBEAT_PATH.exists():
        with open(HEARTBEAT_PATH, "r", encoding="utf-8") as file:
            return {HEARTBEAT_FILE_NAME: file.read()}

    return {}


def write_heartbeat_file(file_to_content: dict[str, str]) -> None:
    """Write the heartbeat file (only the provided file, leave others unchanged)."""
    for file_name, content in file_to_content.items():
        if file_name != HEARTBEAT_FILE_NAME:
            raise ValueError(f"Invalid heartbeat file name: {file_name}")
        elif not isinstance(content, str):
            raise ValueError(f"Invalid content type for heartbeat file: {file_name}")
        elif len(content.strip()) == 0:
            raise ValueError(f"Content is empty for heartbeat file: {file_name}")
        elif heartbeat_content_length(content) > HEARTBEAT_MAX_CONTENT_LENGTH:
            raise ValueError(f"Content too long for heartbeat file: {file_name}")

        with open(HEARTBEAT_PATH, "w", encoding="utf-8") as file:
            file.write(content)
