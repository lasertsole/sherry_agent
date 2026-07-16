import json
from typing import Any
from pathlib import Path
from loguru import logger
from datetime import datetime
from context_engine.curator.constants import CURATOR_LOGS_DIR
from context_engine.curator.helpers import _ensure_dir
from context_engine.curator.classify import (
    _classify_removed_skills,
    _parse_structured_summary,
    _extract_absorbed_into_declarations,
    _reconcile_classification,
)


def _build_rename_summary(
    *,
    before_names: set[str],
    after_report: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
    model_final: str,
) -> str:
    after_names = {r.get("name") for r in after_report if isinstance(r, dict)}
    removed = sorted(before_names - after_names)
    added = sorted(after_names - before_names)
    if not removed:
        return ""

    heuristic = _classify_removed_skills(removed=removed, added=added, after_names=after_names, tool_calls=tool_calls)
    model_block = _parse_structured_summary(model_final)
    destinations = set(after_names) | set(added)
    absorbed_declarations = _extract_absorbed_into_declarations(tool_calls)
    classification = _reconcile_classification(removed=removed, heuristic=heuristic, model_block=model_block, destinations=destinations, absorbed_declarations=absorbed_declarations)

    consolidated = classification["consolidated"]
    pruned = classification["pruned"]

    SHOW = 10
    lines: list[str] = [f"archived {len(consolidated) + len(pruned)} skill(s):"]
    shown = 0
    for entry in consolidated[:SHOW]:
        lines.append(f"  - {entry.get('name', '?')} -> {entry.get('into', '?')}")
        shown += 1
    for entry in pruned[:SHOW - shown]:
        name = entry.get("name", "?") if isinstance(entry, dict) else str(entry)
        lines.append(f"  - {name} — pruned (stale)")
        shown += 1
    total = len(consolidated) + len(pruned)
    if total > SHOW:
        lines.append(f"  ... and {total - SHOW} more")
    lines.append("full report: check curator status")
    if consolidated:
        umbrellas = sorted({e.get("into") for e in consolidated if e.get("into")})
        if umbrellas:
            lines.append(f"keep an umbrella stable: curator pin {umbrellas[0]}")
    return "\n".join(lines)


def _write_run_report(
    *,
    started_at: datetime,
    elapsed_seconds: float,
    auto_counts: dict[str, int],
    auto_summary: str,
    before_report: list[dict[str, Any]],
    before_names: set[str],
    after_report: list[dict[str, Any]],
    llm_meta: dict[str, Any],
) -> Path | None:
    _ensure_dir(CURATOR_LOGS_DIR)

    stamp = started_at.strftime("%Y%m%d-%H%M%S")
    run_dir = CURATOR_LOGS_DIR / stamp
    suffix = 1
    while run_dir.exists():
        suffix += 1
        run_dir = CURATOR_LOGS_DIR / f"{stamp}-{suffix}"
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
    except Exception as e:
        logger.debug("Curator run dir create failed: {}", e)
        return None

    after_by_name = {r.get("name"): r for r in after_report if isinstance(r, dict)}
    after_names = set(after_by_name.keys())
    removed = sorted(before_names - after_names)
    added = sorted(after_names - before_names)
    before_by_name = {r.get("name"): r for r in before_report if isinstance(r, dict)}

    transitions: list[dict[str, str]] = []
    for name in sorted(after_names & before_names):
        s_before = (before_by_name.get(name) or {}).get("state")
        s_after = (after_by_name.get(name) or {}).get("state")
        if s_before and s_after and s_before != s_after:
            transitions.append({"name": name, "from": s_before, "to": s_after})

    tc_counts: dict[str, int] = {}
    for tc in llm_meta.get("tool_calls", []) or []:
        name = tc.get("name", "unknown")
        tc_counts[name] = tc_counts.get(name, 0) + 1

    heuristic = _classify_removed_skills(removed=removed, added=added, after_names=after_names, tool_calls=llm_meta.get("tool_calls", []) or [])
    model_block = _parse_structured_summary(llm_meta.get("final", "") or "")
    destinations = set(after_names) | set(added or [])
    absorbed_declarations = _extract_absorbed_into_declarations(llm_meta.get("tool_calls", []) or [])
    classification = _reconcile_classification(removed=removed, heuristic=heuristic, model_block=model_block, destinations=destinations, absorbed_declarations=absorbed_declarations)
    consolidated = classification["consolidated"]
    pruned = classification["pruned"]

    payload = {
        "started_at": started_at.isoformat(),
        "duration_seconds": round(elapsed_seconds, 2),
        "model": llm_meta.get("model", ""),
        "provider": llm_meta.get("provider", ""),
        "auto_transitions": auto_counts,
        "counts": {
            "before": len(before_names), "after": len(after_names),
            "delta": len(after_names) - len(before_names),
            "archived_this_run": len(removed), "added_this_run": len(added),
            "consolidated_this_run": len(consolidated), "pruned_this_run": len(pruned),
            "state_transitions": len(transitions), "tool_calls_total": sum(tc_counts.values()),
        },
        "tool_call_counts": tc_counts,
        "archived": removed, "consolidated": consolidated, "pruned": pruned,
        "pruned_names": [p["name"] for p in pruned], "added": added,
        "state_transitions": transitions,
        "llm_final": llm_meta.get("final", ""), "llm_summary": llm_meta.get("summary", ""),
        "llm_error": llm_meta.get("error"), "tool_calls": llm_meta.get("tool_calls", []),
    }

    try:
        (run_dir / "run.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except Exception as e:
        logger.debug("Curator run.json write failed: {}", e)

    try:
        (run_dir / "REPORT.md").write_text(_render_report_markdown(payload), encoding="utf-8")
    except Exception as e:
        logger.debug("Curator REPORT.md write failed: {}", e)

    return run_dir


def _render_report_markdown(p: dict[str, Any]) -> str:
    lines: list[str] = []
    started = p.get("started_at", "")
    duration = p.get("duration_seconds", 0) or 0
    mins, secs = divmod(int(duration), 60)
    dur_label = f"{mins}m {secs}s" if mins else f"{secs}s"

    lines.append(f"# Curator run — {started}\n")
    model = p.get("model") or "(not resolved)"
    prov = p.get("provider") or "(not resolved)"
    counts = p.get("counts") or {}
    lines.append(f"Model: `{model}` via `{prov}`  ·  Duration: {dur_label}  ·  Skills: {counts.get('before', 0)} → {counts.get('after', 0)} ({counts.get('delta', 0):+d})\n")

    error = p.get("llm_error")
    if error:
        lines.append(f"> LLM pass error: `{error}`\n")

    auto = p.get("auto_transitions") or {}
    lines.append("## Auto-transitions (pure, no LLM)\n")
    lines.append(f"- checked: {auto.get('checked', 0)}")
    lines.append(f"- marked stale: {auto.get('marked_stale', 0)}")
    lines.append(f"- archived: {auto.get('archived', 0)}")
    lines.append(f"- reactivated: {auto.get('reactivated', 0)}")
    lines.append("")

    tc_counts = p.get("tool_call_counts") or {}
    lines.append("## LLM consolidation pass\n")
    lines.append(f"- tool calls: **{counts.get('tool_calls_total', 0)}** (by name: {', '.join(f'{k}={v}' for k, v in sorted(tc_counts.items())) or 'none'})")
    lines.append(f"- consolidated: **{counts.get('consolidated_this_run', 0)}**")
    lines.append(f"- pruned: **{counts.get('pruned_this_run', 0)}**")
    lines.append("")

    consolidated = p.get("consolidated") or []
    if consolidated:
        lines.append(f"### Consolidated ({len(consolidated)})\n")
        for entry in consolidated[:50]:
            name = entry.get("name", "?")
            into = entry.get("into", "?")
            reason = (entry.get("reason") or "").strip()
            line = f"- `{name}` → `{into}`"
            if reason:
                line += f" — {reason}"
            lines.append(line)
        lines.append("")

    pruned = p.get("pruned") or []
    if pruned:
        lines.append(f"### Pruned ({len(pruned)})\n")
        for entry in pruned[:50]:
            if isinstance(entry, dict):
                name = entry.get("name", "?")
                reason = (entry.get("reason") or "").strip()
                line = f"- `{name}`"
                if reason:
                    line += f" — {reason}"
                lines.append(line)
        lines.append("")

    final = (p.get("llm_final") or "").strip()
    if final:
        lines.append("## LLM final summary\n")
        lines.append(final)
        lines.append("")

    lines.append("## Recovery\n")
    lines.append("- Restore: `curator restore <name>`")
    lines.append("- Archives: `skills/.archive/`")
    lines.append("")

    return "\n".join(lines)
