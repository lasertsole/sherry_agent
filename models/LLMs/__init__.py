import importlib
from typing import Any

_MODULE_MAP = {
    "ReasoningChatOpenAI": ".reasoning_openai",
    "build_main_llm": ".main_llm",
    "build_reasoner_model": ".reasoner_llm",
    "build_auxiliary_llm": ".auxiliary_llm",
    "NormalizingChatModel": ".reasoning_normalizer",
    "build_reasoning_kwargs": ".reasoning_payload",
    "is_openai_reasoning_model": ".reasoning_payload",
    "is_zhipu_reasoning_model": ".reasoning_payload",
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
