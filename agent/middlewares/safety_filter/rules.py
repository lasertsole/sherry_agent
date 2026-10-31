"""Keyword rules: load, validate, compile, scan, replace.

The rules FILE is operator-owned (nothing is bundled). Shape::

    {"version": 1,
     "rules": [{"pattern": "…", "type": "exact|contains|regex",
                "action": "block|replace|warn", "replacement": "…",
                "scope": "input|output|both"}]}

Loading is mtime-cached, so editing the file takes effect without a restart
(the next scan re-compiles). Everything is fail-open: a broken file reads as
"no rules" and never raises into a model call.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from config.features import SAFETY_FILTER
from config.path import WORKSPACE_DIR

__all__ = [
    "FilterHit",
    "apply_replacements",
    "clear_rules_cache",
    "load_rules",
    "rules_file",
    "rules_snapshot",
    "scan",
    "validate_rules",
    "write_rules",
]

#: Caps that keep a hand-edited file from turning every model call into work.
MAX_RULES = 200
MAX_PATTERN_CHARS = 200
MAX_REPLACEMENT_CHARS = 200

_ACTIONS = ("block", "replace", "warn")
_TYPES = ("exact", "contains", "regex")
_SCOPES = ("input", "output", "both")


@dataclass(frozen=True, slots=True)
class CompiledRule:
    """One validated rule with its pattern pre-compiled (regex) or literal."""

    pattern: str
    match_type: str
    action: str
    replacement: str
    scope: str
    regex: re.Pattern[str] | None = None

    def search(self, text: str) -> str | None:
        """The first matched text, or None."""
        if self.match_type == "exact":
            return self.pattern if text == self.pattern else None
        if self.match_type == "regex":
            match = self.regex.search(text) if self.regex is not None else None
            return match.group(0) if match else None
        return self.pattern if self.pattern in text else None


@dataclass(frozen=True, slots=True)
class FilterHit:
    """One rule that matched a piece of text."""

    pattern: str
    matched_text: str
    action: str
    replacement: str
    scope: str


_LOCK = threading.Lock()
_rules: tuple[CompiledRule, ...] = ()
_loaded_key: tuple[str, int, int] | None = None


def rules_file() -> Path:
    """Absolute path of the keyword rules file (workspace-relative setting)."""
    name = str(SAFETY_FILTER["keyword_file"] or "sensitive_words.json").strip()
    # The setting is a FILE NAME inside the workspace: refuse traversal.
    safe = Path(name).name or "sensitive_words.json"
    return WORKSPACE_DIR / safe


def validate_rules(payload: Any) -> list[dict[str, Any]]:
    """Validate a whole rules document; returns the normalized rule list.

    Raises ``ValueError`` with a human message for the first problem — the save
    endpoint relays it to the settings tab.
    """
    if not isinstance(payload, dict):
        raise ValueError("rules document must be an object")
    rules = payload.get("rules")
    if not isinstance(rules, list):
        raise ValueError("'rules' must be a list")
    if len(rules) > MAX_RULES:
        raise ValueError(f"too many rules (max {MAX_RULES})")
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(rules, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"rule #{index}: must be an object")
        pattern = raw.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip():
            raise ValueError(f"rule #{index}: 'pattern' must be a non-empty string")
        if len(pattern) > MAX_PATTERN_CHARS:
            raise ValueError(f"rule #{index}: pattern longer than {MAX_PATTERN_CHARS} chars")
        match_type = str(raw.get("type") or "contains").strip().lower()
        if match_type not in _TYPES:
            raise ValueError(f"rule #{index}: 'type' must be one of {', '.join(_TYPES)}")
        action = str(raw.get("action") or "block").strip().lower()
        if action not in _ACTIONS:
            raise ValueError(f"rule #{index}: 'action' must be one of {', '.join(_ACTIONS)}")
        scope = str(raw.get("scope") or "both").strip().lower()
        if scope not in _SCOPES:
            raise ValueError(f"rule #{index}: 'scope' must be one of {', '.join(_SCOPES)}")
        replacement = raw.get("replacement")
        replacement = replacement if isinstance(replacement, str) else ""
        if len(replacement) > MAX_REPLACEMENT_CHARS:
            raise ValueError(
                f"rule #{index}: replacement longer than {MAX_REPLACEMENT_CHARS} chars"
            )
        if match_type == "regex":
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"rule #{index}: invalid regex: {exc}") from exc
        normalized.append(
            {
                "pattern": pattern,
                "type": match_type,
                "action": action,
                "scope": scope,
                "replacement": replacement,
            }
        )
    return normalized


def _compile(rule: dict[str, Any]) -> CompiledRule:
    return CompiledRule(
        pattern=rule["pattern"],
        match_type=rule["type"],
        action=rule["action"],
        replacement=rule["replacement"],
        scope=rule["scope"],
        regex=re.compile(rule["pattern"]) if rule["type"] == "regex" else None,
    )


def load_rules() -> tuple[CompiledRule, ...]:
    """Load + compile the rules file (mtime-cached; missing/broken = no rules)."""
    global _rules, _loaded_key
    path = rules_file()
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        with _LOCK:
            _rules = ()
            _loaded_key = None
        return ()
    with _LOCK:
        if key == _loaded_key:
            return _rules
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            normalized = validate_rules(payload)
            compiled = tuple(_compile(rule) for rule in normalized)
        except (OSError, ValueError) as exc:
            # Fail-open on a broken file: the operator sees the reason here and
            # in the tab's snapshot; no model call may die on it.
            logger.warning("safety filter: cannot load rules from {}: {}", path, exc)
            compiled = ()
        _rules = compiled
        _loaded_key = key
        return _rules


def clear_rules_cache() -> None:
    """Forget the compiled rules (tests, and the save endpoint after a write)."""
    global _rules, _loaded_key
    with _LOCK:
        _rules = ()
        _loaded_key = None


def write_rules(payload: Any) -> list[dict[str, Any]]:
    """Validate and persist a rules document; returns the normalized rules."""
    normalized = validate_rules(payload)
    path = rules_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {"version": 1, "rules": normalized}
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    clear_rules_cache()
    return normalized


def rules_snapshot() -> dict[str, Any]:
    """What the settings tab shows: the path, the count, and the loaded rules."""
    path = rules_file()
    return {
        "path": str(path),
        "exists": path.is_file(),
        "count": len(load_rules()),
        "rules": [
            {
                "pattern": rule.pattern,
                "type": rule.match_type,
                "action": rule.action,
                "scope": rule.scope,
                "replacement": rule.replacement,
            }
            for rule in load_rules()
        ],
    }


def scan(text: str, *, scope: str) -> list[FilterHit]:
    """Every rule that matches ``text`` for one scope (input / output)."""
    if not text:
        return []
    hits: list[FilterHit] = []
    for rule in load_rules():
        if rule.scope not in (scope, "both"):
            continue
        matched = rule.search(text)
        if matched:
            hits.append(
                FilterHit(
                    pattern=rule.pattern,
                    matched_text=matched,
                    action=rule.action,
                    replacement=rule.replacement,
                    scope=rule.scope,
                )
            )
    return hits


def apply_replacements(text: str, hits: list[FilterHit]) -> str:
    """Apply the ``replace`` hits to one text (literal replacement, no escapes)."""
    result = text
    for hit in hits:
        if hit.action != "replace":
            continue
        replacement = hit.replacement or SAFETY_FILTER["safe_replacement_message"]
        result = result.replace(hit.matched_text, replacement)
    return result
