"""Message-bus bounded-queue configuration."""

from typing import TypedDict


class BusConfig(TypedDict):
    """Message-bus bounded-queue configuration."""

    queue_maxsize: int


BUS: BusConfig = {
    "queue_maxsize": 1000,
}
