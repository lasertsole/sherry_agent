"""Curator umbrella-skill generation: multifile parsing and the LLM pass.

The orchestrator re-imports ``_generate_umbrella_skill`` so the test-pinned patch
point stays on the orchestrator namespace.
"""

from loguru import logger

from context_engine.curator.refresh import _resolve_skill_writer


_UMBRELLA_FILE_DELIM = "<<<"  # file-path block delimiter; a line matching '<<<PATH>>>'
_UMBRELLA_ALLOWED_SUBDIRS = (
    "references",
    "templates",
    "scripts",
    "assets",
    "examples",
    "resources",
)


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


#: Token budgets for the frontmatter conditions the fallback writes (the same
#: currency as ``skills.loader``): CJK costs ~2 chars/token, ASCII ~4.
_WHEN_TO_USE_MAX_TOKENS = 50
_WHEN_NOT_TO_USE_MAX_TOKENS = 50


def _bounded(text: str, max_tokens: int) -> str:
    """Trim one condition line to its token budget (CJK-aware)."""
    from pub.func.message.token_truncation import truncate_by_tokens

    return truncate_by_tokens(
        text.strip(),
        max_tokens,
        head_ratio=0.6,
        tail_ratio=0.3,
        omission_template="...[truncated {omitted} chars]...",
    )


def _extract_use_cases_from_reasons(reasons: list[str]) -> str:
    """Heuristic ``when_to_use`` for the fallback: the merge reasons' triggers.

    Looks for the wording merge reasons actually use ("when …", "use …",
    "for …", "trigger") and keeps up to three distinct fragments; with nothing
    to extract it states the umbrella's own scope so the field is never empty
    (``when_to_use`` is REQUIRED on every skill).
    """
    triggers: list[str] = []
    for reason in reasons:
        lowered = reason.lower()
        for marker in ("when ", "use ", "for ", "trigger"):
            index = lowered.find(marker)
            if index == -1:
                continue
            fragment = reason[index : index + 150].split(".")[0].strip()
            if fragment and fragment not in triggers:
                triggers.append(fragment)
            break
    if not triggers:
        head = reasons[0].split(":")[0].strip() if reasons else ""
        return _bounded(
            f"Use when the task involves {head.lower()}"
            if head
            else "See the skill body for usage conditions.",
            _WHEN_TO_USE_MAX_TOKENS,
        )
    return _bounded("; ".join(triggers[:3]), _WHEN_TO_USE_MAX_TOKENS)


def _extract_exclusions_from_reasons(reasons: list[str]) -> str:
    """Heuristic ``when_not_to_use`` for the fallback (``""`` when none found).

    Optional by design — an empty result means the frontmatter field is left
    out entirely rather than filled with a placeholder.
    """
    exclusions: list[str] = []
    for reason in reasons:
        lowered = reason.lower()
        for marker in ("not ", "avoid ", "alternative", "except "):
            index = lowered.find(marker)
            if index == -1:
                continue
            fragment = reason[index : index + 150].split(".")[0].strip()
            if fragment and fragment not in exclusions:
                exclusions.append(fragment)
            break
    if not exclusions:
        return ""
    return _bounded("; ".join(exclusions[:3]), _WHEN_NOT_TO_USE_MAX_TOKENS)


def _fallback_umbrella(umbrella: str, reasons: list[str], source_content: str) -> str:
    """Deterministic umbrella SKILL.md used when LLM generation is unavailable.

    Two levels of conditions, mirroring what the LLM path is asked for: the
    FRONTMATTER covers the whole umbrella (``when_to_use`` required,
    ``when_not_to_use`` only when an exclusion was found), and every
    sub-capability section opens with its own ``**When to use:**`` line so the
    model can pick the right section after ``skill_view``.
    """
    use_cases = _extract_use_cases_from_reasons(reasons)
    not_use_cases = _extract_exclusions_from_reasons(reasons)

    frontmatter = [
        "---",
        f"name: {umbrella}",
        f"description: Umbrella skill consolidating {len(reasons)} related skills.",
        f"when_to_use: {use_cases}",
    ]
    if not_use_cases:
        frontmatter.append(f"when_not_to_use: {not_use_cases}")
    frontmatter.append("created_by: curator")

    sections: list[str] = []
    for reason in reasons:
        name, _, detail = reason.partition(":")
        sub_when = _extract_use_cases_from_reasons([reason])
        sub_not_when = _extract_exclusions_from_reasons([reason])
        section = f"## {name.strip() or reason[:50]}\n\n**When to use:** {sub_when}\n"
        if sub_not_when:
            section += f"\n**When NOT to use:** {sub_not_when}\n"
        section += f"\n{detail.strip() or reason}\n"
        sections.append(section)

    body = f"# {umbrella}\n\n## When to use\n\n{use_cases}\n\n"
    if not_use_cases:
        body += f"## When NOT to use\n\n{not_use_cases}\n\n"
    body += "\n".join(sections)
    return (
        "\n".join(frontmatter)
        + "\n---\n\n"
        + body
        + "\n\n---\n\nConsolidated source content:\n\n"
        + source_content
        + "\n"
    )


def _generate_umbrella_skill(
    umbrella: str, reasons: list[str], source_content: str, file_inventory: str = ""
) -> tuple[str, dict[str, str]]:
    """Generate an umbrella SKILL.md plus optional supporting files.

    Returns ``(main_content, supporting_files)`` where ``supporting_files`` maps
    subdirectory paths (e.g. ``references/api.md``) to file content.  If the LLM
    output cannot be split into blocks, ``supporting_files`` is empty and the
    whole response is used as the main content (historical behavior).
    """
    writer = _resolve_skill_writer("split_oversized_skill")
    if writer is None:
        return _fallback_umbrella(umbrella, reasons, source_content), {}
    char_target = writer.umbrella_skill_char_target()
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
        "- The frontmatter of SKILL.md must have: name, description, when_to_use, created_by: curator.\n"
        "  - description: one-line capability summary.\n"
        "  - when_to_use: REQUIRED — the specific scenarios in which the agent should "
        "load this whole skill.\n"
        "  - when_not_to_use: optional — exclusions or alternatives; omit it when there "
        "are none.\n\n"
        "LENGTH BUDGET (important):\n"
        f"- Keep SKILL.md itself concise and under ~{char_target:,} characters. "
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
        "- Include a '## When to use' section at the top (elaborates the frontmatter "
        "when_to_use).\n"
        "- EVERY '##' sub-capability section MUST open with:\n"
        "    **When to use:** <the scenario within the umbrella's scope that should "
        "select THIS section — narrower than the frontmatter's when_to_use, never a "
        "repeat of it>\n"
        "  and MAY add:\n"
        "    **When NOT to use:** <exclusions specific to this section, when any>"
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
        response = llm.invoke(
            [
                SystemMessage(content=system_msg),
                HumanMessage(content=user_msg),
            ]
        )
        text = str(response.content).strip() if response and response.content else ""
        main_content, supporting_files = _parse_multifile_umbrella(text)
        if main_content:
            main_content, supporting_files = writer.split_oversized_skill(
                main_content, char_target, supporting_files
            )
            return main_content, supporting_files
    except Exception as e:
        logger.warning("Curator LLM umbrella generation failed: {}", e)
    return _fallback_umbrella(umbrella, reasons, source_content), {}
