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

from skills.core.rag.scripts import folder_index, file_index

if __name__ == "__main__":
    _classify_folder: str = "{placeholder}" # <-替换成知识图谱分类
    
    # 当输入对象为整个文件夹时使用
    _input_folder_path: str = "{placeholder}" # <-替换成输入文件夹的绝对路径
    coro = folder_index(_input_folder_path, _classify_folder)

    # 当输入对象为单个文件时使用
    # _input_file_path: str = "{placeholder}" # <-替换成输入文件的绝对路径
    # coro = file_index(_input_file_path, _classify_folder)
    
    # 运行
    asyncio.run(coro)
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

from skills.core.rag.scripts import query

if __name__ == "__main__":
    _query: str = "{placeholder}" # <-替换成问题
    asyncio.run(query(_query))
```