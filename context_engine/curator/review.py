"""Curator LLM review pass and agent-created-skill candidate snapshot.

The orchestrator re-imports these names so the public API and the test-pinned
private surface (``orchestrator._report_cache`` reset, ``_generate_umbrella_skill``
patch points) stay on the orchestrator namespace.
"""

from typing import TYPE_CHECKING, Any

from context_engine.curator.usage import agent_created_report

if TYPE_CHECKING:
    from context_engine.curator.run_state import _ReviewRun


CURATOR_REVIEW_PROMPT = (
    "You are running as the background skill CURATOR. This is an "
    "UMBRELLA-BUILDING consolidation pass, not a passive audit.\n\n"
    "The goal is a LIBRARY OF CLASS-LEVEL INSTRUCTIONS. A collection of hundreds of "
    "narrow skills is a FAILURE — not a feature.\n\n"
    "Hard rules — do not violate:\n"
    "1. DO NOT touch bundled or built-in skills.\n"
    "2. DO NOT delete any skill. Archiving is the maximum destructive action.\n"
    "3. DO NOT touch pinned skills.\n"
    "4. DO NOT use usage counters as sole reason to skip consolidation.\n\n"
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


_report_cache: list | None = None


def reset_report_cache() -> None:
    """Drop the cached agent-created-skill report (called at run start)."""
    global _report_cache
    _report_cache = None


def _cached_agent_created_report(*, refresh: bool = False) -> list:
    global _report_cache
    if refresh or _report_cache is None:
        _report_cache = agent_created_report()
    return _report_cache


def _render_candidate_list() -> str:
    rows = _cached_agent_created_report()
    rows = [r for r in rows if not r.get("pinned")]
    if not rows:
        return "No agent-created skills to review."
    lines = [f"Agent-created skills ({len(rows)}):\n"]
    for r in rows:
        desc = r.get("description", "")
        desc_part = f"  desc={desc}" if desc else ""
        lines.append(
            f"- {r['name']}  state={r['state']}  "
            f"use={r.get('use_count', 0)}  "
            f"last_activity={r.get('last_activity_at') or 'never'}"
            f"{desc_part}"
        )
    return "\n".join(lines)


def _run_llm_review(prompt: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "final": "",
        "summary": "",
        "model": "",
        "provider": "",
        "tool_calls": [],
        "error": None,
    }
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from models import build_main_llm

        llm = build_main_llm(temperature=0.3)
        system_msg = "You are a skill librarian. Review and consolidate overlapping skills into umbrella skills."
        response = llm.invoke(
            [
                SystemMessage(content=system_msg),
                HumanMessage(content=prompt),
            ]
        )
        final = str(response.content).strip() if response and response.content else ""
        result["final"] = final
        result["summary"] = (final[:240] + "…") if len(final) > 240 else (final or "no change")
    except Exception as e:
        result["error"] = str(e)
        result["summary"] = f"error: {e}"
    return result


def _run_llm_consolidation_pass(run: "_ReviewRun") -> None:
    llm_meta: dict[str, Any] = {
        "final": "",
        "summary": "",
        "model": "",
        "provider": "",
        "tool_calls": [],
        "error": None,
    }
    try:
        candidate_list = _render_candidate_list()
        if "No agent-created skills" in candidate_list:
            run.final_summary = f"{run.prefix}{run.auto_summary}; llm: skipped (no candidates)"
            llm_meta["summary"] = "skipped (no candidates)"
        else:
            if run.dry_run:
                prompt = f"{CURATOR_DRY_RUN_BANNER}\n{CURATOR_REVIEW_PROMPT}\n{candidate_list}"
            else:
                prompt = f"{CURATOR_REVIEW_PROMPT}\n{candidate_list}"
            llm_meta = _run_llm_review(prompt)
            run.final_summary = (
                f"{run.prefix}{run.auto_summary}; llm: {llm_meta.get('summary', 'no change')}"
            )
    except Exception as e:
        run.final_summary = f"{run.prefix}{run.auto_summary}; llm: error ({e})"
        llm_meta = {
            "final": "",
            "summary": f"error ({e})",
            "model": "",
            "provider": "",
            "tool_calls": [],
            "error": str(e),
        }
    run.llm_meta = llm_meta
