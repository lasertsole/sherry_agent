"""Live-stream safety filter: keyword rules + optional LLM moderation.

Nothing here is a bundled word list — the module only defines the INFRASTRUCTURE
(a rules file the operator fills in), because shipping a curated list of
"sensitive words" is a legal question this project does not answer. ``enabled``
is FALSE by default, so an untouched install is byte-for-byte the old behaviour.

Editable values (``enabled`` / ``keyword_file`` / ``llm_moderation_*``) live in
``sherry.jsonc`` as ``safety_filter.*`` and are read LIVE (mtime-cached), not at
import time: the 系统配置 → 直播安全 tab flips them from a switch, and a switch
that needs a backend restart is not a switch.
"""

from typing import TypedDict


class SafetyFilterConfig(TypedDict):
    """Bounds and defaults the safety-filter middleware obeys."""

    #: Master switch (sherry.jsonc ``safety_filter.enabled``). OFF means every
    #: hook early-returns — the pre-filter behaviour.
    enabled: bool
    #: Keyword rules file, relative to the workspace directory.
    keyword_file: str
    #: Streaming hold-back window (characters). Text is scanned BEFORE it is
    #: emitted; this many characters stay unseen until the next scan, so a
    #: pattern longer than this can only be caught at end-of-stream.
    stream_buffer_window: int
    #: ``off`` | ``on_keyword_hit`` | ``always`` (sherry.jsonc
    #: ``safety_filter.llm_moderation_mode``).
    llm_moderation_mode: str
    #: Model name for the review call (sherry.jsonc
    #: ``safety_filter.llm_moderation_model``). EMPTY = not configured, and the
    #: moderation stays off whatever the mode says.
    llm_moderation_model: str
    #: Reply the input filter returns instead of calling the model.
    safe_refusal_message: str
    #: Text the output/stream filters substitute for blocked content.
    safe_replacement_message: str
    #: Bounded per-session filter log (the settings tab lists the newest ones).
    log_max_entries: int


SAFETY_FILTER: SafetyFilterConfig = {
    "enabled": False,
    "keyword_file": "sensitive_words.json",
    "stream_buffer_window": 20,
    "llm_moderation_mode": "off",
    "llm_moderation_model": "",
    "safe_refusal_message": "抱歉，我无法回答这个问题。",
    "safe_replacement_message": "[该内容已被安全过滤拦截]",
    "log_max_entries": 200,
}
