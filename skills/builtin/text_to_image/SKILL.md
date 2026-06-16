---
name: text_to_image
description: When a user needs to generate an image from a text description, use the python_repl tool to call generate_image().
---

```python
from skills.builtin.text_to_image.scripts import generate_image

if __name__ == '__main__':
    # Test: pass a prompt directly
    prompt: str = "{placeholder}"  # <- the user's text description for the image
    generate_image(prompt)
```
