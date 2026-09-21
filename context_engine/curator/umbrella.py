"""Curator umbrella-skill generation: multifile parsing and the LLM pass.

Split out of ``orchestrator.py``; the orchestrator re-imports ``_generate_umbrella_skill``
so the test-pinned patch point stays on the orchestrator namespace.
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


def _fallback_umbrella(umbrella: str, reasons: list[str], source_content: str) -> str:
    """Deterministic umbrella SKILL.md used when LLM generation is unavailable."""
    return (
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
        "- The frontmatter of SKILL.md must have: name, description, created_by: curator.\n\n"
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
