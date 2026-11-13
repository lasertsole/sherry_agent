---
name: text_to_image
description: When a user needs to generate an image from a text description, use the terminal tool to run a small Python script that calls generate_image().
when_to_use: "When the user asks to generate or illustrate an image from a textual description."
when_not_to_use: "For image editing/understanding, diagrams better drawn as SVG/Mermaid, or when no image model is configured."
---

Run this through the terminal tool. It injects `SHERRY_SESSION_ID`, which the
script needs to store the image in the session's artifact store:

```python
from skills.builtin.text_to_image.scripts import generate_image

if __name__ == "__main__":
    prompt: str = "{placeholder}"  # <- the user's text description for the image
    # Optional: size="1024x1024", quality="standard", n=2, model="gpt-image-1"
    print(generate_image(prompt))
```

`generate_image` prints one `sherry-artifact://<session>/<name>` URI per image
and returns the same URIs newline-joined. The chat renders those references as
thumbnails in the tool card (click for the full image), so relay the URIs (or
the returned text) in your answer instead of describing the file path. `n` is
clamped by the model's own limit (`dall-e-3` always returns one image). On
failure the script prints `[error] ...` and returns an empty string — report
that error to the user instead of retrying blindly.
