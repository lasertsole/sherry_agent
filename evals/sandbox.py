"""Cross-platform eval sandbox.

Redirects every persistence backend of the agent runtime into a per-run
directory and strips the tool surface available to spawned children, so eval
runs never touch the real repo DBs or the real workspace.

Platform compatibility (Windows / macOS / Linux):
- Persistence redirection uses pathlib + temp dirs only — no shell, no
  symlinks, no POSIX-only calls. Every redirected symbol is read at call time
  by its owning module, so patching after import is effective.
- Child tool restriction is a name allowlist applied by patching
  ``agent.tools.build_main_tools``; both spawn call sites import it lazily at
  call time, so the patch is honored for every spawned child.
- ``terminal`` / ``python_repl`` are excluded from the allowlist entirely.
  Should a future suite need them, the project's own OS sandbox applies via
  ``SANDBOX_POLICY`` (bwrap on Linux, seatbelt on macOS, silent degrade on
  Windows — see agent/tools/pub_base/sandbox.py).
"""

from __future__ import annotations

import asyncio
import importlib
import os
import platform
from pathlib import Path
from typing import Any

SAFE_TOOL_NAMES = frozenset({"read_file", "search_files", "question"})

_STORE_MODULES: dict[str, str] = {
    "agent.tools.taskflow.registry.store_sqlite": "taskflow_registry.db",
    "agent.tools.subagent.registry.store_sqlite": "subagent_registry.db",
    "agent.tools.todolist.registry.store_sqlite": "todos.db",
}


class EvalSandbox:
    """Redirect runtime persistence + child tool policy into *results_dir*."""

    def __init__(self, results_dir: Path) -> None:
        self.root = results_dir / "sandbox"
        self.workspace = self.root / "workspace"
        self.src = self.root / "src"
        self.auto_skills_dir = self.root / "skills" / "auto"
        self.knowledge_plans_dir = self.workspace / "knowledge" / "plans"
        self._originals: dict[object, dict[str, Any]] = {}
        self._applied = False

    def _set(self, module: object, attr: str, value: Any) -> None:
        self._originals.setdefault(module, {})[attr] = getattr(module, attr, None)
        setattr(module, attr, value)

    def _mkdir_parents(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)

    def _redirect_store(self, module_name: str, db_filename: str) -> None:
        module = importlib.import_module(module_name)
        db_dir = self.root / "stores" / module_name.rsplit(".", 1)[-1]
        db_dir.mkdir(parents=True, exist_ok=True)
        self._set(module, "_DB_DIR", db_dir)
        self._set(module, "_DB_PATH", db_dir / db_filename)
        self._set(module, "_initialized", False)
        self._set(module, "_init_loop", None)
        self._set(module, "_init_lock", asyncio.Lock())
        self._set(module, "_sync_tables_ready", False)

        pending = importlib.import_module("agent.tools.subagent.registry.pending_injections")
        pending_db = self.root / "stores" / "subagent_registry.db"
        self._mkdir_parents(pending_db)
        self._set(pending, "_DB_PATH", pending_db)

    def _restrict_child_tools(self) -> None:
        agent_tools = importlib.import_module("agent.tools")
        original = agent_tools.build_main_tools
        cache: list | None = None

        def safe_build_main_tools() -> list:
            nonlocal cache
            if cache is None:
                cache = [
                    tool for tool in original() if getattr(tool, "name", "") in SAFE_TOOL_NAMES
                ]
            return list(cache)

        self._set(agent_tools, "build_main_tools", safe_build_main_tools)

    def _redirect_skills_and_knowledge(self) -> None:
        """Redirect auto-skill + plan-knowledge writes into the sandbox.

        Additive isolation for suites that exercise the plan-extraction pass.
        ``skill_manage`` and the skill-usage / skill-utils helpers bind
        ``config.AUTO_SKILLS_DIR`` at import time, and the plan-knowledge store
        binds ``config.path.PLAN_KNOWLEDGE_DIR``; each owning module is patched
        directly so every write lands under this run's directory.

        ``build_skills_snapshot`` is neutralized because it rewrites the tracked
        ``skills/skills_snapshot.json`` in the real repo (the loader keeps
        reading the real skill roots); the snapshot is irrelevant to evals.
        """
        self.auto_skills_dir.mkdir(parents=True, exist_ok=True)
        for module_name in (
            "agent.tools.skill_tools.skill_manage",
            "agent.tools.pub_base.skill_usage",
            "agent.tools.pub_base.skill_utils",
        ):
            try:
                module = importlib.import_module(module_name)
            except ImportError:
                continue
            if hasattr(module, "AUTO_SKILLS_DIR"):
                self._set(module, "AUTO_SKILLS_DIR", self.auto_skills_dir)

        skills_pkg = importlib.import_module("skills")
        self._set(skills_pkg, "build_skills_snapshot", lambda: None)

        self.knowledge_plans_dir.mkdir(parents=True, exist_ok=True)
        knowledge_store = importlib.import_module("agent.tools.todolist.knowledge.knowledge_store")
        self._set(knowledge_store, "_KNOWLEDGE_ROOT", self.knowledge_plans_dir)
        config_path = importlib.import_module("config.path")
        self._set(config_path, "PLAN_KNOWLEDGE_DIR", self.knowledge_plans_dir)

    def apply(self) -> None:
        """Apply every redirection. Idempotent within one instance."""
        if self._applied:
            return
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.src.mkdir(parents=True, exist_ok=True)

        for module_name, db_filename in _STORE_MODULES.items():
            self._redirect_store(module_name, db_filename)

        mes_db = importlib.import_module("context_engine.store.db")
        mes_memory_dir = self.src / "store" / "mes_memory"
        mes_memory_dir.mkdir(parents=True, exist_ok=True)
        self._set(mes_db, "_db_path", mes_memory_dir / "mes_memory.db")
        self._set(mes_db, "_db", None)
        mes_core = importlib.import_module("context_engine.store.core")
        self._set(mes_core, "_db", mes_db.get_db())

        for module_name in (
            "agent.checkpointer.async_sqlite_checkpointer",
            "agent.checkpointer.thread_safe_checkpointer",
        ):
            checkpoint_dir = self.src / "checkpoints"
            checkpoint_dir.mkdir(parents=True, exist_ok=True)
            self._set(importlib.import_module(module_name), "SRC_DIR", checkpoint_dir.parent)

        state_register = importlib.import_module("runtime.state_register")
        state_db = self.src / "data" / "state_register.db"
        state_db.parent.mkdir(parents=True, exist_ok=True)
        self._set(state_register.state_register_db, "db_path", state_db)
        # The singleton created its schema in the original DB at import; the
        # redirected file starts empty, so re-run the idempotent init here.
        state_register.state_register_db._init_db()

        path_utils = importlib.import_module("agent.tools.pub_base.path_utils")
        self._set(path_utils, "ROOT_DIR", self.workspace)

        memory = importlib.import_module("agent.tools.memory")
        memory_dir = self.root / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        self._set(memory, "MEMORY_DIR", memory_dir)
        tiered = importlib.import_module("agent.tools.memory_tiered")
        facts_dir = self.root / "memory" / "facts"
        facts_dir.mkdir(parents=True, exist_ok=True)
        self._set(tiered, "FACTS_DIR", facts_dir)
        self._set(tiered, "tiered_store", None)

        drain = importlib.import_module("agent.middlewares.subagent_completion_drain")

        async def _noop_backflow(*args: object, **kwargs: object) -> None:
            return None

        self._set(drain, "_backflow_shared_memory", _noop_backflow)
        self._restrict_child_tools()
        self._redirect_skills_and_knowledge()

        self._set(os, "environ", {**os.environ})
        os.environ["SANDBOX_POLICY"] = "auto" if platform.system() == "Windows" else "required"
        self._applied = True

    def restore(self) -> None:
        """Restore every patched symbol to its pre-sandbox value."""
        for module, attrs in self._originals.items():
            for attr, value in attrs.items():
                try:
                    setattr(module, attr, value)
                except (AttributeError, TypeError):
                    pass
        self._applied = False
