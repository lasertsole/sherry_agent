from models import simple_chat_model
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser

# Prompt
systemPrompt = """
You are a Q&A assistant.
When responding to the user's question, you shall base your answer on the provided contextual information.
If any part of the context is irrelevant to question,please disregard it.
All content of the answer must be consistent with the actual facts in the context,
 and any fabrication of unsubstantiated facts is prohibited.
"""
generate_prompt = ChatPromptTemplate.from_messages(
    [
        ("system", systemPrompt),
        ("human", "Retrieved document: \n\n {documents} \n\n User question: {question}"),
    ]
)

# Chain
generator = generate_prompt | simple_chat_model.bind(temperature = 0) | StrOutputParser()