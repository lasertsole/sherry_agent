import os
import shutil
from datetime import datetime
from typing import Any, Literal
from config import SESSIONS_DIR
from langgraph.runtime import Runtime
from langchain.agents.middleware import after_agent, AgentState


def todo_cleaner_builder(session_id: str, task_id: str, mode: Literal["delete", "archive"] = "archive"):
    """
    创建一个 TODO 清理中间件

    Args:
        session_id: 会话 ID
        task_id: 任务 ID
        mode: 清理模式
            - "delete": 直接删除文件
            - "archive": 移动到归档文件夹并添加时间戳
    """

    @after_agent
    def todo_cleaner(state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        """
        在 Agent 执行结束后清除或归档 TODO 文件
        """
        todo_file = SESSIONS_DIR / session_id / "todo" / f"{task_id}.md"

        if not todo_file.exists():
            return None

        try:
            if mode == "delete":
                # 方案 A：直接删除
                os.remove(todo_file)
                print(f"[Todo Cleaner] 🗑️ Deleted: {todo_file}")

            elif mode == "archive":
                # 方案 B：归档处理
                archive_dir = SESSIONS_DIR / session_id / "todo_archive"
                archive_dir.mkdir(parents=True, exist_ok=True)

                # 生成带时间戳的文件名，防止重名覆盖
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                new_filename = f"{task_id}_{timestamp}.md"
                target_path = archive_dir / new_filename

                shutil.move(str(todo_file), str(target_path))
                print(f"[Todo Cleaner] 📂 Archived: {todo_file} -> {target_path}")

        except Exception as e:
            print(f"[Todo Cleaner] ❌ Failed to process todo file: {e}")

    return todo_cleaner
