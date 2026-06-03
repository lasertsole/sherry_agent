---
name: rag_anything
description: 私有知识库，用于将多模态文件或文件夹进行知识图谱索引，并支持多跳图检索
---

**以下是将指定目录下的所有文件加入rag-anything图谱中：**
```python 
import os
import sys
import asyncio
from pathlib import Path

# 动态添加项目根目录到 sys.path
current_file = Path(__file__).resolve()
project_root: Path = current_file.parents[2]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from config import SRC_DIR
from rag import get_rag_anything
from raganything import RAGAnything

async def main(input_folder_path: str, classify_folder: str) -> None:
    rag: RAGAnything = await get_rag_anything()
        
    await rag.process_folder_complete(
        folder_path=input_folder_path,
        output_dir=SRC_DIR / "rag"/ "rag_anything" / classify_folder / "output",
        parse_method="auto",
        recursive=True,
        max_workers=4,
    )
    print("done")
if __name__ == "__main__":
    _input_folder_path: str = "{placeholder}" # <-替换成输入材料的绝对路径
    _classify_folder: str = "{placeholder}" # <-替换成输出类别
    asyncio.run(main(_input_folder_path, _classify_folder))
```

**以下是向rag-anything提出用户问题：**
```python 
import os
import sys
import asyncio
from pathlib import Path

# 动态添加项目根目录到 sys.path
current_file = Path(__file__).resolve()
project_root: Path = current_file.parents[2]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from rag import get_rag_anything
from raganything import RAGAnything

async def main(query: str) -> None:
    try:
        rag: RAGAnything = await get_rag_anything()
            
        res = await rag.aquery(query)
        print(res)
    except Exception as e:
        print(e)
        
if __name__ == "__main__":
    _query: str = "{placeholder}" # <-替换成问题
    asyncio.run(main(_query))
```