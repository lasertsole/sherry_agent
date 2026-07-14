import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from loguru import logger

from skills.curator.config import get_consolidate, get_min_idle_hours
from skills.curator.state import load_state, save_state
from skills.curator.usage import agent_created_report
from skills.curator.transitions import should_run_now, apply_automatic_transitions
from skills.curator.helpers import _cron_referenced_skills
from skills.curator.report import _build_rename_summary, _write_run_report


CURATOR_REVIEW_PROMPT = (
    "You are running as the background skill CURATOR. This is an "
    "UMBRELLA-BUILDING consolidation pass, not a passive audit.\n\n"
    "The goal is a LIBRARY OF CLASS-LEVEL INSTRUCTIONS. A collection of hundreds of "
    "narrow skills is a FAILURE — not a feature.\n\n"
    "Hard rules — do not violate:\n"
    "1. DO NOT touch bundled or built-in skills.\n"
    "2. DO NOT delete any skill. Archiving is the maximum destructive action.\n"
    "3. DO NOT touch pinned skills.\n"
    "4. DO NOT archive skills referenced by cron jobs.\n"
    "5. DO NOT use usage counters as sole reason to skip consolidation.\n\n"
    "Consolidation strategies:\n"
    "a. MERGE INTO EXISTING UMBRELLA — patch it to add labeled sections, archive siblings.\n"
    "b. CREATE NEW UMBRELLA — write class-level skill, archive siblings.\n"
    "c. DEMOTE TO references/ templates/ scripts/ — move narrow content into umbrella's "
    "support directories, archive old sibling.\n\n"
    "When done, produce:\n"
    "## Structured summary (required)\n"
    "```yaml\n"
    "consolidations:\n"
    "  - from: <old-skill-name>\n"
    "    into: <umbrella-skill-name>\n"
    "    reason: <why merged>\n"
    "prunings:\n"
    "  - name: <skill-name>\n"
    "    reason: <why archived>\n"
    "```\n"
)

CURATOR_DRY_RUN_BANNER = (
    "═══════════════════════════════════════════════\n"
    "DRY-RUN — REPORT ONLY. DO NOT MUTATE THE SKILL LIBRARY.\n"
    "═══════════════════════════════════════════════\n\n"
    "Produce the same summary you would on a live run, but describe "
    "actions you WOULD take, not actions you took.\n\n"
)


def _render_candidate_list() -> str:
    rows = agent_created_report()
    if not rows:
        return "No agent-created skills to review."
    cron_referenced = _cron_referenced_skills()
    lines = [f"Agent-created skills ({len(rows)}):\n"]
    for r in rows:
        lines.append(
            f"- {r['name']}  state={r['state']}  "
            f"pinned={'yes' if r.get('pinned') else 'no'}  "
            f"cron={'yes' if r['name'] in cron_referenced else 'no'}  "
            f"use={r.get('use_count', 0)}  "
            f"last_activity={r.get('last_activity_at') or 'never'}"
        )
    return "\n".join(lines)


def _run_llm_review(prompt: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "final": "", "summary": "", "model": "", "provider": "",
        "tool_calls": [], "error": None,
    }
    try:
        from langchain_core.messages import HumanMessage
        from models import build_main_llm
        from workspace.prompt_builder import build_system_prompt

        llm = build_main_llm(temperature=0.3)
        messages = [
            {"role": "system", "content": build_system_prompt()},
            {"role": "user", "content": prompt},
        ]
        response = llm.invoke([HumanMessage(content=prompt)])
        final = str(response.content).strip() if response and response.content else ""
        result["final"] = final
        result["summary"] = (final[:240] + "…") if len(final) > 240 else (final or "no change")
    except Exception as e:
        result["error"] = str(e)
        result["summary"] = f"error: {e}"
    return result


def run_curator_review(
    on_summary: Optional[Callable[[str], None]] = None,
    synchronous: bool = True,
    dry_run: bool = False,
    consolidate: Optional[bool] = None,
) -> Dict[str, Any]:
    if consolidate is None:
        consolidate = get_consolidate()
    start = datetime.now(timezone.utc)

    if dry_run:
        try:
            report = agent_created_report()
            counts = {"checked": len(report), "marked_stale": 0, "archived": 0, "reactivated": 0, "seeded": 0}
        except Exception:
            counts = {"checked": 0, "marked_stale": 0, "archived": 0, "reactivated": 0, "seeded": 0}
    else:
        counts = apply_automatic_transitions(now=start)

    auto_parts = []
    if counts["marked_stale"]:
        auto_parts.append(f"{counts['marked_stale']} marked stale")
    if counts["archived"]:
        auto_parts.append(f"{counts['archived']} archived")
    if counts["reactivated"]:
        auto_parts.append(f"{counts['reactivated']} reactivated")
    auto_summary = ", ".join(auto_parts) if auto_parts else "no changes"

    state = load_state()
    if not dry_run:
        state["last_run_at"] = start.isoformat()
        state["run_count"] = int(state.get("run_count", 0)) + 1
    prefix = "dry-run auto: " if dry_run else "auto: "
    state["last_run_summary"] = f"{prefix}{auto_summary}"
    save_state(state)

    def _llm_pass() -> None:
        nonlocal auto_summary

        try:
            before_report = agent_created_report()
        except Exception:
            before_report = []
        before_names = {r.get("name") for r in before_report if isinstance(r, dict)}

        if not consolidate:
            final_summary = f"{prefix}{auto_summary}; llm: skipped (consolidation off)"
            llm_meta: Dict[str, Any] = {"final": "", "summary": "skipped (consolidation off)", "model": "", "provider": "", "tool_calls": [], "error": None}
            elapsed = (datetime.now(timezone.utc) - start).total_seconds()
            try:
                after_report = agent_created_report()
            except Exception:
                after_report = []
            try:
                report_path = _write_run_report(started_at=start, elapsed_seconds=elapsed, auto_counts=counts, auto_summary=auto_summary, before_report=before_report, before_names=before_names, after_report=after_report, llm_meta=llm_meta)
                rp = str(report_path) if report_path else None
            except Exception:
                rp = None
            state2 = load_state()
            state2["last_run_duration_seconds"] = round(elapsed, 2)
            state2["last_run_summary"] = final_summary
            if rp:
                state2["last_report_path"] = rp
            save_state(state2)
            if on_summary:
                try:
                    on_summary(f"curator: {final_summary}")
                except Exception:
                    pass
            return

        llm_meta: Dict[str, Any] = {"final": "", "summary": "", "model": "", "provider": "", "tool_calls": [], "error": None}
        try:
            candidate_list = _render_candidate_list()
            if "No agent-created skills" in candidate_list:
                final_summary = f"{prefix}{auto_summary}; llm: skipped (no candidates)"
                llm_meta["summary"] = "skipped (no candidates)"
            else:
                if dry_run:
                    prompt = f"{CURATOR_DRY_RUN_BANNER}\n{CURATOR_REVIEW_PROMPT}\n{candidate_list}"
                else:
                    prompt = f"{CURATOR_REVIEW_PROMPT}\n{candidate_list}"
                llm_meta = _run_llm_review(prompt)
                final_summary = f"{prefix}{auto_summary}; llm: {llm_meta.get('summary', 'no change')}"
        except Exception as e:
            final_summary = f"{prefix}{auto_summary}; llm: error ({e})"
            llm_meta = {"final": "", "summary": f"error ({e})", "model": "", "provider": "", "tool_calls": [], "error": str(e)}

        try:
            rename_lines = _build_rename_summary(before_names=before_names, after_report=agent_created_report(), tool_calls=llm_meta.get("tool_calls", []) or [], model_final=llm_meta.get("final", "") or "")
            if rename_lines:
                final_summary = f"{final_summary}\n{rename_lines}"
        except Exception as e:
            logger.debug("Curator rename summary build failed: {}", e)

        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        try:
            after_report = agent_created_report()
        except Exception:
            after_report = []
        try:
            report_path = _write_run_report(started_at=start, elapsed_seconds=elapsed, auto_counts=counts, auto_summary=auto_summary, before_report=before_report, before_names=before_names, after_report=after_report, llm_meta=llm_meta)
            rp = str(report_path) if report_path else None
        except Exception:
            rp = None

        state2 = load_state()
        state2["last_run_duration_seconds"] = round(elapsed, 2)
        state2["last_run_summary"] = final_summary
        if rp:
            state2["last_report_path"] = rp
        save_state(state2)

        if on_summary:
            try:
                on_summary(f"curator: {final_summary}")
            except Exception:
                pass

    if synchronous:
        _llm_pass()
    else:
        t = threading.Thread(target=_llm_pass, daemon=True, name="curator-review")
        t.start()

    return {
        "started_at": start.isoformat(),
        "auto_transitions": counts,
        "summary_so_far": auto_summary,
    }


def maybe_run_curator(
    *,
    idle_for_seconds: Optional[float] = None,
    on_summary: Optional[Callable[[str], None]] = None,
) -> Optional[Dict[str, Any]]:
    try:
        if not should_run_now():
            return None
        if idle_for_seconds is not None:
            min_idle_s = get_min_idle_hours() * 3600.0
            if idle_for_seconds < min_idle_s:
                return None
        return run_curator_review(on_summary=on_summary)
    except Exception as e:
        logger.debug("maybe_run_curator failed: {}", e)
        return None
