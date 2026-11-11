"""Local chat-model daemon (auxiliary LLM) — server and its client shim.

The auxiliary GGUF used to be loaded inside the orchestrator on first use and
released after every generation. The daemon keeps the 9B model resident in a
child process, so the main process never maps the weights or allocates the KV
cache; the client is a drop-in replacement for the two raw llama-cpp methods the
local chat path uses (``create_chat_completion`` and
``create_chat_completion_openai_v1``).
"""
