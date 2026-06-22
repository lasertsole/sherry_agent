import uuid
import asyncio
from pathlib import Path
from models import main_llm
from .type import SubAgentOutput
from workspace import CORE_FILE_NAMES
from logging import Logger, getLogger
from .commander import build_commander
from skills.loader import get_skills_text
from config import SRC_DIR, WORKSPACE_DIR
from bus import InboundMessage, MessageBus
from typing import Any, Callable, Awaitable
from langgraph.graph.state import CompiledStateGraph
from workspace.prompt_builder import build_system_prompt
from pub_func import render_template_file, build_agent_config
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage


logger: Logger = getLogger(__name__)
_current_dir = Path(__file__).parent
_template_dir = (_current_dir / "templates").resolve()

class SubagentManager:
    """Manages background subagent execution."""

    """单例模式"""
    _instance = None
    _initialized = False

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(
        self,
        bus: MessageBus | None = None,
    ):
        if bus is None:
            bus = MessageBus()
        self._bus = bus
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._session_tasks: dict[str, set[str]] = {}  # session_id -> {task_id, ...}
        self._consumer: Callable[[InboundMessage], Awaitable[None]] | None = None

        # 如果有运行中的事件循环，则使用它， 否则创建一个新的
        try:
            self._event_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._event_loop = asyncio.new_event_loop()

        SubagentManager._initialized = True

    async def spawn(
        self,
        session_id: str,
        task: str,
        label: str | None = None,
    ) -> str:
        """Spawn a subagent to execute a task in the background."""
        task_id = str(uuid.uuid4())[:8]
        display_label = label or task[:30] + ("..." if len(task) > 30 else "")

        bg_task = self._event_loop.create_task(
            self._run_subagent(
                session_id= session_id,
                task_id= task_id,
                task= task,
                label= display_label
            )
        )

        self._running_tasks[task_id] = bg_task
        self._session_tasks.setdefault(session_id, set()).add(task_id)

        def _cleanup(_: asyncio.Task) -> None:
            self._running_tasks.pop(task_id, None)
            if ids := self._session_tasks.get(session_id):
                ids.discard(task_id)
                if not ids:
                    del self._session_tasks[session_id]

        bg_task.add_done_callback(_cleanup)

        logger.info("Spawned subagent [{}]: {}", task_id, display_label)
        return f"Subagent [{display_label}] started (id: {task_id}). I'll notify you when it completes."

    def get_event_loop(self) -> asyncio.AbstractEventLoop:
        """Get the event loop."""
        return self._event_loop
    
    def get_buts(self)-> MessageBus:
        return self._bus

    @staticmethod
    def _build_subagent_prompt() -> str:
        """
        创建子代理提示词

        Returns:
            子代理提示词
        """

        skill_guide_text: str = f"""
        补充说明：
        1.将<skill_folder>替换成技能文件SKILL.md所在的目录 比如技能文件在 "./skills/text_to_image/SKILL.md", 那么文件目录就在 "./skills/text_to_image"
        2.技能生成的临时资源（如图片、语音等）存放在{(SRC_DIR / "temp").as_posix()}目录下
        """
        skill_paths:str = get_skills_text(selected_skill_names = None, exclude_auth_skills=True) # 排除高权限技能
        skill_paths = f"{skill_paths}\n\n{skill_guide_text}"

        file_paths: list[str] = []

        # 确保一定有核心文件
        for core_file in CORE_FILE_NAMES:
            path = WORKSPACE_DIR / core_file
            if not path.exists():
                continue
            file_paths.append(path.read_text(encoding="utf-8"))

        parts = [skill_paths, *file_paths]

        return "\n\n".join(p for p in parts if p)

    async def _run_subagent(
        self,
        session_id: str,
        task_id: str,
        task: str,
        label: str,
    ) -> None:
        """Execute the subagent task and announce the result."""
        logger.info("Subagent [{}] starting task: {}", task_id, label)

        try:
            agent: CompiledStateGraph = build_commander(session_id=session_id, task_id=task_id)
            agent_res: dict[str, Any] = await agent.ainvoke(
                input={"messages": [HumanMessage(content=task)]},
                config=build_agent_config(session_id=session_id, args=[{"recursion_limit": 30}])
            )
            structured_response: SubAgentOutput = agent_res.get("structured_response", {})

            announce_content: str = render_template_file(
                file_path=(_template_dir / "subagent_announce.md").resolve().as_posix(),
                variables={
                    "label": label,
                    "status_text": "completed successfully" if structured_response.status == "ok" else "failed",
                    "task": task,
                    "finish_reason": structured_response.finish_reason,
                    "result": structured_response.result,
                }
            )

        except Exception as e:
            announce_content: str = render_template_file(
                file_path=(_template_dir / "subagent_announce.md").resolve().as_posix(),
                variables={
                    "label": label,
                    "status_text": "crash error",
                    "task": task,
                    "finish_reason": str(e),
                    "result": "",
                }
            )

        msg = InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id="direct",
            content=announce_content,
            session_id=session_id,
            metadata={
                "injected_event": "subagent_result",
                "subagent_task_id": task_id,
            },
        )

        await self._bus.publish_inbound(msg)


    async def cancel_by_session(self, session_id: str) -> int:
        """Cancel all subagents for the given session. Returns count canceled."""
        tasks = [self._running_tasks[tid] for tid in self._session_tasks.get(session_id, [])
                 if tid in self._running_tasks and not self._running_tasks[tid].done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    def set_consumer(self, consumer: Callable[[InboundMessage], Awaitable[None]])->None:
        self._consumer = consumer

    async def _consume_loop(self):
        while True:
            msg: InboundMessage = await self._bus.consume_inbound()

            if self._consumer:
                # 将结果变信息得符合人设性格
                messages = [SystemMessage(
                    content=
                    build_system_prompt()
                    + '\n\nPlease convey the results to the user in a tone that matches the character persona, and tell user where result is.'
                ), HumanMessage(content=msg.content)]

                res_msg: AIMessage = main_llm.invoke(messages)
                msg.content = res_msg.content

                # 返回结果
                await self._consumer(msg)

    def start_service(self) -> None:
        if not self._event_loop.is_running():
            self._event_loop.create_task(self._consume_loop())

            # 防止重复运行报错
            try:
                self._event_loop.run_forever()
            except Exception:
                pass

    def get_running_count(self) -> int:
        """Return the number of currently running subagents."""
        return len(self._running_tasks)

    def get_running_count_by_session(self, session_id: str) -> int:
        """Return the number of currently running subagents for a session."""
        tids: set[str] = self._session_tasks.get(session_id, set())
        return sum(
            1 for tid in tids
            if tid in self._running_tasks and not self._running_tasks[tid].done()
        )

subagent_manager = SubagentManager()