# ruff: noqa: F401 — the facade intentionally re-exports the historical names.
"""Backward-compatible facade for the split background-review modules.

The seven jobs that used to live in this one file now have dedicated sibling
modules. This facade re-exports every symbol (public and private) so existing
imports keep working, and installs a small module proxy that forwards attribute
ASSIGNMENTS to every implementation module defining the same name. That preserves
the historical monkeypatch seam (``background_reviews._create_... = fake``) used
by the background-review test suites without keeping the implementation here.
"""

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, override

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from loguru import logger

from config.features import MEMORY_TOOL, PTC, BACKGROUND_REVIEW_CONFIG, SUMMARIZATION
from config.path import resolve_plan_path
from pub.func import sanitize_tool_use_result_pairing
from pub.func.message.system_reminder import is_system_reminder
from pub.func.message.workspace_notice import is_workspace_notice
from runtime import StateKey, state_register_db, state_register_mem
from runtime.lane import LaneType, lane_slot

from .skill_review import (
    changed_skill_paths,
    judge_skill_update,
    restore_auto_skills,
    snapshot_auto_skills,
)

from ._common import (
    _msg_to_text,
    _msg_type,
    _render_discarded_text,
    _render_facts_context,
    _render_prompt_facts,
)
from .background_scaffold import (
    StateSchema,
    _BACKGROUND_REVIEW_MEMORY_LOCK_KEY,
    _BACKGROUND_REVIEW_TASKS,
    _BackgroundReviewLimitTool,
    _SKILL_REVIEW_LOCK_KEY,
    _background_reviews_locked,
    _build_session_model,
    _create_background_review_agent,
    _get_iteration_budget,
    _session_tool_face,
)
from .memory_review import (
    _MEMORY_CONSOLIDATION_PROMPT,
    _MEMORY_REVIEW_PROMPT,
    _build_memory_review_agent,
    _cache_hit_tokens,
    _create_memory_review_agent,
    _execute_memory_review,
    _format_entry_created,
    _invoke_memory_review,
    _memory_only_tool_face,
    _memory_review_messages,
    _render_memory_entries_for_consolidation,
    _run_memory_review,
    run_memory_consolidation,
    schedule_memory_review,
)
from .session_end_salvage import (
    _SESSION_END_REVIEW_FIRST_PROMPT,
    _SESSION_END_REVIEW_INFLIGHT,
    _SESSION_END_REVIEW_REST_PROMPT,
    _SESSION_END_REVIEW_SESSION_SUFFIX,
    _SESSION_END_REVIEW_TASKS,
    _SESSION_END_SNAPSHOT_TIMEOUT_S,
    _rebuild_session_end_prompt,
    _run_session_end_salvage,
    _run_session_end_wave,
    _session_end_cutoff,
    _snapshot_session_for_salvage,
    salvage_session_memory,
)
from .handoff_tree import (
    _MAX_SUBAGENT_RESULT_CHARS,
    _PLAN_CONTEXT_LIST_CAP,
    _PLAN_REF_STATE_KEY,
    _RELEVANCE_ACTIVE,
    _RELEVANCE_CANCELLED,
    _RELEVANCE_SUPERSEDED,
    _RELEVANCE_UNKNOWN,
    _aggregate_handoffs,
    _assemble_handoff_tree,
    _build_plan_context,
    _classify_run_relevance,
    _collect_skill_review_feedback,
    _compact_aggregate,
    _filter_runs_by_snapshot,
    _handoff_label,
    _loaded_skill_details,
    _read_flow_status,
    _read_plan_snapshot,
    _read_subagent_runs,
    _recent_usage_skill_names,
    _resolve_plan_ref,
    _resolve_position,
    _row_skill_name,
    _row_text,
    _walk_handoff,
)
from .ptc_compile import (
    _compile_ptc_scripts,
    _owning_skill_after_review,
    _ptc_tool_signatures,
    _review_skill_names,
    _step_task,
)
from .skill_review_runner import (
    _GeneralizabilityVerdict,
    _SKILL_REVIEW_ENABLED,
    _SKILL_REVIEW_FAILED_AT_KEY,
    _SKILL_REVIEW_FAILED_HASH_KEY,
    _SKILL_REVIEW_FIRED_KEY,
    _SKILL_REVIEW_PROMPT,
    _SKILL_REVIEW_SNAPSHOT_HASH_KEY,
    _SKILL_REVIEW_TOOL_NAMES,
    _clear_skill_review_failed,
    _create_skill_review_agent,
    _detect_todo_all_complete,
    _extract_no_op_reason,
    _judge_task_generalizability,
    _last_ai_text,
    _mark_skill_review_failed,
    _plan_snapshot_hash,
    _run_skill_review,
    _skill_review_failure_active,
    _skill_review_tool_face,
    _write_handoff_tree_safely,
    maybe_trigger_skill_review,
)
from .compression_todo import (
    _COMPRESSION_TODO_CONTEXT_TEMPLATE,
    _COMPRESSION_TODO_LOCK_KEY,
    _COMPRESSION_TODO_METADATA_KEY,
    _COMPRESSION_TODO_PROMPT,
    _COMPRESSION_TODO_SESSION_SUFFIX,
    _COMPRESSION_TODO_TASKS,
    _build_main_session_todowrite,
    schedule_compression_todo_update,
    update_todos_from_compaction,
)

import sys as _sys
import types as _types

from . import (
    _common as _common_mod,
    background_scaffold as _scaffold_mod,
    compression_todo as _compression_todo_mod,
    handoff_tree as _handoff_tree_mod,
    memory_review as _memory_review_mod,
    ptc_compile as _ptc_compile_mod,
    session_end_salvage as _salvage_mod,
    skill_review_runner as _skill_runner_mod,
)

_OWNER_MODULES = (
    _common_mod,
    _scaffold_mod,
    _memory_review_mod,
    _salvage_mod,
    _handoff_tree_mod,
    _ptc_compile_mod,
    _skill_runner_mod,
    _compression_todo_mod,
)
#: Names that belong to the facade itself, never forwarded.
_SHIM_INTERNALS = frozenset({"__all__", "_OWNER_MODULES", "_SHIM_INTERNALS"})


class _CompatModule(_types.ModuleType):
    """A module whose attribute writes are visible to the implementation modules.

    Tests patch internals through this facade (``background_reviews._create_...``);
    the implementations look names up in their own module dicts, so a write here
    must land in every module that defines the name — including modules that
    imported an alias at module load time.
    """

    def __setattr__(self, name: str, value: object) -> None:
        _types.ModuleType.__setattr__(self, name, value)
        if name.startswith("__") or name in _SHIM_INTERNALS:
            return
        for module in _OWNER_MODULES:
            if name in module.__dict__:
                _types.ModuleType.__setattr__(module, name, value)

    def __delattr__(self, name: str) -> None:
        _types.ModuleType.__delattr__(self, name)
        if name.startswith("__") or name in _SHIM_INTERNALS:
            return
        for module in _OWNER_MODULES:
            if name in module.__dict__:
                _types.ModuleType.__delattr__(module, name)


_sys.modules[__name__].__class__ = _CompatModule
