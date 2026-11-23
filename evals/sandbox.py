"""Cross-platform eval sandbox.

Redirects every persistence backend of the agent runtime into a per-run
directory, so eval runs never touch the real repo DBs or the real workspace.

Spawned children keep the FULL tool surface by default: agentic coding evals
must be able to delegate implementation to a child, and the official external
harness imposes no tool interface at all. ``restrict_child_tools=True`` is the
opt-in isolation mode for suites that must not let a child mutate anything.

Platform compatibility (Windows / macOS / Linux):
- Persistence redirection uses pathlib + temp dirs only — no shell, no
  symlinks, no POSIX-only calls. Every redirected symbol is read at call time
  by its owning module, so patching after import is effective.
- The opt-in child allowlist is applied by patching
  ``agent.tools.build_main_tools``; both spawn call sites import it lazily at
  call time, so the patch is honored for every spawned child.
- In isolation mode ``terminal`` / ``python_repl`` are excluded from the
  allowlist entirely. Should a suite need isolation *and* those tools, the
  project's own OS sandbox applies via ``SANDBOX_POLICY`` (bwrap on Linux,
  seatbelt on macOS, silent degrade on Windows — see
  agent/tools/pub_base/sandbox.py).
"""

from __future__ import annotations

import asyncio
import importlib
import os
import platform
import threading
from pathlib import Path
from typing import Any

import aiosqlite
from loguru import logger

SAFE_TOOL_NAMES = frozenset({"read_file", "search_files", "question"})

_STORE_MODULES: tuple[str, ...] = (
    "agent.tools.taskflow.registry.store_sqlite",
    "agent.tools.subagent.registry.store_sqlite",
    "agent.tools.todolist.registry.store_sqlite",
)


class EvalSandbox:
    """Redirect runtime persistence + (optionally) child tool policy."""

    def __init__(self, results_dir: Path, *, restrict_child_tools: bool = False) -> None:
        self.root = results_dir / "sandbox"
        self.workspace = self.root / "workspace"
        self.src = self.root / "src"
        self.auto_skills_dir = self.root / "skills" / "auto"
        self.knowledge_plans_dir = self.workspace / "knowledge" / "plans"
        self.restrict_child_tools = restrict_child_tools
        self._originals: dict[object, dict[str, Any]] = {}
        self._applied = False
        self._opened_connections: list[aiosqlite.Connection] = []
        #: Process-global eval overrides captured so consecutive suites in one
        #: interpreter cannot inherit a previous suite's settings.
        self._original_run_timeout: float | None = None

    def _set(self, module: object, attr: str, value: Any) -> None:
        # Capture the FIRST pre-sandbox value only. Several stores share the
        # same module (``pending_injections`` is patched once per registry
        # redirect); overwriting the snapshot on a later pass would make
        # restore() put the sandbox path back instead of the real DB path.
        attrs = self._originals.setdefault(module, {})
        if attr not in attrs:
            attrs[attr] = getattr(module, attr, None)
        setattr(module, attr, value)

    def _mkdir_parents(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)

    def _redirect_store(self, module_name: str, registry: Path) -> None:
        module = importlib.import_module(module_name)
        self._mkdir_parents(registry)
        self._set(module, "_DB_DIR", registry.parent)
        self._set(module, "_DB_PATH", registry)
        self._set(module, "_initialized", False)
        self._set(module, "_init_loop", None)
        self._set(module, "_init_lock", asyncio.Lock())
        self._set(module, "_sync_tables_ready", False)

        pending = importlib.import_module("agent.tools.subagent.registry.pending_injections")
        self._set(pending, "_DB_DIR", registry.parent)
        self._set(pending, "_DB_PATH", registry)
        queue = importlib.import_module("server.queue.user_input_queue")
        self._set(queue, "_DB_PATH", registry)
        ledger = importlib.import_module("agent.tools.pub_base.evidence_ledger")
        self._set(ledger.EvidenceLedger, "LEDGER_PATH", str(registry))

    def _restrict_child_tools(self) -> None:
        """Apply the opt-in read-only allowlist to spawned children."""
        if not self.restrict_child_tools:
            return
        # NOTE: this patches the SHARED builder, so a suite must import
        # agent.core BEFORE apply() — that binds the real builder at module
        # load, while the lazy imports in the spawn/steer paths see this patch.
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

        Additive isolation for suites that exercise the skill review.
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
            "agent.middlewares.summarization.skill_review",
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
        self._track_aiosqlite_connections()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.src.mkdir(parents=True, exist_ok=True)

        registry = self.root / "stores" / "registry.db"
        for module_name in _STORE_MODULES:
            self._redirect_store(module_name, registry)

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

        state_register = importlib.import_module("runtime.session.state_register")
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

        self._restrict_child_tools()
        self._redirect_skills_and_knowledge()

        # deepswe's _eval_child_run_timeout_s() raises the process-wide
        # SubagentConfig.run_timeout_seconds. Snapshot it here so restore()
        # returns the product default rather than leaking into the next suite.
        from agent.tools.subagent.config import get_config

        self._original_run_timeout = get_config().run_timeout_seconds

        # The subagent-completion backflow is NOT server-dependent: it just
        # reloads the shared memory store, whose DB path was redirected above
        # (``memory.MEMORY_DIR``/``context_engine.store.db._db``). No-op'ing it
        # made a child's memory writes invisible to the parent turn. Leave the
        # production behavior intact.

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
        if self._original_run_timeout is not None:
            from agent.tools.subagent.config import get_config

            get_config().run_timeout_seconds = self._original_run_timeout
            self._original_run_timeout = None
        # Undo any evals-side iteration-budget override (also process-global).
        from evals.shared.eval_iteration_budget import restore_eval_budget

        restore_eval_budget()
        self._applied = False

    def _track_aiosqlite_connections(self) -> None:
        recorded: list[aiosqlite.Connection] = []
        real_connect = aiosqlite.connect

        def _recording_connect(*args: Any, **kwargs: Any) -> aiosqlite.Connection:
            conn = real_connect(*args, **kwargs)
            recorded.append(conn)
            return conn

        self._opened_connections = recorded
        self._set(aiosqlite, "connect", _recording_connect)

    def close_aiosqlite_connections(self) -> int:
        """Close every aiosqlite connection opened while the sandbox was applied.

        Spawned children leak checkpointer connections that production code
        does not close (#26); each one parks a non-daemon worker thread that
        blocks interpreter shutdown — the reason the suites used os._exit().
        Recording at aiosqlite.connect catches every connection regardless of
        origin (same technique as tests/conftest.py).

        ``conn.close()`` needs an event loop for its completion future, which
        the connection's own worker thread resolves. Most suites call this
        after ``asyncio.run()`` has returned, but several (DeepSWE, devbench,
        …) call it from inside an async ``finally``, where ``asyncio.run()``
        raises "cannot be called from a running event loop" for EVERY
        connection — leaving all worker threads alive and the process hanging
        in ``threading._shutdown`` long after the report is written. Detect
        that case and run the closes on a short-lived helper thread with its
        own loop; the future is created on that loop, so the worker threads
        can still resolve it.
        """
        conns = list(self._opened_connections)
        self._opened_connections.clear()
        if not conns:
            return 0

        def _close_all() -> tuple[int, list[BaseException]]:
            closed = 0
            errors: list[BaseException] = []

            async def _do() -> None:
                nonlocal closed
                for conn in conns:
                    try:
                        await conn.close()
                        closed += 1
                    except Exception as exc:  # noqa: BLE001 — teardown never masks results
                        errors.append(exc)

            try:
                asyncio.run(_do())
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            return closed, errors

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            closed, errors = _close_all()
        else:
            box: dict[str, tuple[int, list[BaseException]]] = {}

            def _worker() -> None:
                box["result"] = _close_all()

            thread = threading.Thread(target=_worker, daemon=True, name="eval-aiosqlite-close")
            thread.start()
            thread.join(timeout=30)
            if "result" not in box:
                logger.debug("eval teardown: aiosqlite close timed out")
                return 0
            closed, errors = box["result"]

        for exc in errors:
            logger.debug(
                "eval teardown: aiosqlite close skipped",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
        return closed
