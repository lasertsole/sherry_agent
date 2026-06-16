---
name: video_text_to_text
description: When the user needs to transcribe video (such as .mp4, .mkv, .avi) into text, use the python_repl tool to generate text.
---

## Use this if the backend supports video input natively

```python
from skills.builtin.core import vtt

if __name__ == '__main__':
    video_path: str = "{placeholder}"  # <- replace with the absolute path of the input video file
    query: str = "{placeholder}"  # <- replace with the query
    vtt(video_path, query)
```

## Use the fallback when video input is unsupported but image input is accepted

```python
from skills.builtin.core import vtt_fackback

if __name__ == '__main__':
    video_path: str = "{placeholder}"  # <- replace with the absolute path of the input video file
    query: str = "{placeholder}"  # <- replace with the query
    interval_sec: float = float(
        "{placeholder}")  # <- replace with the interval seconds, value in [0.5, 3.0],please accord to the video length
    vtt_fackback(video_path, query, interval_sec)
```