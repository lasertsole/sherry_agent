import os
from typing import Any
from config import ENV_PATH
from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain_core.runnables import ConfigurableField

# Load environment variables
load_dotenv(ENV_PATH, override = True)
api_key = os.getenv("MAIN_LLM_API_KEY")
api_name = os.getenv("MAIN_LLM_NAME")
model_provider = os.getenv("MAIN_LLM_PROVIDER")
api_base = os.getenv("MAIN_LLM_API_BASE")
max_tokens = os.getenv("MAIN_LLM_MAX_TOKEN")
if max_tokens:
    max_tokens = min(int(max_tokens), 65536)

model_config:dict[str, Any] = {
    "model_provider": model_provider,
    "model": api_name,
    "api_key": api_key,
    "base_url": api_base,
    "temperature": 0,
    "max_retries": 2,
    "profile": {"max_input_tokens": max_tokens}  # Set model context window size
}
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
    model = model.configurable_fields(
        temperature=ConfigurableField(id="temperature")
    )
    if temperature is not None:
        model = model.bind(temperature=temperature)
    return model
