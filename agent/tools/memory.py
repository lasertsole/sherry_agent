#!/usr/bin/env python3
"""
Memory Tool Module - Persistent Curated Memory

Provides bounded, SQLite-backed memory that persists across sessions. Three stores:
  - memory: the agent's personal notes and observations (environment facts,
    project conventions, tool quirks, things learned)
  - user: what the agent knows about the user (preferences, communication style,
    expectations, workflow habits)
  - facts: SYSTEM-LEVEL / CROSS-PROJECT pitfalls and conventions (true in ANY
    project; always injected into the system prompt; the least-used entries roll
    off at the cap)

All three are injected into the system prompt as a frozen snapshot at session start.
Mid-session writes reach SQLite immediately (durable) but do NOT change the system
prompt -- this preserves the prefix cache for the entire session. The snapshot
refreshes on the next session start.

Entries are ordered ``created_at ASC, id ASC`` -- the old top-to-bottom file
order -- so an unchanged store renders the identical prompt block.

Character limits (not tokens) because char counts are model-independent.

Design:
- Single `memory` tool with action parameter: add, replace, remove, read
- replace/remove use short unique substring matching (not full text or IDs)
- every entry records its origin session and usage count; reads increment usage
- Behavioral guidance lives in the tool schema description
- Frozen snapshot pattern: system prompt is stable, tool responses show live state
"""

import json
import re
import time
from datetime import UTC, datetime
from loguru import logger
from typing import Annotated, Any, Literal, override
from pydantic import BaseModel, Field
from langchain.tools import BaseTool
from langchain_core.callbacks import CallbackManagerForToolRun
from langgraph.prebuilt.tool_node import InjectedState

from config.features import MEMORY_TOOL
from context_engine.store.memory_entry_repository import (
    MEMORY_TARGETS,
    MemoryEntryRepository,
    memory_entry_repo,
)
from agent.tools.pub_base.tool_utils import (
    SessionId,
    resolve_tool_session_id,
    tool_error as _tool_error,
)

ENTRY_DELIMITER = "\n§\n"
# Subset of invisible chars for injection detection

_INVISIBLE_CHARS = {
    "\u200b",
    "\u200c",
    "\u200d",
    "\u2060",
    "\ufeff",
    "\u202a",
    "\u202b",
    "\u202c",
    "\u202d",
    "\u202e",
}
_MEMORY_THREAT_PATTERNS = [
    # Prompt injection
    # ``(?:\s+\w+){0,8}\s+`` tolerates the words a real payload slips between the
    # verb and its object: without it the canonical phrasing
    # "ignore all previous instructions" did NOT match (measured), which is the
    # one shape this guard exists for. Same bound as the shared scanner.
    (r"ignore(?:\s+\w+){0,8}\s+(previous|all|above|prior)\s+instructions", "prompt_injection"),
    (r"you\s+are\s+now\s+", "role_hijack"),
    (r"do\s+not\s+tell\s+the\s+user", "deception_hide"),
    (r"system\s+prompt\s+override", "sys_prompt_override"),
    (
        r"disregard(?:\s+\w+){0,8}\s+(your|all|any)\s+(instructions|rules|guidelines)",
        "disregard_rules",
    ),
    (
        r"act\s+as\s+(if|though)\s+you\s+(have\s+no|don\'t\s+have)\s+(restrictions|limits|rules)",
        "bypass_restrictions",
    ),
    # Exfiltration via curl/wget with secrets
    (r"curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", "exfil_curl"),
    (r"wget\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", "exfil_wget"),
    (r"cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc)", "read_secrets"),
    # Persistence via shell rc
    (r"authorized_keys", "ssh_backdoor"),
    (r"\$HOME/\.ssh|\~/\.ssh", "ssh_access"),
    # C2 / promptware: a stored note is the cheapest place to park a beacon, and
    # every note is re-injected into a later system prompt.
    (r"register\s+as\s+a\s+node", "c2_register_node"),
    (r"(heartbeat|beacon)\s+to\s+", "c2_heartbeat"),
    (r"pull\s+tasking", "c2_pull_tasking"),
    # The bare names are ordinary English ("a sliver of hope", "mythic lore"), so a
    # name only counts with its tool-shaped qualifier or an explicit C2 marker beside
    # it. Measured: the unqualified alternation refused "Sliver-haired detective notes".
    (r"cobalt\s+strike", "c2_known_framework"),
    (
        r"(?:sliver|havoc|mythic|brainworm)[\s_-]*(?:server|client|c2|beacon|implant|payload|listener)",
        "c2_known_framework",
    ),
    (
        r"\b(?:c2|c&c|command[\s-]+and[\s-]+control)\b.{0,40}(?:sliver|havoc|mythic|brainworm)",
        "c2_known_framework",
    ),
    # Rewriting the agent's own instruction files from inside a note.
    (r"(modify|overwrite|replace)\s+.{0,60}(AGENTS|CLAUDE)\.md", "rewrite_instruction_file"),
]

# Why this table instead of ``agent.security.threat_patterns``': memory entries
# are prose ABOUT the system, so the attacker-surface tiers refuse legitimate
# notes (one mentioning ``.bashrc`` or a ``KEY=`` variable name is not an
# attack). These patterns target instructions and exfiltration commands only;
# the shared scanner keeps guarding tool output, where nothing is prose.


def _scan_memory_content(content: str) -> str | None:
    """Scan memory content for injection/exfil patterns. Returns error string if blocked."""
    # Check invisible unicode
    for char in _INVISIBLE_CHARS:
        if char in content:
            return f"Blocked: content contains invisible unicode character U+{ord(char):04X} (possible injection)."

    # Check threat patterns
    for pattern, pid in _MEMORY_THREAT_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            return f"Blocked: content matches threat pattern '{pid}'. Memory entries are injected into the system prompt and must not contain injection or exfiltration payloads."

    return None


def _entry_date(created_at: int | None) -> str:
    """UTC calendar date for a provenance marker (epoch milliseconds)."""
    if not created_at:
        return "unknown"
    try:
        return datetime.fromtimestamp(created_at / 1000, tz=UTC).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return "unknown"


def _public_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """JSON-safe subset of a memory row (no embedding blobs)."""
    return {
        "id": entry["id"],
        "target": entry["target"],
        "content": entry["content"],
        "entry_type": entry.get("entry_type"),
        "origin_session": entry.get("origin_session"),
        "created_at": entry.get("created_at"),
        "updated_at": entry.get("updated_at"),
        "last_used": entry.get("last_used"),
        "usage_count": entry.get("usage_count", 0),
        "archived": bool(entry.get("archived")),
        "archived_reason": entry.get("archived_reason"),
    }


class MemoryStore:
    """Bounded curated memory backed by SQLite. One instance per process.

    Maintains two parallel states:
      - _system_prompt_snapshot: frozen at load time, used for system prompt
        injection. Never mutated mid-session. Keeps prefix cache stable.
      - _entries_by_target / memory_entries / user_entries / facts_entries: live
        state, mutated by tool calls, persisted to SQLite immediately. Tool
        responses always reflect this live state.
    """

    #: UI-editable setting keys inside the memory database.
    _SETTING_KEYS: tuple[str, ...] = (
        "memory_char_limit",
        "user_char_limit",
        "facts_char_limit",
        "max_unused_days",
    )

    def __init__(
        self,
        memory_char_limit: int = MEMORY_TOOL["memory_char_limit"],
        user_char_limit: int = MEMORY_TOOL["user_char_limit"],
        facts_char_limit: int = MEMORY_TOOL["facts_char_limit"],
        *,
        repo: MemoryEntryRepository | None = None,
        max_unused_days: int = MEMORY_TOOL["max_unused_days"],
    ):
        self._repo = repo
        self.memory_entries: list[str] = []
        self.user_entries: list[str] = []
        self.facts_entries: list[str] = []
        self.memory_char_limit = memory_char_limit
        self.user_char_limit = user_char_limit
        self.facts_char_limit = facts_char_limit
        self.max_unused_days = max_unused_days
        # Constructor values are the fallback when no UI override is stored; the
        # production singleton is constructed from the code defaults.
        self._default_limits: dict[str, int] = {
            "memory_char_limit": int(memory_char_limit),
            "user_char_limit": int(user_char_limit),
            "facts_char_limit": int(facts_char_limit),
            "max_unused_days": int(max_unused_days),
        }
        # Cached rows (with ids/provenance) parallel to the public str lists.
        self._entries_by_target: dict[str, list[dict[str, Any]]] = {
            target: [] for target in MEMORY_TARGETS
        }
        # Frozen snapshot for system prompt -- set at load_from_store()
        self._system_prompt_snapshot: dict[str, str] = {"memory": "", "user": "", "facts": ""}
        self._settings_loaded = False

    # -- repository / settings ---------------------------------------------

    @property
    def repo(self) -> MemoryEntryRepository:
        return self._repo or memory_entry_repo

    def reload_limits(self) -> None:
        """Apply UI-edited limits over the code defaults (unset = default).

        The defaults are re-applied first: a deleted override must fall back,
        not leave a previous setting's value in place.
        """
        for name, value in self._default_limits.items():
            setattr(self, name, value)
        settings: dict[str, str] = {}
        try:
            settings = self.repo.get_settings()
        except Exception:  # noqa: BLE001 - a broken DB must not break memory
            logger.debug("memory store: could not read settings; using defaults", exc_info=True)
        for name in self._SETTING_KEYS:
            raw = settings.get(name)
            if raw is None:
                continue
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if value <= 0:
                continue
            setattr(self, name, value)
        self._settings_loaded = True

    def effective_limits(self) -> dict[str, Any]:
        return {
            "memory_char_limit": self.memory_char_limit,
            "user_char_limit": self.user_char_limit,
            "facts_char_limit": self.facts_char_limit,
            "max_unused_days": self.max_unused_days,
            "semantic_recall_enabled": bool(MEMORY_TOOL["semantic_recall_enabled"]),
            "semantic_recall_top_k": int(MEMORY_TOOL["semantic_recall_top_k"]),
            "semantic_recall_char_budget": int(MEMORY_TOOL["semantic_recall_char_budget"]),
        }

    def update_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        """Persist the UI-editable limits and apply them to this store."""
        accepted: dict[str, int] = {}
        for name in self._SETTING_KEYS:
            if name not in values or values[name] is None:
                continue
            try:
                parsed = int(values[name])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} must be an integer") from exc
            if parsed <= 0:
                raise ValueError(f"{name} must be positive")
            accepted[name] = parsed
        if accepted:
            self.repo.set_settings(accepted)
        self.reload_limits()
        return self.effective_limits()

    # -- loading ------------------------------------------------------------

    def load_from_store(self) -> None:
        """Load active entries from SQLite and capture the prompt snapshot."""
        self.reload_limits()
        for target in ("user", "memory", "facts"):
            fresh = self.repo.active_entries_ordered(target)
            # Deduplicate by content (preserves the first occurrence), matching
            # the old file loader; ids stay on the surviving row.
            seen: set[str] = set()
            deduped: list[dict[str, Any]] = []
            for entry in fresh:
                content = entry["content"]
                if content in seen:
                    continue
                seen.add(content)
                deduped.append(entry)
            self._set_target(target, deduped)

        # Capture frozen snapshot for system prompt injection. Empty stores render
        # to "" so an empty store never produces an empty prompt block.
        self._system_prompt_snapshot = {
            target: self._render_block(target, self._entries_by_target[target])
            for target in ("memory", "user", "facts")
        }

    def reset_in_memory(self) -> None:
        """Forget the live rows and frozen snapshot WITHOUT touching SQLite.

        The test isolation fixture runs before every test; a full
        ``load_from_store()`` there opened the database and rendered three
        blocks ~8,000 times, adding minutes to the unit gate. Tests that need
        real rows call ``load_from_store()`` themselves.
        """
        for target in MEMORY_TARGETS:
            self._set_target(target, [])
        self._system_prompt_snapshot = {"memory": "", "user": "", "facts": ""}
        for name, value in self._default_limits.items():
            setattr(self, name, value)
        self._settings_loaded = False

    def load_from_disk(self) -> None:
        """Backward-compatible alias for ``load_from_store``.

        The call sites and tests still use this name; the backing store is
        SQLite now (the legacy ``.md`` files are imported once by
        ``context_engine.store.memory_migration``).
        """
        self.load_from_store()

    def refresh_live(self) -> None:
        """Re-read the live entries WITHOUT re-freezing the prompt snapshot.

        External writers (the memory panel / REST API) must become visible to
        the next tool call, but re-rendering the snapshot mid-session would
        change the system prompt and invalidate the provider prefix cache.
        """
        for target in MEMORY_TARGETS:
            self._set_target(target, self.repo.active_entries_ordered(target))

    # -- live views ---------------------------------------------------------

    def _entries_for(self, target: str) -> list[dict[str, Any]]:
        return self._entries_by_target.get(target, [])

    def _contents_for(self, target: str) -> list[str]:
        return [entry["content"] for entry in self._entries_for(target)]

    def _set_target(self, target: str, entries: list[dict[str, Any]]) -> None:
        self._entries_by_target[target] = entries
        contents = [entry["content"] for entry in entries]
        if target == "user":
            self.user_entries = contents
        elif target == "facts":
            self.facts_entries = contents
        else:
            self.memory_entries = contents

    def _char_count(self, target: str) -> int:
        contents = self._contents_for(target)
        if not contents:
            return 0
        return len(ENTRY_DELIMITER.join(contents))

    def _char_limit(self, target: str) -> int:
        if target == "user":
            return self.user_char_limit
        if target == "facts":
            return self.facts_char_limit
        return self.memory_char_limit

    @staticmethod
    def _joined(contents: list[str]) -> str:
        return ENTRY_DELIMITER.join(contents)

    @staticmethod
    def _age_key(entry: dict[str, Any]) -> tuple[int, int, int]:
        """Least-active first: idle time, then usage, then creation/id."""
        return (
            int(entry.get("last_used") or entry.get("created_at") or 0),
            int(entry.get("usage_count") or 0),
            int(entry.get("id") or 0),
        )

    def _evict_least_active_to_fit(
        self,
        entries: list[dict[str, Any]],
        limit: int,
        *,
        reason: str = "evicted",
        archive: bool = True,
    ) -> list[dict[str, Any]]:
        """Remove least-active entries (in place) until the joined text fits.

        Stops at one persisted entry so a single oversized entry is reported
        instead of silently removed. FACTS uses this usage-aware order; the old
        behavior was strictly oldest-first.

        ``archive=False`` lets a caller compute the victim set and only commit
        it when the add is actually accepted — otherwise a rejected oversized
        entry would still have deleted rows.
        """
        evicted: list[dict[str, Any]] = []
        # Only persisted rows are candidates: the caller's new-entry stub (no
        # ``id``) must never be chosen, or the add would evict the very entry
        # it is about to insert and still exceed the limit.
        while len(entries) > 1 and len(self._joined([e["content"] for e in entries])) > limit:
            candidates = [entry for entry in entries if "id" in entry]
            if not candidates:
                break
            victim = min(candidates, key=self._age_key)
            entries.remove(victim)
            if archive:
                self.repo.archive_entry(int(victim["id"]), reason)
            evicted.append(victim)
        return evicted

    @staticmethod
    def _target_for_entry(entry: str) -> str:
        """Route a flushed entry to its target.

        ``User:``-prefixed entries (case-insensitive, any spaces before the
        colon) belong in ``user``; everything else (Environment / Project /
        Decision / Tool / no prefix) goes to ``memory``.
        """
        return "user" if re.match(r"^\s*user\s*:", entry, re.IGNORECASE) else "memory"

    @staticmethod
    def _infer_entry_type(target: str, content: str) -> str | None:
        if target == "user":
            return "user"
        if target == "facts":
            return "system"
        match = re.match(r"^\s*([A-Za-z ]+)\s*:", content)
        if not match:
            return "project"
        prefix = match.group(1).strip().lower()
        if prefix == "user":
            return "user"
        if prefix in {"feedback", "correction", "preference"}:
            return "feedback"
        if prefix in {"reference", "tool", "environment"}:
            return "reference"
        return "project"

    # -- writes -------------------------------------------------------------

    def add(
        self,
        target: str,
        content: str,
        *,
        origin_session: str | None = None,
    ) -> dict[str, Any]:
        """Append a new entry. Returns error if it would exceed the char limit.

        ``facts`` is nudge-maintained and high-churn, so when the new entry
        would overflow the facts budget the least-active entries roll off first
        (``COALESCE(last_used, created_at)`` then ``usage_count``) and the new
        entry still lands. ``memory`` / ``user`` keep the interactive
        reject-on-overflow rule.
        """
        content = content.strip()
        if not content:
            return {"success": False, "error": "Content cannot be empty."}

        # Scan for injection/exfiltration before accepting
        scan_error = _scan_memory_content(content)
        if scan_error:
            return {"success": False, "error": scan_error}

        if not self._settings_loaded:
            self.reload_limits()

        entries = list(self.repo.active_entries_ordered(target))
        contents = [entry["content"] for entry in entries]
        limit = self._char_limit(target)

        # Reject exact duplicates
        if content in contents:
            self._set_target(target, entries)
            return self._success_response(target, "Entry already exists (no duplicate added).")

        new_entry_stub = {"content": content}
        all_entries: list[dict[str, Any]] = entries + [new_entry_stub]  # type: ignore[list-item]
        evicted: list[dict[str, Any]] = []
        if target == "facts":
            evicted = self._evict_least_active_to_fit(all_entries, limit, archive=False)

        new_total = len(self._joined([e["content"] for e in all_entries]))
        if new_total > limit:
            # Nothing was archived yet: a refused add must not delete rows.
            current = len(self._joined(contents))
            self._set_target(target, entries)
            return {
                "success": False,
                "error": (
                    f"Memory at {current:,}/{limit:,} chars. "
                    f"Adding this entry ({len(content)} chars) would exceed the limit. "
                    f"Replace or remove existing entries first."
                ),
                "current_entries": contents,
                "usage": f"{current:,}/{limit:,}",
            }

        for victim in evicted:
            self.repo.archive_entry(int(victim["id"]), "evicted")
        entry = self.repo.add_entry(
            target,
            content,
            entry_type=self._infer_entry_type(target, content),
            origin_session=origin_session,
        )
        survivors = [e for e in all_entries if e is not new_entry_stub]
        survivors.append(entry)
        self._set_target(target, survivors)

        if evicted:
            return self._success_response(
                target,
                f"Entry added (evicted {len(evicted)} least-used entries to stay within the limit).",
            )
        return self._success_response(target, "Entry added.")

    def _append_to_target(
        self,
        target: str,
        candidates: list[str],
        *,
        origin_session: str | None = None,
    ) -> dict[str, Any]:
        """Append ``candidates`` to one target under its own limit."""
        entries = list(self.repo.active_entries_ordered(target))
        limit = self._char_limit(target)

        existing_set = {entry["content"] for entry in entries}
        new_items = [entry for entry in candidates if entry not in existing_set]

        all_entries: list[dict[str, Any]] = entries + [
            {"content": entry}
            for entry in new_items  # type: ignore[list-item]
        ]
        if target == "facts":
            self._evict_least_active_to_fit(all_entries, limit)
        else:
            # MEMORY / USER keep the old oldest-first rollover.
            while (
                len(all_entries) > 1
                and len(self._joined([e["content"] for e in all_entries])) > limit
            ):
                victim = all_entries.pop(0)
                if "id" in victim:
                    self.repo.archive_entry(int(victim["id"]), "evicted")

        combined = self._joined([entry["content"] for entry in all_entries])

        # A stub still in ``all_entries`` survived eviction; one popped earlier
        # was dropped to fit and must not be inserted after the fact.
        surviving_new = [entry["content"] for entry in all_entries if "id" not in entry]
        survivors: list[dict[str, Any]] = [entry for entry in all_entries if "id" in entry]
        if surviving_new:
            for item in surviving_new:
                survivors.append(
                    self.repo.add_entry(
                        target,
                        item,
                        entry_type=self._infer_entry_type(target, item),
                        origin_session=origin_session,
                    )
                )
            self._set_target(target, self.repo.active_entries_ordered(target))
        else:
            self._set_target(target, survivors)

        return {
            "added": len(surviving_new),
            "deduplicated": len(candidates) - len(new_items),
            "entry_count": len(all_entries),
            "usage": f"{len(combined)}/{limit} chars",
        }

    def append_entries(
        self,
        new_entries: str,
        *,
        origin_session: str | None = None,
    ) -> dict[str, Any]:
        """Append multiple §-delimited entries, routed by category.

        ``User:``-prefixed entries go to ``user``; all others go to ``memory``.
        Unlike ``add()`` (one entry, reject on overflow), this batch method skips
        entries already present and evicts the oldest entries to stay within
        each target's own char limit.
        """
        raw = new_entries.strip()
        if not raw:
            return {"success": True, "message": "No entries to add."}

        parsed = [entry.strip() for entry in re.split(r"\s*§\s*", raw) if entry.strip()]
        if not parsed:
            return {"success": True, "message": "No entries to add."}

        if not self._settings_loaded:
            self.reload_limits()

        candidates: list[str] = []
        for entry in parsed:
            scan_error = _scan_memory_content(entry)
            if scan_error:
                logger.warning(f"Memory append: rejected entry -- {scan_error}")
                continue
            candidates.append(entry)
        if not candidates:
            return {"success": True, "message": "No entries to add."}

        by_target: dict[str, list[str]] = {"memory": [], "user": []}
        for entry in candidates:
            by_target[self._target_for_entry(entry)].append(entry)

        results: dict[str, dict[str, Any]] = {}
        for target in ("memory", "user"):
            if by_target[target]:
                results[target] = self._append_to_target(
                    target, by_target[target], origin_session=origin_session
                )

        labels = {"memory": "MEMORY.md", "user": "USER.md"}
        written = [labels[t] for t in ("memory", "user") if results.get(t, {}).get("added", 0) > 0]
        total_added = sum(r["added"] for r in results.values())
        total_deduped = sum(r["deduplicated"] for r in results.values())

        if not written:
            message = "All entries already exist (no duplicates added)."
        else:
            message = (
                f"Added {total_added} entries to {', '.join(written)} "
                f"(deduplicated {total_deduped})."
            )

        return {
            "success": True,
            "message": message,
            "entry_count": sum(r["entry_count"] for r in results.values()),
            "usage": "; ".join(
                f"{labels[t]} {results[t]['usage']}" for t in ("memory", "user") if t in results
            ),
        }

    def replace(
        self,
        target: str,
        old_text: str,
        new_content: str,
        *,
        origin_session: str | None = None,
    ) -> dict[str, Any]:
        """Find entry containing old_text substring, replace it with new_content."""
        del origin_session  # an update keeps the entry's original provenance
        old_text = old_text.strip()
        new_content = new_content.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}
        if not new_content:
            return {
                "success": False,
                "error": "new_content cannot be empty. Use 'remove' to delete entries.",
            }

        # Scan replacement content for injection/exfiltration
        scan_error = _scan_memory_content(new_content)
        if scan_error:
            return {"success": False, "error": scan_error}

        if not self._settings_loaded:
            self.reload_limits()

        entries = list(self.repo.active_entries_ordered(target))
        matches = [entry for entry in entries if old_text in entry["content"]]

        if not matches:
            self._set_target(target, entries)
            return {"success": False, "error": f"No entry matched '{old_text}'."}

        if len(matches) > 1:
            unique_texts = {entry["content"] for entry in matches}
            if len(unique_texts) > 1:
                previews = [
                    entry["content"][:80] + ("..." if len(entry["content"]) > 80 else "")
                    for entry in matches
                ]
                return {
                    "success": False,
                    "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                    "matches": previews,
                }

        victim = matches[0]
        limit = self._char_limit(target)
        test_contents = [
            new_content if entry["id"] == victim["id"] else entry["content"] for entry in entries
        ]
        new_total = len(self._joined(test_contents))
        if new_total > limit:
            return {
                "success": False,
                "error": (
                    f"Replacement would put memory at {new_total:,}/{limit:,} chars. "
                    f"Shorten the new content or remove other entries first."
                ),
            }

        self.repo.update_entry(int(victim["id"]), content=new_content)
        self._set_target(target, self.repo.active_entries_ordered(target))
        return self._success_response(target, "Entry replaced.")

    def remove(self, target: str, old_text: str) -> dict[str, Any]:
        """Archive the entry containing old_text substring."""
        old_text = old_text.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}

        entries = list(self.repo.active_entries_ordered(target))
        matches = [entry for entry in entries if old_text in entry["content"]]

        if not matches:
            self._set_target(target, entries)
            return {"success": False, "error": f"No entry matched '{old_text}'."}

        if len(matches) > 1:
            unique_texts = {entry["content"] for entry in matches}
            if len(unique_texts) > 1:
                previews = [
                    entry["content"][:80] + ("..." if len(entry["content"]) > 80 else "")
                    for entry in matches
                ]
                return {
                    "success": False,
                    "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                    "matches": previews,
                }

        victim = matches[0]
        self.repo.archive_entry(int(victim["id"]), "manual")
        self._set_target(target, self.repo.active_entries_ordered(target))
        return self._success_response(target, "Entry removed.")

    # -- reads --------------------------------------------------------------

    def read(self, target: str, query: str | None = None) -> list[dict[str, Any]]:
        """Return active entries (optionally substring-filtered) and count a use.

        Every returned entry increments ``usage_count`` — this is the signal the
        usage-aware facts eviction consumes. A full read counts every entry, as
        the plan specifies.
        """
        entries = self.repo.active_entries_ordered(target)
        if query:
            needle = query.strip().lower()
            if needle:
                entries = [entry for entry in entries if needle in entry["content"].lower()]
        for entry in entries:
            self.repo.increment_usage(int(entry["id"]))
        self._set_target(target, self.repo.active_entries_ordered(target))
        return [
            _public_entry(entry)
            for entry in self.repo.get_entries_by_ids([int(e["id"]) for e in entries])
        ]

    def format_for_system_prompt(self, target: str) -> str | None:
        """Return the frozen memory block for prompt injection.

        This is the state captured at ``load_from_store()`` time, NOT live
        state; mid-session writes stay in SQLite and do not disturb the prompt
        cache. Returns None for an empty block.
        """
        block = self._system_prompt_snapshot.get(target, "")
        return block if block else None

    def format_live_content(self, target: str) -> str:
        """Return the store's current content, delimiter-joined.

        Unlike ``format_for_system_prompt`` this queries SQLite, so nudge
        prompts can hand the model the latest state; returns "" when empty.
        """
        entries = self.repo.active_entries_ordered(target)
        return self._joined([entry["content"] for entry in entries])

    # -- internal helpers ---------------------------------------------------

    def _success_response(self, target: str, message: str = None) -> dict[str, Any]:
        entries = self._entries_for(target)
        current = self._char_count(target)
        limit = self._char_limit(target)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0

        resp = {
            "success": True,
            "target": target,
            "entries": [entry["content"] for entry in entries],
            "usage": f"{pct}% — {current:,}/{limit:,} chars",
            "entry_count": len(entries),
        }
        if message:
            resp["message"] = message
        return resp

    @staticmethod
    def _render_entry(entry: dict[str, Any]) -> str:
        """Entry text plus its provenance marker (only when origin is known)."""
        content = entry["content"]
        origin = entry.get("origin_session")
        if not origin:
            return content
        used = int(entry.get("usage_count") or 0)
        return (
            f"{content}\n"
            f"[origin: {origin}, created: {_entry_date(entry.get('created_at'))}, used: {used}x]"
        )

    def _render_block(self, target: str, entries: list[dict[str, Any]]) -> str:
        """Render a system prompt block with header, usage indicator and origins.

        An empty store renders to "" — callers skip falsy block strings, so an
        empty store never injects an empty header-only block.
        """
        limit = self._char_limit(target)
        contents = [entry["content"] for entry in entries]
        if not contents:
            return ""
        current = len(self._joined(contents))
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0
        content = ENTRY_DELIMITER.join(self._render_entry(entry) for entry in entries)

        if target == "user":
            header = f"USER PROFILE (who the user is) [{pct}% — {current:,}/{limit:,} chars]"
        elif target == "facts":
            header = f"FACTS (system-level pitfalls and conventions) [{pct}% — {current:,}/{limit:,} chars]"
        else:
            header = f"MEMORY (your personal notes) [{pct}% — {current:,}/{limit:,} chars]"

        separator = "═" * 46
        return f"{separator}\n{header}\n{separator}\n{content}"


memory_store: MemoryStore = MemoryStore()


def memory_tool(
    action: str,
    target: str = "memory",
    content: str | None = None,
    old_text: str | None = None,
    query: str | None = None,
    session_id: str = "",
    origin_session: str | None = None,
) -> str:
    """
    Single entry point for the memory tool. Dispatches to MemoryStore methods.

    Returns JSON string with results.
    """
    start_time = time.time()
    content_preview = content[:50] if content else ""
    old_text_preview = old_text[:50] if old_text else ""
    origin = origin_session or session_id or None

    logger.debug(
        f"Memory tool called: action={action}, target={target}, "
        f"content_preview='{content_preview}', old_text_preview='{old_text_preview}', "
        f"origin={origin}"
    )

    if action in ("add", "replace", "remove", "read") and target not in MEMORY_TARGETS:
        logger.warning(f"Invalid memory target: {target}")
        return _tool_error(
            f"Invalid target '{target}'. Use 'memory', 'user' or 'facts'.", success=False
        )

    try:
        if action == "add":
            if not content:
                return _tool_error("Content is required for 'add' action.", success=False)
            result = memory_store.add(target, content, origin_session=origin)

        elif action == "replace":
            if not old_text:
                return _tool_error("old_text is required for 'replace' action.", success=False)
            if not content:
                return _tool_error("content is required for 'replace' action.", success=False)
            result = memory_store.replace(target, old_text, content, origin_session=origin)

        elif action == "remove":
            if not old_text:
                return _tool_error("old_text is required for 'remove' action.", success=False)
            result = memory_store.remove(target, old_text)

        elif action == "read":
            entries = memory_store.read(target, query)
            result = {
                "success": True,
                "target": target,
                "entries": entries,
                "entry_count": len(entries),
                "query": query or "",
            }

        else:
            return _tool_error(
                f"Unknown action '{action}'. Use: add, replace, remove, read",
                success=False,
            )

        elapsed = time.time() - start_time
        logger.debug(
            f"Memory tool completed: action={action}, target={target}, duration={elapsed:.3f}s"
        )

        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        elapsed = time.time() - start_time
        logger.exception(
            f"Memory tool failed: action={action}, target={target}, "
            f"duration={elapsed:.3f}s, error={str(e)}"
        )
        raise


# =============================================================================
# OpenAI Function-Calling Schema
# =============================================================================


class MemoryActionSchema(BaseModel):
    """Schema for memory tool arguments."""

    action: Literal["add", "replace", "remove", "read"] = Field(
        description=(
            "The action to perform: 'add' / 'replace' / 'remove' / 'read' on "
            "the memory, user or facts store."
        )
    )
    target: str = Field(
        default="memory",
        description="Which store: 'memory', 'user' or 'facts'.",
    )
    content: str | None = Field(
        default=None,
        description="Entry content (add/replace).",
    )
    old_text: str | None = Field(
        default=None,
        description="Short unique substring for replace/remove.",
    )
    query: str | None = Field(
        default=None,
        description="Optional substring filter for the 'read' action.",
    )

    #: Runtime-injected; excluded from the model-facing schema.
    session_id: SessionId = ""
    # The session-end salvage runs under a derived state key; this carries the
    # real (soon-to-be-deleted) session id for provenance.
    memory_origin_session: Annotated[str, InjectedState("memory_origin_session")] = ""


class MemoryTool(BaseTool):
    name: str = "memory"
    description: str = (
        "Save durable information to persistent memory that survives across sessions. "
        "Memory is injected into future turns, so keep it compact and focused on facts "
        "that will still matter later.\n\n"
        "WHEN TO SAVE (do this proactively, don't wait to be asked):\n"
        "- User corrects you or says 'remember this' / 'don't do that again'\n"
        "- User shares a preference, habit, or personal detail (name, role, timezone, coding style)\n"
        "- You discover something about the environment (OS, installed tools, project structure)\n"
        "- You learn a convention, API quirk, or workflow specific to this user's setup\n"
        "- You identify a stable fact that will be useful again in future sessions\n\n"
        "PRIORITY: User preferences and corrections > environment facts > procedural knowledge. "
        "The most valuable memory prevents the user from having to repeat themselves.\n\n"
        "Do NOT save task progress, session outcomes, completed-work logs, or temporary TODO "
        "state to memory; use session_search to recall those from past transcripts.\n"
        "If you've discovered a new way to do something, solved a problem that could be "
        "necessary later, save it as a skill with the skill tool.\n\n"
        "THREE TARGETS:\n"
        "- 'user': who the user is -- name, role, preferences, communication style, pet peeves\n"
        "- 'memory': your notes -- environment facts, project conventions, tool quirks, lessons learned\n"
        "- 'facts': SYSTEM-LEVEL / CROSS-PROJECT pitfalls that are true in ANY project "
        "(not project-specific; always injected into the system prompt; least-used entries roll off)\n\n"
        "ROUTING: a lesson bound to a specific file/module/test belongs in a "
        "'<module>-notes' skill (skill_manage), not in memory; user preferences belong "
        "in 'user'; SYSTEM-LEVEL / CROSS-PROJECT pitfalls belong in 'facts' (only if true "
        "in ANY project); project-specific notes and conventions belong in 'memory'.\n\n"
        "ACTIONS: add (new entry), replace (update existing -- old_text identifies it), "
        "remove (archive -- old_text identifies it), read (return entries and mark them used; "
        "an optional query filters by substring).\n\n"
        "SKIP: trivial/obvious info, things easily re-discovered, raw data dumps, and temporary task state.\n\n"
    )
    args_schema: type[BaseModel] = MemoryActionSchema
    # scope="main_only": usable ONLY by the main agent; subagents can never get
    # this tool (enforced non-overridably by
    # agent/tools/subagent/spawn/inherited_tool_policy.apply_tool_policy).
    metadata: dict = {
        "idempotent": True,
        "nudge": True,
        "nudge_memory": True,
        "scope": "main_only",
        "side_effect_scope": "session",
    }

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)

    @override
    def _run(
        self,
        action: str,
        target: str = "memory",
        content: str | None = None,
        old_text: str | None = None,
        query: str | None = None,
        session_id: str = "",
        memory_origin_session: str = "",
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        resolved = resolve_tool_session_id(session_id, run_manager)
        return memory_tool(
            action,
            target,
            content,
            old_text,
            query=query,
            session_id=resolved,
            origin_session=memory_origin_session or None,
        )

    @override
    async def _arun(
        self,
        action: str,
        target: str = "memory",
        content: str | None = None,
        old_text: str | None = None,
        query: str | None = None,
        session_id: str = "",
        memory_origin_session: str = "",
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        resolved = resolve_tool_session_id(session_id, run_manager)
        return memory_tool(
            action,
            target,
            content,
            old_text,
            query=query,
            session_id=resolved,
            origin_session=memory_origin_session or None,
        )


def build_memory_tool() -> MemoryTool:
    tool: MemoryTool = MemoryTool()
    tool.handle_tool_error = True
    return tool
