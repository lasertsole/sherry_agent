"""Skill-quality gate for the plan-completion nudge.

The nudge writes directly into ``skills/auto``; this module gives the caller a
safe transaction around those writes and a semantic judge for the result:

* :class:`SkillTreeSnapshot` captures the auto-skill tree before a nudge attempt;
* :func:`restore_auto_skills` rolls a rejected attempt back (including the
  skills snapshot index and usage sidecar);
* :func:`judge_skill_update` asks the auxiliary model whether the change is
  grounded in the plan evidence, reusable, and well triggered. A no-op is only
  accepted when the judge says the evidence genuinely had no reusable signal.

The module is deliberately independent of the nudge prompt so the gate can be
tested with plain dictionaries and temporary skill roots.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from config import AUTO_SKILLS_DIR
from config.features import NUDGE

__all__ = [
    "SkillJudgement",
    "SkillTreeSnapshot",
    "changed_skill_paths",
    "describe_skill_changes",
    "judge_skill_update",
    "restore_auto_skills",
    "snapshot_auto_skills",
]

_MAX_JUDGE_FILE_CHARS = 20_000
_MAX_JUDGE_TOTAL_CHARS = 30_000


@dataclass(frozen=True)
class SkillTreeSnapshot:
    """Content snapshot of ``skills/auto`` (relative paths -> bytes)."""

    files: dict[str, bytes] = field(default_factory=dict)
    dirs: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class SkillJudgement:
    """The judge's verdict for one nudge attempt."""

    passed: bool
    verdict: str
    grounded_in_run: bool
    reusable: bool
    trigger_quality: int
    quality: int
    no_op_justified: bool
    feedback: str

    @property
    def failed(self) -> bool:
        return not self.passed


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _is_transient(name: str) -> bool:
    return name.endswith(".lock") or name.endswith(".tmp")


def _is_telemetry_path(name: str) -> bool:
    """Usage-sidecar churn is not a skill edit the judge should score."""
    return name == ".usage.json" or name.startswith(".usage/")


def snapshot_auto_skills(root: Path | None = None) -> SkillTreeSnapshot:
    """Capture every file (and directory) under the auto-skill root."""
    root = Path(root or AUTO_SKILLS_DIR)
    if not root.exists():
        return SkillTreeSnapshot()
    files: dict[str, bytes] = {}
    dirs: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        if path.is_dir():
            dirs.add(_relative(path, root))
            continue
        if not path.is_file() or _is_transient(path.name):
            continue
        try:
            files[_relative(path, root)] = path.read_bytes()
        except OSError as exc:
            logger.warning("skill-quality: cannot snapshot {}: {}", path, exc)
    return SkillTreeSnapshot(files=files, dirs=frozenset(dirs))


def changed_skill_paths(
    before: SkillTreeSnapshot,
    after: SkillTreeSnapshot,
) -> set[str]:
    """Relative paths whose bytes were created, changed, or deleted."""
    names = set(before.files) | set(after.files)
    return {name for name in names if before.files.get(name) != after.files.get(name)}


def describe_skill_changes(
    before: SkillTreeSnapshot,
    after: SkillTreeSnapshot,
    *,
    per_file_chars: int = _MAX_JUDGE_FILE_CHARS,
    total_chars: int = _MAX_JUDGE_TOTAL_CHARS,
) -> str:
    """Render changed files for the judge, bounded by a total char budget."""
    chunks: list[str] = []
    used = 0
    for name in sorted(changed_skill_paths(before, after)):
        if _is_telemetry_path(name):
            continue
        if name not in after.files:
            chunk = f"### {name}\n(deleted)\n"
        else:
            text = after.files[name].decode("utf-8", errors="replace")
            if len(text) > per_file_chars:
                text = text[:per_file_chars] + "\n...[truncated]"
            chunk = f"### {name}\n{text}\n"
        if used + len(chunk) > total_chars:
            chunks.append("...[remaining changed files truncated]")
            break
        chunks.append(chunk)
        used += len(chunk)
    return "".join(chunks) or "(no skill files changed)"


def restore_auto_skills(
    snapshot: SkillTreeSnapshot,
    root: Path | None = None,
) -> None:
    """Restore ``skills/auto`` to *snapshot* and rebuild its index.

    Only paths under the auto root are touched; builtin/plugin skills are never
    part of a snapshot and are therefore never restored or deleted.
    """
    root = Path(root or AUTO_SKILLS_DIR)
    root.mkdir(parents=True, exist_ok=True)
    before = snapshot_auto_skills(root)

    for name in sorted(set(before.files) - set(snapshot.files), reverse=True):
        path = root / name
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("skill-quality: cannot remove {} during rollback: {}", path, exc)

    for name, content in snapshot.files.items():
        path = root / name
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        except OSError as exc:
            logger.warning("skill-quality: cannot restore {}: {}", path, exc)

    # Drop directories created only by the rejected attempt (deepest first).
    current_dirs = snapshot_auto_skills(root).dirs
    for name in sorted(current_dirs - set(snapshot.dirs), key=lambda item: -item.count("/")):
        try:
            (root / name).rmdir()
        except OSError:
            pass  # not empty (or already gone): leave it

    try:
        from skills import build_skills_snapshot

        build_skills_snapshot()
    except Exception as exc:  # noqa: BLE001 - index rebuild is best-effort
        logger.warning("skill-quality: skills snapshot rebuild after rollback failed: {}", exc)


_JUDGE_PROMPT = """You are the skill-quality judge for an autonomous plan-completion nudge.

The nudge was given success/failure evidence from one completed plan and asked to
update a class-level skill (or return a justified no-op). Judge ONLY the change
below, not the plan's overall success.

## Plan evidence
{evidence}

## Changed skill files
{changes}

## The nudge's no-op reason
{no_op_reason}

Return ONLY a JSON object with exactly these keys:
{{
  "verdict": "pass" | "retry",
  "grounded_in_run": true | false,
  "reusable": true | false,
  "trigger_quality": 0-10,
  "quality": 0-10,
  "no_op_justified": true | false,
  "feedback": "one or two concrete sentences for the next attempt"
}}

Criteria:
- grounded_in_run: every procedural claim / pitfall / constraint is supported by
  the evidence above. Do not reward invented tool names, commands, or outcomes.
- reusable: class-level guidance useful beyond this one plan. Reject plan IDs,
  session artifacts, one-off task narratives, and environment setup failures.
- trigger_quality: `when_to_use` is specific enough for the skill index to load
  the skill at the right moment.
- quality: overall judgment (accuracy, actionability, density, no duplication).
- If no file changed, `no_op_justified` is true only when the evidence contains
  no reusable procedure, pitfall, or hard constraint. A no-op with reusable
  evidence is a retry.
- If files changed, `no_op_justified` must be false.
- `feedback` must name what to add/fix/remove.
"""


def _strip_json(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("judge returned no JSON object")
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("judge returned a non-object")
    return parsed


def _as_bool(value: Any) -> bool:
    return value is True or str(value).strip().lower() == "true"


def _as_score(value: Any) -> int:
    try:
        return max(0, min(10, int(value)))
    except (TypeError, ValueError):
        return 0


async def judge_skill_update(
    evidence: dict[str, Any],
    before: SkillTreeSnapshot,
    after: SkillTreeSnapshot,
    no_op_reason: str | None,
) -> SkillJudgement:
    """Judge one nudge attempt; judge failures are retryable, never passes."""
    changed = {name for name in changed_skill_paths(before, after) if not _is_telemetry_path(name)}
    reason = (no_op_reason or "").strip()
    if not changed and not reason:
        return SkillJudgement(
            passed=False,
            verdict="retry",
            grounded_in_run=False,
            reusable=False,
            trigger_quality=0,
            quality=0,
            no_op_justified=False,
            feedback="No skill file changed and no no-op reason was provided.",
        )

    prompt = _JUDGE_PROMPT.format(
        evidence=json.dumps(evidence, ensure_ascii=False, indent=2, default=str),
        changes=describe_skill_changes(before, after),
        no_op_reason=reason or "(files changed; no-op not claimed)",
    )
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from models import build_auxiliary_llm

        model = build_auxiliary_llm(temperature=0)
        response = await model.ainvoke(
            [
                SystemMessage(content="Return only the requested JSON object."),
                HumanMessage(content=prompt),
            ]
        )
        raw = response.content if hasattr(response, "content") else str(response)
        data = _strip_json(str(raw))
    except Exception as exc:  # noqa: BLE001 - an unavailable judge is retryable
        logger.warning("skill-quality: judge unavailable/failed: {}", exc)
        return SkillJudgement(
            passed=False,
            verdict="retry",
            grounded_in_run=False,
            reusable=False,
            trigger_quality=0,
            quality=0,
            no_op_justified=False,
            feedback=f"Quality judge unavailable ({type(exc).__name__}: {exc}); retry with the evidence.",
        )

    verdict = str(data.get("verdict") or "retry").strip().lower()
    grounded = _as_bool(data.get("grounded_in_run"))
    reusable = _as_bool(data.get("reusable"))
    trigger = _as_score(data.get("trigger_quality"))
    quality = _as_score(data.get("quality"))
    no_op_ok = _as_bool(data.get("no_op_justified"))
    feedback = str(data.get("feedback") or "").strip()
    threshold = int(NUDGE["skill_nudge_quality_threshold"])

    passed = (
        verdict == "pass"
        and grounded
        and reusable
        and trigger >= 6
        and quality >= threshold
        and (bool(changed) or no_op_ok)
    )
    if not feedback:
        feedback = (
            "Improve grounding, reuse scope, or the when_to_use trigger and retry."
            if not passed
            else ""
        )
    return SkillJudgement(
        passed=passed,
        verdict=verdict,
        grounded_in_run=grounded,
        reusable=reusable,
        trigger_quality=trigger,
        quality=quality,
        no_op_justified=no_op_ok,
        feedback=feedback,
    )
