from .base import BaseChannel as BaseChannel


def __getattr__(name: str):
    if name == "channel_manager":
        from .manager import get_channel_manager

        return get_channel_manager()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
