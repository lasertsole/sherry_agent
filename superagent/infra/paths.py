"""State-directory path layout (prototype ``src/infra/paths.ts``)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StatePaths:
    """All filesystem locations derived from one state directory."""

    root: Path

    @property
    def database(self) -> Path:
        """Path of the orchestrator SQLite database."""
        return self.root / "orchestrator.db"

    @property
    def lock(self) -> Path:
        """Directory holding the single-instance lock."""
        return self.root / "lock"

    @property
    def worker_bin(self) -> Path:
        """Directory with worker CLI shims prepended to PATH."""
        return self.root / "worker-bin"

    @property
    def worker_gitconfig(self) -> Path:
        """Directory with the worker-only git config."""
        return self.root / "worker-gitconfig"

    @property
    def executions(self) -> Path:
        """Root directory for Ticket worktrees."""
        return self.root / "executions"

    @property
    def artifacts(self) -> Path:
        """Root directory for logs and other artifacts."""
        return self.root / "artifacts"

    def execution(self, name: str) -> Path:
        """Return the worktree directory for one Ticket short name."""
        return self.executions / name

    def ensure(self) -> None:
        """Create every managed directory."""
        for path in (
            self.root,
            self.lock,
            self.worker_bin,
            self.worker_gitconfig,
            self.executions,
            self.artifacts,
        ):
            path.mkdir(parents=True, exist_ok=True)


def state_paths(state_dir: str | Path) -> StatePaths:
    """Build the path layout for a state directory."""
    return StatePaths(root=Path(state_dir).expanduser())


__all__ = ["StatePaths", "state_paths"]
