"""Service layer for the session todo list.

Owns three behaviors the store does not:

* **Default-downgrade** — an unknown ``category`` collapses to ``quick`` and
  an unknown ``delegation`` collapses to ``self`` before persistence.
* **Transition barrier** — a todo may only be marked ``completed`` once the
  work it points at is actually finished: a linked subagent run must no longer
  be live, and a linked TaskFlow step must be ``done``. Plain todos (no links)
  are never blocked.
* **Local action semantics** — patch/remove/reattach/add resolve a stable
  ``todo_id``, preserve reference fields, and turn a stale id into
  ``TodoIdNotFoundError`` carrying the current valid list. Removing a flow's
  explicit root triggers an immediate compensation attempt; the sweep is still
  the backstop.

The barrier reaches three external seams (subagent registry liveness, TaskFlow
step status, WS push). Each is a **module-level injectable reference** — the
same pattern as ``agent/tools/taskflow/tools/_dispatch.py`` — so tests can
substitute it without touching the real runtime. The store is reached through
the ``store_sqlite`` module object for the same reason.

The WS push is fail-open: no registered websocket, or a failed ``send_text``,
is logged and swallowed; it never rolls back or masks a successful persistence.
"""

import asyncio

from loguru import logger

from config.features import TODOLIST_INFRA
from runtime import StateKey, state_register_db, state_register_mem
from runtime.session.ws_push import broadcast_to_session

from .config import (
    DEFAULT_CATEGORY,
    DEFAULT_DELEGATION,
    DEFAULT_PRIORITY,
    VALID_CATEGORIES,
    VALID_DELEGATIONS,
)
from .registry import store_sqlite as store

# TaskFlow's terminal step status; a linked step must reach it before the todo
# may complete. Imported lazily by _step_status to keep this module importable
# without the taskflow package.
_TASKFLOW_STEP_DONE = "done"

# TaskFlow terminal statuses: a FLOW-level todo (flow_id with no step_id) is a
# GC root for the whole flow, so it may only be completed once the flow itself
# is terminal. A step-level link keeps the existing per-step barrier.
_TASKFLOW_TERMINAL = frozenset({"done", "failed", "cancelled"})

# Session state key holding the plan file pointer (session-scoped
# ``workspace/sessions/<id>/plans/*.md``) that the prompt builder reads to
# locate the plan's knowledge summary.
_PLAN_REF_STATE_KEY = StateKey.PLAN_REF

#: The auto-inserted acceptance todo. Its content is a system-owned template:
#: a `replace` carrying the exact string is stripped so the list can only ever
#: hold the one real verify row (which carries ``metadata.kind="verify"``).
#: Two variants per language — the parallel-taskflow supplement rides only
#: when the plan has room for it (see ``_verify_content``).
_VERIFY_BASE_BY_LANG: dict[str, str] = {
    "en": (
        "[VERIFY] Run the plan's acceptance checks with a reviewer subagent: spawn "
        'sessions_spawn(functional_role="reviewer"), patch this todo\'s subagent_id '
        "with the returned child_session_key, then re-open only the todos it reports "
        "as failed. Also check the Intent Anchor (goal / must-have / must-NOT-have): "
        "require intent_match + intent_deviations in the report and re-open any "
        "deviating todo."
    ),
    "zh": (
        "[VERIFY] 用 reviewer 子代理对整套计划跑验收：spawn "
        'sessions_spawn(functional_role="reviewer")，把返回的 child_session_key '
        "写进本 todo 的 subagent_id，然后只把报告为失败的 todo 回退重开。"
        "同时对照 Intent Anchor（目标 / 必须 / 禁止）检查：要求报告给出 "
        "intent_match + intent_deviations，并回退重开所有偏离意图的 todo。"
    ),
    "ja": (
        "[VERIFY] reviewer サブエージェントで計画全体の受け入れ検証を行う："
        'sessions_spawn(functional_role="reviewer") を実行し、返された '
        "child_session_key をこの todo の subagent_id に書き、失敗と報告された "
        "todo だけを再オープンする。Intent Anchor（目標/必須/禁止）との照合も行い、"
        "intent_match と intent_deviations の報告を要求し、逸脱した todo を再オープンする。"
    ),
    "ko": (
        "[VERIFY] reviewer 서브에이전트로 계획 전체의 인수 검증을 실행한다: "
        'sessions_spawn(functional_role="reviewer")를 호출하고 반환된 '
        "child_session_key를 이 todo의 subagent_id에 기록한 뒤, 실패로 보고된 "
        "todo만 다시 연다. Intent Anchor(목표/필수/금지)와도 대조하여 "
        "intent_match 및 intent_deviations 보고를 요구하고, 이탈한 todo를 다시 연다."
    ),
}
#: The parallel-taskflow supplement: appended only for plans where the verify
#: actually has multiple fronts to cover (a TaskFlow link on some todo, or
#: ≥ 2 work items). A single unlinked step's verify is not told to fan out.
_VERIFY_PARALLEL_SUFFIX_BY_LANG: dict[str, str] = {
    "en": (
        " Run the verification itself with parallel TaskFlow whenever possible: "
        "independent acceptance items are sibling branches verified concurrently."
    ),
    "zh": "尽量用并行 taskflow 测：验收项之间无依赖时写成并列分支并行跑验收。",
    "ja": "可能な限り並列 TaskFlow で検証する：依存のない検証項目は並列な兄弟ブランチにして並行で検証する。",
    "ko": "가능한 한 병렬 TaskFlow로 검증한다: 의존이 없는 검증 항목은 병렬 형제 브랜치로 만들어 동시에 검증한다.",
}
_VERIFY_TODO_CONTENT = _VERIFY_BASE_BY_LANG["en"] + _VERIFY_PARALLEL_SUFFIX_BY_LANG["en"]
_VERIFY_CONTENT_BY_LANG: dict[str, str] = {
    lang: base + _VERIFY_PARALLEL_SUFFIX_BY_LANG[lang]
    for lang, base in _VERIFY_BASE_BY_LANG.items()
}
_VERIFY_CONTENT_VARIANTS = frozenset(_VERIFY_CONTENT_BY_LANG.values()) | frozenset(
    _VERIFY_BASE_BY_LANG.values()
)
_VERIFY_METADATA: dict = {"kind": "verify", "auto_inserted": True}
_ACTIVE_STATUSES = frozenset({"pending", "in_progress"})
_TERMINAL_STATUSES = frozenset({"completed", "cancelled"})


class TodoStoreError(Exception):
    """Raised when a todo mutation is rejected (transition barrier)."""


class TodoReferenceError(Exception):
    """Raised when dropping a flow root is refused by the reference policy."""


class TodoIdNotFoundError(Exception):
    """A local todo operation named an id that is not in this session.

    Carries the current valid list so the tool layer can tell the model exactly
    which ids exist instead of returning an opaque failure.
    """

    def __init__(self, todo_id: int, valid_todos: list[dict], *, field: str = "todo_id") -> None:
        self.todo_id = todo_id
        self.valid_todos = valid_todos
        self.field = field
        super().__init__(f"{field} {todo_id} not found")


def _verify_parallel_warranted(todos: list[dict]) -> bool:
    """Whether the verify earns the parallel-taskflow supplement.

    Either condition qualifies: a todo OTHER than the verify row itself carries
    a TaskFlow link (``flow_id``/``step_id``), or the plan minus the verify row
    has at least two items. A single unlinked step has nothing to fan out.
    """
    work = _work_todos(todos)
    if len(work) >= 2:
        return True
    return any(
        str(t.get("flow_id") or "").strip() or str(t.get("step_id") or "").strip() for t in work
    )


def _verify_content(todos: list[dict] | None = None) -> str:
    """The verify todo's text in the workspace's language (English fallback).

    The parallel-taskflow supplement rides only when the plan warrants it
    (``_verify_parallel_warranted``); a bare single-step plan gets the plain
    acceptance text.
    """
    try:
        from config.path import resolve_workspace_template_lang

        lang = resolve_workspace_template_lang()
    except Exception:  # noqa: BLE001 - the content must never block a mutation
        lang = "en"
    base = _VERIFY_BASE_BY_LANG.get(lang, _VERIFY_BASE_BY_LANG["en"])
    suffix = _VERIFY_PARALLEL_SUFFIX_BY_LANG.get(lang, _VERIFY_PARALLEL_SUFFIX_BY_LANG["en"])
    if todos is not None and not _verify_parallel_warranted(todos):
        return base
    return base + suffix


def is_verify_todo(todo: dict) -> bool:
    """True only for the SYSTEM verify todo (metadata.kind), never by content."""
    metadata = todo.get("metadata")
    return isinstance(metadata, dict) and str(metadata.get("kind") or "") == "verify"


#: Terminal run outcomes that are NOT a passing review. ``ok`` is the only
#: success; an absent outcome (legacy/partial records) stays ``finished`` so the
#: fail-open default is unchanged.
_VERIFY_FAILED_OUTCOMES = frozenset({"error", "timeout", "killed", "budget_exhausted", "unknown"})


def _reviewer_outcome(child_session_key: str) -> str | None:
    """The linked reviewer run's terminal outcome value, or ``None`` when unknown."""
    try:
        run = _get_run_by_child_session_key(child_session_key)
    except Exception:  # noqa: BLE001 - a registry failure must not change the state
        return None
    if run is None:
        return None
    outcome = getattr(getattr(run, "execution", None), "outcome", None)
    status = getattr(outcome, "status", None)
    value = str(getattr(status, "value", status) or "").strip().lower()
    return value or None


def verify_subagent_state(todo: dict) -> str:
    """``none`` | ``running`` | ``finished`` | ``failed`` for a verify todo.

    ``failed`` means the linked reviewer run is over but did NOT end with an OK
    verdict (budget_exhausted / timeout / killed / error, plus the explicit
    ``unknown`` status). Its report is partial and must not be read as a pass;
    an ABSENT outcome (missing run / legacy record) keeps the legacy
    ``finished`` reading (fail-open).
    """
    subagent_id = str(todo.get("subagent_id") or "").strip()
    if not subagent_id:
        return "none"
    if _is_subagent_running(subagent_id):
        return "running"
    outcome = _reviewer_outcome(subagent_id)
    if outcome is not None and outcome in _VERIFY_FAILED_OUTCOMES:
        return "failed"
    return "finished"


def verify_budget_exhausted(session_id: str) -> bool:
    """Public read of the exhausted-verify escape flag (used by the gate)."""
    return _read_verify_budget_flag(session_id)


async def set_verify_budget_exhausted(session_id: str, value: bool) -> None:
    """Public write of the exhausted-verify escape flag (used by the gate)."""
    await _set_verify_budget_flag(session_id, value)


def verify_respawns(todo: dict) -> int:
    """How many times this verify todo's reviewer link was replaced."""
    metadata = todo.get("metadata")
    if not isinstance(metadata, dict):
        return 0
    try:
        return max(0, int(metadata.get("verify_respawns") or 0))
    except (TypeError, ValueError):
        return 0


def _verify_enabled(session_id: str) -> bool:
    """Normalization is a MAIN-agent contract: children never grow a verify todo."""
    if not bool(TODOLIST_INFRA.get("verify_todo_enabled", True)):
        return False
    return ":subagent:" not in session_id and ":swarm:" not in session_id


def _active_verify_todos(todos: list[dict]) -> list[dict]:
    return [t for t in todos if is_verify_todo(t) and t.get("status") in _ACTIVE_STATUSES]


def _work_todos(todos: list[dict]) -> list[dict]:
    """Todos a verification would cover: non-verify and not cancelled."""
    return [t for t in todos if not is_verify_todo(t) and t.get("status") != "cancelled"]


def _read_verify_budget_flag(session_id: str) -> bool:
    """The exhausted-verify escape hatch (mem first, durable mirror second)."""
    try:
        if state_register_mem.get_state(session_id, StateKey.VERIFY_BUDGET_EXHAUSTED, False):
            return True
        return bool(
            state_register_db.get_state(session_id, StateKey.VERIFY_BUDGET_EXHAUSTED, False)
        )
    except Exception:  # noqa: BLE001 - a broken register must not block a transition
        return False


async def _set_verify_budget_flag(session_id: str, value: bool) -> None:
    """Dual-write the exhausted flag; a failed durable mirror is logged only."""
    try:
        state_register_mem.set_state(session_id, StateKey.VERIFY_BUDGET_EXHAUSTED, value)
    except Exception:  # noqa: BLE001
        logger.debug("verify flag: mem write failed", exc_info=True)
    try:
        await asyncio.to_thread(
            state_register_db.set_state, session_id, StateKey.VERIFY_BUDGET_EXHAUSTED, value
        )
    except Exception:  # noqa: BLE001
        logger.debug("verify flag: db write failed", exc_info=True)


async def _kill_verify_subagent(todo: dict, session_id: str) -> None:
    """Kill the reviewer linked to a verify todo that is being removed/cancelled.

    Injectable seam: tests substitute this module-level reference. Fail-open —
    an orphaned reviewer must not block the todo mutation.
    """
    subagent_id = str(todo.get("subagent_id") or "").strip()
    if not subagent_id:
        return
    try:
        run = _get_run_by_child_session_key(subagent_id)
        if run is None or not _is_live_unended_run(run):
            return
        from agent.tools.subagent.control.kill import kill_subagent_run_with_cascade
        from agent.tools.subagent.spawn.privilege import resolve_caller_session_key

        await kill_subagent_run_with_cascade(
            run.run_id,
            reason="verify_todo_removed",
            cascade=True,
            requester_session_key=resolve_caller_session_key(session_id),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("verify todo: failed to kill reviewer {}: {}", subagent_id, exc)


async def _normalize_verify(
    session_id: str, todos: list[dict], *, inherited: dict | None = None
) -> list[dict]:
    """Keep exactly one verify todo as the LAST step of any non-empty plan.

    No minimum work-item count: the acceptance check is unconditional, so a
    single-item plan is verified too (that was the measured gap). Idempotent
    and MAIN-agent-only. ``inherited`` is the active verify captured before a
    full replacement: its reviewer link survives the rewrite (the row itself
    cannot, because replace deletes every row).
    """
    if not _verify_enabled(session_id):
        return todos
    work = _work_todos(todos)
    active = _active_verify_todos(todos)
    has_active_work = any(t.get("status") in _ACTIVE_STATUSES for t in work)
    has_any_verify = any(is_verify_todo(t) for t in todos)
    skipped = any(
        is_verify_todo(t)
        and t.get("status") == "cancelled"
        and (t.get("metadata") or {}).get("cancelled_reason") == "budget_exhausted"
        for t in todos
    )
    if not work:
        # An EMPTY plan (or one whose work is all cancelled) has nothing to
        # verify: drop the active verify and any reviewer still attached to it.
        # This is the only remaining structural guard — the acceptance step
        # itself is unconditional.
        if inherited is not None:
            await _kill_verify_subagent(inherited, session_id)
        for todo in active:
            await _kill_verify_subagent(todo, session_id)
            await store.remove_todo_by_id(session_id, int(todo["todo_id"]))
        return await store.get_todos(session_id) if active else todos

    if not active:
        # All work terminal + a terminal verify already exists => the plan is
        # finished (or acceptance was deliberately skipped): do not re-arm it.
        # A new plan has active work, and a first-time replace has no verify at
        # all, so both still get one.
        if skipped or not (has_active_work or not has_any_verify):
            return todos
        payload = {
            "content": _verify_content(todos),
            "status": "pending",
            "priority": "high",
            "category": "deep",
            "delegation": "subagent",
        }
        metadata = dict(_VERIFY_METADATA)
        if inherited is not None:
            if inherited.get("subagent_id"):
                payload["subagent_id"] = inherited["subagent_id"]
            if inherited.get("notes"):
                payload["notes"] = inherited["notes"]
            metadata.update(inherited.get("metadata") or {})
            metadata.update(_VERIFY_METADATA)
        # A fresh verify starts with no inherited exhausted escape.
        await _set_verify_budget_flag(session_id, False)
        await store.add_todo(session_id, payload, system_metadata=metadata)
        return await store.get_todos(session_id)

    if len(active) > 1:
        keep = max(active, key=lambda t: int(t.get("position") or 0))
        for todo in active:
            if todo.get("todo_id") == keep.get("todo_id"):
                continue
            await _kill_verify_subagent(todo, session_id)
            await store.remove_todo_by_id(session_id, int(todo["todo_id"]))
        active = [keep]

    verify = active[0]
    # Keep the verify's wording in step with the plan: the parallel-taskflow
    # supplement is conditional (warranted when another todo carries a TaskFlow
    # link or the plan has ≥ 2 work items), and that condition can flip AFTER
    # the row was first inserted — a plan that grows from one item to three
    # must start telling the reviewer to fan out. Only system-owned wording is
    # ever rewritten (both variants are ours to pick between).
    desired = _verify_content(todos)
    current = str(verify.get("content") or "")
    if current != desired and current in _VERIFY_CONTENT_VARIANTS:
        await store.patch_todo_by_id(session_id, int(verify["todo_id"]), content=desired)
    latest = await store.get_todos(session_id)
    max_position = max(int(t.get("position") or 0) for t in latest)
    if int(verify.get("position") or 0) != max_position:
        await store.move_todo_to_tail(session_id, int(verify["todo_id"]))
        latest = await store.get_todos(session_id)
    return latest


def _get_run_by_child_session_key(child_session_key: str):
    """Injectable seam: look up a subagent run record by child session key."""
    from agent.tools.subagent.registry import get_run_by_child_session_key

    return get_run_by_child_session_key(child_session_key)


def _is_live_unended_run(run) -> bool:
    """Injectable seam: True while the run is RUNNING or INTERRUPTED."""
    from agent.tools.subagent.registry import is_live_unended_run

    return is_live_unended_run(run)


async def _load_flow(flow_id: str, session_id: str) -> dict | None:
    """Injectable seam: read-only TaskFlow flow lookup (never schedules).

    Reads are session-scoped: a flow owned by another session reads as missing.
    """
    from agent.tools.taskflow.registry import get_flow

    return await get_flow(flow_id, session_id)


def _step_status(step: dict) -> str:
    """Injectable seam: derive a TaskFlow step's status (legacy-aware)."""
    from agent.tools.taskflow.tools._shared import step_status

    return step_status(step)


def _validate_todos(todos: list[dict]) -> list[dict]:
    """Return shallow copies with the category/delegation vocabularies default-downgraded.

    Status and priority are left exactly as given. Copies (not in-place edits)
    keep the caller's dicts untouched.
    """
    validated: list[dict] = []
    for todo in todos:
        item = dict(todo)
        if item.get("category") not in VALID_CATEGORIES:
            item["category"] = DEFAULT_CATEGORY
        if item.get("delegation") not in VALID_DELEGATIONS:
            item["delegation"] = DEFAULT_DELEGATION
        validated.append(item)
    return validated


def _is_subagent_running(child_session_key: str) -> bool:
    """True when a run record exists for the key and is still live.

    A missing record is not a blocker: the subagent either never registered or
    has already been swept, and the todo must remain completable.
    """
    run = _get_run_by_child_session_key(child_session_key)
    if run is None:
        return False
    return _is_live_unended_run(run)


async def _read_taskflow_step_status(
    flow_id: str, step_id: str, session_id: str
) -> tuple[str | None, str | None]:
    """Read a linked TaskFlow step's status, or the reason it is unresolvable.

    Returns ``(status, problem)`` with exactly one meaningful value. ``problem``
    is a human-readable reason when the flow is absent (or owned by another
    session) or the step is not in it; both are treated as "not done" by the
    barrier.
    """
    flow = await _load_flow(flow_id, session_id)
    if flow is None:
        return None, f"TaskFlow flow '{flow_id}' not found"
    steps = (flow.get("state") or {}).get("steps") or []
    for step in steps:
        if step.get("step_id") == step_id:
            return _step_status(step), None
    return None, f"TaskFlow step '{step_id}' not found in flow '{flow_id}'"


async def _assert_transition_allowed(
    todo: dict,
    session_id: str,
    *,
    target_status: str = "completed",
    todos: list[dict] | None = None,
) -> None:
    """Raise :class:`TodoStoreError` unless a transition may proceed.

    Work todos keep the historical ``completed`` gate: a linked subagent run
    must no longer be live, and a linked TaskFlow step must be ``done``. The
    SYSTEM verify todo adds its own contract:

    - ``completed`` requires a linked reviewer (``subagent_id``), a finished
      reviewer run, and every work todo already terminal;
    - ``cancelled`` is allowed only by the exhausted-verify escape flag or when
      the whole plan was cancelled; otherwise the model could skip acceptance.

    Plain todos carry neither link and are therefore never blocked.
    """
    if is_verify_todo(todo):
        current = todos if todos is not None else await store.get_todos(session_id)
        work = [t for t in current if not is_verify_todo(t)]
        if target_status == "completed":
            subagent_id = str(todo.get("subagent_id") or "").strip()
            if not subagent_id:
                raise TodoStoreError(
                    "Cannot complete the verify todo: no reviewer subagent is linked. "
                    "Spawn sessions_spawn(functional_role='reviewer'), then patch this todo's "
                    "subagent_id with the returned child_session_key."
                )
            if _is_subagent_running(subagent_id):
                raise TodoStoreError(
                    f"Cannot complete the verify todo: reviewer {subagent_id} is still "
                    "running. Wait for it to finish first."
                )
            # Machine check for intent drift: the approved plan file must still
            # hash to the revision the anchor was written for. A model can edit
            # notes, but it cannot silently redefine the approved intent.
            from agent.tools.plan_mode.intent_anchor import plan_drifted

            if plan_drifted(session_id):
                raise TodoStoreError(
                    "Cannot complete the verify todo: the approved plan changed since "
                    "its intent was anchored. Call enter_plan_mode(replan=True), get the "
                    "new plan approved, then verify again."
                )
            open_work = [t for t in work if t.get("status") not in _TERMINAL_STATUSES]
            if open_work:
                ids = ", ".join(f"#{t.get('todo_id')} ({t.get('status')})" for t in open_work[:8])
                raise TodoStoreError(
                    f"Cannot complete the verify todo: work todos are still open: {ids}. "
                    "Re-open/fix the failed ones first; verify is the last step."
                )
            return
        if target_status == "cancelled":
            if _read_verify_budget_flag(session_id):
                # The caller records the exhausted-escape marker and clears the
                # flag after the row is written; clearing here would lose it.
                return
            if all(t.get("status") == "cancelled" for t in work):
                return
            raise TodoStoreError(
                "The verify todo cannot be cancelled directly; complete it via the "
                "reviewer, or cancel the whole plan (every work todo cancelled)."
            )
        return

    subagent_id = todo.get("subagent_id")
    if subagent_id and _is_subagent_running(str(subagent_id)):
        raise TodoStoreError(
            f"Cannot mark todo completed: subagent {subagent_id} is still running. "
            "Wait for it to finish first."
        )

    flow_id = todo.get("flow_id")
    step_id = todo.get("step_id")
    if flow_id and step_id:
        status, problem = await _read_taskflow_step_status(str(flow_id), str(step_id), session_id)
        if problem is not None:
            raise TodoStoreError(
                f"Cannot mark todo completed: {problem}. Call taskflow_step(action='wait_all') then "
                "taskflow_step(action='resume') to inject the result before marking completed."
            )
        if status != _TASKFLOW_STEP_DONE:
            raise TodoStoreError(
                f"Cannot mark todo completed: TaskFlow step {step_id} "
                f"(flow {flow_id}) is '{status}'. Call taskflow_step(action='wait_all') then "
                "taskflow_step(action='resume') to inject the result before marking completed."
            )
    elif flow_id:
        # Flow-level root: the whole flow must be terminal first. This is what
        # keeps a multi-step flow alive while an early step is done.
        flow = await _load_flow(str(flow_id), session_id)
        if flow is None:
            # The flow was pruned/removed; there is no running work left to
            # gate. Refusing here would strand the todo forever.
            return
        if str(flow.get("status") or "") not in _TASKFLOW_TERMINAL:
            raise TodoStoreError(
                f"Cannot mark todo completed: TaskFlow flow '{flow_id}' is "
                f"'{flow.get('status')}'. Finish or cancel the flow first."
            )


def _plan_is_gone(todos: list[dict]) -> bool:
    """Whether the live plan is over: no todos, or every todo is terminal.

    The intent anchor belongs to ONE plan (one todo list). It must be cleared
    when that list is gone so a stale intent cannot leak into the NEXT task —
    the anchor rides the todo reminder, so an orphaned one silently re-anchors
    unrelated later work. A list is over when it is empty (all rows removed /
    replaced with nothing) or every row is ``completed``/``cancelled``.

    While ANY todo is still active (``pending``/``in_progress``), the plan —
    and therefore its intent — is live. Keeping the anchor through the verify
    step is essential: ``_assert_transition_allowed`` calls ``plan_drifted``
    before the verify todo may complete, and that gate needs the anchor.
    """
    if not todos:
        return True
    return all(t.get("status") in _TERMINAL_STATUSES for t in todos)


def _sync_intent_anchor(session_id: str, todos: list[dict]) -> None:
    """Clear the approved-plan intent anchor once its todo list is finished.

    Kept strictly out of the drift gate's way: this runs from
    ``_push_todo_update`` AFTER the write and after ``_normalize_verify``, so a
    verify todo that was just completed already passed ``plan_drifted`` while
    the anchor was still present. Cleanup is best-effort and never raises.
    """
    if not session_id or not _plan_is_gone(todos):
        return
    try:
        from agent.tools.plan_mode.intent_anchor import clear_intent_anchor

        clear_intent_anchor(session_id)
    except Exception:  # noqa: BLE001 - lifecycle cleanup never breaks a write
        logger.debug("intent anchor: todo-gone cleanup failed ({})", session_id, exc_info=True)


async def _push_todo_update(session_id: str, todos: list[dict]) -> None:
    """Sync anchor lifecycle, then best-effort ``todo_updated`` WS frame.

    Mirrors ``server/service/heartbeat.py``: an absent websocket or a failed
    send is logged and swallowed so it cannot break the persistence path.
    """
    _sync_intent_anchor(session_id, todos)
    try:
        payload = {
            "event": "todo_updated",
            "session_id": session_id,
            "content": {"todos": todos},
        }
        await broadcast_to_session(session_id, payload, ensure_ascii=False)
    except Exception as e:
        logger.warning("Failed to push todo_updated for session {}: {}", session_id, e)


async def _compensate_removed_flow(session_id: str, flow_id: str) -> None:
    """Fail a flow immediately when its explicit root todo was removed.

    The background sweep is the backstop; an explicit ``remove`` should not
    wait for the grace window when no other active todo still references the
    flow. Fail-open: a lookup/write failure leaves the sweep to collect it.
    """
    try:
        from agent.tools.taskflow.registry import store_sqlite as flow_store
        from agent.tools.taskflow.tools._shared import is_terminal

        if flow_id in await store.get_active_flow_ids(session_id):
            return
        flow = await flow_store.get_flow(flow_id, session_id)
        if flow is None or is_terminal(str(flow.get("status") or "")):
            return
        await flow_store.fail_flow_for_gc(flow, reason="todo_removed_explicitly")
    except Exception as exc:  # noqa: BLE001 - the unreferenced-flow sweep is the backstop
        logger.warning("todo remove: immediate flow compensation failed for {}: {}", flow_id, exc)


def _set_reference_warning(session_id: str, flow_ids: list[str]) -> None:
    """Park a model-facing warning about flows auto-cancelled by a replacement."""
    try:
        state_register_mem.set_state(
            session_id,
            StateKey.TODO_REFERENCE_WARNING,
            list(dict.fromkeys(str(flow_id) for flow_id in flow_ids if flow_id)),
        )
    except Exception:  # noqa: BLE001 - warning is best-effort
        logger.debug("todo reference warning write failed", exc_info=True)


async def _trigger_session_sweep(session_id: str) -> None:
    """Run the unreferenced-flow sweep for this session right after a replace.

    Session-scoped only (the system-wide scan stays on its own timer) and with
    the CONFIGURED grace window: a flow whose root todo was completed moments
    ago is still on its finish path, and collecting it here would race
    ``taskflow_flow(action="finish")``. The replacement's targeted compensation
    already handles the flows it explicitly dropped; this pass is the backstop
    for older leftovers.
    """
    try:
        from agent.tools.subagent.registry.sweeper import _unreferenced_flow_sweep

        await _unreferenced_flow_sweep(session_ids={session_id})
    except Exception as exc:  # noqa: BLE001 - todo replacement must not be blocked
        logger.warning("post-replace unreferenced-flow sweep failed: {}", exc)


class TodoService:
    """Session-scoped todo mutations: validate, barrier, persist, push, return."""

    @staticmethod
    async def update_todos(
        session_id: str,
        todos: list[dict],
        plan_ref: str | None = None,
    ) -> list[dict]:
        """Full-replace a session's todos, enforcing the default-downgrade
        and the transition barrier.

        A non-empty ``plan_ref`` is remembered once per session under the
        ``plan_ref`` state key; the prompt builder reads it to locate the
        plan's knowledge summary. The barrier runs BEFORE any write, so a
        rejected transition leaves the persisted rows untouched. Returns the
        freshly read-back list.
        """
        validated = _validate_todos(todos)
        # The model may copy the verify row it saw in the reminder. That copy
        # cannot carry metadata.kind (the tool never exposes it), so stripping
        # by the exact system template leaves exactly one real verify row.
        validated = [t for t in validated if t.get("content") not in _VERIFY_CONTENT_VARIANTS]
        for todo in validated:
            if todo.get("status") == "completed":
                await _assert_transition_allowed(todo, session_id, todos=validated)

        old_todos = await store.get_todos(session_id)
        old_verify = next(iter(_active_verify_todos(old_todos)), None)
        old_flow_ids = {str(t.get("flow_id")) for t in old_todos if t.get("flow_id")}
        new_flow_ids = {str(t.get("flow_id")) for t in validated if t.get("flow_id")}
        orphaned = sorted(old_flow_ids - new_flow_ids)
        mode = str(TODOLIST_INFRA.get("orphan_flow_on_drop", "refuse")).lower()
        if mode not in {"cancel", "refuse", "sweep_only"}:
            mode = "refuse"
        if orphaned and mode == "refuse":
            raise TodoReferenceError(
                "Cannot replace the todo list: it would drop the GC-root todo of "
                f"active flow(s) {', '.join(orphaned)}. Keep each flow's todo in the "
                'replacement list (or use todowrite(action="patch") to edit items), '
                'or finish/cancel the flow first with taskflow_flow(action="finish") '
                '/ (action="cancel").'
            )
        if orphaned and mode == "cancel":
            from agent.tools.taskflow.tools._shared import compensate_orphaned_flows

            cancelled = await compensate_orphaned_flows(
                session_id, orphaned, reason="todo_dropped_in_full_replace"
            )
            if cancelled:
                cancelled_ids = [str(flow.get("flow_id") or "") for flow in cancelled]
                logger.warning(
                    "update_todos auto-cancelled {} orphaned flow(s): {}",
                    len(cancelled_ids),
                    cancelled_ids,
                )
                _set_reference_warning(session_id, cancelled_ids)

        await store.replace_all(session_id, validated)
        latest = await _normalize_verify(
            session_id, await store.get_todos(session_id), inherited=old_verify
        )
        if plan_ref:
            from runtime import state_register_db

            # SQLite write on an async path: keep the (sub-millisecond but
            # fsync-bound) statement off the event loop.
            await asyncio.to_thread(
                state_register_db.set_state, session_id, _PLAN_REF_STATE_KEY, plan_ref
            )
        await _push_todo_update(session_id, latest)
        if TODOLIST_INFRA.get("trigger_sweep_after_replace", True):
            await _trigger_session_sweep(session_id)
        return latest

    @staticmethod
    def consume_reference_warning(session_id: str) -> list[str]:
        """Take and clear the pending reference-integrity warning for a session."""
        try:
            value = state_register_mem.get_state(session_id, StateKey.TODO_REFERENCE_WARNING, [])
            state_register_mem.delete_state(session_id, StateKey.TODO_REFERENCE_WARNING)
        except Exception:  # noqa: BLE001 - warning is best-effort
            return []
        if not isinstance(value, list):
            return []
        return [str(flow_id) for flow_id in value if str(flow_id or "").strip()]

    @staticmethod
    async def add_todo(
        session_id: str,
        *,
        content: str,
        flow_id: str | None = None,
        step_id: str | None = None,
        category: str | None = None,
        after_todo_id: int | None = None,
        plan_ref: str | None = None,
    ) -> list[dict]:
        """Insert one pending todo, at the tail or after a stable todo_id.

        Unlike ``update_todos`` this is a single INSERT, so it cannot clobber a
        concurrent full replacement. The category vocabulary is default-
        downgraded like every other write. ``after_todo_id`` is the stable
        anchor for an in-list insertion; None appends at the tail.
        """
        item = _validate_todos(
            [
                {
                    "content": content,
                    "status": "pending",
                    "priority": DEFAULT_PRIORITY,
                    "category": category or DEFAULT_CATEGORY,
                    "flow_id": flow_id,
                    "step_id": step_id,
                    "plan_ref": plan_ref,
                }
            ]
        )[0]
        if after_todo_id is not None:
            anchor = await store.get_todo_by_id(session_id, int(after_todo_id))
            if anchor is not None and is_verify_todo(anchor):
                raise TodoReferenceError(
                    "Cannot insert after the verify todo; it must remain last. "
                    "Pass after_todo_id=None (the new todo is placed before it)."
                )
        inserted = await store.add_todo(session_id, item, after_todo_id=after_todo_id)
        if inserted is None:
            raise TodoIdNotFoundError(
                int(after_todo_id or 0),
                await store.get_todos(session_id),
                field="after_todo_id",
            )
        if plan_ref:
            from runtime import state_register_db

            await asyncio.to_thread(
                state_register_db.set_state,
                session_id,
                _PLAN_REF_STATE_KEY,
                plan_ref,
            )
        latest = await _normalize_verify(session_id, await store.get_todos(session_id))
        await _push_todo_update(session_id, latest)
        return latest

    @staticmethod
    async def patch_todo(
        session_id: str,
        todo_id: int,
        *,
        content: str | None = None,
        status: str | None = None,
        priority: str | None = None,
        category: str | None = None,
        notes: str | None = None,
        subagent_id: str | None = None,
    ) -> dict:
        """Patch one todo by its stable id; reference fields stay untouched.

        ``notes`` is model-writable evidence; ``subagent_id`` links a reviewer to
        the verify todo (a changed link counts as a re-spawn).
        """
        if content is not None and not str(content).strip():
            raise TodoStoreError("Cannot patch todo: content must be a non-empty string")
        normalized_category = category
        if category is not None and category not in VALID_CATEGORIES:
            normalized_category = DEFAULT_CATEGORY

        todo = await store.get_todo_by_id(session_id, todo_id)
        if todo is None:
            raise TodoIdNotFoundError(todo_id, await store.get_todos(session_id))

        verify = is_verify_todo(todo)
        metadata: dict | None = None
        if verify and status in {"completed", "cancelled"}:
            await _assert_transition_allowed(todo, session_id, target_status=str(status))
            if status == "cancelled" and _read_verify_budget_flag(session_id):
                # Remember WHY acceptance was skipped so normalization does not
                # immediately append a fresh verify for an abandoned plan.
                metadata = dict(todo.get("metadata") or {})
                metadata["cancelled_reason"] = "budget_exhausted"
        elif status == "completed":
            await _assert_transition_allowed(
                todo, session_id, todos=await store.get_todos(session_id)
            )

        if verify and subagent_id is not None:
            previous = str(todo.get("subagent_id") or "").strip()
            if previous and previous != subagent_id:
                metadata = dict(metadata or todo.get("metadata") or {})
                metadata["verify_respawns"] = verify_respawns(todo) + 1

        updated = await store.patch_todo_by_id(
            session_id,
            todo_id,
            content=content,
            status=status,
            priority=priority,
            category=normalized_category,
            subagent_id=subagent_id,
            notes=notes,
            metadata=metadata,
        )
        if updated is None:
            raise TodoIdNotFoundError(todo_id, await store.get_todos(session_id))
        if verify and status in {"completed", "cancelled"}:
            await _set_verify_budget_flag(session_id, False)
        if verify and status == "cancelled":
            await _kill_verify_subagent(updated, session_id)
        latest = await _normalize_verify(session_id, await store.get_todos(session_id))
        await _push_todo_update(session_id, latest)
        return updated

    @staticmethod
    async def remove_todo(session_id: str, todo_id: int) -> dict:
        """Remove one todo by id; a removed flow root is compensated immediately.

        The system verify todo is never removable: it is completed or cancelled
        through its own guarded transitions, so the model cannot delete its way
        out of acceptance.
        """
        existing = await store.get_todo_by_id(session_id, todo_id)
        if existing is None:
            raise TodoIdNotFoundError(todo_id, await store.get_todos(session_id))
        if is_verify_todo(existing):
            raise TodoStoreError(
                "The system verify todo cannot be removed. Complete it via its reviewer, "
                "or cancel the whole plan and cancel it instead."
            )
        removed = await store.remove_todo_by_id(session_id, todo_id)
        if removed is None:
            raise TodoIdNotFoundError(todo_id, await store.get_todos(session_id))
        if removed.get("flow_id"):
            await _compensate_removed_flow(session_id, str(removed["flow_id"]))
        latest = await _normalize_verify(session_id, await store.get_todos(session_id))
        await _push_todo_update(session_id, latest)
        return removed

    @staticmethod
    async def reattach_todo(session_id: str, todo_id: int, flow_id: str) -> dict:
        """Bind an existing todo as the GC root of an active flow."""
        flow = await _load_flow(flow_id, session_id)
        if flow is None:
            raise TodoStoreError(f"Cannot reattach: TaskFlow flow '{flow_id}' not found")
        if str(flow.get("status") or "") in _TASKFLOW_TERMINAL:
            raise TodoStoreError(
                f"Cannot reattach: TaskFlow flow '{flow_id}' is terminal "
                f"(status={flow.get('status')})"
            )
        updated = await store.set_todo_flow_id_by_id(session_id, todo_id, flow_id)
        if updated is None:
            raise TodoIdNotFoundError(todo_id, await store.get_todos(session_id))
        await _push_todo_update(session_id, await store.get_todos(session_id))
        return updated

    @staticmethod
    async def list_orphan_flows(session_id: str) -> list[dict]:
        """Active flows in this session that no active todo currently references."""
        from agent.tools.taskflow.registry import store_sqlite as flow_store

        roots = await store.get_active_flow_ids(session_id)
        flows = await flow_store.get_non_terminal_flows(session_id)
        orphans: list[dict] = []
        for flow in flows:
            flow_id = str(flow.get("flow_id") or "")
            if not flow_id or flow_id in roots:
                continue
            state = flow.get("state") or {}
            orphans.append(
                {
                    "flow_id": flow_id,
                    "status": flow.get("status"),
                    "description": str(state.get("description") or ""),
                    "child_session_key": flow.get("child_session_key") or "",
                    "expected_revision": int(flow.get("expected_revision") or 0),
                }
            )
        return orphans

    @staticmethod
    async def complete_todos_for_flows(session_id: str, flow_ids: list[str]) -> int:
        """Mark active todos completed when their flow finishes successfully."""
        completed = await store.complete_todos_for_flows(session_id, flow_ids)
        if completed:
            latest = await _normalize_verify(session_id, await store.get_todos(session_id))
            await _push_todo_update(session_id, latest)
        return completed

    @staticmethod
    async def get_todos(session_id: str) -> list[dict]:
        """Read the session's todos from the store."""
        return await store.get_todos(session_id)

    @staticmethod
    async def get_active_flow_ids(session_id: str) -> set[str]:
        """The session's GC roots: flow ids referenced by active todos."""
        return await store.get_active_flow_ids(session_id)

    @staticmethod
    async def cancel_todos_for_flows(session_id: str, flow_ids: list[str]) -> int:
        """Cancel active todos whose referenced flows are already terminal-failed."""
        cancelled = await store.cancel_todos_for_flows(session_id, flow_ids)
        if cancelled:
            latest = await _normalize_verify(session_id, await store.get_todos(session_id))
            await _push_todo_update(session_id, latest)
        return cancelled


__all__ = [
    "TodoIdNotFoundError",
    "TodoReferenceError",
    "TodoService",
    "TodoStoreError",
    "_TASKFLOW_TERMINAL",
    "_VERIFY_TODO_CONTENT",
    "_normalize_verify",
    "_plan_is_gone",
    "_sync_intent_anchor",
    "is_verify_todo",
    "set_verify_budget_exhausted",
    "verify_budget_exhausted",
    "verify_respawns",
    "verify_subagent_state",
    "_assert_transition_allowed",
    "_compensate_removed_flow",
    "_push_todo_update",
    "_read_taskflow_step_status",
    "_trigger_session_sweep",
    "_validate_todos",
]
