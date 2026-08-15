from server.trigger.core import app
from loguru import logger
from skills.loader import scan_skills, parse_frontmatter
from config import SKILLS_DIR


# Canonical mapping between the on-disk top-level skill directories under
# `skills/` and the API category values exposed to clients.
#
#   disk dir    -> API category
#   ------------   ------------
#   builtin        builtin        (内置：项目内置技能)
#   auto           auto           (自动：智能体自行学习/生成的技能)
#   plugins        third_party    (第三方：用户/插件安装的技能)
#
# The API category string is the authoritative source consumed by the client;
# unknown directories degrade safely to third_party so they remain visible
# in the skill manager instead of disappearing.
_DISK_TO_CATEGORY = {
    "builtin": "builtin",
    "auto": "auto",
    "plugins": "third_party",
}


def _get_category(location: str) -> str:
    # location is like "./skills/<category>/<rest>/SKILL.md" — the category is
    # the second top-level path segment under the skills/ root.
    parts = location.strip("./").split("/")
    # parts[1] is the category dir (builtin/auto/plugins); parts[0] is "skills".
    if len(parts) < 2 or not parts[1]:
        return "third_party"
    return _DISK_TO_CATEGORY.get(parts[1], "third_party")


@app.get("/skills")
async def list_skills_handler(request):
    skills = scan_skills(use_cache=False)
    result = []
    for s in skills:
        result.append({
            "name": s["name"],
            "description": s["description"],
            "location": s["location"],
            "category": _get_category(s["location"]),
        })
    result.sort(key=lambda x: (x["category"], x["name"]))
    logger.debug(f"Listed skills: count={len(result)}")
    return {"skills": result}


@app.get("/skills/*skill_path")
async def read_skill_handler(request, path_params):
    from pathlib import Path
    from config import ROOT_DIR

    skill_path = path_params["skill_path"]
    full_path = ROOT_DIR / skill_path
    if not full_path.exists() or not full_path.is_file():
        logger.warning(f"Skill file not found: {skill_path}")
        return {"error": "Skill file not found"}, {}, 404

    content = full_path.read_text(encoding="utf-8")
    meta = parse_frontmatter(content)
    category = _get_category(f"./{skill_path}")

    logger.debug(f"Read skill: path={skill_path}, name={meta.get('name', '')}")
    return {
        "name": str(meta.get("name", full_path.parent.name)),
        "description": str(meta.get("description", "")),
        "content": content,
        "category": category,
        "location": f"./{skill_path}",
    }
