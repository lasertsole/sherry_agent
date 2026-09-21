import os
from typing import Any
from pathlib import Path
from config import ENV_PATH
from config.features import LLM_CLIENT_DEFAULTS
from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from models.LLMs.reasoning_normalizer import NormalizingChatModel
from models.env_builder import ModelEnvBuilder

# Locate current directory
current_dir = Path(__file__).parent.resolve()

# Load environment variables
load_dotenv(ENV_PATH, override=True)
max_tokens = os.getenv("REASONER_LLM_MAX_TOKEN")
if max_tokens:
    max_tokens = min(int(max_tokens), LLM_CLIENT_DEFAULTS["reasoner_max_tokens_cap"])

model_config: dict[str, Any] = ModelEnvBuilder(
    {
        "model_provider": "REASONER_LLM_PROVIDER",
        "model": "REASONER_LLM_NAME",
        "api_key": "REASONER_LLM_API_KEY",
        "base_url": "REASONER_LLM_API_BASE",
    },
    strip=False,
).build(
    {
        "temperature": 0.5,
        "max_retries": LLM_CLIENT_DEFAULTS["reasoner_max_retries"],
        # Explicit bounded window for each LLM request (seconds).
        "timeout": LLM_CLIENT_DEFAULTS["reasoner_timeout"],
        "profile": {"max_input_tokens": max_tokens},  # Set model context window size
    }
)


def build_reasoner_model():
    model = init_chat_model(**model_config)
    return NormalizingChatModel(inner=model)
