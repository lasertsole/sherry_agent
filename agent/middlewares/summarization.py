from typing import Any
from langgraph.runtime import Runtime
from langgraph.typing import ContextT
from typing_extensions import override
from langchain.agents import AgentState
from context_engine import nudge_messages
from langchain.agents.middleware import SummarizationMiddleware
from langchain_core.messages import BaseMessage, SystemMessage, RemoveMessage, HumanMessage


class Summarization(SummarizationMiddleware):
    def __init__(self, session_id: str, **kwargs):
        super().__init__(**kwargs)

        self._session_id: str = session_id

    @override
    async def abefore_model(
        self, state: AgentState[Any], runtime: Runtime[ContextT]
    ) -> dict[str, Any] | None:
        # 复制一份消息列表，避免对原消息列表的修改
        copy_state: AgentState[Any] = state.copy()
        state_mes_list_copy: list[BaseMessage] = state["messages"].copy()
        copy_state["messages"] = state_mes_list_copy
        
        # 保留系统提示信息
        remain_system_mes: SystemMessage | None = None

        for i, m in enumerate(state_mes_list_copy):
            if isinstance(m, SystemMessage):
                remain_system_mes = m

                # 移除掉原消息列表内的系统提示信息，避免系统提示信息影响到后面信息的压缩内容,降低有效压缩信息密度
                del state_mes_list_copy[i]
                break

        if remain_system_mes is None:
            return None

        # 保留最后一条用户信息
        remain_human_mes: HumanMessage | None = None

        for i in range(len(state_mes_list_copy) - 1, -1, -1):
            if isinstance(state_mes_list_copy[i], HumanMessage):
                remain_human_mes = state_mes_list_copy[i]
                break

        # 压缩信息
        res: dict[str, Any] = await super().abefore_model(copy_state, runtime)
        if res is None:
            return None

        # 获取压缩后的信息列表
        reduce_messages: list[BaseMessage] = res["messages"]

        # 插回系统提示信息
        system_insert_index = -1
        for i, m in enumerate(reduce_messages):
            if isinstance(m, RemoveMessage):
                system_insert_index = i + 1  # 记录后一个位置
                break

        if system_insert_index > 0:
            reduce_messages.insert(system_insert_index, remain_system_mes)
        else:
            reduce_messages.insert(0, remain_system_mes)

        # 插入最后一条用户信息
        last_human_mes: HumanMessage | None = None
        for i in range(len(reduce_messages) - 1, -1, -1):
            if isinstance(reduce_messages[i], HumanMessage):
                last_human_mes = reduce_messages[i]
                break

        if remain_human_mes != last_human_mes:
            reduce_messages.insert(system_insert_index + 1, remain_human_mes)

        # 压缩时前重置 memory
        from tools import memory_store
        memory_store.load_from_disk()

        # 总结用户偏好存入memory.md和user.md
        await nudge_messages(session_id= self._session_id, nudge_turn = 0)

        # 压缩时后重置 memory
        memory_store.load_from_disk()

        return res