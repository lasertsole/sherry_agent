"""Token-estimation constants shared by truncation helpers."""

from typing import TypedDict


class TokenEstimationConfig(TypedDict):
    """Token-estimation constants shared by truncation helpers."""

    chars_per_token: int
    chars_per_token_cjk: int


TOKEN_ESTIMATION: TokenEstimationConfig = {
    "chars_per_token": 4,
    "chars_per_token_cjk": 2,
}
