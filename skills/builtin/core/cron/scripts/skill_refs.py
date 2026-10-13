"""Cron-job skill-reference tracking and maintenance.

A cron job may bind skills (``payload.skills``) whose contents are pre-loaded
into its prompt on every run. Two consequences need machinery outside the cron
engine itself:

- **Renames must follow through.** When a skill is consolidated into an
  umbrella or pruned by the curator (or deleted through ``skill_manage``), the
  jobs that bound it would keep loading a name that no longer resolves. Both
  entry points call :func:`rewrite_skill_refs` so a job's binding list follows
  the move (``X -> Y``) or drops the dead name.
- **A bound skill is in use.** :func:`referenced_skill_names` lets the curator's
  inactivity transitions refuse to archive a skill some job still references —
  archiving it would silently break that job.

Both helpers read/write ``cron_jobs.json`` directly rather than through
``CronService``: importing the service would drag in the agent/channel stack at
curator time, and the service reloads the store on mtime change anyway, so an
out-of-band rewrite is picked up by the next access.

Surgical by design: only the ``payload.skills`` keys are touched — every other
field (schedule, state, unknown future keys) is preserved byte-for-byte and the
file keeps its 2-space, non-ASCII-escaped JSON formatting.
"""

import json
from pathlib import Path

from loguru import logger

from config import ROOT_DIR

#: Same store the cron service owns (``skills/builtin/core/cron/scripts/base.py``).
CRON_STORE_PATH: Path = ROOT_DIR / "cron_jobs.json"


def _read_store() -> dict | None:
    """Read and parse the cron store; ``None`` when missing or unreadable."""
    try:
        if not CRON_STORE_PATH.exists():
            return None
        data = json.loads(CRON_STORE_PATH.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 - maintenance must never raise
        logger.warning("skill_refs: failed to read cron store: {}", e)
        return None
    return data if isinstance(data, dict) else None


def referenced_skill_names() -> set[str]:
    """All skill names bound by any cron job (enabled or disabled).

    Returns an empty set when the store is missing, unreadable or holds no
    bindings — callers treat that as "nothing to protect".
    """
    data = _read_store()
    if data is None:
        return set()
    names: set[str] = set()
    for job in data.get("jobs", []):
        if not isinstance(job, dict):
            continue
        skills = (job.get("payload") or {}).get("skills")
        if isinstance(skills, list):
            names.update(s for s in skills if isinstance(s, str) and s.strip())
    return names


def rewrite_skill_refs(consolidated: dict[str, str], pruned: set[str]) -> int:
    """Rewrite cron job skill references after consolidation / pruning.

    Args:
        consolidated: ``old_skill_name -> umbrella_name`` map; every occurrence
            of an old name is replaced by its umbrella (duplicates collapse,
            first-occurrence order preserved).
        pruned: skill names archived with no forwarding target; they are
            dropped from every binding list.

    Returns the number of jobs whose ``payload.skills`` changed. A job whose
    list becomes empty gets ``skills: null`` (back to skill-free), never
    ``[]`` — the loader treats both as "no skills" but null is the canonical
    serialization. Best-effort: failures are logged, never raised, and the file
    is only rewritten when something actually changed.
    """
    if not consolidated and not pruned:
        return 0
    data = _read_store()
    if data is None:
        return 0

    changed = 0
    for job in data.get("jobs", []):
        if not isinstance(job, dict):
            continue
        payload = job.get("payload")
        if not isinstance(payload, dict):
            continue
        skills = payload.get("skills")
        if not isinstance(skills, list) or not skills:
            continue

        new_skills: list[str] = []
        seen: set[str] = set()
        for entry in skills:
            if not isinstance(entry, str):
                continue
            target = consolidated.get(entry, entry)
            if target in pruned:
                continue
            if target not in seen:
                new_skills.append(target)
                seen.add(target)

        if new_skills == skills:
            continue
        payload["skills"] = new_skills or None
        changed += 1

    if not changed:
        return 0

    try:
        CRON_STORE_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:  # noqa: BLE001 - maintenance must never raise
        logger.warning("skill_refs: failed to rewrite cron store: {}", e)
        return 0
    logger.info(
        "skill_refs: rewrote skill references in {} cron job(s) (consolidated={}, pruned={})",
        changed,
        sorted(consolidated),
        sorted(pruned),
    )
    return changed
