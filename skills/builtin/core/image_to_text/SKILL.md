---
name: image_to_text
description: Recognize and describe the content of an image. Accepts either a local file path OR a URL (http/https). Use this whenever the user uploads or refers to an image and expects you to see/analyze it — even if the image is provided as a URL, not a local path. Enables non-multimodal LLMs to have vision capabilities.
---

**Recognize an image (local file path OR URL):**

The `image_path` argument accepts BOTH a local absolute file path AND an http/https URL. If the user uploaded an image, the middleware message tells you its exact location — pass that value directly to `image_path`.

Use the **`terminal` tool** to run the skill script (the `python_repl` tool cannot import skill modules because its builtins are restricted). Run:

```bash
python -c "from skills.builtin.core.image_to_text.scripts import itt; print(itt(image_path='{replace with the image location}', user_text='{replace with the user question about the image}'))"
```

- `image_path`: the image's local path or URL given in the middleware message.
- `user_text`: the user's question/instruction about the image.

**Priority:** When the user uploads or asks you to look at an image, you MUST run this skill script and actually recognize the image — do not answer with a generic response without looking at it. The printed output is the ground-truth recognition result.
