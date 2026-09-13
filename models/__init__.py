import importlib
from typing import Any

_MODULE_MAP = {
    "build_main_llm": ".LLMs",
    "build_reasoner_model": ".LLMs",
    "build_auxiliary_llm": ".LLMs",
    "NormalizingChatModel": ".LLMs",
    "ReasoningChatOpenAI": ".LLMs",
    "build_reasoning_kwargs": ".LLMs",
    "is_openai_reasoning_model": ".LLMs",
    "is_zhipu_reasoning_model": ".LLMs",
    "ITTT_model": ".ITTT_model",
    "VTTT_model": ".VTTT_model",
    "build_embed_model": ".embed_model",
    "reranker_model": ".reranker_model",
}

__all__ = list(_MODULE_MAP.keys())


def __getattr__(name: str) -> Any:
    if name in _MODULE_MAP:
        module = importlib.import_module(_MODULE_MAP[name], __package__)
        attr = getattr(module, name)
        globals()[name] = attr
        return attr

    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


def __dir__() -> list[str]:
    return __all__
