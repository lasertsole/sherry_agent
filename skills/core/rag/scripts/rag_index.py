"""
rag_index.py — 将指定目录下的所有文件加入 rag-anything 知识图谱

用法:
    python rag_index.py <input_folder_path> <classify_folder>

示例:
    python rag_index.py /path/to/documents my_docs
"""

import sys
from pathlib import Path

# 动态添加项目根目录到 sys.path
current_file = Path(__file__).resolve()
# skills/core/rag/scripts/rag_index.py -> parents[4] = 项目根目录
project_root: Path = current_file.parents[4]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from config import SRC_DIR
from rag import get_rag_anything
from raganything import RAGAnything


async def folder_index(input_folder_path: str, classify_folder: str) -> None:
    """将指定文件夹中的文件索引到 rag-anything 知识图谱中"""
    rag: RAGAnything = await get_rag_anything()

    await rag.process_folder_complete(
        folder_path=input_folder_path,
        output_dir=SRC_DIR / "rag" / "rag_anything" / classify_folder / "output",
        parse_method="auto",
        recursive=True,
        max_workers=4,
    )
    print(f"✅ 索引完成！文件夹 '{input_folder_path}' 已加入知识图谱分类 '{classify_folder}'")


async def file_index(input_file_path: str, classify_folder: str) -> None:
    rag: RAGAnything = await get_rag_anything()

    await rag.process_document_complete(
        file_path=input_file_path,
        output_dir=SRC_DIR / "rag" / "rag_anything" / classify_folder / "output",
        parse_method="auto",
        recursive=True,
        max_workers=4,
    )
    print(f"✅ 索引完成！文件 '{input_file_path}' 已加入知识图谱分类 '{classify_folder}'")