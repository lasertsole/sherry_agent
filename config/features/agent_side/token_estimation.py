"""Token-estimation constants shared by truncation helpers."""

from typing import TypedDict


class TokenEstimationConfig(TypedDict):
    """Token-estimation constants shared by truncation helpers."""

    chars_per_token: int
    chars_per_token_cjk: int
    # Fixed per-block costs for a multimodal content list. A base64 / ``data:``
    # payload must never be counted as text: 5 MB of base64 is ~1.25M "tokens"
    # at ``chars_per_token=4``, which would fire compression on a single image.
    tokens_per_image_block: int
    tokens_per_audio_block: int
    tokens_per_video_block: int
    tokens_per_unknown_block: int


TOKEN_ESTIMATION: TokenEstimationConfig = {
    "chars_per_token": 4,
    "chars_per_token_cjk": 2,
    # 85 is the fixed cost ``langchain_core.count_tokens_approximately`` uses
    # per image block (OpenAI's low-resolution image token cost).
    "tokens_per_image_block": 85,
    # Audio / video carry no duration metadata at estimation time, so a real
    # token count is not derivable. These values are deliberately conservative
    # (over-estimate rather than leak the base64 char count) yet tiny against
    # the 128K window: audio charges a short utterance's transcription cost,
    # video charges roughly twelve sampled image frames.
    "tokens_per_audio_block": 256,
    "tokens_per_video_block": 1024,
    # Any block the classifier does not recognise — including an unknown type
    # hiding a base64 payload — is charged the image floor, so it can never be
    # counted as text while arbitrary cheap blocks stay cheap.
    "tokens_per_unknown_block": 85,
}
