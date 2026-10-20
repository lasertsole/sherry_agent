"""Official ToolBench task data (instructions + test-query ids).

Source: the official ToolBench dataset (OpenBMB). Its canonical download is the
Google Drive folder / Tsinghua Cloud link in
https://github.com/OpenBMB/ToolBench; for reproducible CI the suite pins the
HuggingFace mirror https://huggingface.co/datasets/Adorg/ToolBench at commit
0c25df5e6937aae854c2cc1bb203297600afce8b, which carries the same
``instruction/``, ``test_query_ids/`` and ``toolenv/`` trees. The suite also
clones the official code repository https://github.com/OpenBMB/ToolBench
(commit d56fdd89faf8c91fa135090b212bb9057ee5cfc2) for its evaluator, prompt text
and tool-schema helpers.

No question is generated here. Every task is one official query selected by the
official ``test_query_ids`` file for its subset.

The full instruction files are large (G1 ≈ 630 MB, G2 ≈ 730 MB, G3 ≈ 315 MB).
They are downloaded once into the eval cache. ``--dry-run`` falls back to the
official ``data_example`` shipped in the repo when the full file is absent, so
the schema can be validated without a 1.7 GB download (the run itself always
uses the full file).
"""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from evals import external_bench

TOOLBENCH_REPO = "https://github.com/OpenBMB/ToolBench.git"
#: The commit the prompt / evaluator / schema helpers were read from.
TOOLBENCH_PIN = "d56fdd89faf8c91fa135090b212bb9057ee5cfc2"
HF_REPO = "Adorg/ToolBench"
HF_PIN = "0c25df5e6937aae854c2cc1bb203297600afce8b"

SUBSET_SOURCES: dict[str, str] = {
    "G1_instruction": "G1",
    "G1_category": "G1",
    "G1_tool": "G1",
    "G2_category": "G2",
    "G2_instruction": "G2",
    "G3_instruction": "G3",
}
DEFAULT_SUBSET = "G1_instruction"
DEFAULT_SLICE = 5

CACHE_DIR = external_bench.CACHE_ROOT / "toolbench"
DATA_DIR = CACHE_DIR / "data"


@dataclass(frozen=True)
class Task:
    """One official ToolBench query."""

    subset: str
    query_id: int
    query: str
    api_list: tuple[dict[str, Any], ...]
    official_test_id: bool = True
    supported: bool = True
    unsupported_reason: str = ""

    @property
    def task_id(self) -> str:
        """A stable id across subsets (the subset is part of it)."""
        return f"{self.subset}-{self.query_id}"


def fetch_repo() -> tuple[Path, str]:
    """Clone/check out the official ToolBench repository (evaluator + examples)."""
    return external_bench.sync_repo(
        CACHE_DIR / "repo", TOOLBENCH_REPO, TOOLBENCH_PIN, label="ToolBench"
    )


def _hf_url(path: str) -> str:
    return f"https://huggingface.co/datasets/{HF_REPO}/resolve/{HF_PIN}/" + quote(path, safe="/")


def _download(path: str, dest: Path) -> None:
    if dest.is_file() and dest.stat().st_size > 0:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    print(f"[evals] downloading {path} → {dest} …", flush=True)
    request = urllib.request.Request(_hf_url(path), headers={"User-Agent": "sherry-evals"})
    with urllib.request.urlopen(request, timeout=120) as response, part.open("wb") as handle:
        while chunk := response.read(1024 * 1024):
            handle.write(chunk)
    part.replace(dest)


def ensure_data(subsets: tuple[str, ...]) -> Path:
    """Download the official instruction + test-id files needed by *subsets*."""
    for subset in subsets:
        source = SUBSET_SOURCES[subset]
        _download(
            f"instruction/{source}_query.json", DATA_DIR / "instruction" / f"{source}_query.json"
        )
        _download(
            f"test_query_ids/{subset}_test_query_ids.json",
            DATA_DIR / "test_query_ids" / f"{subset}_test_query_ids.json",
        )
    return DATA_DIR


def _query_file(root: Path, source: str) -> Path:
    return root / "instruction" / f"{source}_query.json"


def _test_ids(root: Path, subset: str) -> set[str] | None:
    path = root / "test_query_ids" / f"{subset}_test_query_ids.json"
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        return {str(key) for key in payload}
    if isinstance(payload, list):
        return {str(key) for key in payload}
    return None


def load_tasks(
    subsets: tuple[str, ...] = (DEFAULT_SUBSET,),
    *,
    example: bool = False,
) -> dict[str, list[Task]]:
    """Load the official test queries for *subsets*.

    ``example=True`` reads the repo's official ``data_example`` files (five
    queries) and treats all of them as the set — DRY-RUN / offline schema
    validation only, never a scored run.
    """
    repo_root, _ = fetch_repo()
    if example:
        root = repo_root / "data_example"
    else:
        # An official ToolBench dataset download can be pointed at directly
        # (<root>/instruction/... + <root>/test_query_ids/...), skipping the
        # HF-mirror download.
        override = os.environ.get("SHERRY_TOOLBENCH_DATA", "").strip()
        root = Path(override).expanduser() if override else ensure_data(subsets)
    loaded: dict[str, list[Task]] = {}
    for subset in subsets:
        source = SUBSET_SOURCES[subset]
        path = _query_file(root, source)
        if not path.is_file():
            raise RuntimeError(f"ToolBench instruction file not found: {path}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise RuntimeError(f"ToolBench instruction file is not a list: {path}")
        test_ids = None if example else _test_ids(root, subset)
        official = test_ids is not None
        tasks: list[Task] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            query_id = entry.get("query_id")
            if query_id is None:
                continue
            try:
                numeric_id = int(query_id)
            except (TypeError, ValueError):
                continue
            if official and str(numeric_id) not in test_ids:
                continue
            query = str(entry.get("query") or "").strip()
            api_list = entry.get("api_list") or []
            reason = ""
            if not query:
                reason = "official query is empty"
            elif not isinstance(api_list, list) or not api_list:
                reason = "official query carries no api_list"
            tasks.append(
                Task(
                    subset=subset,
                    query_id=numeric_id,
                    query=query,
                    api_list=tuple(api for api in api_list if isinstance(api, dict)),
                    official_test_id=official,
                    supported=not reason,
                    unsupported_reason=reason,
                )
            )
        tasks.sort(key=lambda task: task.query_id)
        loaded[subset] = tasks
    return loaded


def all_tasks(tasks: dict[str, list[Task]]) -> list[Task]:
    """Flatten the per-subset mapping in the official subset order."""
    return [task for subset in SUBSET_SOURCES if subset in tasks for task in tasks[subset]]


def task_ids(tasks: dict[str, list[Task]], subsets: tuple[str, ...]) -> tuple[str, ...]:
    """The official ids of *subsets*, in query-id order."""
    return tuple(task.task_id for subset in subsets for task in tasks.get(subset, ()))


def select(tasks: dict[str, list[Task]], ids: tuple[str, ...]) -> list[Task]:
    """Resolve ``<subset>-<query_id>`` ids and refuse unknown ones."""
    index = {task.task_id: task for task in all_tasks(tasks)}
    missing = [task_id for task_id in ids if task_id not in index]
    if missing:
        raise KeyError(f"unknown ToolBench task id(s): {missing}")
    return [index[task_id] for task_id in ids]
