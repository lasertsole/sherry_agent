import asyncio
from typing import Any
from models import chat_model
from skills import get_skills_text
from logging import Logger, getLogger
from config import SRC_DIR, WORKSPACE_DIR
from workspace import CORE_FILE_NAMES
from langchain.agents import create_agent
from langgraph.graph.state import CompiledStateGraph
from langchain_core.messages import HumanMessage
from langchain.agents.middleware import SummarizationMiddleware
from pub_func import render_template_file, build_agent_config

logger: Logger = getLogger(__name__)


class WorkerExecutor:
    def __init__(self, session_id: str, task_id: str):
        self._session_id = session_id
        self._task_id = task_id

    @staticmethod
    def _build_worker_prompt() -> str:
        skill_guide_text = f"""
        补充说明：
        1.将<skill_folder>替换成技能文件SKILL.md所在的目录 比如技能文件在 "./skills/text_to_image/SKILL.md", 那么文件目录就在 "./skills/text_to_image"
        2.技能生成的临时资源（如图片、语音等）存放在{(SRC_DIR / "temp").as_posix()}目录下
        """
        skill_paths = get_skills_text(selected_skill_names=None, exclude_auth_skills=True)
        skill_paths = f"{skill_paths}\n\n{skill_guide_text}"

        file_paths = []
        for core_file in CORE_FILE_NAMES:
            path = WORKSPACE_DIR / core_file
            if not path.exists():
                continue
            file_paths.append(path.read_text(encoding="utf-8"))

        parts = [skill_paths, *file_paths]
        return "\n\n".join(p for p in parts if p)

    async def execute(self, label: str, description: str, timeout_mins: int = 5) -> dict[str, Any]:
        from ...type import SubAgentOutput
        
        timeout_seconds = timeout_mins * 60

        async def run_task() -> dict:
            from context_engine import assemble

            assemble_result = await assemble(user_text=description, messages=[])
            graph_system_prompt_addition = assemble_result.get("system_prompt_addition", "")

            system_prompt = (
                self._build_worker_prompt()
                + graph_system_prompt_addition
                + "\n\nComplete the task as simply as possible, and terminate immediately upon completion."
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

            agent_res = await agent.ainvoke(
                input={"messages": [HumanMessage(content=description)]},
                config=build_agent_config(session_id=self._session_id, args=[{"recursion_limit": 50}])
            )

            structured_response: SubAgentOutput = agent_res.get("structured_response", {})
            
            return {
                "status": structured_response.status,
                "label": label,
                "result": structured_response.result,
                "finish_reason": structured_response.finish_reason,
                "output": structured_response.result,
            }

        try:
            result = await asyncio.wait_for(run_task(), timeout=timeout_seconds)
            return result
        except asyncio.TimeoutError:
            logger.error("Worker [%s] timed out after %s minutes", label, timeout_mins)
            return {
                "status": "failed",
                "label": label,
                "error": f"Task timed out after {timeout_mins} minutes",
            }
        except Exception as e:
            logger.error("Worker [%s] failed: %s", label, e)
            return {
                "status": "failed",
                "label": label,
                "error": str(e),
            }