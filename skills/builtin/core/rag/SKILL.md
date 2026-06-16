---
name: rag_anything
description: Private knowledge base for indexing multimodal files or folders into a knowledge graph, supporting multi-hop graph retrieval
---

**Index all files under a directory into the rag-anything graph:**

```python
import asyncio
from skills.builtin.core import folder_index, file_index

if __name__ == "__main__":
    _classify_folder: str = "{placeholder}"  # <- replace with the knowledge graph category

    # Use this when the input is an entire folder
    _input_folder_path: str = "{placeholder}"  # <- replace with the absolute path of the input folder
    coro = folder_index(_input_folder_path, _classify_folder)

    # Use this when the input is a single file
    # _input_file_path: str = "{placeholder}"  # <- replace with the absolute path of the input file
    # coro = file_index(_input_file_path, _classify_folder)

    # Run
    asyncio.run(coro)
```

**Query the rag-anything knowledge graph:**

```python
import asyncio
from skills.builtin.core import query

if __name__ == "__main__":
    _query: str = "{placeholder}"  # <- replace with the question
    asyncio.run(query(_query))
```