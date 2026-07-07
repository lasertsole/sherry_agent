import os
import shutil
import textwrap
from typing import Any
from loguru import logger
from datetime import datetime
from dotenv import load_dotenv
from langgraph.runtime import Runtime
from typing_extensions import override
from config import SESSIONS_DIR, ENV_PATH
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage, BaseMessage
from langchain.agents.middleware import after_agent, AgentState


load_dotenv(ENV_PATH, override=True)

_SUBAGENT_TODO_DONE_FUNC: str | None = os.getenv("SUBAGENT_TODO_DONE_FUNC")

class TODOManager(AgentMiddleware):
    @override
    def after_agent(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        """
        Clean up or archive the TODO file after Agent execution
        """
        master_session_id: str = state.get("master_session_id", "")
        if not master_session_id.strip():
            raise RuntimeError("IterationBudget: master_session_id is required")

        task_id: str = state.get("task_id", "")
        if not task_id.strip():
            raise RuntimeError("IterationBudget: task_id is required")

        todo_file = SESSIONS_DIR / master_session_id / "todo" / f"{task_id}.md"
        logger.info("TODOManager after_agent todo file exists: {}", todo_file)

        if not todo_file.exists():
            return None

        try:
            if _SUBAGENT_TODO_DONE_FUNC == "delete":
                # Option A: Direct deletion
                os.remove(todo_file)
                print(f"[Todo Cleaner] Deleted: {todo_file}")

            else:
                # Option B: Archive processing
                archive_dir = SESSIONS_DIR / master_session_id / "todo_archive"
                archive_dir.mkdir(parents=True, exist_ok=True)

                # Generate filename with timestamp to prevent overwrites
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                new_filename = f"{task_id}_{timestamp}.md"
                target_path = archive_dir / new_filename

                shutil.move(str(todo_file), str(target_path))
                print(f"[Todo Cleaner] Archived: {todo_file} -> {target_path}")
        except Exception as e:
            logger.warning(f"[Todo Cleaner] Failed to process todo file: {e}")

    @override
    def before_model(
        self,
        state: AgentState,
        runtime: Runtime
    ) -> dict[str, Any] | None:
        master_session_id: str = state.get("master_session_id", "")
        if not master_session_id.strip():
            raise RuntimeError("IterationBudget: master_session_id is required")

        task_id: str = state.get("task_id", "")
        if not task_id.strip():
            raise RuntimeError("IterationBudget: task_id is required")

        messages: list[BaseMessage] = state.get("messages", [])

        todo_file = SESSIONS_DIR / master_session_id / "todo" / f"{task_id}.md"
        logger.info("TODOManager before_model todo file exists: {}", todo_file)

        if not todo_file.exists():
            return None

        try:
            todo_text = todo_file.read_text(encoding="utf-8")
        except Exception:
            return None

        injection_content = textwrap.dedent(f"""\
            [SYSTEM CONTEXT - TODO LIST UPDATE]

            Here is the current status of your task plan. 
            Please refer to this information to decide your next action.

            {todo_text}
        """)

        injection_message = HumanMessage(
            content=injection_content
        )

        new_messages = messages + [injection_message]

        return {"messages": new_messages}