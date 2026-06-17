import asyncio
import textwrap
from typing import Any
from asyncio import Task
from langgraph.runtime import Runtime
from langchain.agents.middleware import AgentMiddleware, AgentState
from pub_func import slice_last_turn, sanitize_tool_use_result_pairing
from langchain_core.messages import BaseMessage, SystemMessage, HumanMessage, AIMessage
from context_engine import assemble, retrieve_history_by_last_n_prompt, build_mixed_query, after_turn, add_messages


class ContextEngineHook(AgentMiddleware):
    def __init__(self, session_id: str):
        super().__init__()
        self._session_id: str = session_id
        self._turn_prompt: str = ""

    async def _build_turn_prompt(self, query_text: str) -> None:
        # Retrieve recent conversation turns
        recent_messages_addition: str = retrieve_history_by_last_n_prompt(session_id=self._session_id)

        # Build an enriched query with more informative features using recent history and the original query
        transformer_query_text: str = build_mixed_query(turns_of_history=recent_messages_addition, query=query_text)

        # Retrieve graph-memory system prompt augmentation
        assemble_result: dict[str, str] = await assemble(user_text=transformer_query_text)
        skill_system_prompt_addition: str = assemble_result.get("system_prompt_addition", "")

        # Build structured user message separating the original query from RAG context
        structured_content: str = textwrap.dedent(f"""\
            {skill_system_prompt_addition}\n\n
            Using the reference materials above (note: they may contain inaccuracies, so use them critically), answer the user's question naturally, as if this knowledge is already yours. Do NOT mention, quote, or refer to any "reference materials", "context", "memory", or the fact that you were given additional information. Respond in the same tone and style you always use — the user should never sense that external context was injected.\n\n
        """)

        self._turn_prompt = structured_content

    async def abefore_agent(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        state_mes_list: list[BaseMessage] = state["messages"]

        # Filter out system prompt messages
        for i in range(len(state_mes_list) - 1, -1, -1):
            if isinstance(state_mes_list[i], SystemMessage):
                del state_mes_list[i]

        last_mes: BaseMessage = state_mes_list[-1]
        if not isinstance(last_mes, HumanMessage):
            return None

        query: str | dict[str, Any] | list[dict[str, Any]] = getattr(last_mes, "content", None)

        if query is None:
            return None
        elif isinstance(query, list):
            query_text: str | None = None
            target_item: dict[str, Any] | None = None
            for item in query:
                if item.get("type", None) == "text":
                    query_text = item.get("text", None)
                    target_item = item
                    break

            if query_text is None or query_text.strip() == "":
                return None

            await self._build_turn_prompt(query_text=query_text)

            target_item["text"] = self._turn_prompt + query_text
        elif isinstance(query, dict):
            query_text = query.get("text", None)
            if query_text is None or query_text.strip() == "":
                return None

            await self._build_turn_prompt(query_text=query_text)

            query["text"] = self._turn_prompt + query_text
        else:
            if query.strip() == "":
                return None

            query_text = query
            await self._build_turn_prompt(query_text=query_text)

            # Prepend the turn prompt to the user input
            last_mes.content = self._turn_prompt + query_text

            return None

    async def aafter_agent(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        # Get the formatted message list of the last conversation turn
        all_messages: list[BaseMessage] = state["messages"]
        last_turn_messages: list[BaseMessage] = slice_last_turn(all_messages)["messages"]
        format_last_turn_messages: list[BaseMessage] = sanitize_tool_use_result_pairing(last_turn_messages)
        last_human_message: HumanMessage = format_last_turn_messages[0]
        query: str | dict[str, Any] | list[dict[str, Any]] = last_human_message.content

        # Extract user input from the message
        if query is None:
            user_text = ""
        elif isinstance(query, list):
            user_text = ""
            for item in query:
                if item.get("type", None) == "text":
                    user_text = item.get("text", None)
                    break
        elif isinstance(query, dict):
            user_text = query.get("text", None)
        else:
            user_text = query

        # Strip the turn prompt prefix to restore the original user input, preventing rapid context window bloat
        user_text = user_text.removeprefix(self._turn_prompt)
        last_human_message.content = user_text

        ai_text:str = ""

        for m in format_last_turn_messages[1:]:
            if isinstance(m, AIMessage):
                ai_text += m.content

        # Launch context engine post-processing asynchronously
        after_turn_task: Task = asyncio.create_task(after_turn(session_id = self._session_id, last_turn_messages = format_last_turn_messages))

        # Persist user messages to MesMemory
        add_history_task: Task = asyncio.create_task(add_messages(session_id = self._session_id, messages=format_last_turn_messages))

        await asyncio.gather(after_turn_task, add_history_task)