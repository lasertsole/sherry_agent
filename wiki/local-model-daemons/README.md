# 🧠 Local Model Daemons: Embed, Reranker & Local Chat

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> The local GGUF models (bge-m3 embeddings, bge-reranker, the auxiliary chat model) no longer load inside the orchestrator. Each family is served by a detached child process that loads its weights once, answers loopback HTTP JSON and exits after an idle window. The main process keeps only the request/response payloads.

**Source of truth:** `models/daemon/base.py`, `models/embed_model/text/daemon.py`, `models/embed_model/text/core.py`, `models/reranker_model/daemon.py`, `models/reranker_model/__init__.py`, `models/LLMs/local_daemon/server.py`, `models/LLMs/local_daemon/client.py`, `models/LLMs/auxiliary_llm/core.py`, `config/features/infra_side/local_daemons.py`, `packaging/backend_entry.py`, `skills/builtin/core/speech_to_text/scripts/core.py`. Every claim below was checked against that code and the measurements recorded in “Measured baseline”.

## 🎯 Why a daemon

The backend is ONE Python process (`server/__main__.py` pins `app.config.processes = 1`): it owns every session, the middleware chain, the WebSocket fan-out and the tool sandbox. Loading a 600 MB embedding model there, or a 5.5 GB chat model, makes the process that must stay responsive the one that is most likely to be killed for memory.

The measured problem with the old in-process embed path:

| Metric (bge-m3 q8_0, this host) | In-process (before) | Daemon (after) |
| :--- | :--- | :--- |
| Hot `Llama(...)` load per embed call | ~1.27–1.30 s | 0 (already loaded) |
| Inference per batch | ~0.32 s | ~0.49 s warm (including loopback HTTP) |
| Main-process RSS during a call | peak ~1017–1055 MB (`ru_maxrss`) | 46.5 → 47.3 MB (`VmRSS`) |
| Model RSS | inside the orchestrator | ~1045 MB in the daemon |
| Vector cosine vs in-process | — | 1.0 (identical) |

**This is memory isolation, not reduction.** With every family active the total (main + daemons) is roughly the same or slightly higher. The gains are: the orchestrator never peaks, a disabled feature spawns nothing, and warm calls are faster because the GGUF is not re-mapped per call.

## 🏗️ Architecture

```text
main process (orchestrator)                     daemon child (detached)
  _LocalEmbeddingBackend.embed(texts)  ─HTTP──▶  POST /embeddings
  _LocalGgufBackend.rank(...)          ─HTTP──▶  POST /rerank | /filter | /predict
  DaemonChatClient.create_chat_...     ─HTTP──▶  POST /v1/chat/completions
                                                 one GGUF, loaded once
                                                 inference serialised by a lock
                                                 idle watchdog exits the process
```

The client half is shared. `DaemonSpec` names the endpoint, the pid file and the import target; `models/daemon/base.py` implements the one lifecycle:

- **Readiness is strict.** `/healthy` must answer `{"ready": true}`. A live process whose model is still loading is NOT ready.
- **Spawn is de-duplicated.** A live recorded pid suppresses a second spawn; a stale pid file is replaced.
- **ReadyState is three-valued.** `READY`, `WARMING` (a process exists, keep waiting) and `UNAVAILABLE` (no process exists and none could be started). The distinction is what lets a client fall back without spawning a duplicate loader.
- **Kill** sends SIGTERM, waits up to 5 s, then SIGKILL, and reaps its own child. The atexit kill is registered only by the process that actually spawned the daemon.
- **Idle exit** is `IdleWatchdog`: after `*_idle_timeout_s` without a request the daemon calls `os._exit(0)`. The next call cold-starts it again.

### Endpoints, ports and pid files

| Family | Port | Pid file | Endpoints |
| :--- | :--- | :--- | :--- |
| Embed | 9012 | `.embed_daemon.pid` | `GET /healthy`, `POST /embeddings` |
| Reranker | 9013 | `.reranker_daemon.pid` | `GET /healthy`, `POST /rerank`, `/filter`, `/predict` |
| Local chat | 9014 | `.llm_daemon.pid` | `GET /healthy`, `POST /v1/chat/completions` |
| Vision chat | 9015 | `.vision_daemon.pid` | `GET /healthy`, `POST /v1/chat/completions` (mmproj) |
| STT (refactored) | 9011 | `.stt_daemon.pid` | `GET /healthy`, `POST /transcribe` |

Pid files and daemon logs live under `APP_HOME` (the writable app-data root in a packaged build; the checkout root otherwise), never under the read-only code tree. Every daemon binds `127.0.0.1` only and speaks JSON without auth, exactly like the existing STT daemon — it is a local cache, not a network service.

### Packaging

A checkout spawns the daemon with `python -c "from <module> import main; main()"`. A frozen PyInstaller build cannot run `-c`, so it re-executes the packaged backend with `--sherry-daemon <module>`; `packaging/backend_entry.py` dispatches to that module's `main()` BEFORE importing `server.__main__`, so a daemon child does not initialise logging, `nest_asyncio` or LangSmith. `--sherry-home` and `--sherry-desktop` are forwarded so the child resolves the same writable home.

## 🔌 The three clients

**Embedding** (`models/embed_model/text/core.py`). The daemon returns raw vectors; the client keeps the L2 normalisation and the ordering check. It sends the batches the call sites already build (`memory_embeddings` 32 per batch, the message indexer 64), so the index path did not need a second batching layer. One retry after a daemon error covers an idle exit or a crash; a second failure reaches the caller (semantic recall already fails open).

**Reranker** (`models/reranker_model/__init__.py`). `rank`, `filter` and `predict` map to one endpoint each; `predict_scores` posts the whole pair list in one request instead of one round-trip per pair. `RERANKER_MODEL_LOCAL=false` keeps the cloud backend untouched.

**Local chat and vision** (`models/LLMs/auxiliary_llm/core.py` / `models/LLMs/base_local_llama.py`). The local GGUF path does NOT use native tool calls: `LocalToolBinder` injects a JSON instruction and `LocalStructuredOutput` patches `create_chat_completion_openai_v1` with instructor. The daemon therefore exposes llama-cpp's own `create_chat_completion` shape, and `models/LLMs/local_daemon/client.py` is a small shim implementing exactly those two methods. `create_chat_completion_openai_v1` validates the response into an OpenAI `ChatCompletion` (adding the id/object/created/model fields instructor expects) and never forwards instructor's control kwargs. `_LocalAuxiliaryChatModel` falls back to in-process `Llama` when the daemon cannot start or is still warming past its budget — behavior first, isolation second. The multimodal base reuses the same client against the port-9015 daemon; its `image_url` content blocks travel unchanged to llama-cpp's Qwen2.5-VL handler.

## 🛟 Failure behaviour and switches

| Situation | Behaviour |
| :--- | :--- |
| Daemon idles out between calls | The client retries once; `ensure_ready` respawns it |
| Daemon is warming (model still loading) | `WARMING`; embed/reranker raise a retryable error; local chat falls back in-process |
| No daemon process can be started | `UNAVAILABLE`; embed/reranker use their historical in-process loader for the rest of the process; local/vision chat use in-process `Llama` |
| Daemon crashes mid-request | The request fails; memory recall keeps its cosine order, index building logs the error |
| Feature disabled | `EMBEDDING_DAEMON_ENABLED=false`, `RERANKER_DAEMON_ENABLED=false`, `AUXILIARY_LLM_DAEMON_ENABLED=false` restore the old path with no daemon at all |

Every setting lives in `LOCAL_DAEMONS` (`config/features/infra_side/local_daemons.py`) and has an environment override: ports (`*_DAEMON_PORT`), readiness budgets (`*_READY_WAIT_S`), idle windows (`*_DAEMON_IDLE_TIMEOUT_S`), request timeouts (`*_HTTP_TIMEOUT_S`), `AUXILIARY_LLM_N_CTX` and `VISION_LLM_DAEMON_*`. The shipped `auxiliary_n_ctx` is the historical 40 960; lowering it also caps the local `max_tokens` at half the window, so a smaller KV cache cannot be paired with an oversized completion request.

## 📏 Measured baseline (2026-11-11, this host)

| Measurement | Result |
| :--- | :--- |
| `import server.__main__` | peak RSS ~26.7 MB, ~0.86 s |
| `agent.core` + `server.trigger.http` | peak RSS ~153.9 MB, ~5.2 s |
| Boot import set | `torch`, `transformers`, `cv2`, `mineru_vl_utils`, `leidenalg`, `snkv`, `tree_sitter`, `gguf`, `llama_cpp` all absent |
| Embed daemon first call (spawn + load) | 3.8 s |
| Embed warm call | 0.49 s (in-process ~2.88 s) |
| Main RSS during embedding | 46.5 → 47.3 MB |
| Daemon RSS | 1045.6 MB |
| Vector cosine (daemon vs in-process) | 1.0 |
| Reranker daemon first call (spawn + 606 MB GGUF load) | 32.8 s |
| Reranker warm rank (4 documents) | 1.33 s (in-process ~31.5 s per call, reload included) |
| Main RSS during rerank | 46.6 → 46.9 MB; daemon 898.4 MB |
| Reranker scores/order (daemon vs in-process) | identical |
| Local chat first invoke (Qwen2.5-0.5B-Instruct Q4, spawn + load) | 3.33 s |
| Local chat warm invoke | 0.77 s |
| Main RSS during local chat | 65.3 → 65.9 MB; daemon 600.4 MB |
| Local chat tool call + structured output | parsed `add(a=2,b=2)`; `Answer(answer='Paris')` |
| Same model loaded in-process | +537.9 MB main-process peak |
| Vision first invoke (Qwen2.5-VL-3B Q4 + mmproj, 96 px image) | 13.61 s (CPU: image encode + prompt) |
| Vision warm invoke | 7.44 s |
| Main RSS during vision | 67.3 → 20.2 MB (peak 68.2); daemon 3 571 MB |
| Vision answer | `Red` (correct), on port 9015 |

**Direction “lazy heavy imports” was skipped by its MEASURED kill criterion:** the full boot chain (module boot + agent core + curator/cron + trigger + services) imports none of `torch` / `transformers` / `cv2` / `mineru_vl_utils` / `leidenalg` / `snkv` / `tree_sitter` / `gguf` / `llama_cpp`. The dependencies are not small either — importing them alone costs ~490 MB / 5.9 s (`torch`), ~41 MB / 9.9 s (`transformers`), and the rest are absent from the base environment — so they are already where the optimization wants them: behind their feature functions. The guard `tests/perf/test_boot_import_set.py` fails if any re-enters boot. The remaining startup cost belongs to `openai` / `langchain` / `langgraph` and the logging/config boot, which every route needs. (`PIL` is boot-imported via the media-compression module; measured at ~49 ms and no measurable RSS peak, so moving it was not worth the churn.)

**Vision parity.** ITTT/VTTT run through the SAME local chat daemon on port 9015: the daemon reads `SHERRY_LOCAL_LLM_MMPROJ_PATH` and adds `Qwen25VLChatHandler` with `n_batch = n_ubatch = n_ctx`, mirroring the in-process `LocalMultimodalLlamaChatBase`. Both families resolve the same Qwen2.5-VL weights + mmproj, so they share one vision daemon; a second distinct vision model would need its own port. `LocalMultimodalLlamaChatBase` tries the daemon first and falls back to the in-process client when it cannot start. Safety moderation still shares the auxiliary client; a moderation call queued behind a long generation can hit its 20 s fail-open budget, which is the same outcome as today.

## 🧪 Tests

- `tests/models/daemon/test_base.py` — pid file, readiness tri-state, spawn command (checkout vs frozen), atexit, kill/reap, request decoding, idle watchdog.
- `tests/models/embed_model/text/test_embed_daemon.py` — real loopback server over a fake model: ordering, batching, validation, health; client: payload, retry, warming, fallback, normalisation.
- `tests/models/reranker_model/test_daemon.py` — rank/filter/predict wire shapes, validation, client retry and fallback.
- `tests/models/LLMs/local_daemon/test_local_daemon.py` — llama-cpp kwargs forwarding, instructor-compatible `ChatCompletion`, aux-model daemon/fallback wiring, `n_ctx`/`max_tokens` clamping, vision daemon (mmproj handler, port 9015) and its fallback.
- `tests/packaging/test_backend_entry.py` — `--sherry-daemon` parsing and dispatch.
- `tests/skills/builtin/core/speech_to_text/test_stt_daemon_pid.py` — the STT refactor keeps the same pid-file contract on the shared lifecycle.
- `tests/config/test_features_infra_side.py` — `LOCAL_DAEMONS` defaults, env overrides, invalid-value fallbacks.

## ⚠️ Limits

- One model instance per port. Two different auxiliary GGUFs (a second local profile) cannot share the daemon; the path is resolved at spawn and the daemon serves the first one it loaded.
- No request queue fairness: `ThreadingHTTPServer` plus one inference lock means the ordering is whatever the OS scheduler gives the waiting threads. A long batch delays a recall, which is acceptable for a local cache.
- No authentication, no TLS, no cross-host access. The daemons are loopback-only by construction (`127.0.0.1`), like the STT daemon.
- The local chat daemon is non-streaming. The local auxiliary path never streams (its call sites are `invoke`/`ainvoke`), so a `stream: true` request is refused instead of being silently downgraded.
