import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any
from config import SESSIONS_DIR


class StateManager:
    def __init__(self, session_id: str, task_id: str):
        self._session_id = session_id
        self._task_id = task_id
        self._state_dir = SESSIONS_DIR / session_id / "todo" / ".state"
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._state_file = self._state_dir / f"{task_id}_execution.json"

    def save_state(
        self,
        status: str,
        current_stage: str = "",
        completed: list[str] | None = None,
        failed: list[str] | None = None,
        pending: list[str] | None = None,
        worker_results: dict[str, Any] | None = None,
    ) -> None:
        state = {
            "task_id": self._task_id,
            "status": status,
            "current_stage": current_stage,
            "completed_tasks": completed or [],
            "failed_tasks": failed or [],
            "pending_tasks": pending or [],
            "worker_results": worker_results or {},
            "timestamp": datetime.now().isoformat(),
        }
        self._state_file.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def load_state(self) -> dict[str, Any] | None:
        if not self._state_file.exists():
            return None
        try:
            return json.loads(self._state_file.read_text(encoding="utf-8"))
        except Exception:
            return None

    def save_checkpoint(self, stage_name: str, data: dict[str, Any]) -> None:
        checkpoint_file = self._state_dir / f"{self._task_id}_checkpoint_{stage_name}.json"
        checkpoint_data = {
            "stage": stage_name,
            "data": data,
            "timestamp": datetime.now().isoformat(),
        }
        checkpoint_file.write_text(
            json.dumps(checkpoint_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def load_checkpoint(self, stage_name: str) -> dict[str, Any] | None:
        checkpoint_file = self._state_dir / f"{self._task_id}_checkpoint_{stage_name}.json"
        if not checkpoint_file.exists():
            return None
        try:
            data = json.loads(checkpoint_file.read_text(encoding="utf-8"))
            return data.get("data")
        except Exception:
            return None

    def list_checkpoints(self) -> list[str]:
        if not self._state_dir.exists():
            return []
        return [
            f.stem.replace(f"{self._task_id}_checkpoint_", "")
            for f in self._state_dir.glob(f"{self._task_id}_checkpoint_*.json")
        ]

    def clear_state(self) -> None:
        if self._state_file.exists():
            self._state_file.unlink()
        for f in self._state_dir.glob(f"{self._task_id}_checkpoint_*.json"):
            f.unlink()

    def has_pending_execution(self) -> bool:
        state = self.load_state()
        if not state:
            return False
        return state.get("status") in ("running", "paused", "interrupted")

    def get_status(self) -> str | None:
        state = self.load_state()
        if not state:
            return None
        return state.get("status")

    @staticmethod
    def cleanup_all_states(session_id: str | None = None, days: int = 3) -> int:
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

            state_dir = session_dir / "todo" / ".state"
            if not state_dir.exists():
                continue

            for f in state_dir.glob("*.json"):
                try:
                    mtime = datetime.fromtimestamp(f.stat().st_mtime)
                    if mtime < cutoff:
                        f.unlink()
                        count += 1
                except Exception:
                    pass

        return count


def build_state_manager(session_id: str, task_id: str) -> StateManager:
    return StateManager(session_id, task_id)