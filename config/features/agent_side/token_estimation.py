"""Token-estimation constant shared by truncation helpers."""

from typing import TypedDict


class TokenEstimationConfig(TypedDict):
    """Token-estimation constant shared by truncation helpers."""

    chars_per_token: int


TOKEN_ESTIMATION: TokenEstimationConfig = {
    "chars_per_token": 4,
}
