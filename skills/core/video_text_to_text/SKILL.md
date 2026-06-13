---
name: video_text_to_text
description: When the user needs to transcribe video (such as .mp4, .mkv, .avi) into text, use the python_repl tool to generate text.
---

```python
from skills.core.video_text_to_text.scripts import vtt

if __name__ == '__main__':
    video_path: str = "{placeholder}"  # <- replace with the absolute path of the input video file
    vtt(video_path)
```