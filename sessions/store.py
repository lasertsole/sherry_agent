"""Session storage for chat history."""

from __future__ import annotations

import json
from typing import Any
from pathlib import Path
from config import SESSIONS_DIR

def _current_jsonl_path(session_id: str) -> str:
    return (Path(SESSIONS_DIR) / f"{session_id}/current.jsonl").as_posix()

def read_current_from_session(session_id: str) -> list[dict[str, Any]]:
    path: Path = Path(_current_jsonl_path(session_id))

    if not path.exists():
        return []

    # 如果文件存在但内容为空，则返回空列表
    if path.stat().st_size == 0:
        return []

    text_lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line.strip()) for line in text_lines if len(line) > 0]