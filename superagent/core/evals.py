"""Eval batching, assessment and Decisions (prototype ``src/core/evals.py``)."""

from __future__ import annotations

from datetime import datetime, timedelta, UTC
from typing import Any

from superagent.core.errors import UserError
from superagent.core.ids import decision_key, key
from superagent.core.model import Decision, DecisionOutcome, Eval, Reason, SourceRef, to_dict
from superagent.core.part import EnginePart
from superagent.core.ports import DomainEvent, DomainEvents
from superagent.limits import EVAL_REQUEUE_LIMIT

GATE_META_KEY = "__gate"


def _deadline(now: str, milliseconds: int) -> datetime:
    base = datetime.fromisoformat(now)
    if base.tzinfo is None:
        base = base.replace(tzinfo=UTC)
    return base + timedelta(milliseconds=milliseconds)


def _reason(value: Any, trigger_id: str | None = None) -> Reason:
    if isinstance(value, Reason):
        return value
    if value is None:
        return Reason("routed", {}, trigger_id)
    if isinstance(value, dict):
        return Reason(str(value.get("code", "")), dict(value.get("values") or {}), trigger_id)
    return Reason(str(value), {}, trigger_id)


def _outcome(value: Any) -> DecisionOutcome:
    if isinstance(value, DecisionOutcome):
        return value
    try:
        return DecisionOutcome(str(value))
    except ValueError as exc:
        raise UserError("invalid_value", {"outcome": value}) from exc


class Evals(EnginePart):
    """One Eval per Thread, with timeout requeue and unavailable fallback."""

    def _find_eval(self, trigger_id: str) -> tuple[Any, Eval] | None:
        for thread in self.engine.repo.threads.list():
            eval_ = thread.eval
            if eval_ is not None and trigger_id in eval_.trigger_ids:
                return thread, eval_
        return None

    def _find_eval_by_id(self, eval_id: str) -> tuple[Any, Eval] | None:
        for thread in self.engine.repo.threads.list():
            eval_ = thread.eval
            if eval_ is not None and eval_.id == eval_id:
                return thread, eval_
        return None

    def _record_decision(
        self,
        thread: Any,
        outcome: DecisionOutcome,
        reason: Reason,
        trigger_ids: list[str],
        tx: Any,
    ) -> Decision:
        engine = self.engine
        now = engine.now()
        decision = Decision(
            id=decision_key(thread.key, now, trigger_ids),
            thread=SourceRef(source=thread.source, id=thread.id),
            outcome=outcome,
            reason=reason,
            at=now,
            trigger_ids=list(trigger_ids),
        )
        engine.repo.decisions.save(decision)
        tx.after_commit(
            lambda: engine.emit(
                DomainEvent(
                    DomainEvents.DECISION_RECORDED,
                    {"decision_id": decision.id, "outcome": outcome.value},
                )
            )
        )
        return decision

    def evaluate_due(self) -> None:
        """Start Evals for queued Threads or requeue timed-out ones."""
        engine = self.engine
        repo = engine.repo
        policy = engine.policy_get()
        if policy.paused:
            return
        now = engine.now()
        for thread in list(repo.threads.list()):
            eval_ = thread.eval
            if eval_ is not None:
                if not eval_.delivered or eval_.deadline is None:
                    continue
                if datetime.fromisoformat(eval_.deadline.isoformat()) <= datetime.fromisoformat(
                    now
                ):
                    self._requeue_timeout(thread, eval_, policy, now)
                continue
            if not thread.queued_trigger_ids:
                continue
            trigger_ids = list(thread.queued_trigger_ids)
            trigger = repo.triggers.get(trigger_ids[0])
            if trigger is None:
                thread.queued_trigger_ids = []
                repo.threads.save(thread)
                continue
            gate = policy.gate(trigger, thread, repo.tickets.list())
            with repo.atomic as tx:
                if gate.strength is None:
                    self._record_decision(
                        thread,
                        DecisionOutcome.IGNORE,
                        Reason("notAdmitted", {}, trigger.id),
                        trigger_ids,
                        tx,
                    )
                    thread.queued_trigger_ids = []
                    thread.expired_count = 0
                    repo.threads.save(thread)
                    continue
                eval_id = key("eval", thread.key, trigger_ids[0], now)
                next_eval = Eval(
                    id=eval_id,
                    thread=SourceRef(source=thread.source, id=thread.id),
                    primary=trigger.id,
                    trigger_ids=trigger_ids,
                    delivered=False,
                )
                thread.eval = next_eval
                thread.queued_trigger_ids = []
                meta = dict(thread.meta or {})
                meta[GATE_META_KEY] = {
                    "strength": gate.strength.value,
                    "basis": gate.basis.value if gate.basis else None,
                    "matched_rules": list(gate.matched_rules),
                    "policy_version": policy.version,
                    "cell": gate.cell.value if gate.cell else None,
                    "scope": gate.scope,
                }
                thread.meta = meta
                repo.threads.save(thread)
                engine.assessor.request(eval_id, trigger_ids)
                engine.effects.enqueue("assessment", eval_id, {"trigger_ids": trigger_ids})
                tx.after_commit(
                    lambda eid=eval_id, tid=trigger.id: engine.emit(
                        DomainEvent(DomainEvents.EVAL_STARTED, {"eval_id": eid, "trigger_id": tid})
                    )
                )

    def _requeue_timeout(self, thread: Any, eval_: Eval, policy: Any, now: str) -> None:
        engine = self.engine
        repo = engine.repo
        with repo.atomic as tx:
            thread.eval = None
            thread.expired_count += 1
            trigger_ids = list(eval_.trigger_ids)
            if thread.expired_count > EVAL_REQUEUE_LIMIT:
                self._record_decision(
                    thread,
                    DecisionOutcome.UNAVAILABLE,
                    Reason("timeout", {"count": thread.expired_count}),
                    trigger_ids,
                    tx,
                )
                thread.expired_count = 0
            else:
                thread.queued_trigger_ids.extend(
                    item for item in trigger_ids if item not in thread.queued_trigger_ids
                )
                tx.after_commit(
                    lambda: engine.emit(
                        DomainEvent(
                            DomainEvents.TRIGGER_REQUEUED,
                            {"thread_key": thread.key, "trigger_ids": trigger_ids},
                        )
                    )
                )
            thread.meta = None
            engine.assessor.release(eval_.id)
            repo.threads.save(thread)

    def candidate(self, trigger_id: str) -> dict[str, Any] | None:
        """Return the main Session card for a Trigger."""
        found = self._find_eval(trigger_id)
        if found is None:
            return None
        thread, eval_ = found
        trigger = self.engine.repo.triggers.get(eval_.primary)
        if trigger is None:
            return None
        gate = dict((thread.meta or {}).get(GATE_META_KEY) or {})
        return {
            "eval_id": eval_.id,
            "trigger": to_dict(trigger),
            "strength": gate.get("strength"),
            "matched_rules": gate.get("matched_rules", []),
            "policy_version": gate.get("policy_version"),
            "other_trigger_ids": [item for item in eval_.trigger_ids if item != trigger.id],
        }

    def candidate_delivered(self, eval_id: str) -> None:
        """Start the Eval deadline once the card reached the main Session."""
        found = self._find_eval_by_id(eval_id)
        if found is None:
            return
        thread, eval_ = found
        policy = self.engine.policy_get()
        eval_.delivered = True
        eval_.deadline = _deadline(self.engine.now(), policy.eval_timeout_ms)
        self.engine.repo.threads.save(thread)

    def decide(self, trigger_id: str, outcome: Any, reason: Any = None) -> None:
        """Record a Decision and apply its side effects."""
        engine = self.engine
        repo = engine.repo
        found = self._find_eval(trigger_id)
        if found is None:
            raise UserError("not_found", {"trigger_id": trigger_id})
        thread, eval_ = found
        policy = engine.policy_get()
        gate = dict((thread.meta or {}).get(GATE_META_KEY) or {})
        gate["trigger_ids"] = list(eval_.trigger_ids)
        resolved = _outcome(outcome)
        normalized_reason = _reason(reason, trigger_id)
        if (
            resolved == DecisionOutcome.ACCEPT
            and gate.get("strength") == "inferred"
            and policy.accept_mode.value != "auto"
        ):
            resolved = DecisionOutcome.IGNORE
            normalized_reason = Reason(
                "ignoredFromCard",
                {"would_accept": True, "trigger_id": trigger_id},
                trigger_id,
            )

        trigger = repo.triggers.get(eval_.primary)
        with repo.atomic as tx:
            self._record_decision(thread, resolved, normalized_reason, eval_.trigger_ids, tx)
            thread.eval = None
            thread.queued_trigger_ids = []
            meta = dict(thread.meta or {})
            meta.pop(GATE_META_KEY, None)
            thread.meta = meta or None
            repo.threads.save(thread)
            engine.assessor.release(eval_.id)
            if resolved == DecisionOutcome.ACCEPT and trigger is not None:
                engine.tickets.create(trigger, thread, gate, normalized_reason)
            elif resolved == DecisionOutcome.STOP:
                engine.tickets.stop_matching(thread, normalized_reason)
            elif resolved == DecisionOutcome.UNAVAILABLE:
                engine.effects.enqueue(
                    "notice",
                    eval_.id,
                    {"text": "An evaluation could not finish.", "event": "unavailable"},
                )

    def reevaluate(self, trigger_id: str) -> None:
        """Requeue a Trigger for a fresh Eval."""
        engine = self.engine
        repo = engine.repo
        found = self._find_eval(trigger_id)
        if found is not None:
            thread, eval_ = found
            with repo.atomic as tx:
                engine.assessor.release(eval_.id)
                thread.eval = None
                thread.queued_trigger_ids.append(trigger_id)
                repo.threads.save(thread)
                tx.after_commit(
                    lambda: engine.emit(
                        DomainEvent(DomainEvents.TRIGGER_REQUEUED, {"trigger_id": trigger_id})
                    )
                )
            return
        trigger = repo.triggers.get(trigger_id)
        if trigger is None:
            raise UserError("not_found", {"trigger_id": trigger_id})
        thread_key = engine.thread_key(trigger.threads[0])
        thread = repo.threads.get(thread_key)
        if thread is None:
            raise UserError("not_found", {"thread_key": thread_key})
        thread.queued_trigger_ids.append(trigger_id)
        repo.threads.save(thread)

    def dismiss(self, decision_id: str) -> None:
        """Mark an unavailable Decision as dismissed."""
        decision = self.engine.repo.decisions.get(decision_id)
        if decision is None:
            raise UserError("not_found", {"decision_id": decision_id})
        values = dict(decision.reason.values)
        values["dismissed"] = True
        decision.reason.values.clear()
        decision.reason.values.update(values)
        self.engine.repo.decisions.save(decision)


__all__ = ["GATE_META_KEY", "Evals"]
