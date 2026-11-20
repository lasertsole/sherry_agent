"""Dataset/environment resolution shared by the external benchmark suites.

One contract for every suite: the dataset directory comes from the suite's own
env var (``GAIA_DATA_DIR`` …), then ``SHERRY_EVAL_DATA_ROOT/<suite>``, then the
run-local ``evals/results/<suite>/data``. A missing dataset is NOT an error —
the suite reports the resolved (missing) path and SKIPS loudly, which is the
plan's answer to "dataset needs auth / cannot be downloaded here".
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Default per-task wall clock (seconds) and the hard ceiling, matching the
#: deepswe suite's bounds.
DEFAULT_TIMEOUT_S = 1800.0
MAX_TIMEOUT_S = 7200.0


def resolve_dataset_dir(suite: str, env_var: str) -> Path:
    """The dataset directory for *suite*: env var → data root → run-local."""
    override = env_var and os.environ.get(env_var, "").strip() or ""
    if override:
        return Path(override).expanduser().resolve()
    data_root = os.environ.get("SHERRY_EVAL_DATA_ROOT", "").strip()
    if data_root:
        return (Path(data_root).expanduser().resolve() / suite).resolve()
    return (REPO_ROOT / "evals" / "results" / suite / "data").resolve()


def dataset_ready(directory: Path, *required_names: str) -> tuple[bool, str]:
    """Whether *directory* exists and carries at least one required file."""
    if not directory.is_dir():
        return False, f"dataset directory not found: {directory}"
    if not required_names:
        return True, str(directory)
    missing = [name for name in required_names if not (directory / name).is_file()]
    if missing:
        return False, f"missing file(s) in {directory}: {', '.join(missing)}"
    return True, str(directory)


def load_json_records(path: Path) -> list[dict[str, Any]]:
    """Load a dataset file: a JSON array, one JSON object per line, or a
    HF-style {"data": [...]} wrapper. Raises ValueError on unusable shapes."""
    text = path.read_text(encoding="utf-8-sig")
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    records: list[Any]
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        records = (
            payload.get("data")
            or payload.get("tasks")
            or payload.get("instances")
            or payload.get("examples")
            or []
        )
    else:
        records = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            try:
                records.append(json.loads(line))
            except ValueError as exc:
                raise ValueError(
                    f"{path.name}: not a JSON array/wrapper and not JSONL "
                    f"({exc} on line {len(records) + 1})"
                ) from exc
    if not isinstance(records, list) or not records:
        raise ValueError(f"{path}: no task records found")
    cleaned = [record for record in records if isinstance(record, dict)]
    if not cleaned:
        raise ValueError(f"{path}: every record is a non-object")
    return cleaned


def pick_dataset_file(directory: Path, *preferred: str) -> Path:
    """The dataset file inside *directory*: a preferred name, else the single
    JSON/JSONL file it carries. Raises when none or several are ambiguous."""
    for name in preferred:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    candidates = sorted(
        entry
        for entry in directory.iterdir()
        if entry.is_file() and entry.suffix in {".json", ".jsonl"}
    )
    if not candidates:
        raise ValueError(f"{directory}: no .json/.jsonl dataset file")
    if len(candidates) > 1:
        raise ValueError(
            f"{directory}: {len(candidates)} dataset files, pass one explicitly: "
            + ", ".join(entry.name for entry in candidates)
        )
    return candidates[0]


def resolve_timeout(
    requested: float | None, *, default: float = DEFAULT_TIMEOUT_S, maximum: float = MAX_TIMEOUT_S
) -> float:
    """Clamp a per-task timeout like the deepswe suite does."""
    value = float(requested) if requested else default
    return max(60.0, min(value, maximum))
