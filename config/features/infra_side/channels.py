"""Channel dependency-install timeout."""

from typing import TypedDict


class ChannelsConfig(TypedDict):
    """Channel dependency-install timeout."""

    dep_install_timeout_seconds: int


CHANNELS: ChannelsConfig = {
    "dep_install_timeout_seconds": 120,
}
