import re
import textwrap
from typing import Any
from pathlib import Path
from pydantic import BaseModel, Field
from langchain_core.tools import BaseTool
from config import SESSIONS_DIR


class TaskItem(BaseModel):
    label: str = Field(description="任务标签")
    description: str = Field(description="任务描述")
    timeout_mins: int = Field(description="超时时间(分钟)", default=5, ge=5, le=30)
    parallel_group: str = Field(description="并联组名称，None表示串联执行", default="None")
    dependencies: list[str] = Field(description="依赖的任务标签列表", default_factory=list)


class ProgramGeneratorArgs(BaseModel):
    todo_content: str = Field(description="Todo列表内容，markdown格式")
    session_id: str = Field(description="会话ID")
    task_id: str = Field(description="任务ID")


class ProgramGenerator(BaseTool):
    name: str = "program_generator"
    description: str = "根据todolist生成Python可执行程序，用于编排多个worker agent的执行"
    args_schema: type[BaseModel] = ProgramGeneratorArgs

    def __init__(self, session_id: str, task_id: str, **kwargs: Any):
        super().__init__(**kwargs)
        self._session_id = session_id
        self._task_id = task_id
        self._todo_dir = SESSIONS_DIR / session_id / "todo"
        self._todo_dir.mkdir(parents=True, exist_ok=True)

    def _parse_todo(self, todo_content: str) -> list[TaskItem]:
        tasks = []
        lines = todo_content.split("\n")
        current_task = None

        for line in lines:
            line = line.strip()
            if not line:
                continue

            task_match = re.match(r"- \[([ x/])\] (\w+): (.+)", line)
            if task_match:
                status, label, description = task_match.groups()
                timeout = 5
                parallel_group = "Default"
                dependencies = []

                current_task = TaskItem(
                    label=label,
                    description=description,
                    timeout_mins=timeout,
                    parallel_group=parallel_group,
                    dependencies=dependencies,
                )
                tasks.append(current_task)
            elif current_task and "Parallel Group:" in line:
                group = line.split("Parallel Group:")[-1].strip()
                current_task.parallel_group = group
            elif current_task and "Dependency:" in line:
                dep = line.split("Dependency:")[-1].strip()
                if dep:
                    current_task.dependencies = [d.strip() for d in dep.split(",")]

        return tasks

    def _generate_worker_execute_code(self, task: TaskItem) -> str:
        return textwrap.dedent(f'''\
            # Task: {task.label}
            try:
                result_{task.label} = await execute_worker(
                    label="{task.label}",
                    description="""{task.description}""",
                    timeout_mins={task.timeout_mins}
                )
                worker_results["{task.label}"] = result_{task.label}
                if result_{task.label}.get("status") != "ok":
                    failed.append("{task.label}")
                    errors["{task.label}"] = result_{task.label}.get("error", "unknown")
                else:
                    completed.append("{task.label}")
                    print(f"COMPLETED: {task.label}")
            except Exception as e:
                failed.append("{task.label}")
                errors["{task.label}"] = str(e)
                print(f"FAILED: {task.label} - {{e}}")
        ''')

    def _generate_stage_code(self, tasks: list[TaskItem], stage_name: str, task_labels: list[str]) -> str:
        if not task_labels:
            return ""

        stage_tasks = [t for t in tasks if t.label in task_labels]
        has_parallel = len(stage_tasks) > 1 and all(t.parallel_group != "None" for t in stage_tasks)

        if has_parallel:
            task_creates = "\n".join([
                f'            tasks["{t.label}"] = tg.create_task(execute_worker("{t.label}", """{t.description}""", {t.timeout_mins}))'
                for t in stage_tasks
            ])
            return textwrap.dedent(f'''\
            # Stage {stage_name}: 并联执行
            async def stage_{stage_name}():
                nonlocal completed, failed, errors, worker_results
                async with asyncio.TaskGroup() as tg:
            {task_creates}
                for name, task in tasks.items():
                    try:
                        result = task.result()
                        worker_results[name] = result
                        if result.get("status") == "ok":
                            completed.append(name)
                            print(f"COMPLETED: {{name}}")
                        else:
                            failed.append(name)
                            errors[name] = result.get("error", "unknown")
                            print(f"FAILED: {{name}} - {{errors[name]}}")
                    except asyncio.CancelledError:
                        failed.append(name)
                        errors[name] = "cancelled"
                        print(f"CANCELLED: {{name}}")
                    except Exception as e:
                        failed.append(name)
                        errors[name] = str(e)
                        print(f"FAILED: {{name}} - {{e}}")
                return worker_results
            ''')
        else:
            code_lines = []
            for t in stage_tasks:
                code_lines.append(self._generate_worker_execute_code(t))
            return textwrap.dedent(f'''\
            # Stage {stage_name}: 串联执行
            async def stage_{stage_name}():
                nonlocal completed, failed, errors, worker_results
            ''') + "\n".join(code_lines)

    def generate_program(self, todo_content: str) -> str:
        tasks = self._parse_todo(todo_content)
        
        groups: dict[str, list[str]] = {}
        sequential: list[str] = []
        
        for task in tasks:
            if task.parallel_group and task.parallel_group != "None":
                groups.setdefault(task.parallel_group, []).append(task.label)
            else:
                sequential.append(task.label)

        program = textwrap.dedent('''\
            import asyncio
            import json
            import signal
            import sys
            import hashlib
            from pathlib import Path
            from datetime import datetime

            SESSION_ID = "{{session_id}}"
            TASK_ID = "{{task_id}}"
            
            CACHE_DIR = Path(f"SESSIONS_DIR/{SESSION_ID}/todo/.cache")
            STATE_DIR = Path(f"SESSIONS_DIR/{SESSION_ID}/todo/.state")
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            
            STATE_FILE = STATE_DIR / f"{TASK_ID}_execution.json"

            def save_state(status: str, current_stage: str = "", completed: list = None, failed: list = None):
                state = {
                    "task_id": TASK_ID,
                    "status": status,
                    "current_stage": current_stage,
                    "completed_tasks": completed or [],
                    "failed_tasks": failed or [],
                    "timestamp": datetime.now().isoformat()
                }
                STATE_FILE.write_text(json.dumps(state, indent=2))

            def load_state() -> dict | None:
                if STATE_FILE.exists():
                    return json.loads(STATE_FILE.read_text())
                return None

            def compute_hash(text: str) -> str:
                return hashlib.md5(text.encode()).hexdigest()[:16]

            def get_cache(label: str, desc_hash: str) -> dict | None:
                f = CACHE_DIR / f"{label}_{desc_hash}.json"
                if f.exists():
                    return json.loads(f.read_text())
                return None

            def set_cache(label: str, desc_hash: str, data: dict):
                f = CACHE_DIR / f"{label}_{desc_hash}.json"
                f.write_text(json.dumps(data, indent=2))

            def save_checkpoint(stage: str, data: dict):
                ckpt_file = STATE_DIR / f"{TASK_ID}_checkpoint_{stage}.json"
                ckpt_file.write_text(json.dumps({"stage": stage, "data": data, "timestamp": datetime.now().isoformat()}, indent=2))

            def signal_handler(signum, frame):
                save_state("interrupted", current_stage=current_stage, completed=completed, failed=failed)
                print("INTERRUPTED: State saved, can be resumed")
                sys.exit(0)

            signal.signal(signal.SIGINT, signal_handler)
            signal.signal(signal.SIGTERM, signal_handler)

            async def execute_worker(label: str, description: str, timeout_mins: int = 5) -> dict:
                desc_hash = compute_hash(description)
                
                cached = get_cache(label, desc_hash)
                if cached:
                    print(f"CACHED: {label}")
                    return cached
                
                result = await call_worker_agent(label, description, timeout_mins)
                
                if result.get("status") == "ok":
                    print(f"SUCCESS: {label} completed")
                    set_cache(label, desc_hash, result)
                else:
                    print(f"FAILED: {label} - {{result.get('error', 'unknown')}}")
                
                return result

            async def call_worker_agent(label: str, description: str, timeout_mins: int) -> dict:
                """调用实际的worker agent执行任务"""
                from .worker_executor import WorkerExecutor
                executor = WorkerExecutor(SESSION_ID, TASK_ID)
                return await executor.execute(label, description, timeout_mins)

            async def main():
                global current_stage
                old_state = load_state()
                if old_state and old_state.get("status") == "interrupted":
                    print(f"RESUMING: From stage {{old_state.get('current_stage', 'unknown')}}")
                
                completed = []
                failed = []
                errors = {{}}
                worker_results = {{}}
                current_stage = "init"
                
                save_state("running", "init")
                
                # ========== 任务执行阶段 ==========
        ''')

        program = program.replace("{{session_id}}", self._session_id)
        program = program.replace("{{task_id}}", self._task_id)

        stage_counter = 1
        all_task_labels = [t.label for t in tasks]
        
        for group_name, task_labels in groups.items():
            stage_code = self._generate_stage_code(tasks, f"{stage_counter}_{group_name}", task_labels)
            program += textwrap.dedent(f'''\
                current_stage = "{group_name}"
                save_state("running", current_stage, completed, failed)
                await stage_{stage_counter}_{group_name}()
                save_checkpoint("{group_name}", {{"completed": completed, "failed": failed}})
            ''') + "\n"
            stage_counter += 1

        if sequential:
            for i, label in enumerate(sequential):
                task = next((t for t in tasks if t.label == label), None)
                if not task:
                    continue
                program += textwrap.dedent(f'''\
                current_stage = "{label}"
                save_state("running", current_stage, completed, failed)
            ''') + self._generate_worker_execute_code(task) + "\n"

        program += textwrap.dedent(f'''\
                if failed:
                    save_state("failed", current_stage, completed, failed)
                    print(f"TASKS_FAILED: {{','.join(failed)}}")
                else:
                    save_state("completed", "finished", completed, [])
                    print("ALL_TASKS_COMPLETED")
                
                return {{
                    "status": "completed" if not failed else "failed",
                    "completed": completed,
                    "failed": failed,
                    "errors": errors,
                    "worker_results": worker_results
                }}
            
            if __name__ == "__main__":
                result = asyncio.run(main())
                print(json.dumps(result))
        ''')

        return program

    def _run(self, todo_content: str, session_id: str, task_id: str, **kwargs: Any) -> str:
        program = self.generate_program(todo_content)
        program_file = self._todo_dir / f"{task_id}_program.py"
        program_file.write_text(program, encoding="utf-8")
        return f"Program generated: {program_file.as_posix()}"

    async def _arun(self, todo_content: str, session_id: str, task_id: str, **kwargs: Any) -> str:
        return self._run(todo_content, session_id, task_id, **kwargs)


def build_program_generator(session_id: str, task_id: str) -> ProgramGenerator:
    tool = ProgramGenerator(session_id, task_id)
    tool.handle_tool_error = True
    return tool