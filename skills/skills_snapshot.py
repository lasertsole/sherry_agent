import os
import json
from config import SKILLS_DIR
from loguru import logger

_BUILTIN_SCAN_TARGETS = (
    ("clawhub", "builtin/core/clawhub"),
)


def _scan_builtin_skills() -> None:
    """Run the security scanner over the built-in curated skills.

    Non-fatal by design: a scanner that is unavailable, errors, or reports a
    suspicious verdict only logs a warning — it must never crash startup or
    break the snapshot build.     Verdicts are surfaced to the user via the log
    file, which the client surfaces in the UI.
    """
    try:
        from server.service.skill_scanner import scan_skill
    except ImportError:
        # `server.service`'s package __init__ recurses into `agent`. When
        # `agent` is imported first (e.g. an integration test importing a
        # middleware before the HTTP server), that recursion fails mid-init.
        # Failing open here keeps startup safe; the scan still runs in the
        # normal server-first path where `server.service` is already imported.
        logger.debug(
            "Built-in skill scan skipped: server.service not yet importable "
            "(agent import recursion)."
        )
        return

    for name, rel_path in _BUILTIN_SCAN_TARGETS:
        skill_md = SKILLS_DIR / f"{rel_path}/SKILL.md"
        if not skill_md.exists():
            continue
        try:
            result = scan_skill(skill_md)
        except Exception as exc:  # noqa: BLE001 - scanner must fail open
            logger.warning(f"Built-in skill scan failed for '{name}': {exc}")
            continue
        if result.is_unavailable:
            continue
        if result.is_do_not_install:
            logger.warning(
                f"Built-in skill '{name}' flagged DO_NOT_INSTALL by security "
                f"scanner (risk_score={result.risk_score}). It remains installed "
                f"but should be audited."
            )
        elif result.is_caution:
            logger.warning(
                f"Built-in skill '{name}' flagged CAUTION by security scanner "
                f"(risk_score={result.risk_score}). Review findings before use."
            )


def build_skills_snapshot() -> None:
    from .loader import scan_skills

    # Scan curated built-in skills (mirrors the third-party upload gate). This
    # is intentionally fire-and-forget; failures degrade to logging only.
    _scan_builtin_skills()

    skills: list[dict[str, str]] = scan_skills(use_cache=False)
    skills_json: str = json.dumps(skills, ensure_ascii=False, indent=4)
    with open(os.path.join(SKILLS_DIR, 'skills_snapshot.json'), 'w', encoding='utf-8') as f:
        f.write(skills_json)

def read_skills_snapshot() -> list[dict[str, str]] | None:
    file_path:str = os.path.join(SKILLS_DIR, 'skills_snapshot.json')

    if os.path.exists(file_path):
        with open(os.path.join(SKILLS_DIR, file_path), 'r', encoding='utf-8') as f:
            skills_json:str = f.read()
            skills: list[dict[str, str]] = json.loads(skills_json)
            return skills

    return None