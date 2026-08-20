---
name: speech_to_text
description: Transcribe speech (such as .mp3, .wav, .ogg) into text. Use whenever the user uploads or refers to an audio file and expects you to understand its spoken content. Enables non-multimodal LLMs to transcribe audio by running this skill's script via the terminal tool.
---

## Usage

This skill provides the STT recognition logic. Use skill discovery to load this SKILL.md, then run the script with the **`terminal` tool** (the `python_repl` tool cannot import skill modules because its builtins are restricted).

- When audio needs transcribing, call `skill_view` with name `speech_to_text` (this file), then run:

```bash
python -c "from skills.builtin.core.speech_to_text.scripts import stt; print(stt(audio_path='{replace with the audio location}'))"
```

- `audio_path` accepts a local absolute file path.
- The first-ever call cold-starts a local STT daemon in the background, so it may return a `[warm-up]` notice instead of text. On receiving `[warm-up]`, **retry the exact same command shortly** (a few seconds) — once the model finishes loading it returns the transcription promptly. Do not abandon the task on `[warm-up]`; it is a signal to retry, not an error.
- The printed output is the ground-truth transcription text — base your reply on it.