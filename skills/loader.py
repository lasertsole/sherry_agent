"""Skills loader and snapshot builder."""

import json
import yaml
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as _xml_escape
from loguru import logger
from config import (
    ROOT_DIR,
    SKILLS_DIR,
    SKILLS_STATE_FILE,
    SKILL_DISCOVERY_ROOTS,
    is_allowed_skill_path,
)


def _xml_text(value: Any) -> str:
    """Escape a value for the ``<available_skills>`` prompt block.

    ``name`` and ``description`` come from SKILL.md frontmatter, which is
    untrusted for third-party (uploaded / clawhub) skills: without escaping, a
    description containing ``</description></skill>`` closes the tags and lets
    the skill inject arbitrary instructions into the system prompt.

    @param value Raw frontmatter value (usually a string).
    @returns The escaped text, safe to embed between XML tags.
    """
    return _xml_escape(str(value if value is not None else ""))


def parse_frontmatter(text: str) -> dict[str, Any]:
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    return yaml.safe_load(parts[1]) or {}


def _read_skills_state() -> dict[str, dict[str, bool]]:
    """Read the skills state file defensively.

    Returns a mapping of skill name -> {"active": bool}. Missing or malformed
    files degrade to an empty dict.
    """
    try:
        if not SKILLS_STATE_FILE.exists():
            return {}
        with open(SKILLS_STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        result: dict[str, dict[str, bool]] = {}
        for key, value in data.items():
            if isinstance(value, dict) and isinstance(value.get("active"), bool):
                result[str(key)] = {"active": value["active"]}
        return result
    except (OSError, json.JSONDecodeError):
        return {}


def _is_third_party(location: str) -> bool:
    """Return True if the skill location is under skills/plugins/."""
    return Path(location).parts[:2] == ("skills", "plugins")


def read_skills_snapshot() -> list[dict[str, str]] | None:
    """Read the cached skills snapshot written by build_skills_snapshot().

    Returns None when the snapshot file does not exist. Lives on the loader
    (its only consumer is scan_skills) so skills_snapshot can depend on the
    loader one-directionally instead of forming an import cycle.
    """
    file_path = SKILLS_DIR / "skills_snapshot.json"
    if not file_path.exists():
        return None
    with open(file_path, encoding="utf-8") as f:
        return json.loads(f.read())


# Scope visibility values for the ``scope:`` frontmatter field. The canonical
# implementation lives in agent/tools/pub_base/skill_utils.py
# (normalize_skill_scope / skill_visible_to). It is deliberately duplicated
# here as a tiny helper instead of imported: skill_utils imports skills.loader
# at module level (circular import), and importing anything from the
# agent.tools package would drag its heavyweight __init__ (full tool registry
# + langchain) into the lightweight `skills` package import chain.
_SKILL_SCOPES = ("all", "main_only", "subagent_only")


def _normalize_scope(raw: Any) -> str:
    """Normalize a ``scope:`` frontmatter value; invalid/absent -> "all"."""
    if raw is None:
        return "all"
    value = str(raw).strip().lower()
    return value if value in _SKILL_SCOPES else "all"


def _skill_visible_to(skill: dict[str, Any], caller_scope: str) -> bool:
    """Return True when *skill* is visible to a caller with *caller_scope*.

    Visibility contract: "main" sees skills whose scope != "subagent_only";
    "subagent" sees skills whose scope != "main_only". Unknown caller scopes
    degrade to "main"; unknown skill scopes default to "all" (both callers).

    Canonical implementation: agent/tools/pub_base/skill_utils.py
    (see the comment on _SKILL_SCOPES above for why this is duplicated).
    """
    scope = _normalize_scope(skill.get("scope") if isinstance(skill, dict) else None)
    caller = str(caller_scope or "").strip().lower()
    if caller not in ("main", "subagent"):
        caller = "main"
    if caller == "subagent":
        return scope != "main_only"
    return scope != "subagent_only"


def scan_skills(use_cache: bool = True) -> list[dict[str, Any]]:
    if use_cache:
        cached: list[dict[str, str]] | None = read_skills_snapshot()

        if cached:
            return cached

    state = _read_skills_state()

    skills: list[dict[str, Any]] = []
    seen_paths = set()  # for deduplication

    for skill_file in SKILLS_DIR.glob("**/SKILL.md"):
        if not is_allowed_skill_path(skill_file, SKILLS_DIR):
            logger.warning(
                "Ignoring SKILL.md outside allowed roots "
                f"({', '.join(SKILL_DISCOVERY_ROOTS)}): {skill_file}"
            )
            continue
        if skill_file in seen_paths:
            continue
        seen_paths.add(skill_file)

        content = skill_file.read_text(encoding="utf-8")
        meta = parse_frontmatter(content)
        name = str(meta.get("name", skill_file.parent.name))
        desc = str(meta.get("description", ""))
        scope = _normalize_scope(meta.get("scope"))
        rel = skill_file.relative_to(ROOT_DIR)
        location = f"./{rel.as_posix()}"

        if _is_third_party(location):
            # Uploaded third-party skills default to inactive unless the state
            # file explicitly marks them active.
            active = bool(state.get(name, {}).get("active", False))
        else:
            # Builtin / auto skills are always active.
            active = True

        skills.append(
            {
                "name": name,
                "description": desc,
                "location": location,
                "scope": scope,
                "active": active,
            }
        )

    skills.sort(key=lambda x: x["name"])
    return skills


#: Longest skill description served to the client (same bound the tool
#: catalogue uses: the UI shows it as a hover tooltip, and the full text stays
#: in the SKILL.md).
_DESCRIPTION_MAX_CHARS = 240

#: Skills no session config may drop: the multimedia handling chain. A user can
#: send an image / an audio clip / a video at any moment, and these are what turn
#: it into something the model can read (or, for ``text_to_image``, what lets it
#: answer with one) — 预设-技能 shows them as locked rows and
#: ``agent_config_service`` refuses a payload that omits one.
REQUIRED_SKILLS: frozenset[str] = frozenset(
    {
        "image_to_text",
        "speech_to_text",
        "video_text_to_text",
        "text_to_image",
    }
)


def skill_required(name: str) -> bool:
    """Whether *name* may never be left out of a session's skill index."""
    return name in REQUIRED_SKILLS


def skills_catalog(caller_scope: str = "main") -> list[dict[str, Any]]:
    """The skills the 技能 tab lists: ``[{name, description, builtin, required}, ...]``.

    Only what the index can actually contain: skills VISIBLE to *caller_scope*
    and currently active (an inactive uploaded skill is toggled in 菜单-技能, and
    listing it here would let a preset select something the index never shows).
    ``builtin`` is the 第三方 split — everything outside ``skills/plugins/`` —
    and ``required`` mirrors :data:`REQUIRED_SKILLS`.

    Required skills come FIRST: the UI renders this order, and the locked
    multimedia rows belong at the top where they read as the baseline.

    @param caller_scope The perspective the list is filtered for (main/subagent).
    @returns The catalogue, required-first, then by name.
    """
    catalog: list[dict[str, Any]] = []
    for skill in scan_skills():
        if not skill.get("active", True) or not _skill_visible_to(skill, caller_scope):
            continue
        name = str(skill.get("name", ""))
        catalog.append(
            {
                "name": name,
                "description": str(skill.get("description", ""))[:_DESCRIPTION_MAX_CHARS],
                "builtin": not _is_third_party(str(skill.get("location", ""))),
                "required": skill_required(name),
            }
        )
    catalog.sort(key=lambda entry: (not entry["required"], entry["name"]))
    return catalog


def get_skills_text(
    selected_skill_names: list[str] | None = None,
    caller_scope: str = "main",
    *,
    exact: bool = False,
) -> str:
    """
    Get the skills XML.
    :param selected_skill_names: list of selected skill names
    :param caller_scope: the caller's perspective ("main" or "subagent"). Skills
        scoped "main_only" are invisible to subagents; "subagent_only" skills are
        invisible to main (see the ``scope:`` frontmatter field; default "all"
        makes them visible to both).
    :param exact: when True the list is AUTHORITATIVE — an empty list selects no
        skill at all. The session's own selection (预设-技能 tab) is exact: it
        may legitimately hold every skill unchecked. When False (the historical
        contract) an empty/absent list means "every visible skill", which the
        delegate path relies on for its own empty fallback.
    :return: skills xml
    """
    skills: list[dict[str, Any]] = scan_skills()

    select_all = selected_skill_names is None or (len(selected_skill_names) == 0 and not exact)
    selected = None if select_all else set(selected_skill_names)
    final_skills: list[dict[str, Any]] = []
    for s in skills:
        if not _skill_visible_to(s, caller_scope):
            continue
        if selected is None or s["name"] in selected:
            final_skills.append(s)

    # Filter out inactive skills (uploaded third-party skills default to inactive).
    final_skills = [s for s in final_skills if s.get("active", True)]

    lines = ["<available_skills>"]
    for s in final_skills:
        lines.append("  <skill>")
        lines.append(f"    <name>{_xml_text(s['name'])}</name>")
        lines.append(f"    <description>{_xml_text(s['description'])}</description>")
        lines.append("  </skill>")
    lines.append("</available_skills>")
    return "\n".join(lines)
