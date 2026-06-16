---
name: image_to_text
description: Parse an image from a file path to obtain a description, enabling non-multimodal LLMs to have vision capabilities.
---

**Parse a regular image:**

```python
from skills.builtin.core.image_to_text.scripts import itt

if __name__ == '__main__':
    user_text: str = "{placeholder}"  # <- replace with the absolute path of the input image
    image_path: str = "{placeholder}"  # <- input the user's question about the image

    itt(image_path=image_path, user_text=user_text)
```
