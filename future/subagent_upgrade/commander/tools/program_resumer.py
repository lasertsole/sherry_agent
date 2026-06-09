import json
from pathlib import Path
from typing import Any
from pydantic import BaseModel, Field
from langchain_core.tools import BaseTool
from config import SESSIONS_DIR


class ProgramResumerArgs(BaseModel):
    task_id: str = Field(description="任务ID")
    session_id: str = Field(description="会话ID")
    strategy: str = Field(
        description="恢复策略: continue(继续执行) | fast_retry(快速重试) | gentle_retry(温和重试) | full_reset(完全重置)",
        default="continue"
    )
    max_retries: int = Field(description="最大重试次数", default=3)


class ProgramResumer(BaseTool):
    name: str = "program_resumer"
    description: str = "恢复中断的程序执行"
    args_schema: type[BaseModel] = ProgramResumerArgs

    def __init__(self, session_id: str, task_id: str, **kwargs: Any):
        super().__init__(**kwargs)
        self._session_id = session_id
        self._task_id = task_id
        self._todo_dir = SESSIONS_DIR / session_id / "todo"

    def _run(self, task_id: str, session_id: str, strategy: str = "continue", max_retries: int = 3, **kwargs: Any) -> str:
        state_dir = SESSIONS_DIR / session_id / "todo" / ".state"
        state_file = state_dir / f"{task_id}_execution.json"
        cache_dir = SESSIONS_DIR / session_id / "todo" / ".cache"

        if not state_file.exists():
            return json.dumps({
                "status": "error",
                "message": f"No execution state found for task {task_id}",
            }, ensure_ascii=False, indent=2)

        try:
            state_data = json.loads(state_file.read_text(encoding="utf-8"))

            if strategy == "full_reset":
                if state_file.exists():
                    state_file.unlink()
                for f in state_dir.glob(f"{task_id}_checkpoint_*.json"):
                    f.unlink()
                for f in cache_dir.glob("*.json"):
                    f.unlink()

                return json.dumps({
                    "status": "reset",
                    "message": f"Task {task_id} has been fully reset, all caches and states cleared",
                    "can_resume": True,
                }, ensure_ascii=False, indent=2)

            elif strategy in ("fast_retry", "gentle_retry"):
                failed_tasks = state_data.get("failed_tasks", [])
                if not failed_tasks:
                    return json.dumps({
                        "status": "no_failed_tasks",
                        "message": "No failed tasks to retry",
                    }, ensure_ascii=False, indent=2)

                state_data["status"] = "running"
                state_data["failed_tasks"] = []
                state_data["retry_strategy"] = strategy
                state_data["max_retries"] = max_retries
                state_file.write_text(json.dumps(state_data, indent=2, ensure_ascii=False), encoding="utf-8")

                return json.dumps({
                    "status": "retrying",
                    "strategy": strategy,
                    "failed_tasks": failed_tasks,
                    "message": f"Will retry {len(failed_tasks)} failed tasks with strategy: {strategy}",
                    "can_resume": True,
                }, ensure_ascii=False, indent=2)

            elif strategy == "continue":
                current_stage = state_data.get("current_stage", "unknown")
                state_data["status"] = "running"
                state_file.write_text(json.dumps(state_data, indent=2, ensure_ascii=False), encoding="utf-8")

                return json.dumps({
                    "status": "resumed",
                    "current_stage": current_stage,
                    "message": f"Resuming from stage: {current_stage}",
                    "can_resume": True,
                }, ensure_ascii=False, indent=2)

            else:
                return json.dumps({
                    "status": "error",
                    "message": f"Unknown strategy: {strategy}",
                }, ensure_ascii=False, indent=2)

        except Exception as e:
            return json.dumps({
                "status": "error",
                "message": str(e),
            }, ensure_ascii=False, indent=2)

    async def _arun(self, task_id: str, session_id: str, strategy: str = "continue", max_retries: int = 3, **kwargs: Any) -> str:
        return self._run(task_id, session_id, strategy, max_retries, **kwargs)


def build_program_resumer(session_id: str, task_id: str) -> ProgramResumer:
    tool = ProgramResumer(session_id, task_id)
    tool.handle_tool_error = True
    return tool