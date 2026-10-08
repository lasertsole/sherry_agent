"""Silent-degradation heuristic for native multimodal attempts.

When a model accepts the media blocks without error but ignores them, the
capability cache is never updated and every later turn retries the native
probe. The only evidence available is the model's own reply, so this module
detects the high-precision signal: the model *self-reporting* that it cannot
perceive media, or explicitly asking the user to describe the attached media.

The literal "the reply never mentions the image content" heuristic from the
original plan is deliberately NOT implemented — a capable model can describe
an image without using any media keyword (``The cat is orange``), so that
heuristic would mark capable models blind and silently degrade quality.
Precision wins over recall here: a false negative merely retries the native
probe once more, while a false positive forces the slower skill path forever.
"""

import re

_MEDIA_CONTEXT_WINDOW = 40

_BLINDNESS_RE = re.compile(
    r"(?:"
    r"cannot see|can'?t see|can not see|unable to see"
    r"|cannot view|can'?t view|can not view|unable to view"
    r"|cannot process|can'?t process|unable to process"
    r"|cannot access|can'?t access|unable to access"
    r"|do(?:n'?t| not) have (?:the )?(?:ability|capacity) to see"
    r"|no vision"
    r"|无法(?:查看|看到|看见|识别|访问|读取|感知)"
    r"|看不到|看不见|没有视觉|沒有視覺|无视觉|無視覺"
    r"|画像を見ることが(?:でき|出来)(?:ません|ない|ず|なく|ぬ|まへん)"
    r"|画像を認識でき(?:ません|ない|ず|なく|ぬ)"
    r"|画像が見え(?:ません|ない|ず|なく|ぬ)"
    r"|視覚がな(?:い|く|かっ)"
    r"|이미지를 볼 수 없|이미지를 인식할 수 없|시각이 없"
    r")",
    re.IGNORECASE,
)

_DESCRIBE_REQUEST_RE = re.compile(
    r"(?:"
    r"please describe|could you describe|can you describe|would you describe"
    r"|请描述|請描述|麻烦描述|能否描述"
    r"|説明して|説明していただけ|説明してもらえ"
    r"|설명해|설명해 주|설명 부탁"
    r")",
    re.IGNORECASE,
)

_MEDIA_CONTEXT_RE = re.compile(
    r"(?:"
    r"image|images|picture|pictures|photo|photos|media|attachment|attachments"
    r"|vision|visual"
    r"|图片|图像|照片|媒体|媒體|附件|图|圖|视觉|視覺"
    r"|画像|写真|メディア|添付|視覚"
    r"|이미지|사진|미디어|첨부|시각"
    r")",
    re.IGNORECASE,
)


def _has_media_context(text: str, start: int, end: int) -> bool:
    """True when a media word sits within the window around a match.

    The window keeps an unrelated "I can't see why…" in prose from matching a
    media word that appears far away in the same reply.
    """
    window = text[max(0, start - _MEDIA_CONTEXT_WINDOW) : end + _MEDIA_CONTEXT_WINDOW]
    return _MEDIA_CONTEXT_RE.search(window) is not None


def detect_media_blindness(text: str) -> bool:
    """True when `text` self-reports an inability to perceive media.

    Pure regex, case-insensitive, no LLM call. A blindness phrase only counts
    when a media word appears near it (or the phrase itself names the media),
    which keeps the detector precision-first per the module docstring.
    """
    if not text:
        return False
    for match in _BLINDNESS_RE.finditer(text):
        if _has_media_context(text, match.start(), match.end()):
            return True
    for match in _DESCRIBE_REQUEST_RE.finditer(text):
        if _has_media_context(text, match.start(), match.end()):
            return True
    return False
