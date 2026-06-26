import os
from typing import Any
from pathlib import Path
from config import ENV_PATH
from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain_core.runnables import ConfigurableField

# Locate current directory
current_dir = Path(__file__).parent.resolve()

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
    "profile": {"max_input_tokens": max_tokens, "repetition_penalty": 1.2}  # Set model context window size
}
model_config = {k: v for k, v in model_config.items() if v is not None and v != ""}
main_llm = init_chat_model(**model_config)  # Instantiate model
main_llm = main_llm.configurable_fields(
    temperature=ConfigurableField(
        id="temperature",
    )
)