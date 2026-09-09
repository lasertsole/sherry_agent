"""Skill catalog endpoints: list skills and read a skill's files."""

from server.trigger.core import app
from loguru import logger
from skills.loader import scan_skills, parse_frontmatter
from context_engine.curator.usage import is_pinned

from server.trigger.http.skills._shared import _build_skill_file_tree, _get_category


@app.get("/skills")
async def list_skills_handler(request):
    skills = scan_skills(use_cache=False)
    result = []
    for s in skills:
        result.append(
            {
                "name": s["name"],
                "description": s["description"],
                "location": s["location"],
                "category": _get_category(s["location"]),
                # Visibility scope from the SKILL.md frontmatter (default "all").
                "scope": s.get("scope", "all"),
                # Pin/fix state is surfaced so the client can render the correct
                # controls (fixed skills can't be deleted; pinned/fixed are shown).
                "pinned": is_pinned(s["name"]),
            }
        )
    result.sort(key=lambda x: (x["category"], x["name"]))
    logger.debug(f"Listed skills: count={len(result)}")
    return {"skills": result}


@app.get("/skills/*skill_path")
async def read_skill_handler(request, path_params):
    from config import ROOT_DIR

    skill_path = path_params["skill_path"]
    full_path = ROOT_DIR / skill_path
    if not full_path.exists() or not full_path.is_file():
        logger.warning(f"Skill file not found: {skill_path}")
        return {"error": "Skill file not found"}, {}, 404

    content = full_path.read_text(encoding="utf-8")
    meta = parse_frontmatter(content)
    category = _get_category(f"./{skill_path}")
    files = _build_skill_file_tree(full_path.parent)

    logger.debug(f"Read skill: path={skill_path}, name={meta.get('name', '')}, files={len(files)}")
    return {
        "name": str(meta.get("name", full_path.parent.name)),
        "description": str(meta.get("description", "")),
        "content": content,
        "category": category,
        "location": f"./{skill_path}",
        "files": files,
    }
