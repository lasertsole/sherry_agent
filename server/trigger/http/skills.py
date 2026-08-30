import json
import os
import tempfile
from pathlib import Path

from robyn import Response
from server.trigger.core import app
from loguru import logger
from skills.loader import scan_skills, parse_frontmatter
from config import PLUGIN_SKILLS_DIR, SKILLS_STATE_FILE
from server.service.skill_scanner import (
    ScanResult,
    build_caution_warnings,
    build_reject_message,
    scan_skill,
)
from agent.tools.skill_tools.skill_manage import (
    _MAX_NAME_LENGTH,
    _VALID_NAME_RE,
    _MAX_SKILL_CONTENT_CHARS,
)
from context_engine.curator.usage import (
    delete_skill,
    is_pinned,
    pin_skill,
    unpin_skill,
)


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


_SKIP_DIRS = {"__pycache__", ".git", ".venv", "node_modules"}
_SKIP_SUFFIXES = {".pyc", ".pyo"}


def _build_skill_file_tree(skill_root: Path):
    """Build a recursive tree of the skill directory (relative to `skill_root`).

    Each returned node is ``{"path", "name", "type", "content"}`` where ``type``
    is either ``"file"`` or ``"dir"``. Files carry their UTF-8 text content
    (``read_text`` may raise on non-text binaries; those are skipped). Files at
    the root level are sorted first, then directories, both alphabetically.
    Cache/venv noise is excluded.
    """
    nodes: list[dict] = []

    def walk(dir_path: Path, rel_prefix: str = "") -> None:
        entries = sorted(dir_path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        for entry in entries:
            rel = f"{rel_prefix}/{entry.name}" if rel_prefix else entry.name
            if entry.is_dir():
                if entry.name in _SKIP_DIRS:
                    continue
                nodes.append({"path": rel, "name": entry.name, "type": "dir"})
                walk(entry, rel)
            elif entry.is_file() and entry.suffix not in _SKIP_SUFFIXES:
                try:
                    text = entry.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    logger.debug(f"Skipping non-text skill file: {entry}")
                    continue
                nodes.append({"path": rel, "name": entry.name, "type": "file", "content": text})

    walk(skill_root)
    return nodes


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


# =============================================================================
# Skills state file helpers
# =============================================================================


def _read_skills_state() -> dict[str, dict[str, bool]]:
    """Read the skills state file defensively.

    Returns a mapping of skill name -> {"active": bool}. Missing or malformed
    files degrade to an empty dict.
    """
    try:
        if not SKILLS_STATE_FILE.exists():
            return {}
        with open(SKILLS_STATE_FILE, "r", encoding="utf-8") as f:
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


def _write_skills_state(state: dict[str, dict[str, bool]]) -> None:
    """Write the skills state file atomically (temp file + os.replace)."""
    SKILLS_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        dir=str(SKILLS_STATE_FILE.parent),
        prefix=f".{SKILLS_STATE_FILE.name}.tmp.",
        suffix="",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=4)
        os.replace(temp_path, SKILLS_STATE_FILE)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            logger.error("Failed to remove temp state file %s", temp_path, exc_info=True)
        raise


def _rebuild_snapshot() -> None:
    """Rebuild skills_snapshot.json after a mutation."""
    try:
        from skills.skills_snapshot import build_skills_snapshot

        build_skills_snapshot()
    except Exception:
        logger.exception("Failed to rebuild skills snapshot")


def _validate_skill_name(name: str) -> str | None:
    """Validate a skill name. Returns an error message or None if valid."""
    if not name:
        return "Skill name is required."
    if len(name) > _MAX_NAME_LENGTH:
        return f"Skill name exceeds {_MAX_NAME_LENGTH} characters."
    if not _VALID_NAME_RE.match(name):
        return (
            f"Invalid skill name '{name}'. Use lowercase letters, numbers, "
            f"hyphens, dots, and underscores. Must start with a letter or digit."
        )
    return None


# =============================================================================
# Skill upload / toggle endpoints
# =============================================================================


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


# =============================================================================
# Auto-skill deletion
# =============================================================================
#
# Deleting an auto skill is pined-aware: `delete_skill` rejects any skill that
# is pinned, and resolves nested `skills/auto/<category>/<skill>/`
# paths via `_skill_dir`.


def _json_response(status_code: int, payload: dict[str, object]) -> Response:
    return Response(
        status_code=status_code,
        headers={"Content-Type": "application/json"},
        description=json.dumps(payload, ensure_ascii=False),
    )


@app.post("/skills/delete")
async def delete_auto_skill_handler(request):
    """Delete an auto skill from disk.

    Accepts a JSON body: {"name": string}. The user-facing client MUST show a
    confirmation dialog before invoking this endpoint, since deletion is
    irreversible. Pinned or fixed skills are rejected by `delete_skill`.
    """
    try:
        body = request.json()
    except Exception:
        body = None

    if not isinstance(body, dict):
        return _json_response(400, {"success": False, "message": "Invalid JSON body"})

    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        return _json_response(400, {"success": False, "message": "Missing or invalid 'name'"})
    name = name.strip()

    err = _validate_skill_name(name)
    if err:
        return _json_response(400, {"success": False, "message": err})

    ok, msg = delete_skill(name)
    logger.info(f"Auto skill delete: name={name}, ok={ok} msg={msg}")
    if not ok:
        return _json_response(400, {"success": False, "message": msg})

    # Rebuild the skills snapshot so the deleted skill disappears from the
    # agent's effective skill prompt.
    _rebuild_snapshot()
    return _json_response(200, {"success": True, "name": name})


# =============================================================================
# Auto-skill pin / unpin
# =============================================================================
#
# Pinning is curator-aware: pinned skills bypass all automatic transitions and
# are rejected by `delete_skill`. The endpoint toggles the `pinned` flag in the
# usage record via `pin_skill` / `unpin_skill`, mirroring the delete handler's
# name validation and snapshot rebuild.


@app.post("/skills/pin")
async def pin_auto_skill_handler(request):
    """Pin or unpin an auto skill.

    Accepts a JSON body: {"name": string, "pinned": bool}. When `pinned` is
    true the skill is pinned (curator will never merge or remove it); when
    false it is unpinned. The skill must already exist under `skills/auto/`.
    """
    try:
        body = request.json()
    except Exception:
        body = None

    if not isinstance(body, dict):
        return _json_response(400, {"success": False, "message": "Invalid JSON body"})

    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        return _json_response(400, {"success": False, "message": "Missing or invalid 'name'"})
    name = name.strip()

    pinned = body.get("pinned")
    if not isinstance(pinned, bool):
        return _json_response(
            400,
            {"success": False, "message": "Missing or invalid 'pinned' (must be boolean)"},
        )

    err = _validate_skill_name(name)
    if err:
        return _json_response(400, {"success": False, "message": err})

    ok, msg = pin_skill(name) if pinned else unpin_skill(name)
    logger.info(f"Auto skill pin: name={name}, pinned={pinned}, ok={ok} msg={msg}")
    if not ok:
        return _json_response(400, {"success": False, "message": msg})

    # Rebuild the skills snapshot so the agent's effective skill prompt reflects
    # the new pinned state.
    _rebuild_snapshot()
    return _json_response(200, {"success": True, "name": name, "pinned": pinned})
