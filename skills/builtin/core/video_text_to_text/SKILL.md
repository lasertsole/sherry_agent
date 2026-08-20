---
name: video_text_to_text
description: Describe/transcribe video content (such as .mp4, .mkv, .avi) into text. Use whenever the user uploads or refers to a video file and expects you to understand what happens in it. Enables non-multimodal LLMs to understand video by running this skill's script via the terminal tool.
---

## Usage

This skill provides the VTT recognition logic. Use skill discovery to load this SKILL.md, then run the script with the **`terminal` tool** (the `python_repl` tool cannot import skill modules because its builtins are restricted).

- When video needs describing, call `skill_view` with name `video_text_to_text` (this file), then run:

```bash
python -c "from skills.builtin.core.video_text_to_text.scripts import vtt; print(vtt(video_path='{replace with the video location}', query='{replace with the question about the video}'))"
```

- `video_path` accepts a local absolute file path.
- `query` defaults to "what happen in the video?" when omitted.
- The script automatically falls back to frame-level image extraction when needed. The printed output is the ground-truth description — base your reply on it.