import os
from typing import Any
from config import ENV_PATH
from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from models.LLMs.reasoning_normalizer import NormalizingChatModel

# Load environment variables
load_dotenv(ENV_PATH, override = True)
api_key = os.getenv("MAIN_LLM_API_KEY")
api_name = os.getenv("MAIN_LLM_NAME")
model_provider = os.getenv("MAIN_LLM_PROVIDER")
api_base = os.getenv("MAIN_LLM_API_BASE")
max_tokens = os.getenv("MAIN_LLM_MAX_TOKEN")
if max_tokens:
    max_tokens = int(max_tokens)

# DeepSeek thinking mode for deepseek-chat (V3.2+) is OFF by default
# (matching the non-reasoning behaviour users expect from deepseek-chat).
# Set MAIN_LLM_ENABLE_THINKING=true in .env to opt in. When enabled, the mode
# is carried via extra_body so langchain's ChatDeepSeek threads it directly into
# the API request body. Thinking mode supports tool calls; the chain-of-thought
# surfaces on AIMessageChunk.additional_kwargs["reasoning_content"] and is
# streamed to the client as {"type": "reasoning"} by server/service/messages.py.
enable_thinking = os.getenv("MAIN_LLM_ENABLE_THINKING", "").strip().lower() == "true"

model_config:dict[str, Any] = {
    "model_provider": model_provider,
    "model": api_name,
    "api_key": api_key,
    "base_url": api_base,
    "temperature": 0,
    "max_retries": 2,
    "profile": {"max_input_tokens": max_tokens},  # Set model context window size
}
if enable_thinking:
    model_config["extra_body"] = {"thinking": {"type": "enabled"}}
model_config = {k: v for k, v in model_config.items() if v is not None and v != ""}

def build_main_llm(temperature: float | None = None):
    """Create a fresh LLM instance bound to the current event loop.

    The module-level ``main_llm`` singleton is created at import time on the
    main thread.  Its internal ``openai.AsyncOpenAI`` → ``httpx.AsyncClient``
    transport pool contains ``asyncio.Lock`` objects that are bound to the
    event loop active at creation time.  When the subagent daemon thread
    tries to use this same client via ``agent.ainvoke()``, those locks
    deadlock silently.

    Call this factory from any async context (e.g. the subagent daemon
    thread) to get a fresh instance whose transport pool is correctly
    bound to the *current* event loop.
    """
    model = init_chat_model(**model_config)
    model = NormalizingChatModel(inner=model)
    if temperature is not None:
        model = model.bind(temperature=temperature)
    return model
