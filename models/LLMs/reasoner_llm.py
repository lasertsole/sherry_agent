import os
from typing import Any
from pathlib import Path
from config.path import load_env
from config.features import LLM_CLIENT_DEFAULTS
from langchain.chat_models import init_chat_model
from models.LLMs.provider_max_output import configured_max_output, resolve_max_output_kwargs
from models.LLMs.provider_repetition_penalty import family_repetition_penalty_kwargs
from models.LLMs.reasoning_normalizer import NormalizingChatModel
from models.env_builder import ModelEnvBuilder

# Locate current directory
current_dir = Path(__file__).parent.resolve()

# Load environment variables (.env, then the network.jsonc overlay — which wins)
load_env()
max_tokens = os.getenv("REASONER_LLM_MAX_TOKEN")
if max_tokens:
    try:
        max_tokens = min(int(max_tokens), LLM_CLIENT_DEFAULTS["reasoner_max_tokens_cap"])
    except ValueError as exc:
        raise ValueError(
            f"REASONER_LLM_MAX_TOKEN must be an integer number of tokens, got {max_tokens!r}"
        ) from exc

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
        **resolve_max_output_kwargs(
            os.getenv("REASONER_LLM_PROVIDER"),
            os.getenv("REASONER_LLM_NAME"),
            configured_max_output("REASONER_LLM_MAX_OUTPUT_TOKEN"),
        ),
        **family_repetition_penalty_kwargs(
            "REASONER_LLM",
            os.getenv("REASONER_LLM_PROVIDER"),
            os.getenv("REASONER_LLM_NAME"),
        ),
    }
)


def build_reasoner_model():
    model = init_chat_model(**model_config)
    return NormalizingChatModel(inner=model)
