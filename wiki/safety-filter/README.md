# Live-Stream Safety Filter

> Keyword rules on input and output, an optional LLM review, and a stream cut
> that scans text BEFORE it is sent.

## 🎯 Overview

Nothing between the model and the viewer filtered text before this feature.
The filter adds three layers, all OFF by default (`sherry.jsonc`
`safety_filter.enabled`):

1. **Input keyword rules** — a blocked message is refused without calling the
   provider (`agent/middlewares/safety_filter/middlewares.py::InputSafetyFilter`).
2. **LLM review (optional)** — a configured review model judges the message; the
   review only runs with the master switch, a non-`off` strategy AND a model name.
3. **Output keyword rules** — the finished answer is blocked or partly replaced
   (`OutputSafetyFilter`), and a streamed answer is scrubbed chunk by chunk by
   `agent/middlewares/safety_filter/stream_guard.py`.

The operator owns the rules: the app ships **no word list**, because a curated
list is a legal decision this project does not make. Rules live in the file
named by `safety_filter.keyword_file` under the workspace directory.

## ⚙️ Configuration

`sherry.jsonc`, read LIVE (mtime-cached, no restart):

- `safety_filter.enabled` — the master switch.
- `safety_filter.llm_moderation_mode` — `off` / `on_keyword_hit` / `always`.
- `safety_filter.llm_moderation_model` — review model name; EMPTY means the LLM
  review never runs, whatever the strategy says.
- `safety_filter.keyword_file` — rules file name inside the workspace.

The settings tab is **System Config → Live safety** (`SafetyFilterPanel.vue`): the master
switch, the LLM switch (disabled until a model name is typed), the strategy
select, the rules editor with a test scan, and the recent filter events.

## 🧩 Layers

- Rules: `{"rules": [{"pattern": "…", "type": "exact|contains|regex",
  "action": "block|replace|warn", "scope": "input|output|both",
  "replacement": "…"}]}`. Validation caps the rule count, pattern length and
  replacement length, and refuses an invalid regex before writing.
- `block` refuses / replaces the whole answer; `replace` substitutes the hit;
  `warn` only reports. The input side never rewrites the user's own message.
- Every decision records one event (bounded ring, newest first) and pushes a
  `safety_filter` WS frame; the client shows a toast (block = error level).
- The LLM review fails OPEN: a timeout, a request failure or an unparseable
  answer lets the turn through and logs a warning — the provider's own
  alignment is still in the loop.

## 🚂 Streaming Guard

`StreamTextGuard` holds the last `stream_buffer_window` characters (20 by
default) and releases only text it has already scanned:

- a `block` match anywhere in the buffer latches the guard, drops the buffered
  text and emits the replacement notice once;
- a `replace` match ending inside the releasable prefix is substituted first; a
  match that the release point would cut in half holds the release back;
- a pattern longer than the window can only complete in the tail, so it is
  caught by the end-of-stream flush — at most `len(match) - 1` characters can be
  visible before the cut.

A streaming answer never reaches `OutputSafetyFilter`: the guard scrubs each
chunk before it becomes a frame, so the client and the persisted row agree.

## 🌐 HTTP API

| Method | Path | Body / query |
| :--- | :--- | :--- |
| `GET` | `/safety-filter/rules` | — |
| `PUT` | `/safety-filter/rules` | `{rules: [...]}` |
| `POST` | `/safety-filter/test` | `{text, scope}` |
| `GET` | `/safety-filter/log` | `?session_id=<sid>&limit=50` |

The switches and the review model are `sherry.jsonc` keys written through
`PUT /sherry-config`, not a bespoke endpoint.

## 🧪 Tests

`tests/agent/middlewares/safety_filter/test_rules.py` (validation, scopes,
hot reload, literal replacement), `test_middlewares.py` (input block without a
provider call, warn/replace pass-through, LLM gating and fail-open, output
replacement) and `test_stream_guard.py` (hold-back window, block latch,
replacement before emission, the end-of-stream flush). The settings tab is
pinned by `client/app/pages/home/components/__tests__/SafetyFilterPanel.integration.test.ts`.
