"""Panel strings in zh and en (prototype ``src/client/locales.ts``)."""

from __future__ import annotations

STRINGS: dict[str, dict[str, str]] = {
    "overview": {"zh": "总览", "en": "Overview"},
    "source": {"zh": "来源", "en": "Source"},
    "trigger": {"zh": "触发器", "en": "Trigger"},
    "eval": {"zh": "评估", "en": "Eval"},
    "execution": {"zh": "执行", "en": "Execution"},
    "settings": {"zh": "设置", "en": "Settings"},
    "needs_attention": {"zh": "需要你处理", "en": "Needs your attention"},
    "healthy": {"zh": "运行正常", "en": "Healthy"},
    "retry": {"zh": "重试", "en": "Retry"},
    "stop": {"zh": "停止", "en": "Stop"},
    "evaluate_again": {"zh": "再次评估", "en": "Evaluate again"},
    "saved": {"zh": "已保存", "en": "Saved"},
    "stale_revision": {"zh": "设置已被其他人修改", "en": "Settings changed elsewhere"},
}


def text(key: str, lang: str = "en") -> str:
    """Return one translated string; unknown keys fall back to the key."""
    return STRINGS.get(key, {}).get(lang, STRINGS.get(key, {}).get("en", key))


def keys(lang: str) -> set[str]:
    """Return the keys available for a language."""
    return {key for key, value in STRINGS.items() if lang in value}


__all__ = ["STRINGS", "keys", "text"]
