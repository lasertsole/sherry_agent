"""
ClawHub command runner — replaces {{ROOT_DIR}} placeholders with the actual project root path.

Security: after an ``install``/``update`` that writes into ``skills/plugins/``,
the newly downloaded third-party skills are passed through the SkillSpector
security scanner (the same gate used by the HTTP upload endpoint). A skill
flagged ``DO_NOT_INSTALL`` is rolled back (directory removed + state entry
cleared) so a malicious skill can never become active. A ``CAUTION`` skill is
kept but logged. If the scanner is unavailable the download is allowed but
warned (fail-open, matching the upload path's dev-convenience policy).
"""
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from config import ROOT_DIR, PLUGIN_SKILLS_DIR, SKILLS_STATE_FILE
from loguru import logger
from pydantic import validate_call

#: Commands that can write third-party skills into ``skills/plugins/``.
_MUTATING_COMMANDS = {"install", "update"}


def _resolve_workdir(workdir: str) -> str:
    """Replace {{ROOT_DIR}} placeholder with the actual project root path."""
    return workdir.replace("{{ROOT_DIR}}", str(ROOT_DIR))


# =============================================================================
# Post-install security scan (mirrors the HTTP upload gate in
# server/trigger/http/skills.py). Runs SkillSpector over third-party skills that
# clawhub just wrote into skills/plugins/, rolling back any DO_NOT_INSTALL skill.
# =============================================================================

def _read_state() -> dict[str, dict[str, bool]]:
    """Read skills/plugins/.state.json defensively; degrade to {} on error."""
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


def _write_state(state: dict[str, dict[str, bool]]) -> None:
    """Write skills/plugins/.state.json atomically (temp file + os.replace)."""
    SKILLS_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    import os
    import tempfile

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


def _remove_tree(path: Path) -> None:
    """Remove a directory tree, with Windows-reserved-filename fallback.

    Mirrors agent/tools/skill_tools/skill_manage.py::_force_remove_tree.
    """
    try:
        shutil.rmtree(path)
    except OSError:
        if os.name != "nt":
            raise
        logger.warning("shutil.rmtree failed for '{}'; falling back to cmd rd", path)
        result = subprocess.run(
            ["cmd", "/c", "rd", "/s", "/q", str(path)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise OSError(
                f"Failed to remove '{path}' even with cmd fallback: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            ) from None


def _scan_plugin_skills() -> dict[str, Any]:
    """Scan third-party skills in ``skills/plugins/`` and roll back dangerous ones.

    A skill whose ScanResult is ``DO_NOT_INSTALL`` is deleted from disk and its
    entry dropped from ``.state.json`` so it can never be activated. ``CAUTION``
    skills are kept but logged. If the scanner is unavailable/errors the scan is
    skipped with a warning (fail-open) — matching the upload path's rule that a
    missing scanner must not break the app.

    Returns a summary dict (``scanned``, ``rolled_back``, ``caution``, ``skipped``)
    describing the outcome, for surfaced in the clawhub command's return payload.
    """
    summary: dict[str, int] = {"scanned": 0, "rolled_back": 0, "caution": 0, "skipped": 0}
    try:
        from server.service.skill_scanner import build_reject_message, scan_skill
    except ImportError:
        # Same import-recursion caveat as skills/skills_snapshot.py: importing
        # `server.service` from the agent/skills side may fail mid-init. Fail
        # open so a clawhub install keeps working without a verdict; the scan
        # still runs in the normal server path where server.service exists.
        logger.warning(
            "SkillScanner unavailable in clawhub context; skipping post-install "
            "security scan (fail-open)."
        )
        return summary

    if not PLUGIN_SKILLS_DIR.is_dir():
        return summary

    # Each skill is its own directory under skills/plugins/. Nested author-scoped
    # installs produce <author>/<slug>/, but the skill root is always the top-level
    # directory directly under PLUGIN_SKILLS_DIR. Walk SKILL.md files to locate
    # every skill root, then scan the whole skill directory (not just SKILL.md).
    skill_roots = set()
    for skill_md in PLUGIN_SKILLS_DIR.glob("**/SKILL.md"):
        rel = skill_md.relative_to(PLUGIN_SKILLS_DIR)
        top = rel.parts[0]
        skill_roots.add(PLUGIN_SKILLS_DIR / top)

    for skill_root in sorted(skill_roots):
        name = skill_root.name
        try:
            scan_result = scan_skill(skill_root)
        except Exception as exc:  # noqa: BLE001 - scanner must fail open
            logger.warning(f"Skill scan failed for '{name}' after clawhub install: {exc}")
            summary["skipped"] += 1
            continue

        if scan_result.is_unavailable:
            logger.warning(
                f"Skill security scanner unavailable; keeping '{name}' from clawhub "
                "without a scan verdict (fail-open)."
            )
            summary["skipped"] += 1
            continue

        summary["scanned"] += 1
        if scan_result.is_do_not_install:
            reject = build_reject_message(scan_result)
            logger.warning(
                f"Skill '{name}' installed via clawhub flagged DO_NOT_INSTALL by "
                f"security scanner (risk_score={scan_result.risk_score}); rolling back. "
                f"Reason: {reject}"
            )
            try:
                _remove_tree(skill_root)
            except Exception as exc:  # noqa: BLE001 - removal must not crash
                logger.error(f"Failed to roll back skill '{name}' after DO_NOT_INSTALL: {exc}")
                summary["skipped"] += 1
                continue
            # Drop the state entry so the skill cannot be toggled active later.
            state = _read_state()
            if name in state:
                del state[name]
                try:
                    _write_state(state)
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"Failed to prune state for rolled-back skill '{name}': {exc}")
            summary["rolled_back"] += 1
            logger.info(f"Rolled back skill '{name}' downloaded via clawhub (DO_NOT_INSTALL).")
        elif scan_result.is_caution:
            summary["caution"] += 1
            logger.warning(
                f"Skill '{name}' installed via clawhub flagged CAUTION by security "
                f"scanner (risk_score={scan_result.risk_score}); kept but should be audited."
            )

    # If any skills were rolled back, reflect that in the state file for names
    # that no longer exist on disk (defensive cleanup of orphaned entries).
    if summary["rolled_back"]:
        current = _read_state()
        prune_needed = False
        for key in list(current.keys()):
            if not (PLUGIN_SKILLS_DIR / key).is_dir():
                del current[key]
                prune_needed = True
        if prune_needed:
            try:
                _write_state(current)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Failed to prune orphaned state entries after clawhub scan: {exc}")

    return summary


@validate_call
def run_clawhub_command(command: list[str]) -> dict[str, Any]:
    """
    Run a clawhub command with {{ROOT_DIR}} placeholders resolved.

    On a successful ``install``/``update`` that targets ``skills/plugins/``, the
    newly downloaded third-party skills are passed through the SkillSpector
    security scanner; ``DO_NOT_INSTALL`` skills are rolled back (see
    :func:`_scan_plugin_skills`).

    Args:
        command: List of command arguments, e.g. ["install", "my-skill", "--workdir", "{{ROOT_DIR}}"]

    Returns:
        dict with keys: success (bool), stdout (str), stderr (str), and optionally
        ``scan`` (dict) describing the post-install security-scan outcome.
    """
    # Resolve {{ROOT_DIR}} in all arguments
    resolved = [_resolve_workdir(arg) for arg in command]

    cmd = ["npx", "--yes", "clawhub@latest"] + resolved

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        success = result.returncode == 0
        payload: dict[str, Any] = {
            "success": success,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }
        if success and command and command[0].lower() in _MUTATING_COMMANDS:
            payload["scan"] = _scan_plugin_skills()
            # New third-party skills are now on disk under skills/plugins/ —
            # rebuild the skills snapshot so the loaded skill roster reflects them.
            try:
                from skills import build_skills_snapshot
                build_skills_snapshot()
            except Exception:  # noqa: BLE001 - snapshot failure must not break install
                logger.warning("Failed to rebuild skills snapshot after clawhub install/update.")
        return payload
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "stdout": "",
            "stderr": "Command timed out after 120 seconds",
            "returncode": -1,
        }
    except FileNotFoundError:
        return {
            "success": False,
            "stdout": "",
            "stderr": "npx not found. Please install Node.js first.",
            "returncode": -1,
        }
    except Exception as e:
        return {
            "success": False,
            "stdout": "",
            "stderr": f"Error: {e}",
            "returncode": -1,
        }
