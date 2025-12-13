import uuid
import jieba
from typing import List
from pathlib import Path
from langchain_core.documents import Document
from models import embed_model, simple_chat_model
from langchain_community.vectorstores import FAISS
from langchain_classic.storage import LocalFileStore
from langchain_core.prompts import ChatPromptTemplate
from langchain_community.retrievers import BM25Retriever
from langchain_core.output_parsers import StrOutputParser
from langchain_community.document_loaders import TextLoader
from langchain_classic.retrievers.multi_vector import SearchType
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_classic.retrievers import MultiVectorRetriever, MultiQueryRetriever, EnsembleRetriever


# 获取当前所在文件夹
current_dir = Path(__file__).parent.resolve()

def format_doc(doc: Document, separators: list) -> Document:
    for sep in separators:
        doc.page_content = doc.page_content.strip(sep).strip()
    return doc

# 设置文档唯一id
id_key = "doc_id"

# 如果output文件已存在，则不重复持久化
indexFolderPath = current_dir / "output"
store = LocalFileStore(indexFolderPath)

separators = ["\n\n\n"]
# 创建分词器
text_splitter = RecursiveCharacterTextSplitter(
    separators=separators,
    is_separator_regex=False,
    chunk_size=1,
    chunk_overlap=0
)

# 加载原文档
info_path = current_dir / "input/allCharacters.txt"
loader = TextLoader(info_path, encoding="utf-8")
documents = loader.load()
docs = text_splitter.split_documents(documents)
docs = [format_doc(doc, separators) for doc in docs]

# 生成对话模型
llm = simple_chat_model.bind(temperature=0.8)

if not indexFolderPath.exists():
    # 构造生成摘要链
    summarize_chain = (
            {"doc": lambda x: x.page_content}
            | ChatPromptTemplate.from_template("Summarize the following document:\n\n{doc}")
            | llm
            | StrOutputParser()
    )

    # 批量生成摘要
    summaries = summarize_chain.batch(docs, {"max_concurrency": 5})

    # 根据docs生成doc_id列表

    doc_ids = [str(uuid.uuid4()) for _ in docs]

    # 将id列表绑定
    summary_docs = [
        Document(page_content = s, metadata = {id_key: doc_ids[i]})
        for i, s in enumerate(summaries)
    ]

    # 创建原向量存储
    vector_store = FAISS.from_documents(docs, embedding = embed_model)

    # 创建多向量召回器
    vector_retriever = MultiVectorRetriever(
        vectorstore = vector_store,
        byte_store = store,
        id_key = id_key,
        search_type = SearchType.similarity_score_threshold
    )

    # 将摘要持久化
    vector_retriever.vectorstore.add_documents(summary_docs)

    # 将doc_id和doc一一对应，并存入docstore
    vector_retriever.docstore.mset(list(zip(doc_ids, docs)))

    # 持久化FAISS向量文件
    vector_store.save_local(indexFolderPath.as_posix())

else:
    ### 召回 ###
    vector_store = FAISS.load_local(
        embeddings = embed_model,
        folder_path = indexFolderPath.as_posix(),
        allow_dangerous_deserialization = True,
    )

    # 创建多向量召回器
    vector_retriever = MultiVectorRetriever(
        vectorstore = vector_store,
        byte_store = store,
        id_key = id_key,
        search_type = SearchType.similarity_score_threshold
    )

vector_retriever = MultiQueryRetriever.from_llm(retriever = vector_retriever, llm = llm)

# 自定义中文预处理函数
def chinese_preprocessing_func(text: str) -> List[str]:
    # 使用 jieba 进行中文分词
    return list(jieba.cut(text))

BM25_retriever = BM25Retriever.from_documents(
    documents = docs,
    preprocess_func = chinese_preprocessing_func
)

retriever = EnsembleRetriever(retrievers=[vector_retriever, BM25_retriever], weights=[0.5, 0.5])

def _query_background_info(query:str) -> List[str]:
    ### 检验参数 ###
    if (query is None):
        raise ValueError("query is None")
    elif (isinstance(query, str) == False):
        raise TypeError("query has is type error")
    elif (len(query) == 0):
        raise ValueError("query is empty")

    documents = retriever.invoke(query, k = 10, score_threshold = 0.5)
    retrieveResults = [doc.page_content for doc in documents]
    return retrieveResults

#query_background_info = Tool(name="query_background_info", func=_query_background_info, description="""当需要回答 魔法少女的魔女审批 有关知识时调用此工具，
# 输入为具体问题，输出为知识库检索到的答案""")