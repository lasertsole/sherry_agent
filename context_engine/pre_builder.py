import textwrap
from config import ASSISTANT_NAME
from models import simple_chat_model
from langchain_core.messages import SystemMessage, HumanMessage


def build_mixed_query(query: str, turns_of_history: str = "") -> str:
    """
    基于对话历史改写用户查询

    Args:
        query: 用户当前问题
        turns_of_history: 历史对话上下文（可选）

    Returns:
        str: 改写后的查询文本
    """

    if query is None or not query.strip():
        return ""
    if turns_of_history == "":
        return query

    system_prompt_template: str = textwrap.dedent("""\
    You are a professional assistant specialized in query rewriting.

    ### Task: Query Rewriting
    Rewrite the user's query to be more complete and clear based on conversation history.

    ### Constraints:
    - Replace "you" with {ai_name}
    - Replace pronouns (她/它/他) with actual names based on context
    - Resolve ambiguous references using conversation history
    - IMPORTANT: Must NEVER return empty. Always return either the rewritten query or the original query.
    - Keep the query concise but information-rich
    - Return ONLY the rewritten query text, no JSON, no explanations

    ### Query Rewriting Examples:
    Example 1:
    <history>
        <turn>
            小雪今天在参加翻跟头比赛。
            小雪啊,翻跟斗一向拿手。
        </turn>
    </history>
    query: '你猜她拿了第几名?' -> '你猜小雪拿了第几名?'

    Example 2:
    <history>
        <turn>
            iphone17摄像头参数怎么样?
            4800 万像素融合式主摄:26 毫米焦距,ƒ/1.6 光圈,传感器位移式光学图像防抖功能
        </turn>
    </history>
    query: '参数那么高啊,那这个参数跟真正的相机比如何?' -> '4800 万像素融合式主摄跟真正的相机比如何?'

    Example 3:
    <history>
        No conversation history available.
    </history>
    query: '今天天气怎么样?' -> '今天天气怎么样?'
    """)

    system_prompt = system_prompt_template.format(ai_name=ASSISTANT_NAME)

    user_prompt_template: str = textwrap.dedent("""\
    =================Conversation History=================
    {turns_of_history}

    =================Current Query=================
    {query}

    Please output ONLY the rewritten query text:
    """)

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_prompt_template.format(
            turns_of_history=turns_of_history if turns_of_history else "No conversation history available.",
            query=query
        ))
    ]

    response = simple_chat_model.invoke(messages)
    mixed_query = response.content.strip() if response.content else query

    # 确保不为空
    if not mixed_query:
        mixed_query = query

    return mixed_query