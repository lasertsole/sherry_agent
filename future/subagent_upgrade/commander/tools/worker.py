import asyncio
from typing import Any
from pathlib import Path
from models import chat_model
from config import SESSIONS_DIR
from skills import get_skills_text
from ...type import SubAgentOutput
from logging import Logger, getLogger
from pydantic import BaseModel, Field
from workspace import CORE_FILE_NAMES
from langchain.agents import create_agent
from langchain_core.tools import BaseTool
from config import SRC_DIR, WORKSPACE_DIR
from context_engine import assemble, after_turn
from langgraph.graph.state import CompiledStateGraph
from langchain_core.messages import HumanMessage, BaseMessage
from langchain.agents.middleware import SummarizationMiddleware
from pub_func import render_template_file, slice_last_turn, sanitize_tool_use_result_pairing, build_agent_config

logger: Logger = getLogger(__name__)


class WorkerTask(BaseModel):
    label: str = Field(description="任务标签")
    description: str = Field(description="任务具体描述")
    timeout_mins: int = Field(description="超时时间,超过超时时间任务会强行结束，单位为 分钟。取值范围：5-30, 默认为5", default=5, ge=5, le=30)


class WorkerArgs(BaseModel):
    worker_tasks: list[WorkerTask] = Field(description="任务列表,用于执行逻辑上没有依赖关系、并且不互相干扰的任务")


class Worker(BaseTool):
    name: str = "worker"
    description: str = "执行多个没有依赖关系、可以并行执行互不干扰的子任务。返回子任务执行结果列表。"
    args_schema: type[BaseModel] | None = WorkerArgs

    _current_dir = Path(__file__).parents[2].resolve()
    _template_dir = (_current_dir / "templates").resolve()

    def __init__(self, session_id: str, task_id: str, **kwargs: Any):
        super().__init__(**kwargs)
        self._session_id: str = session_id
        self._task_id: str = task_id

        todo_dir: Path = SESSIONS_DIR / session_id / "todo"
        todo_dir.mkdir(parents=True, exist_ok=True)

        self._file_path: Path = todo_dir / f"{self._task_id}.md"

    @staticmethod
    def _build_worker_prompt() -> str:
        skill_guide_text: str = f"""
        补充说明：
        1.将<skill_folder>替换成技能文件SKILL.md所在的目录 比如技能文件在 "./skills/text_to_image/SKILL.md", 那么文件目录就在 "./skills/text_to_image"
        2.技能生成的临时资源（如图片、语音等）存放在{(SRC_DIR / "temp").as_posix()}目录下
        """
        skill_paths: str = get_skills_text(selected_skill_names=None, exclude_auth_skills=True)
        skill_paths = f"{skill_paths}\n\n{skill_guide_text}"

        file_paths: list[str] = []

        for core_file in CORE_FILE_NAMES:
            path = WORKSPACE_DIR / core_file
            if not path.exists():
                continue
            file_paths.append(path.read_text(encoding="utf-8"))

        parts = [skill_paths, *file_paths]

        return "\n\n".join(p for p in parts if p)

    async def _arun_task(
        self,
        label: str,
        description: str,
        timeout_mins: int,
    ) -> str:
        messages: list[BaseMessage] = []
        try:
            timeout_seconds: int = timeout_mins * 60

            async def execute_task() -> str:
                assemble_result: dict[str, str] = await assemble(user_text=description, messages=[])
                graph_system_prompt_addition: str = assemble_result.get("system_prompt_addition", "")

                system_prompt = (
                    self._build_worker_prompt()
                    + graph_system_prompt_addition
                    + "\n\n Complete the task as simply as possible, and terminate immediately upon completion to submit the results."
                )

                from tools import build_core_tools

                agent: CompiledStateGraph = create_agent(
                    system_prompt=system_prompt,
                    model=chat_model,
                    tools=build_core_tools(self._session_id),
                    middleware=[
                        SummarizationMiddleware(
                            model=chat_model,
                            trigger=("messages", 20),
                            keep=("messages", 10),
                        ),
                    ],
                    response_format=SubAgentOutput
                )
                agent_res: dict[str, Any] = await agent.ainvoke(
                    input={"messages": [HumanMessage(content=description)]},
                    config=build_agent_config(session_id=self._session_id, args=[{"recursion_limit": 50}])
                )
                structured_response: SubAgentOutput = agent_res.get("structured_response", {})

                nonlocal messages
                messages = agent_res.get("messages", [])

                announce_content: str = render_template_file(
                    file_path=(self._template_dir / "subagent_announce.md").resolve().as_posix(),
                    variables={
                        "label": label,
                        "status_text": "completed successfully" if structured_response.status == "ok" else "failed",
                        "task": description,
                        "finish_reason": structured_response.finish_reason,
                        "result": structured_response.result,
                    }
                )

                return announce_content

            result: str = await asyncio.wait_for(execute_task(), timeout=timeout_seconds)
            return result

        except asyncio.TimeoutError:
            logger.error("Subagent [%s] timed out after %s minutes", label, timeout_mins)
            return render_template_file(
                file_path=(self._template_dir / "subagent_announce.md").resolve().as_posix(),
                variables={
                    "label": label,
                    "status_text": "failed",
                    "task": description,
                    "result": f"Task timed out after {timeout_mins} minutes",
                }
            )

        except Exception as e:
            logger.error("Subagent [%s] failed: %s", self._task_id, e)
            return render_template_file(
                file_path=(self._template_dir / "subagent_announce.md").resolve().as_posix(),
                variables={
                    "label": label,
                    "status_text": "failed",
                    "task": description,
                    "result": str(e),
                }
            )

        finally:
            if messages and len(messages) > 0:
                last_turn_messages: list[BaseMessage] = slice_last_turn(messages)["messages"]
                format_last_turn_messages: list[BaseMessage] = sanitize_tool_use_result_pairing(last_turn_messages)
                asyncio.create_task(after_turn(session_id=self._session_id, last_turn_messages=format_last_turn_messages))

    async def _arun(
        self,
        worker_tasks: list[WorkerTask],
        **kwargs: Any
    ) -> list[str]:
        tasks_list: list[asyncio.Task] = []
        for t in worker_tasks:
            task: asyncio.Task = asyncio.create_task(self._arun_task(t.label, t.description, t.timeout_mins))
            tasks_list.append(task)

        results: tuple[Any, ...] = await asyncio.gather(*tasks_list)

        return list(results)

    def _run(
        self,
        worker_tasks: list[WorkerTask],
        **kwargs: Any
    ) -> list[str]:
        return asyncio.run(self._arun(worker_tasks, **kwargs))


def build_worker_tool(session_id: str, task_id: str) -> Worker:
    tool: Worker = Worker(session_id, task_id)
    tool.handle_tool_error = True
    return tool