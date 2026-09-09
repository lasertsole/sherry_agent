"""Skill upload / toggle endpoints."""

import json
import tempfile
from pathlib import Path

from robyn import Response
from server.trigger.core import app
from loguru import logger
from skills.loader import parse_frontmatter
from config import PLUGIN_SKILLS_DIR
from server.service.skill_scanner import (
    ScanResult,
    build_caution_warnings,
    build_reject_message,
    scan_skill,
)
from agent.tools.skill_tools.skill_manage import _MAX_SKILL_CONTENT_CHARS

from server.trigger.http.skills._shared import (
    _read_skills_state,
    _rebuild_snapshot,
    _validate_skill_name,
    _write_skills_state,
)


@app.post("/skills/upload")
async def upload_skill_handler(request):
    """Upload a third-party SKILL.md via multipart/form-data.

    Accepts a file field named "file" containing SKILL.md bytes. The skill name
    is taken from the multipart field "name" if present, else parsed from the
    frontmatter `name:` of the SKILL.md, else the uploaded filename stem.
    Uploaded skills default to inactive in the state file.
    """
    files = getattr(request, "files", None) or {}
    form_data = getattr(request, "form_data", None) or {}

    # Robyn (0.84) keys `request.files` by the multipart *filename* (e.g.
    # "SKILL.md"), NOT by the form field name ("file"). So instead of a literal
    # `files.get("file")` lookup, take the first file part regardless of its key.
    file_bytes = next(iter(files.values()), None) if files else None
    if not file_bytes:
        logger.warning("Skill upload rejected: missing file part in multipart body")
        return Response(
            status_code=400,
            headers={"Content-Type": "application/json"},
            description='{"success": false, "message": "Missing file part in multipart body"}',
        )

    if isinstance(file_bytes, str):
        content = file_bytes
    else:
        content = file_bytes.decode("utf-8", errors="replace")

    # Validate content size.
    if len(content) > _MAX_SKILL_CONTENT_CHARS:
        logger.warning(f"Skill upload rejected: content too large ({len(content)} chars)")
        return Response(
            status_code=400,
            headers={"Content-Type": "application/json"},
            description=(
                '{"success": false, "message": "'
                f"Skill content is {len(content):,} characters (limit: {_MAX_SKILL_CONTENT_CHARS:,})"
                '"}'
            ),
        )

    # Determine the skill name: form field "name" > frontmatter name > filename stem.
    name: str | None = None
    form_name = form_data.get("name")
    if form_name and isinstance(form_name, str):
        name = form_name.strip()

    if not name:
        meta = parse_frontmatter(content)
        fm_name = meta.get("name")
        if fm_name:
            name = str(fm_name).strip()

    if not name:
        # Fall back to the uploaded filename stem. Robyn 0.84 keys `request.files`
        # by the multipart filename (not by field name), so derive it from there.
        filename = next(iter(files.keys()), "") if files else ""
        if filename:
            name = Path(filename).stem.strip()

    if not name:
        logger.warning("Skill upload rejected: could not determine skill name")
        return Response(
            status_code=400,
            headers={"Content-Type": "application/json"},
            description='{"success": false, "message": "Could not determine skill name"}',
        )

    err = _validate_skill_name(name)
    if err:
        logger.warning(f"Skill upload rejected: invalid name '{name}': {err}")
        return Response(
            status_code=400,
            headers={"Content-Type": "application/json"},
            description=json.dumps({"success": False, "message": err}),
        )

    # Guard against path traversal: the validated name only contains [a-z0-9._-],
    # but double-check the resolved path stays within PLUGIN_SKILLS_DIR.
    skill_dir = PLUGIN_SKILLS_DIR / name
    try:
        resolved = skill_dir.resolve()
        resolved.relative_to(PLUGIN_SKILLS_DIR.resolve())
    except (ValueError, OSError):
        logger.warning(f"Skill upload rejected: path traversal detected for '{name}'")
        return Response(
            status_code=400,
            headers={"Content-Type": "application/json"},
            description='{"success": false, "message": "Invalid skill path"}',
        )

    # Run the security scanner BEFORE writing anything to disk. The skill
    # directory does not exist yet, so the uploaded content is staged in a
    # temporary directory and scanned in place — nothing reaches PLUGIN_SKILLS_DIR
    # unless the scan passes.
    with tempfile.TemporaryDirectory(prefix="skillscan_") as _tmp:
        tmp_dir = Path(_tmp)
        tmp_skill_dir = tmp_dir / name
        tmp_skill_dir.mkdir(parents=True, exist_ok=True)
        (tmp_skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
        scan_result: ScanResult = scan_skill(tmp_skill_dir)
        _err = build_reject_message(scan_result)
        if _err:
            logger.warning(
                f"Skill upload rejected by security scanner '{name}': {scan_result.risk_recommendation} "
                f"(score={scan_result.risk_score})"
            )
            return Response(
                status_code=400,
                headers={"Content-Type": "application/json"},
                description=json.dumps({"success": False, "message": _err}),
            )

    # Write SKILL.md.
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(content, encoding="utf-8")

    # Set active state to False in the state file (create if missing).
    state = _read_skills_state()
    state[name] = {"active": False}
    _write_skills_state(state)

    # Rebuild the skills snapshot.
    _rebuild_snapshot()

    logger.info(f"Skill uploaded: name={name}, path={skill_md}")
    # Attach CAUTION scanner findings as non-blocking advisory warnings so the
    # client can surface them (per the skill-security design: CAUTION allows the
    # upload but reports the flags). SAFE / UNAVAILABLE / DO_NOT_INSTALL produce
    # an empty list here — DO_NOT_INSTALL is already rejected above.
    warnings = build_caution_warnings(scan_result)
    if warnings:
        logger.warning(f"Skill uploaded with CAUTION flags: name={name}, warnings={warnings!r}")
    payload = {"success": True, "warnings": warnings}
    return Response(
        status_code=200,
        headers={"Content-Type": "application/json"},
        description=json.dumps(payload),
    )


@app.post("/skills/toggle")
async def toggle_skill_handler(request):
    """Toggle a skill's active flag.

    Accepts a JSON body: {"name": string, "active": bool}.
    """
    try:
        body = request.json()
    except Exception:
        body = None

    if not isinstance(body, dict):
        logger.warning("Skill toggle rejected: invalid JSON body")
        return Response(
            status_code=400,
            headers={"Content-Type": "application/json"},
            description='{"success": false, "message": "Invalid JSON body"}',
        )

    name = body.get("name")
    active = body.get("active")

    if not isinstance(name, str) or not name.strip():
        logger.warning("Skill toggle rejected: missing or invalid 'name'")
        return Response(
            status_code=400,
            headers={"Content-Type": "application/json"},
            description='{"success": false, "message": "Missing or invalid \'name\'"}',
        )
    name = name.strip()

    if not isinstance(active, bool):
        logger.warning(f"Skill toggle rejected: invalid 'active' for '{name}'")
        return Response(
            status_code=400,
            headers={"Content-Type": "application/json"},
            description='{"success": false, "message": "Missing or invalid \'active\' (must be boolean)"}',
        )

    err = _validate_skill_name(name)
    if err:
        logger.warning(f"Skill toggle rejected: invalid name '{name}': {err}")
        return Response(
            status_code=400,
            headers={"Content-Type": "application/json"},
            description=json.dumps({"success": False, "message": err}),
        )

    # Verify the skill exists on disk.
    skill_dir = PLUGIN_SKILLS_DIR / name
    if not (skill_dir / "SKILL.md").exists():
        logger.warning(f"Skill toggle rejected: skill '{name}' not found")
        return Response(
            status_code=404,
            headers={"Content-Type": "application/json"},
            description=json.dumps({"success": False, "message": f"Skill '{name}' not found"}),
        )

    # Update the state file.
    state = _read_skills_state()
    state[name] = {"active": active}
    _write_skills_state(state)

    # Rebuild the skills snapshot.
    _rebuild_snapshot()

    logger.info(f"Skill toggled: name={name}, active={active}")
    return Response(
        status_code=200,
        headers={"Content-Type": "application/json"},
        description='{"success": true}',
    )
