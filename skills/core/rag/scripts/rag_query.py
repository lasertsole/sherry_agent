"""
rag_query.py — 向 rag-anything 知识图谱提出查询问题

用法:
    python rag_query.py "<query_string>"

示例:
    python rag_query.py "橘雪莉和远野汉娜是什么关系？"
"""

import sys
from pathlib import Path

# 设置标准输出编码为utf-8，避免Windows GBK编码问题
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

# 动态添加项目根目录到 sys.path
current_file = Path(__file__).resolve()
# skills/core/rag/scripts/rag_query.py -> parents[4] = 项目根目录
project_root: Path = current_file.parents[4]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from rag import get_rag_anything
from raganything import RAGAnything


async def query(question: str) -> None:
    """向 rag-anything 知识图谱提问"""
    try:
        rag: RAGAnything = await get_rag_anything()
        res = await rag.aquery(question)
        print(f"[查询] {question}")
        print(f"[回答] {res}")
    except Exception as e:
        print(f"[错误] 查询出错: {e}")