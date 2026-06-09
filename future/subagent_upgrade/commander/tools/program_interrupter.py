import os
import signal
from pathlib import Path
from typing import Any
from pydantic import BaseModel, Field
from langchain_core.tools import BaseTool
from config import SESSIONS_DIR


class ProgramInterrupterArgs(BaseModel):
    task_id: str = Field(description="任务ID")
    session_id: str = Field(description="会话ID")


class ProgramInterrupter(BaseTool):
    name: str = "program_interrupter"
    description: str = "中断正在执行的程序"
    args_schema: type[BaseModel] = ProgramInterrupterArgs

    def __init__(self, session_id: str, task_id: str, **kwargs: Any):
        super().__init__(**kwargs)
        self._session_id = session_id
        self._task_id = task_id
        self._todo_dir = SESSIONS_DIR / session_id / "todo"
        self._state_dir = self._todo_dir / ".state"

    def _find_running_process(self) -> int | None:
        import psutil
        
        proc_name = "python"
        state_file = self._state_dir / f"{self._task_id}_execution.json"
        
        if not state_file.exists():
            return None
            
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                cmdline = proc.info.get('cmdline', [])
                if cmdline and self._task_id in ' '.join(cmdline):
                    return proc.info['pid']
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        
        return None

    def _run(self, task_id: str, session_id: str, **kwargs: Any) -> str:
        state_dir = SESSIONS_DIR / session_id / "todo" / ".state"
        state_file = state_dir / f"{task_id}_execution.json"
        
        if not state_file.exists():
            return f'{{"status": "not_found", "message": "No execution state found for task {task_id}"}}'
        
        import json
        try:
            state_data = json.loads(state_file.read_text(encoding="utf-8"))
            
            if state_data.get("status") not in ("running", "paused", "interrupted"):
                return f'{{"status": "idle", "message": "Task {task_id} is not running"}}'
            
            pid = self._find_running_process()
            if pid:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            
            state_data["status"] = "interrupted"
            state_data["timestamp"] = str(__import__('datetime').datetime.now().isoformat())
            state_file.write_text(json.dumps(state_data, indent=2, ensure_ascii=False), encoding="utf-8")
            
            return json.dumps({
                "status": "interrupted",
                "message": f"Task {task_id} has been interrupted, state saved for recovery",
                "can_resume": True,
            }, ensure_ascii=False, indent=2)
            
        except Exception as e:
            return json.dumps({
                "status": "error",
                "message": str(e),
            }, ensure_ascii=False, indent=2)

    async def _arun(self, task_id: str, session_id: str, **kwargs: Any) -> str:
        return self._run(task_id, session_id, **kwargs)


import json


def build_program_interrupter(session_id: str, task_id: str) -> ProgramInterrupter:
    tool = ProgramInterrupter(session_id, task_id)
    tool.handle_tool_error = True
    return tool