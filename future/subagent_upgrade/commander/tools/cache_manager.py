import json
import hashlib
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any
from config import SESSIONS_DIR


class CacheManager:
    def __init__(self, session_id: str, task_id: str):
        self._session_id = session_id
        self._task_id = task_id
        self._cache_dir = SESSIONS_DIR / session_id / "todo" / ".cache"
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def _get_cache_key(self, label: str, description: str) -> str:
        content = f"{label}:{description}"
        return hashlib.md5(content.encode()).hexdigest()[:16]

    def get_cache_file(self, label: str, description: str) -> Path:
        key = self._get_cache_key(label, description)
        return self._cache_dir / f"{label}_{key}.json"

    def has_cache(self, label: str, description: str) -> bool:
        cache_file = self.get_cache_file(label, description)
        if not cache_file.exists():
            return False
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            return data.get("status") == "ok"
        except Exception:
            return False

    def get_cache(self, label: str, description: str) -> dict[str, Any] | None:
        cache_file = self.get_cache_file(label, description)
        if not cache_file.exists():
            return None
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            return None

    def set_cache(self, label: str, description: str, result: dict[str, Any]) -> None:
        cache_file = self.get_cache_file(label, description)
        cache_data = {
            "label": label,
            "description": description,
            "status": result.get("status", "ok"),
            "result": result.get("result", ""),
            "output": result.get("output", ""),
            "timestamp": datetime.now().isoformat(),
        }
        cache_file.write_text(json.dumps(cache_data, ensure_ascii=False, indent=2), encoding="utf-8")

    def clear_cache(self, label: str | None = None) -> int:
        count = 0
        if label is None:
            for f in self._cache_dir.glob("*.json"):
                f.unlink()
                count += 1
        else:
            for f in self._cache_dir.glob(f"{label}_*.json"):
                f.unlink()
                count += 1
        return count

    def cleanup_expired(self, days: int = 3) -> int:
        cutoff = datetime.now() - timedelta(days=days)
        count = 0
        for f in self._cache_dir.glob("*.json"):
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime)
                if mtime < cutoff:
                    f.unlink()
                    count += 1
            except Exception:
                pass
        return count

    @staticmethod
    def cleanup_all_sessions(session_id: str | None = None, days: int = 3) -> int:
        cutoff = datetime.now() - timedelta(days=days)
        count = 0

        sessions_dir = SESSIONS_DIR
        if not sessions_dir.exists():
            return 0

        for session_dir in sessions_dir.iterdir():
            if not session_dir.is_dir():
                continue
            if session_id and session_dir.name != session_id:
                continue

            cache_dir = session_dir / "todo" / ".cache"
            if not cache_dir.exists():
                continue

            for f in cache_dir.glob("*.json"):
                try:
                    mtime = datetime.fromtimestamp(f.stat().st_mtime)
                    if mtime < cutoff:
                        f.unlink()
                        count += 1
                except Exception:
                    pass

        return count


def build_cache_manager(session_id: str, task_id: str) -> CacheManager:
    return CacheManager(session_id, task_id)