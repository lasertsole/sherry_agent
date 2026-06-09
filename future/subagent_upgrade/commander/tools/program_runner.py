import asyncio
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from pydantic import BaseModel, Field
from langchain_core.tools import BaseTool
from config import SESSIONS_DIR


class ProgramRunnerArgs(BaseModel):
    task_id: str = Field(description="任务ID")
    session_id: str = Field(description="会话ID")
    timeout_mins: int = Field(description="程序总超时时间(分钟)", default=30, ge=5, le=120)


class ProgramRunner(BaseTool):
    name: str = "program_runner"
    description: str = "执行生成的Python程序并解析输出"
    args_schema: type[BaseModel] = ProgramRunnerArgs

    def __init__(self, session_id: str, task_id: str, **kwargs: Any):
        super().__init__(**kwargs)
        self._session_id = session_id
        self._task_id = task_id
        self._todo_dir = SESSIONS_DIR / session_id / "todo"

    def _parse_output(self, output: str) -> dict[str, Any]:
        completed = []
        failed = []
        errors = {}
        stage = "unknown"
        status = "completed"

        success_pattern = re.compile(r"SUCCESS: (\w+)", re.MULTILINE)
        failed_pattern = re.compile(r"FAILED: (\w+)(?: - (.+))?", re.MULTILINE)
        cached_pattern = re.compile(r"CACHED: (\w+)", re.MULTILINE)
        completed_pattern = re.compile(r"COMPLETED: (\w+)", re.MULTILINE)
        interrupted_pattern = re.compile(r"INTERRUPTED:", re.MULTILINE)
        all_completed_pattern = re.compile(r"ALL_TASKS_COMPLETED", re.MULTILINE)
        tasks_failed_pattern = re.compile(r"TASKS_FAILED: (.+)", re.MULTILINE)

        for match in success_pattern.finditer(output):
            label = match.group(1)
            if label not in completed:
                completed.append(label)

        for match in failed_pattern.finditer(output):
            label = match.group(1)
            error_msg = match.group(2) if match.group(2) else "unknown"
            if label not in failed:
                failed.append(label)
            errors[label] = error_msg

        for match in cached_pattern.finditer(output):
            label = match.group(1)
            if label not in completed:
                completed.append(label)

        for match in completed_pattern.finditer(output):
            label = match.group(1)
            if label not in completed:
                completed.append(label)

        if interrupted_pattern.search(output):
            status = "interrupted"
        elif all_completed_pattern.search(output):
            status = "completed"
        elif tasks_failed_pattern.search(output):
            status = "failed"
        elif failed:
            status = "failed"

        return {
            "status": status,
            "completed_tasks": completed,
            "failed_tasks": failed,
            "errors": errors,
            "raw_output": output,
        }

    def _determine_strategy(self, result: dict[str, Any]) -> tuple[str | None, str]:
        failed_tasks = result.get("failed_tasks", [])
        errors = result.get("errors", {})

        if not failed_tasks:
            return None, "All tasks completed successfully"

        if len(failed_tasks) >= 3:
            return "full_reset", f"连续失败 {len(failed_tasks)} 个任务，建议完全重置"

        error_types = []
        for task in failed_tasks:
            error = errors.get(task, "").lower()
            if "timeout" in error or "network" in error or "connection" in error:
                error_types.append("temporary")
            elif "permission" in error or "denied" in error:
                error_types.append("permission")
            elif "not found" in error or "invalid" in error:
                error_types.append("semantic")
            else:
                error_types.append("unknown")

        if any(et == "temporary" for et in error_types):
            return "fast_retry", "检测到临时性错误，建议快速重试"
        elif any(et in ("permission", "semantic") for et in error_types):
            return "gentle_retry", "检测到语义/权限错误，建议温和重试"
        else:
            return "gentle_retry", "建议分析错误后温和重试"

    def _run(self, task_id: str, session_id: str, timeout_mins: int = 30, **kwargs: Any) -> str:
        todo_dir = SESSIONS_DIR / session_id / "todo"
        program_file = todo_dir / f"{task_id}_program.py"

        if not program_file.exists():
            return f'{{"status": "error", "message": "Program file not found: {program_file.as_posix()}"}}'

        try:
            result = subprocess.run(
                [sys.executable, program_file.as_posix()],
                capture_output=True,
                text=True,
                timeout=timeout_mins * 60,
                cwd=todo_dir.as_posix(),
            )

            output = result.stdout + result.stderr
            parsed = self._parse_output(output)
            strategy, recommendation = self._determine_strategy(parsed)

            response = {
                "status": parsed["status"],
                "strategy_needed": strategy,
                "failed_tasks": [
                    {"label": label, "error": errors.get(label, "unknown")}
                    for label in parsed["failed_tasks"]
                ],
                "completed_tasks": parsed["completed_tasks"],
                "can_resume": parsed["status"] in ("interrupted", "failed"),
                "recommendation": recommendation,
                "stage": "unknown",
            }

            return json.dumps(response, ensure_ascii=False, indent=2)

        except subprocess.TimeoutExpired:
            return json.dumps({
                "status": "timeout",
                "strategy_needed": "gentle_retry",
                "message": f"Program execution timed out after {timeout_mins} minutes",
                "can_resume": True,
                "recommendation": "执行超时，建议检查程序是否有死循环或长时间阻塞任务",
            }, ensure_ascii=False, indent=2)
        except Exception as e:
            return json.dumps({
                "status": "error",
                "message": str(e),
                "can_resume": False,
            }, ensure_ascii=False, indent=2)

    async def _arun(self, task_id: str, session_id: str, timeout_mins: int = 30, **kwargs: Any) -> str:
        return self._run(task_id, session_id, timeout_mins, **kwargs)


import json


def build_program_runner(session_id: str, task_id: str) -> ProgramRunner:
    tool = ProgramRunner(session_id, task_id)
    tool.handle_tool_error = True
    return tool