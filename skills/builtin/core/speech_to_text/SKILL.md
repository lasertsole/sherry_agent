---
name: speech_to_text
description: When the user needs to transcribe speech (such as .mp3, .wav, .ogg) into text, use the python_repl tool to generate text.
---

```python
from skills.builtin.core.speech_to_text.scripts import stt

if __name__ == '__main__':
    audio_path: str = "{placeholder}"  # <- replace with the absolute path of the input audio file
    res = stt(audio_path)
    print(res)
```