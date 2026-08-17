import asyncio
from pathlib import Path
from loguru import logger
from typing import Any, Callable
from datetime import datetime, timezone
from context_engine.curator.usage import agent_created_report
from context_engine.curator.state import load_state, save_state
from context_engine.curator.config import get_consolidate, get_min_idle_hours
from context_engine.curator.report import _build_rename_summary, _write_run_report
from context_engine.curator.transitions import should_run_now, apply_automatic_transitions


def _resolve_skill_dir(name: str) -> Path | None:
    """Resolve a skill directory by leaf name, recursing into category subdirs.

    Skills under ``skills/auto/`` live at depth 2
    (``skills/auto/<category>/<skill>/SKILL.md``), so a flat
    ``AUTO_SKILLS_DIR / name`` lookup misses nested skills.  Walk ``**/SKILL.md``
    and match by parent dir name (mirrors ``skill_manage._find_skill``).
    """
    from context_engine.curator.constants import AUTO_SKILLS_DIR

    candidate = AUTO_SKILLS_DIR / name
    if candidate.is_dir() and (candidate / "SKILL.md").exists():
        return candidate
    if not AUTO_SKILLS_DIR.exists():
        return None
    for skill_md in AUTO_SKILLS_DIR.glob("**/SKILL.md"):
        if skill_md.parent.name == name:
            return skill_md.parent
    return None


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


def _render_candidate_list() -> str:
    rows = agent_created_report()
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
        "final": "", "summary": "", "model": "", "provider": "",
        "tool_calls": [], "error": None,
    }
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from models import build_main_llm

        llm = build_main_llm(temperature=0.3)
        system_msg = "You are a skill librarian. Review and consolidate overlapping skills into umbrella skills."
        response = llm.invoke([
            SystemMessage(content=system_msg),
            HumanMessage(content=prompt),
        ])
        final = str(response.content).strip() if response and response.content else ""
        result["final"] = final
        result["summary"] = (final[:240] + "…") if len(final) > 240 else (final or "no change")
    except Exception as e:
        result["error"] = str(e)
        result["summary"] = f"error: {e}"
    return result


def run_curator_review(
    on_summary: Callable[[str], None] | None = None,
    dry_run: bool = False,
    consolidate: bool | None = None,
) -> dict[str, Any]:
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

    # --- synchronous LLM pass (blocks caller) ---
    try:
        before_report = agent_created_report()
    except Exception:
        before_report = []
    before_names = {r.get("name") for r in before_report if isinstance(r, dict)}

    if not consolidate:
        final_summary = f"{prefix}{auto_summary}; llm: skipped (consolidation off)"
        llm_meta: dict[str, Any] = {"final": "", "summary": "skipped (consolidation off)", "model": "", "provider": "", "tool_calls": [], "error": None}
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
        return {
            "started_at": start.isoformat(),
            "auto_transitions": counts,
            "summary_so_far": auto_summary,
        }

    llm_meta: dict[str, Any] = {"final": "", "summary": "", "model": "", "provider": "", "tool_calls": [], "error": None}
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

    if not dry_run:
        try:
            _apply_consolidation(llm_meta.get("final", ""))
        except Exception as e:
            logger.debug("Curator consolidation apply failed: {}", e)

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

    result: dict[str, Any] = {
        "started_at": start.isoformat(),
        "auto_transitions": counts,
        "summary_so_far": auto_summary,
    }
    # LLM 层失败（未配置/调用异常）时，携带错误标记，供 HTTP handler 区分为
    # success=False，避免前端误报「维护完成」。
    if llm_meta.get("error"):
        result["error"] = str(llm_meta["error"])
        result["summary_so_far"] = f"{auto_summary}; llm: {llm_meta.get('summary') or 'error'}"
    return result


def maybe_run_curator(
    *,
    idle_for_seconds: float | None = None,
    on_summary: Callable[[str], None] | None = None,
) -> dict[str, Any] | None:
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


_UMBRELLA_FILE_DELIM = "<<<"  # file-path block delimiter; a line matching '<<<PATH>>>'
_UMBRELLA_ALLOWED_SUBDIRS = ("references", "templates", "scripts", "assets", "examples", "resources")


def _parse_multifile_umbrella(text: str) -> tuple[str, dict[str, str]]:
    """Split an LLM response into (SKILL.md content, {subdir/path: content}).

    The LLM is instructed to emit one or more blocks, each starting on its own
    line with a ``<<<PATH>>>`` header (e.g. ``<<<SKILL.md>>>``,
    ``<<<references/api.md>>>``, ``<<<examples/demo.py>>>``).  The header line is
    removed and the rest of the block is its content.

    Parsing is intentionally tolerant:
    - If no valid block headers are found, the whole text is treated as SKILL.md
      (degenerates to the historical single-string behavior).
    - A block may be ``SKILL.md`` (canonical main file) or a path under one of the
      allowed umbrella subdirectories.  Any other path (or an empty body) is
      dropped with a warning.
    - A leading/trailing ``` code fence around the whole response is stripped.
    """
    import re

    text = text.strip()
    text = text.removeprefix("```markdown").removeprefix("```md").removeprefix("```")
    text = text.removesuffix("```").strip()

    header_re = re.compile(rf"^{re.escape(_UMBRELLA_FILE_DELIM)}\s*([^\s]+?)\s*>+$", re.MULTILINE)
    matches = list(header_re.finditer(text))
    if not matches:
        return text, {}

    main: str = ""
    files: dict[str, str] = {}
    # Content before the first header shouldn't exist; ignore if it does.
    for i, m in enumerate(matches):
        start = m.end() + 1  # skip the newline after the header
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip("\n")
        path = m.group(1).strip().lstrip("/")
        if path == "SKILL.md":
            main = body.strip()
        elif path and path.split("/", 1)[0] in _UMBRELLA_ALLOWED_SUBDIRS and body.strip():
            files[path] = body.strip()
        else:
            logger.debug("Curator: ignoring umbrella block with invalid path '{}'", path)
    if not main:
        main = text.strip()
    return main, files


def _generate_umbrella_skill(umbrella: str, reasons: list[str], source_content: str, file_inventory: str = "") -> tuple[str, dict[str, str]]:
    """Generate an umbrella SKILL.md plus optional supporting files.

    Returns ``(main_content, supporting_files)`` where ``supporting_files`` maps
    subdirectory paths (e.g. ``references/api.md``) to file content.  If the LLM
    output cannot be split into blocks, ``supporting_files`` is empty and the
    whole response is used as the main content (historical behavior).
    """
    from agent.tools.skill_tools.skill_manage import _UMBRELLA_SKILL_CHAR_TARGET, split_oversized_skill
    from langchain_core.messages import HumanMessage, SystemMessage
    from models import build_main_llm

    llm = build_main_llm(temperature=0.3)
    allowed_subs = ", ".join(_UMBRELLA_ALLOWED_SUBDIRS)
    system_msg = (
        "You are a skill librarian. You are creating a consolidated umbrella skill by merging "
        "several related narrow skills into one comprehensive, well-organized skill.\n\n"
        "OUTPUT FORMAT (strict):\n"
        f"- Output one or more file blocks. Each block must START on its own line with a "
        f"'<<<PATH>>>' header (the '<<<' and '>>>' delimiters literally).\n"
        f"- The FIRST block MUST be '<<<SKILL.md>>>' — the main skill document "
        "(YAML frontmatter + markdown body).\n"
        f"- You MAY add more blocks, each under an allowed supporting subdirectory: "
        f"{allowed_subs}. Example: '<<<references/api.md>>>', '<<<examples/demo.py>>>', "
        f"'<<<scripts/helper.sh>>>', '<<<resources/data.json>>>'.\n"
        "- Do NOT wrap blocks in code fences.\n"
        "- The frontmatter of SKILL.md must have: name, description, created_by: curator.\n\n"
        "LENGTH BUDGET (important):\n"
        f"- Keep SKILL.md itself concise and under ~{_UMBRELLA_SKILL_CHAR_TARGET:,} characters. "
        "It is loaded into the agent's prompt every time the skill is used, so bloating it "
        "wastes tokens.\n"
        "- When the merged content would exceed that budget, OFFLOAD bulky material into "
        "supporting subdirectory blocks instead of inflating SKILL.md: long reference/API "
        "docs go to references/, worked runnable examples to examples/, helper logic to "
        "scripts/, templates/data/tool configs to templates/ or resources/.\n"
        "- SKILL.md should reference each supporting file with a relative link "
        "(e.g. [api.md](references/api.md)) and a one-line description of when to read it.\n\n"
        "SYNTHESIS RULES:\n"
        "- Synthesize and deduplicate: merge overlapping instructions, unify code patterns, "
        "remove redundancy, keep every unique technique.\n"
        "- Organize SKILL.md with clear sections. Use ## headings for each concern area.\n"
        "- Preserve all useful code examples, but consolidate similar ones (dedupe or move "
        "to examples/ rather than repeating inline).\n"
        "- Include a '## When to use' section at the top.\n"
        "- Reference the supporting files that were migrated from the original skills "
        f"(under references/, templates/, scripts/) with relative links.\n"
        "- Keep the result concise but complete — do NOT lose any substantive content."
    )
    user_msg = (
        f"Create an umbrella skill named '{umbrella}'.\n\n"
        f"Merge reasons:\n" + "\n".join(reasons) + "\n\n"
        f"Source skills to merge:\n\n{source_content}"
    )
    if file_inventory:
        user_msg += f"\n\nMigrated supporting files (already moved into the umbrella skill directory):\n\n{file_inventory}"
    try:
        response = llm.invoke([
            SystemMessage(content=system_msg),
            HumanMessage(content=user_msg),
        ])
        text = str(response.content).strip() if response and response.content else ""
        main_content, supporting_files = _parse_multifile_umbrella(text)
        if main_content:
            main_content, supporting_files = split_oversized_skill(main_content, _UMBRELLA_SKILL_CHAR_TARGET, supporting_files)
            return main_content, supporting_files
    except Exception as e:
        logger.warning("Curator LLM umbrella generation failed: {}", e)
    fallback = (
        "---\n"
        f"name: {umbrella}\n"
        f"description: Umbrella skill consolidating {len(reasons)} related skills.\n"
        "created_by: curator\n"
        "---\n\n"
        f"# {umbrella}\n\n"
        "Consolidated from the following skills:\n\n"
        + "\n".join(reasons)
        + "\n\n"
        + source_content
        + "\n"
    )
    return fallback, {}


def _refresh_all_cached_system_prompts() -> None:
    """Rebuild and overwrite the cached system_prompt for every known session.

    After Curator consolidates/prunes skills the skill snapshot baked into
    each session's cached system_prompt is stale.  This forces a fresh
    build_system_prompt(session_id=sid) and writes it into both mem and db stores so the
    next turn picks up the new skill list immediately.
    """
    try:
        from runtime import state_register_mem, state_register_db
        from workspace.prompt_builder import build_system_prompt

        session_ids = state_register_db.get_all_session_ids()
        if not session_ids:
            logger.info("Curator: no sessions to refresh system_prompt for")
            return

        for sid in session_ids:
            new_prompt = build_system_prompt(session_id=sid)
            state_register_mem.set_state(sid, "system_prompt", new_prompt)
            state_register_db.set_state(sid, "system_prompt", new_prompt)

        logger.info("Curator: refreshed cached system_prompt for {} session(s)", len(session_ids))
    except Exception:
        logger.exception("Curator: failed to refresh cached system_prompts")


def _apply_consolidation(llm_final: str) -> None:
    from context_engine.curator.classify import _parse_structured_summary
    from context_engine.curator.usage import delete_skill, seed_record_if_missing
    from agent.tools.skill_tools.skill_manage import _create_skill, _write_file

    parsed = _parse_structured_summary(llm_final)
    consolidations = parsed.get("consolidations", [])
    prunings = parsed.get("prunings", [])
    if not consolidations and not prunings:
        return

    umbrella_names = set()
    for entry in consolidations:
        into = entry.get("into", "").strip()
        if into:
            umbrella_names.add(into)

    for umbrella in sorted(umbrella_names):
        skill_dir = _resolve_skill_dir(umbrella)
        if skill_dir is not None:
            continue
        merged_skills = [e for e in consolidations if e.get("into", "").strip() == umbrella]

        source_blocks: list[str] = []
        for entry in merged_skills:
            src_name = entry.get("from", "").strip()
            src_dir = _resolve_skill_dir(src_name)
            src_md = src_dir / "SKILL.md" if src_dir is not None else None
            if src_md and src_md.exists():
                src_text = src_md.read_text(encoding="utf-8")
                source_blocks.append(f"### {src_name}\n\n{src_text}")
            else:
                source_blocks.append(f"### {src_name}\n\n{entry.get('reason', '')}")

        reasons = [f"- {e.get('from', '?')}: {e.get('reason', '')}" for e in merged_skills]
        merged_content = "\n\n".join(source_blocks)

        file_inventory_lines: list[str] = []
        for entry in merged_skills:
            src_name = entry.get("from", "").strip()
            src_dir = _resolve_skill_dir(src_name)
            if src_dir is None:
                continue
            for subdir in ("references", "templates", "scripts", "assets"):
                src_sub = src_dir / subdir
                if not src_sub.is_dir():
                    continue
                for f in src_sub.iterdir():
                    if f.is_file():
                        file_inventory_lines.append(f"- {subdir}/{f.name} (from {src_name})")

        file_inventory = "\n".join(file_inventory_lines)
        umbrella_content, supporting_files = _generate_umbrella_skill(umbrella, reasons, merged_content, file_inventory)

        if umbrella_content.startswith("---"):
            umbrella_content = umbrella_content + "\n"
        result = _create_skill(umbrella, umbrella_content)
        if result.get("success"):
            logger.info("Curator created umbrella skill: {}", umbrella)
        else:
            logger.warning("Curator failed to create umbrella '{}': {}", umbrella, result.get("error"))
            continue

        seed_record_if_missing(umbrella)

        # Write supporting files the LLM split out of the main SKILL.md, then
        # migrate any source subdirectory files (skip ones already written).
        written = {p for p in supporting_files}
        for file_path, file_content in supporting_files.items():
            wr = _write_file(umbrella, file_path, file_content)
            if wr.get("success"):
                logger.debug("Curator wrote umbrella support file {}/{}", umbrella, file_path)
            else:
                logger.warning("Curator failed to write {}/{}: {}", umbrella, file_path, wr.get("error"))

        for entry in merged_skills:
            src_name = entry.get("from", "").strip()
            src_dir = _resolve_skill_dir(src_name)
            if src_dir is None:
                continue
            for subdir in ("references", "templates", "scripts", "assets", "examples", "resources"):
                src_sub = src_dir / subdir
                if not src_sub.is_dir():
                    continue
                for f in src_sub.iterdir():
                    if not f.is_file():
                        continue
                    file_path = f"{subdir}/{f.name}"
                    if file_path in written:
                        logger.debug("Curator: skip migrating {}/{} (umbrella support file already written)", src_name, file_path)
                        continue
                    file_content = f.read_text(encoding="utf-8")
                    wr = _write_file(umbrella, file_path, file_content)
                    if wr.get("success"):
                        logger.debug("Curator migrated {}/{} -> {}/{}", src_name, f.name, umbrella, f.name)
                    else:
                        logger.warning("Curator failed to migrate {}/{}: {}", src_name, f.name, wr.get("error"))

    for entry in consolidations:
        name = entry.get("from", "").strip()
        into = entry.get("into", "").strip()
        if not name or not into:
            continue
        ok, msg = delete_skill(name, absorbed_into=into)
        if ok:
            logger.info("Curator deleted '{}': {}", name, msg)
        else:
            logger.warning("Curator failed to delete '{}': {}", name, msg)

    for entry in prunings:
        name = entry.get("name", "").strip()
        if not name:
            continue
        in_consolidation = any(e.get("from", "").strip() == name for e in consolidations)
        if in_consolidation:
            continue
        ok, msg = delete_skill(name)
        if ok:
            logger.info("Curator pruned '{}': {}", name, msg)
        else:
            logger.warning("Curator failed to prune '{}': {}", name, msg)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None and loop.is_running():
        asyncio.ensure_future(asyncio.to_thread(_refresh_all_cached_system_prompts))
    else:
        asyncio.run(asyncio.to_thread(_refresh_all_cached_system_prompts))
