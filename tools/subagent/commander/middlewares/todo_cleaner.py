import os
import shutil
from loguru import logger
from datetime import datetime
from typing import Any, Literal
from config import SESSIONS_DIR
from langgraph.runtime import Runtime
from langchain.agents.middleware import after_agent, AgentState


def todo_cleaner_builder(session_id: str, task_id: str, mode: Literal["delete", "archive"] = "archive"):
    """
    Create a TODO cleanup middleware

    Args:
        session_id: Session ID
        task_id: Task ID
        mode: Cleanup mode
            - "delete": Directly delete the file
            - "archive": Move to archive folder with timestamp
    """

    @after_agent
    def todo_cleaner(state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        """
        Clean up or archive the TODO file after Agent execution
        """
        todo_file = SESSIONS_DIR / session_id / "todo" / f"{task_id}.md"

        if not todo_file.exists():
            return None

        try:
            if mode == "delete":
                # Option A: Direct deletion
                os.remove(todo_file)
                print(f"[Todo Cleaner] Deleted: {todo_file}")

            elif mode == "archive":
                # Option B: Archive processing
                archive_dir = SESSIONS_DIR / session_id / "todo_archive"
                archive_dir.mkdir(parents=True, exist_ok=True)

                # Generate filename with timestamp to prevent overwrites
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                new_filename = f"{task_id}_{timestamp}.md"
                target_path = archive_dir / new_filename

                shutil.move(str(todo_file), str(target_path))
                print(f"[Todo Cleaner] Archived: {todo_file} -> {target_path}")

        except Exception as e:
            logger.warning(f"[Todo Cleaner] Failed to process todo file: {e}")

    return todo_cleaner
