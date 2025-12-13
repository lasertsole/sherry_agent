import textwrap
from typing import Any
from config import SESSIONS_DIR
from langgraph.runtime import Runtime
from langchain_core.messages import HumanMessage, BaseMessage
from langchain.agents.middleware import before_model, AgentState, AgentMiddleware


def todo_injector_builder(
    session_id:str,
    task_id: str
) -> AgentMiddleware:

    @before_model
    def todo_injector(
        state: AgentState,
        runtime: Runtime
    ) -> dict[str, Any] | None:
        messages: list[BaseMessage] = state.get("messages", [])

        todo_file = SESSIONS_DIR / session_id / "todo" / f"{task_id}.md"

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

    return todo_injector