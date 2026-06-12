---
name: text_to_image
description: 当用户需要根据文字描述生成图片时，使用 python_repl工具生成图片。
---

```python
import sys
from pathlib import Path

# 动态添加项目根目录到 sys.path
current_file = Path(__file__).resolve()
current_folder: Path = current_file.parent
if current_folder.as_posix() not in sys.path:
    sys.path.insert(0, current_folder.as_posix())

from skills.core.text_to_image.scripts import generate_image

if __name__ == '__main__':
    # 测试用：直接传入prompt
    result = generate_image("测试图片")
    print(f"生成结果: {result}")
```
