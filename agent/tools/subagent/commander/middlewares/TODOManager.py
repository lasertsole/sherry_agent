import os
import shutil
import textwrap
from typing import Any
from pathlib import Path
from loguru import logger
from datetime import datetime
from dotenv import load_dotenv
from langgraph.runtime import Runtime
from typing_extensions import override
from config import SESSIONS_DIR, ENV_PATH
from langchain.agents.middleware import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage, BaseMessage


load_dotenv(ENV_PATH, override=True)

_SUBAGENT_TODO_DONE_FUNC: str | None = os.getenv("SUBAGENT_TODO_DONE_FUNC")

_GOAL_SECTION_HEADER = "## Original Goal"
_GOAL_SECTION_END = "## /Original Goal"
_TASK_SECTION_HEADER = "## Task Plan"


def _todo_file_path(master_session_id: str, task_id: str) -> Path:
    return SESSIONS_DIR / master_session_id / "todo" / f"{task_id}.md"


def persist_goal(master_session_id: str, task_id: str, goal_text: str) -> None:
    """Persist the original goal into the TODO file.

    The file uses a structured format with two sections:
      ## Original Goal      (managed by persist_goal / TODOManager)
      ## /Original Goal
      ## Task Plan           (managed by the commander via todo_writer)

    If the file already exists, only the goal section is updated without
    touching the task plan section.  If the file does not exist, a new one
    is created with the goal section and an empty task plan section.
    """
    path = _todo_file_path(master_session_id, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        existing = path.read_text(encoding="utf-8")
        task_part = _extract_task_section(existing)
        new_content = (
            f"{_GOAL_SECTION_HEADER}\n\n{goal_text}\n\n{_GOAL_SECTION_END}\n\n"
            f"{_TASK_SECTION_HEADER}\n\n{task_part}"
        )
    else:
        new_content = (
            f"{_GOAL_SECTION_HEADER}\n\n{goal_text}\n\n{_GOAL_SECTION_END}\n\n"
            f"{_TASK_SECTION_HEADER}\n\n"
        )

    path.write_text(new_content, encoding="utf-8")


def load_goal(master_session_id: str, task_id: str) -> str | None:
    """Load the original goal from the TODO file."""
    path = _todo_file_path(master_session_id, task_id)
    if not path.exists():
        return None
    try:
        return _extract_goal_section(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _extract_goal_section(content: str) -> str | None:
    """Extract goal text between ## Original Goal and ## /Original Goal."""
    start_marker = f"{_GOAL_SECTION_HEADER}\n"
    end_marker = f"\n{_GOAL_SECTION_END}"
    start_idx = content.find(start_marker)
    if start_idx < 0:
        return None
    end_idx = content.find(end_marker, start_idx)
    if end_idx < 0:
        return None
    return content[start_idx + len(start_marker):end_idx].strip() or None


def _extract_task_section(content: str) -> str:
    """Extract task plan text after ## Task Plan."""
    marker = f"{_TASK_SECTION_HEADER}\n"
    idx = content.find(marker)
    if idx < 0:
        return ""
    return content[idx + len(marker):]


class TODOManager(AgentMiddleware):

    @staticmethod
    def _goal_in_messages(goal_text: str, messages: list[BaseMessage]) -> bool:
        """Check if the goal text already exists verbatim in a non-summary HumanMessage."""
        for m in messages:
            if isinstance(m, HumanMessage) and not getattr(m, "additional_kwargs", {}).get("lc_source"):
                content = m.content if isinstance(m.content, str) else ""
                if goal_text.strip() and goal_text.strip() in content:
                    return True
        return False

    @override
    def after_agent(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        """Clean up or archive the TODO file after Agent execution."""
        master_session_id: str = state.get("master_session_id", "")
        if not master_session_id.strip():
            raise RuntimeError("TODOManager: master_session_id is required")

        task_id: str = state.get("task_id", "")
        if not task_id.strip():
            raise RuntimeError("TODOManager: task_id is required")

        todo_file = _todo_file_path(master_session_id, task_id)
        logger.debug("TODOManager after_agent todo file exists: {}", todo_file)

        if not todo_file.exists():
            return None

        try:
            if _SUBAGENT_TODO_DONE_FUNC == "delete":
                os.remove(todo_file)
                logger.debug("[Todo Cleaner] Deleted: {}", todo_file)
            else:
                archive_dir = SESSIONS_DIR / master_session_id / "todo_archive"
                archive_dir.mkdir(parents=True, exist_ok=True)

                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                new_filename = f"{task_id}_{timestamp}.md"
                target_path = archive_dir / new_filename

                shutil.move(str(todo_file), str(target_path))
                logger.debug("[Todo Cleaner] Archived: {} -> {}", todo_file, target_path)
        except Exception as e:
            logger.warning("[Todo Cleaner] Failed to process todo file: {}", e)

    @override
    def before_model(
        self,
        state: AgentState,
        runtime: Runtime
    ) -> dict[str, Any] | None:
        master_session_id: str = state.get("master_session_id", "")
        if not master_session_id.strip():
            raise RuntimeError("TODOManager: master_session_id is required")

        task_id: str = state.get("task_id", "")
        if not task_id.strip():
            raise RuntimeError("TODOManager: task_id is required")

        messages: list[BaseMessage] = state.get("messages", [])

        goal_text = load_goal(master_session_id, task_id)
        if goal_text is None:
            for m in messages:
                if isinstance(m, HumanMessage) and not getattr(m, "additional_kwargs", {}).get("lc_source"):
                    goal_text = m.content if isinstance(m.content, str) else str(m.content)
                    persist_goal(master_session_id, task_id, goal_text)
                    logger.debug("TODOManager: persisted original goal for task {}", task_id)
                    break

        parts: list[str] = []

        if goal_text and not self._goal_in_messages(goal_text, messages):
            parts.append(textwrap.dedent(f"""\
                [SYSTEM CONTEXT - ORIGINAL GOAL]

                This is the original task you were assigned. This context is re-injected
                every turn so you never lose sight of the primary objective, even after
                context compression.

                {goal_text}
            """))

        todo_file = _todo_file_path(master_session_id, task_id)
        task_text = ""
        if todo_file.exists():
            try:
                task_text = _extract_task_section(todo_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        if task_text.strip():
            parts.append(textwrap.dedent(f"""\
                [SYSTEM CONTEXT - TODO LIST UPDATE]

                Here is the current status of your task plan.
                Please refer to this information to decide your next action.

                {task_text}
            """))

        if not parts:
            return None

        injection_content = "\n\n".join(parts)
        injection_message = HumanMessage(content=injection_content)

        new_messages = messages + [injection_message]

        return {"messages": new_messages}
