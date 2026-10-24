"""Boot-time cleanup of sub-agent runs whose root session is gone.

``init_registry()`` restores runs before the server serves, and the normal
sweeper *recovers* live runs. This sweep is the deletion counterpart: a run
whose requester chain no longer reaches a known main session (the user deleted
the main session, or a parent run record was lost) is an orphan, and its whole
subtree is removed in one pass.

Conservative by design: a run whose root main session still exists — even a
terminal one — is history and is left alone. Boot has no active turns, so no
kill/wait dance is needed. Failures never block startup.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from loguru import logger

from agent.tools.subagent.types.registry import SubagentRunRecord

__all__ = ["sweep_orphaned_side_chats", "sweep_orphaned_subagent_sessions"]


def _known_main_sessions() -> set[str] | None:
    """Bare ids that still look like live/enumerated MAIN sessions.

    ``None`` means the authoritative message-store enumeration failed: the
    sweep must then do nothing, because treating every run as orphan would
    delete healthy sessions.
    """
    known: set[str] = set()
    try:
        from context_engine import get_session_ids

        for row in get_session_ids():
            sid = str(row.get("session_id") or "")
            if sid and not sid.startswith("agent:"):
                known.add(sid)
    except Exception:
        logger.warning(
            "orphan-session sweep: message-store enumeration failed; skipping sweep",
            exc_info=True,
        )
        return None

    try:
        from runtime import state_register_db
        from agent.tools.subagent.registry.session_keys import normalize_session_key

        for raw in state_register_db.get_all_session_ids():
            sid = normalize_session_key(str(raw))
            if sid and not sid.startswith("agent:"):
                known.add(sid)
    except Exception:
        logger.debug("orphan-session sweep: state-register enumeration failed", exc_info=True)

    # A directory alone is NOT proof of a live main session: a crash between
    # the row purge and the folder removal leaves exactly that shell, and the
    # whole point of this sweep is to reclaim it (with its orphan descendants).
    return known


def _root_main_session(
    run: SubagentRunRecord, by_child: Mapping[str, SubagentRunRecord]
) -> str | None:
    """Resolve a run's root main session id, or None when the chain is broken."""
    from agent.tools.subagent.registry.session_keys import (
        SESSION_KEY_PREFIX,
        normalize_session_key,
    )

    key = run.requester_session_key
    seen: set[str] = set()
    while True:
        if key in by_child:
            if key in seen:
                return None  # cycle: treat as orphan
            seen.add(key)
            key = by_child[key].requester_session_key
            continue
        if key.startswith(SESSION_KEY_PREFIX):
            return normalize_session_key(key)
        return None


async def sweep_orphaned_subagent_sessions() -> dict[str, int]:
    """Delete every run subtree whose root main session no longer exists.

    Returns counts for the boot log. Never raises: an unreadable registry is
    simply "nothing to sweep".
    """
    try:
        from agent.tools.subagent.registry import all_runs, remove_run
        from agent.tools.subagent.registry.helpers import safe_remove_attachments_dir
        from server.service.session_cleanup_service import purge_session_data

        runs = list(all_runs())
        by_child = {run.child_session_key: run for run in runs}
        known = _known_main_sessions()
        if known is None:
            return dict(orphan_runs=0, checkpoint_threads=0, registry_rows=0, session_dirs=0)
        orphan_main: set[str] = set()
        orphan_runs: list = []
        for run in runs:
            root = _root_main_session(run, by_child)
            if root is None or root not in known:
                orphan_runs.append(run)
                if root:
                    orphan_main.add(root)

        # Deduplicate by child session key; a run can only appear once, but a
        # duplicate child key would otherwise purge twice.
        seen: set[str] = set()
        unique_runs = []
        for run in orphan_runs:
            if run.child_session_key in seen:
                continue
            seen.add(run.child_session_key)
            unique_runs.append(run)

        checkpoint_threads = 0
        registry_rows = 0
        for run in unique_runs:
            try:
                await purge_session_data(run.child_session_key)
                await remove_run(run.run_id)
                if run.attachments_dir:
                    safe_remove_attachments_dir(run.attachments_dir, run.attachments_root_dir)
                checkpoint_threads += 1
                registry_rows += 1
            except Exception:
                logger.warning(
                    "orphan-session sweep: failed to purge {}", run.run_id, exc_info=True
                )

        # The root main session's own leftovers (checkpoint/state/folder) are
        # gone from every normal store; remove the remaining shell too.
        for root in orphan_main:
            try:
                await purge_session_data(root)
            except Exception:
                logger.warning("orphan-session sweep: failed to purge root {}", root, exc_info=True)

        # Evidence rows are keyed by MAIN and CHILD session keys. The per-run
        # purges above cover the orphan subtree; this pass catches rows whose
        # session was deleted through another path. Known = main sessions plus
        # every run still in the registry (including history).
        try:
            from config.features import EVIDENCE_LEDGER

            if EVIDENCE_LEDGER["purge_on_boot_sweep"]:
                from agent.tools.pub_base.evidence_ledger import EvidenceLedger
                from agent.tools.subagent.registry.session_keys import (
                    normalize_session_key,
                )

                known_evidence = set(known)
                for run in runs:
                    known_evidence.add(run.child_session_key)
                    known_evidence.add(normalize_session_key(run.child_session_key))
                removed = await asyncio.to_thread(EvidenceLedger.purge_orphans, known_evidence)
                if removed:
                    logger.info("Boot: purged {} orphan evidence row(s)", removed)
        except Exception:
            logger.warning("orphan-session sweep: evidence purge failed", exc_info=True)

        result = dict(
            orphan_runs=len(unique_runs),
            checkpoint_threads=checkpoint_threads,
            registry_rows=registry_rows,
            session_dirs=len(orphan_main),
        )
        if unique_runs:
            logger.info(
                "Boot: purged {} orphan subagent run(s), {} checkpoint thread(s), "
                "{} root session(s)",
                result["orphan_runs"],
                result["checkpoint_threads"],
                result["session_dirs"],
            )
        return result
    except Exception:
        logger.warning("orphan-session sweep failed", exc_info=True)
        return dict(orphan_runs=0, checkpoint_threads=0, registry_rows=0, session_dirs=0)


async def sweep_orphaned_side_chats() -> dict[str, int]:
    """Purge side chats whose parent session no longer exists.

    Side chats are hidden from the normal session list, so nothing else would
    notice that their parent is gone. A child with no messages yet is still
    kept while its parent exists (the user may not have asked anything yet).

    Never raises: an unreadable registry is "nothing to sweep".
    """
    try:
        from server.DAO import side_chat_store
        from server.service.side_chat_service import delete_side_chat

        known = _known_main_sessions()
        if known is None:
            return dict(orphan_side_chats=0)
        purged = 0
        for row in side_chat_store.all_side_chats():
            child = str(row.get("child_session_id") or "")
            parent = str(row.get("parent_session_id") or "")
            if not child or not parent or parent in known:
                continue
            try:
                if await delete_side_chat(child):
                    purged += 1
            except Exception:  # noqa: BLE001 - one orphan must not stop the sweep
                logger.warning(
                    "orphan-session sweep: failed to purge side chat {}", child, exc_info=True
                )
        if purged:
            logger.info("Boot: purged {} orphan side-chat session(s)", purged)
        return dict(orphan_side_chats=purged)
    except Exception:
        logger.warning("orphan-session sweep: side-chat sweep failed", exc_info=True)
        return dict(orphan_side_chats=0)
