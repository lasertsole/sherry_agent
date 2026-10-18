---
name: text_to_image
description: When a user needs to generate an image from a text description, use the python_repl tool to call generate_image().
when_to_use: "When the user asks to generate or illustrate an image from a textual description."
when_not_to_use: "For image editing/understanding, diagrams better drawn as SVG/Mermaid, or when no image model is configured."
---

```python
from skills.builtin.text_to_image.scripts import generate_image

if __name__ == "__main__":
    # Test: pass a prompt directly
    prompt: str = "{placeholder}"  # <- the user's text description for the image
    generate_image(prompt)
```
