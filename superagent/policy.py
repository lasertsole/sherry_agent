"""Policy gate, strength and Mandate revalidation (prototype ``src/policy.py``)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from superagent.admission import CellValue, admission_table, cell_of, strength_of
from superagent.core.model import (
    AcceptMode,
    Actor,
    Mandate,
    MandateBasis,
    Strength,
    Thread,
    Ticket,
    Trigger,
)

_CELL_RANK = {CellValue.IGNORE: 0, CellValue.INFERRED: 1, CellValue.EXPLICIT: 2}


@dataclass(frozen=True)
class GateResult:
    """Authorization decision for one Trigger."""

    strength: Strength | None
    basis: MandateBasis | None = None
    matched_rules: list[str] = field(default_factory=list)
    cell: CellValue | None = None
    scope: str = ""


@dataclass(frozen=True)
class RevalidateResult:
    """Result of checking a Mandate against current Policy."""

    ok: bool
    reason: str = ""


def _ids(values: Any) -> set[str]:
    if not isinstance(values, list):
        return set()
    result: set[str] = set()
    for item in values:
        if isinstance(item, str):
            result.add(item.casefold())
        elif isinstance(item, dict) and item.get("id"):
            result.add(str(item["id"]).casefold())
    return result


def _matching_rules(rules: dict[str, Any], text: str) -> list[str]:
    lowered = text.casefold()
    matched: list[str] = []
    for name, raw in rules.items():
        if not isinstance(raw, dict) or not raw.get("enabled", True):
            continue
        keywords = raw.get("keywords") or []
        if not keywords or any(str(word).casefold() in lowered for word in keywords):
            matched.append(str(name))
    return matched


def _rules_enabled(rules: dict[str, Any], names: list[str]) -> bool:
    return all(
        isinstance(rules.get(name), dict) and rules[name].get("enabled", True) for name in names
    )


def _scope_of(trigger: Trigger) -> str:
    scope = trigger.meta.get("scope")
    if scope:
        return str(scope)
    if trigger.source == "github":
        return "github/pr" if trigger.meta.get("pull_request") else "github/issue"
    return trigger.source


def _is_followup(thread: Thread | None, tickets: list[Ticket]) -> bool:
    if thread is None:
        return False
    return any(
        any(
            sub.thread.source == thread.source and sub.thread.id == thread.id
            for sub in ticket.follows
        )
        for ticket in tickets
    )


class Policy:
    """Stateless Policy rebuilt from settings on each use."""

    def __init__(
        self,
        *,
        version: str,
        scope: str,
        accept_mode: AcceptMode = AcceptMode.OBSERVE,
        paused: bool = False,
        eval_timeout_ms: int = 600_000,
        listening: set[str] | None = None,
        parts: dict[str, Any] | None = None,
        rules: dict[str, Any] | None = None,
    ) -> None:
        self.version = version
        self.scope = scope
        self.accept_mode = accept_mode
        self.paused = paused
        self.eval_timeout_ms = eval_timeout_ms
        self.listening = listening or set()
        self.parts = parts or {}
        self.rules = rules or {}

    def _part(self, source: str) -> dict[str, Any]:
        value = self.parts.get(source)
        return value if isinstance(value, dict) else {}

    def _receive_all(self, source: str) -> bool:
        part = self._part(source)
        return bool(part.get("receive_all", False) or part.get("listen") == "all")

    def _allowed(self, source: str, actor: Actor) -> bool:
        return actor.id.casefold() in _ids(self._part(source).get("allowed"))

    def _bots(self, source: str) -> set[str]:
        return _ids(self._part(source).get("bots"))

    def gate(
        self,
        trigger: Trigger,
        thread: Thread | None,
        tickets: list[Ticket],
    ) -> GateResult:
        """Return the authorization strength for a Trigger."""
        if trigger.source not in self.listening:
            return GateResult(strength=None)
        if thread is None and trigger.threads:
            ref = trigger.threads[0]
            thread = Thread(key=f"{ref.source}:{ref.id}", source=ref.source, id=ref.id)

        part = self._part(trigger.source)
        table = admission_table(part)
        scope = _scope_of(trigger)
        cell = cell_of(table, scope)
        addressed = trigger.addressed is not None
        receive_all = self._receive_all(trigger.source)
        matched = _matching_rules(self.rules, trigger.text)
        followup = _is_followup(thread, tickets)

        if self._allowed(trigger.source, trigger.actor):
            if addressed or followup:
                basis = MandateBasis.ADDRESSED if addressed else MandateBasis.FOLLOWUP
                return GateResult(Strength.EXPLICIT, basis, matched, cell.listed_unaddressed, scope)
            strength = strength_of(cell.listed_unaddressed)
            if not receive_all or strength is None:
                return GateResult(None, cell=cell.listed_unaddressed, scope=scope)
            return GateResult(
                strength, MandateBasis.ADMISSION, matched, cell.listed_unaddressed, scope
            )

        if trigger.actor.id.casefold() in self._bots(trigger.source):
            if addressed or receive_all:
                return GateResult(
                    Strength.INFERRED, MandateBasis.BOT, matched, cell.unlisted_addressed, scope
                )
            return GateResult(None, cell=cell.unlisted_addressed, scope=scope)

        if not trigger.human:
            return GateResult(None, cell=cell.unlisted_unaddressed, scope=scope)

        if addressed:
            value = cell.unlisted_addressed
        elif receive_all:
            value = cell.unlisted_unaddressed
        else:
            return GateResult(None, cell=cell.unlisted_unaddressed, scope=scope)

        strength = strength_of(value)
        if value == CellValue.EXPLICIT and followup and not addressed:
            strength = Strength.EXPLICIT
        elif followup and not addressed and value == CellValue.IGNORE:
            strength = Strength.INFERRED
        if strength is None:
            return GateResult(None, cell=value, scope=scope)
        if strength == Strength.INFERRED and self.accept_mode == AcceptMode.OFF:
            return GateResult(None, cell=value, scope=scope)
        basis = MandateBasis.ADMISSION if addressed else MandateBasis.FOLLOWUP
        return GateResult(strength, basis, matched, value, scope)

    def _auto_inferred(self, rules: list[str]) -> bool:
        return self.accept_mode == AcceptMode.AUTO and _rules_enabled(self.rules, rules)

    def revalidate(self, mandate: Mandate) -> RevalidateResult:
        """Check whether a Mandate still authorizes a resume."""
        if mandate.scope not in self.listening:
            return RevalidateResult(False, "source_not_listened")
        part = self._part(mandate.scope)
        table = admission_table(part)
        sender = mandate.sender.id.casefold()

        if mandate.strength == Strength.EXPLICIT and mandate.via == MandateBasis.FOLLOWUP:
            return RevalidateResult(True)
        if mandate.via == MandateBasis.BOT:
            if sender not in _ids(part.get("bots")):
                return RevalidateResult(False, "bot_removed")
            if mandate.strength == Strength.INFERRED and not self._auto_inferred(
                mandate.matched_rules
            ):
                return RevalidateResult(False, "inferred_disabled")
            return RevalidateResult(True)
        if mandate.via == MandateBasis.ADMISSION:
            cell = cell_of(table, str(mandate.basis_detail.get("scope", mandate.scope)))
            raw_cell = mandate.basis_detail.get("cell")
            try:
                cell_value = CellValue(raw_cell) if raw_cell else cell.listed_unaddressed
            except ValueError:
                cell_value = cell.listed_unaddressed
            current = strength_of(cell_value)
            if current is None:
                return RevalidateResult(False, "admission_removed")
            if mandate.strength == Strength.EXPLICIT and current != Strength.EXPLICIT:
                return RevalidateResult(False, "admission_weakened")
            if mandate.strength == Strength.INFERRED and not self._auto_inferred(
                mandate.matched_rules
            ):
                return RevalidateResult(False, "inferred_disabled")
            return RevalidateResult(True)
        if mandate.strength == Strength.INFERRED:
            if not self._auto_inferred(mandate.matched_rules):
                return RevalidateResult(False, "inferred_disabled")
            return RevalidateResult(True)
        if sender in _ids(part.get("allowed")):
            return RevalidateResult(True)
        return RevalidateResult(False, "sender_removed")

    def admits_inferred(self, rules: list[str] | None = None) -> bool:
        """Return whether inferred candidates may reach assessment."""
        if self.accept_mode == AcceptMode.OFF:
            return False
        if rules:
            return _rules_enabled(self.rules, rules)
        return bool(self.rules)


def _version(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def settings_policy(settings: dict[str, Any], scope: str) -> Policy:
    """Build a fresh Policy from settings for one scope."""
    policy_section = settings.get("policy")
    raw: dict[str, Any] = policy_section if isinstance(policy_section, dict) else settings
    sources = raw.get("sources") or {}
    rules = raw.get("rules") or {}
    listening = raw.get("listening")
    if not isinstance(listening, list):
        listening = list(sources) if isinstance(sources, dict) else []
    accept_mode_raw = raw.get("accept_mode", "observe")
    try:
        accept_mode = AcceptMode(accept_mode_raw)
    except ValueError:
        accept_mode = AcceptMode.OBSERVE
    return Policy(
        version=_version(
            {
                "accept_mode": accept_mode.value,
                "listening": sorted(str(item) for item in listening),
                "parts": sources,
                "rules": rules,
            }
        ),
        scope=scope,
        accept_mode=accept_mode,
        paused=bool(raw.get("paused", False)),
        eval_timeout_ms=int(raw.get("eval_timeout_ms", 600_000)),
        listening={str(item) for item in listening},
        parts=sources if isinstance(sources, dict) else {},
        rules=rules if isinstance(rules, dict) else {},
    )


__all__ = ["GateResult", "Policy", "RevalidateResult", "settings_policy"]
