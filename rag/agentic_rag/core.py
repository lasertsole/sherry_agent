import datetime
from tools import web_search
from models import simple_chat_model
from langchain_core.tools import tool
from pydantic import BaseModel, Field
from langchain.agents import create_agent
from langchain.messages import HumanMessage
from langchain_core.prompts import PromptTemplate
from langgraph.graph import StateGraph, START, END, MessagesState

researcher_systemPrompt = """
你是资深检索策略专家。你的目标是分析用户的问题，并调用检索工具获取准确的事实信息。
你不需要利用自身知识库回答问题，所有信息必须来源于外部工具。
如果没有查到就说不知道，请你如实回答。
"""
#生成agent对象
researcher = create_agent(
    model = simple_chat_model,
    system_prompt = researcher_systemPrompt,
    tools = [web_search],
)

rewriter_systemPrompt = """
您作为专业信息优化助手，需重写用户查询使其更精确且便于检索,并且如果用户的查询是疑问句时，请重写为陈述句。
例子：西瓜多少钱一斤？  改写为：当前西瓜市场零售价格。
辅助信息:当前时间为{dataTime}

查询:{query}
重写后的查询:
"""

rewriter_systemPrompt_Template = PromptTemplate(
    template = rewriter_systemPrompt,
    input_variables = ["query", "dataTime"]
)
rewriter_systemPrompt_Template = rewriter_systemPrompt_Template.partial(dataTime=datetime.datetime.now().strftime("%Y年%m月%d日"))

class ReWriteQueryOutput(BaseModel):
    """字符串列表输出结构"""
    requery: str = Field(
        description="生成的重写后的句子。",
        examples=[
            "当前西瓜市场零售价格。",
            "不同品种（如麒麟瓜、黑美人等）西瓜的产地批发价与市场零售价差异。",
            "影响西瓜价格的主要因素（季节、产地、运输成本等）。",
            "如何根据市场行情判断西瓜的合理购买价格。"]
    )

rewriter = rewriter_systemPrompt_Template | simple_chat_model.with_structured_output(ReWriteQueryOutput, method='json_mode')

class GraphState(MessagesState):
    query: str # 原问题
    requery: str # 改写后的问题
    res: str #查询结果

async def rewriter_node(state: GraphState):
    result = await rewriter.ainvoke({"query": state["query"]})
    return {"requery": result.requery}

async def researcher_node(state: GraphState):
    result = await researcher.ainvoke({"messages": [HumanMessage(state["requery"])]})
    return {"res": result["messages"][-1].content}


workflow = StateGraph(GraphState)
workflow.add_node("rewriter", rewriter_node)
workflow.add_node("researcher", researcher_node)
workflow.add_edge(START, "rewriter")
workflow.add_edge("rewriter", "researcher")
workflow.add_edge("researcher", END)
graph = workflow.compile()

# 工具输入参数
class AgenticRagQuerySchema(BaseModel):
    query: str = Field(description="具体问题")

@tool(args_schema=AgenticRagQuerySchema, description="""
       检索“魔法少女的魔女审批”相关知识的工具函数。

       该函数用于当需要回答与“魔法少女的魔女审批”相关的问题时调用，
       通过本地检索引擎获取知识库中的对应答案。

       Args:
           query (str): 具体的查询问题，需与“魔法少女的魔女审批”主题相关。

       Returns:
           str: 本地检索引擎从知识库中检索到的答案文本。
   """)
async def agentic_rag_query(query:str)->str:
    result = await graph.ainvoke({"query": query})
    return result["res"]
