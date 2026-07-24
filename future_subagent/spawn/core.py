"""Core entry point for sub-agent spawning — orchestrates validation, registration, and async execution.

Pipeline overview:
    1. Input validation (task, agent_id, depth, concurrency, runtime isolation, cwd)
    2. Ownership & capability resolution
    3. Model & thinking plan selection
    4. Thread binding & origin routing
    5. Attachment materialization
    6. Registry registration (with terminal-gen tracking for orphan detection)
    7. Swarm group reservation (if applicable)
    8. System prompt & context assembly
    9. Background task launch via ``_execute_subagent``
   10. Hook dispatch (spawned / progress / ended)
"""

import re
import uuid
import asyncio
from loguru import logger
from typing import Literal
from agent.tools import build_main_tools
from ..config import get_config
from ..types.spawn import SpawnMode, ContextMode
from ..types.capability import SubagentSessionRole
from ..types.registry import SubagentRunRecord, RunOutcome, RunOutcomeStatus, ExecutionStatus
from ..registry import register_run
from ..registry.read import count_active_runs_readonly
from ..registry.reconciliation import resolve_run_orphan_reason
from ..session.cleanup import delete_subagent_session_for_cleanup
from ..capabilities import resolve_subagent_capabilities
from .depth import get_subagent_depth, validate_spawn_depth, validate_concurrent_children
from .target_policy import validate_target_policy
from .plan import resolve_run_timeout_seconds, resolve_model_and_thinking_plan
from .task_name import normalize_subagent_task_name
from .system_prompt import build_subagent_system_prompt
from .initial_message import build_subagent_initial_user_message
from .inherited_tool_policy import apply_tool_policy, DEFAULT_SUBAGENT_BLOCKED_TOOLS
from .context import prepare_spawned_context
from .attachments import materialize_subagent_attachments
from .ownership import resolve_spawn_ownership, SubagentSpawnOwnership
from .accepted_note import resolve_spawn_accepted_note
from .thread_binding import resolve_thread_binding_policy, unbind_thread_on_cleanup, refresh_thread_binding
from .runtime_isolation import resolve_runtime_isolation, validate_runtime_isolation, validate_cwd_restriction
from .origin_routing import resolve_requester_origin_for_child
from .gateway_dispatch import resolve_least_privilege_scopes
from ..swarm.collector import reserve_swarm_run, build_structured_output_prompt
from ..hooks.progress import fire_spawned_hook, fire_progress_hook, fire_ended_hook
# Only alphanumeric, underscore and hyphen allowed — prevents path/injection attacks via agent_id
_VALID_AGENT_ID = re.compile(r"^[a-zA-Z0-9_-]+$")


class SpawnResult:
    """Outcome of a spawn request — accepted, forbidden, or error.

    Attributes:
        status: "accepted" | "forbidden" | "error"
        child_session_key: Session key of the spawned child (set on accepted)
        run_id: Registry run identifier (set on accepted)
        error: Human-readable error message (set on forbidden/error)
        task_name: Normalised task name
        mode: The :class:`SpawnMode` used
        note: Optional human-readable note (e.g. accepted confirmation text)
    """

    def __init__(
        self,
        status: Literal["accepted", "forbidden", "error"],
        child_session_key: str | None = None,
        run_id: str | None = None,
        error: str | None = None,
        task_name: str | None = None,
        mode: SpawnMode | None = None,
        note: str | None = None,
    ):
        self.status = status
        self.child_session_key = child_session_key
        self.run_id = run_id
        self.error = error
        self.task_name = task_name
        self.mode = mode
        self.note = note

    def to_dict(self) -> dict:
        """Serialize the result to a plain dict for API responses."""
        return {
            "status": self.status,
            "child_session_key": self.child_session_key,
            "run_id": self.run_id,
            "error": self.error,
            "task_name": self.task_name,
            "mode": self.mode.value if self.mode else None,
            "note": self.note,
        }


async def spawn_subagent_direct(
    task: str,
    requester_session_key: str,
    agent_id: str = "main",
    task_name: str | None = None,
    label: str | None = None,
    thinking: str | None = None,
    spawn_mode: SpawnMode = SpawnMode.RUN,
    cleanup: Literal["delete", "keep"] = "delete",
    context: ContextMode = ContextMode.ISOLATED,
    attachments: list[dict] | None = None,
    cwd: str | None = None,
    requester_session_id: str | None = None,
    completion_owner_key: str | None = None,
    expects_completion_message: bool = True,
    run_timeout_seconds: float | None = None,
    swarm_group_id: str | None = None,
    output_schema: dict | None = None,
    model: str | None = None,
    launch_fingerprint: str | None = None,
) -> SpawnResult:
    """Validate, register, and launch a sub-agent as a background task.

    Runs through the full spawn pipeline: input validation → depth/concurrency
    checks → ownership resolution → tool policy → registry registration →
    async execution. Returns immediately with an accepted/forbidden/error result.

    Args:
        task: Natural-language task description for the sub-agent.
        requester_session_key: Session key of the parent that is spawning.
        agent_id: Logical agent identifier (must match ``^[a-zA-Z0-9_-]+$``).
        task_name: Short display name; auto-derived from *task* if omitted.
        label: Optional user-facing label.
        thinking: Thinking level override (e.g. "low", "medium", "high").
        spawn_mode: :attr:`SpawnMode.RUN` (fire-and-forget) or :attr:`SpawnMode.SESSION` (persistent).
        cleanup: ``"delete"`` to remove session after completion, ``"keep"`` to retain.
        context: :attr:`ContextMode.ISOLATED` (blank slate) or :attr:`ContextMode.INHERITED` (fork parent).
        attachments: Optional list of attachment dicts to materialise into the child workspace.
        cwd: Working directory for the child; defaults to parent's cwd if ``None``.
        requester_session_id: Optional LangGraph session id of the parent (for context forking).
        completion_owner_key: Override the session that owns the completion callback.
        expects_completion_message: Whether the parent expects a ``sessions_yield`` / ``sessions_send`` on completion.
        run_timeout_seconds: Wall-clock timeout for the sub-agent execution.
        swarm_group_id: If set, register this run into the named swarm group.
        output_schema: JSON Schema dict — child output will be validated against it.
        model: Override the LLM model name for this child.
        launch_fingerprint: Deduplication fingerprint for swarm launches.

    Returns:
        A :class:`SpawnResult` with ``status="accepted"`` on success, or
        ``status="forbidden"`` / ``status="error"`` on failure.
    """
    # --- Phase 1: Input validation ---
    if not task or not task.strip():
        return SpawnResult(status="error", error="Task description is required")

    config = get_config()

    # Reject agent_id values that could enable path traversal or command injection
    if not _VALID_AGENT_ID.match(agent_id):
        return SpawnResult(status="forbidden", error=f"Invalid agent_id: '{agent_id}' (only [a-zA-Z0-9_-] allowed)")

    # When the platform requires explicit agent identification, reject the default "main"
    if config.require_agent_id and agent_id == "main":
        return SpawnResult(status="forbidden", error="agent_id is required (cannot use default 'main')")

    normalized_task_name = normalize_subagent_task_name(task_name)

    # --- Phase 2: Policy & depth/concurrency gate ---
    allowed, reason = validate_target_policy(agent_id, "main")
    if not allowed:
        return SpawnResult(status="forbidden", error=reason)

    parent_depth = get_subagent_depth(requester_session_key)
    child_depth = parent_depth + 1

    allowed, reason = validate_spawn_depth(parent_depth)
    if not allowed:
        return SpawnResult(status="forbidden", error=reason)

    active_count = count_active_runs_readonly(requester_session_key)
    allowed, reason = validate_concurrent_children(active_count)
    if not allowed:
        return SpawnResult(status="forbidden", error=reason)

    # --- Phase 3: Runtime isolation & cwd ---
    isolation_cfg = resolve_runtime_isolation(requester_session_key, agent_id=agent_id, cwd=cwd)
    iso_ok, iso_reason = validate_runtime_isolation(isolation_cfg)
    if not iso_ok:
        return SpawnResult(status="forbidden", error=iso_reason)

    # Inherit the parent's workspace when the caller does not specify one
    if cwd is None:
        from .runtime_isolation import resolve_spawned_workspace_inheritance
        cwd = resolve_spawned_workspace_inheritance(requester_session_key, agent_id)

    # Enforce directory-prefix allowlist from the isolation policy
    if cwd:
        cwd_ok, cwd_reason = validate_cwd_restriction(cwd, isolation_cfg.allowed_cwd_prefixes)
        if not cwd_ok:
            return SpawnResult(status="forbidden", error=cwd_reason)

    # --- Phase 4: Ownership & capability resolution ---
    ownership = resolve_spawn_ownership(
        requester_session_key=requester_session_key,
        completion_owner_key=completion_owner_key,
    )

    # Build a globally-unique session key for the child
    child_session_key = f"agent:{agent_id}:subagent:{uuid.uuid4()}"
    role, _ = resolve_subagent_capabilities(child_depth)

    # --- Phase 5: Model & thinking plan ---
    model_plan = resolve_model_and_thinking_plan(
        model_override=model,
        thinking_override_raw=thinking,
        requester_thinking=None,
        target_agent_thinking=None,
    )

    resolved_model = model_plan.resolved_model
    thinking_resolved = model_plan.thinking_override

    # --- Phase 6: Thread binding & origin routing ---
    child_origin = resolve_requester_origin_for_child(requester_session_key, agent_id=agent_id)
    child_scopes = resolve_least_privilege_scopes(agent_id, role)

    thread_binding = resolve_thread_binding_policy(agent_id, spawn_mode, child_session_key)
    thread_id = thread_binding.thread_id

    # --- Phase 7: Attachment materialization ---
    attachments_dir = None
    attachments_root_dir = None
    attachment_prompt_suffix = ""
    if attachments and config.attachments_enabled:
        from pathlib import Path
        workspace = Path(cwd) if cwd else None
        mat_result = await materialize_subagent_attachments(
            attachments,
            child_workspace=workspace,
            max_files=config.attachments_max_files,
            max_file_bytes=config.attachments_max_file_bytes,
            max_total_bytes=config.attachments_max_total_bytes,
        )
        if mat_result.status == "error":
            await _rollback_spawn(child_session_key, spawn_mode, None, attachments_dir, attachments_root_dir)
            return SpawnResult(status="error", error=mat_result.error)
        if mat_result.status == "ok" and mat_result.abs_dir:
            attachments_dir = mat_result.abs_dir
            attachments_root_dir = mat_result.root_dir
            attachment_prompt_suffix = mat_result.system_prompt_suffix

    # --- Phase 8: Tool policy & registry registration ---
    tool_allow = []
    tool_deny = list(DEFAULT_SUBAGENT_BLOCKED_TOOLS)
    if role == SubagentSessionRole.ORCHESTRATOR:
        # Orchestrators need spawn/yield to manage their own children
        tool_deny = [t for t in tool_deny if t not in ("sessions_spawn", "sessions_yield")]

    run = register_run(
        child_session_key=child_session_key,
        requester_session_key=ownership.completion_requester_session_key,
        task=task,
        task_name=normalized_task_name,
        spawn_mode=spawn_mode,
        cleanup=cleanup,
        context_mode=context,
        agent_id=agent_id,
        thinking=thinking_resolved or thinking,
        depth=child_depth,
        label=label,
        inherited_tool_allow=tool_allow,
        inherited_tool_deny=tool_deny,
        scopes=child_scopes,
        output_schema=output_schema,
        attachments_dir=attachments_dir,
        attachments_root_dir=attachments_root_dir,
        controller_session_key=ownership.controller_session_key,
        completion_owner_session_key=ownership.completion_requester_session_key,
        spawned_by=requester_session_key,
        spawned_cwd=cwd,
        expects_completion_message=expects_completion_message,
        wake_on_descendant_settle=spawn_mode == SpawnMode.RUN,  # RUN mode: auto-resume parent when child tree settles
    )

    # Track the expected terminal generation so orphan detection can notice if the run stalls
    from ..registry.terminal_gen import get_terminal_gen_tracker
    get_terminal_gen_tracker().register_expected(run.run_id, run.generation)

    # --- Phase 9: Swarm group reservation (optional) ---
    if swarm_group_id:
        from ..swarm.collector import configure_swarm_group, get_group_config
        group_cfg = get_group_config(swarm_group_id)
        if group_cfg is None:
            # First run in this group — create the group config on the fly
            from ..types.swarm import SwarmGroupConfig
            configure_swarm_group(SwarmGroupConfig(group_id=swarm_group_id))

        swarm_run = await reserve_swarm_run(swarm_group_id, task, requester_session_key, task_name=normalized_task_name, launch_fingerprint=launch_fingerprint)
        if swarm_run is not None:
            from ..registry.memory import update as update_run
            update_run(run.run_id, swarm_group_id=swarm_group_id, swarm_run_state=swarm_run.swarm_run_state)
            run = get_run(run.run_id) or run  # refresh local reference after in-place update

    # Persist thread-binding metadata onto the run record
    if thread_id:
        from ..registry.memory import update as update_run
        update_run(run.run_id, thread_id=thread_id)

    if child_origin.channel or child_origin.account_id:
        from ..registry.memory import update as update_run
        origin_data = child_origin.model_dump(exclude_none=True)
        update_run(run.run_id, origin_data=origin_data)

    # --- Phase 10: System prompt & context assembly ---
    logger.info(
        "Spawned subagent: run_id={}, child={}, role={}, depth={}, owner={}",
        run.run_id, child_session_key, role, child_depth,
        ownership.completion_requester_display_key,
    )

    system_prompt = build_subagent_system_prompt(
        role=role,
        task=task,
        requester_label=ownership.completion_requester_display_key,
        depth=child_depth,
        max_depth=config.max_spawn_depth,
        child_session_key=child_session_key,
        requester_session_key=ownership.controller_session_key,
        can_spawn=role == SubagentSessionRole.ORCHESTRATOR,
        is_persistent_session=spawn_mode == SpawnMode.SESSION,
    )

    # Append attachment-aware and schema-constraint suffixes to the system prompt
    if attachment_prompt_suffix:
        system_prompt = f"{system_prompt}\n{attachment_prompt_suffix}"

    if output_schema:
        schema_prompt = build_structured_output_prompt(output_schema)
        if schema_prompt:
            system_prompt = f"{system_prompt}\n{schema_prompt}"

    user_message = build_subagent_initial_user_message(
        task,
        depth=child_depth,
        max_depth=config.max_spawn_depth,
        is_persistent_session=spawn_mode == SpawnMode.SESSION,
    )
    forked_messages = await prepare_spawned_context(context, requester_session_id)
    timeout_seconds = resolve_run_timeout_seconds(run_timeout_seconds)

    # Warn if the run already looks orphaned at creation time (e.g. parent disconnected)
    orphan_reason = resolve_run_orphan_reason(run)
    if orphan_reason:
        logger.warning("Spawned run {} appears orphaned at creation: {}", run.run_id, orphan_reason)

    # --- Phase 11: Launch background execution ---
    bg_task = asyncio.create_task(
        _execute_subagent(
            run=run,
            system_prompt=system_prompt,
            user_message=user_message,
            forked_messages=forked_messages,
            tools=build_main_tools(),
            timeout_seconds=timeout_seconds,
            spawn_mode=spawn_mode,
            expects_completion_message=expects_completion_message,
            ownership=ownership,
            model_override=resolved_model,
            output_schema=output_schema,
        )
    )

    from ..registry import register_task
    register_task(run.run_id, bg_task)

    # --- Phase 12: Hook dispatch & return ---
    try:
        await fire_spawned_hook(run)
    except Exception as e:
        logger.debug("fire_spawned_hook error for run {}: {}", run.run_id, e)

    accepted_note = resolve_spawn_accepted_note(spawn_mode, requester_session_key)

    return SpawnResult(
        status="accepted",
        child_session_key=child_session_key,
        run_id=run.run_id,
        task_name=normalized_task_name,
        mode=spawn_mode,
        note=accepted_note,
    )


async def _execute_subagent(
    run: SubagentRunRecord,
    system_prompt: str,
    user_message: str,
    forked_messages: list,
    tools: list | None,
    timeout_seconds: float,
    spawn_mode: SpawnMode = SpawnMode.RUN,
    expects_completion_message: bool = True,
    ownership: SubagentSpawnOwnership | None = None,
    model_override: str | None = None,
    output_schema: dict | None = None,
) -> None:
    """Run a sub-agent to completion, handling timeouts, cancellation, and lifecycle cleanup.

    This is the main execution loop for a spawned sub-agent. It:
      1. Computes the effective tool deny-list from inherited policy + scope gaps.
      2. Builds a child LangGraph agent with filtered tools and role-appropriate LLM.
      3. Invokes the agent with the assembled messages under a wall-clock timeout.
      4. Validates structured output (if an output_schema was provided).
      5. On failure (timeout / kill / error), applies a configurable grace period
         to allow in-flight completion messages to arrive before finalizing.
      6. Always runs lifecycle cleanup (thread unbinding, task removal, run completion).

    Args:
        run: The registry record for this spawn.
        system_prompt: System prompt injected into the child agent.
        user_message: The initial human-message containing the task.
        forked_messages: Prior context messages forked from the parent (may be empty).
        tools: Tool list to make available; ``None`` falls back to :func:`build_main_tools`.
        timeout_seconds: Wall-clock timeout in seconds.
        spawn_mode: :attr:`SpawnMode.RUN` or :attr:`SpawnMode.SESSION`.
        expects_completion_message: Whether the parent awaits a completion callback.
        ownership: Resolved ownership info for completion routing.
        model_override: LLM model name override for this child.
        output_schema: Optional JSON Schema for structured output validation.
    """
    from langchain_core.messages import HumanMessage

    # Compute effective deny-list: start with inherited policy, then add tools
    # whose corresponding scope is absent from the run's granted scopes.
    effective_deny = list(run.inherited_tool_deny)
    if run.scopes:
        scope_tool_map = {
            "subagent:spawn": "sessions_spawn",
            "subagent:kill": "sessions_kill",
            "subagent:yield": "sessions_yield",
            "subagent:send": "sessions_send",
        }
        for scope, tool_name in scope_tool_map.items():
            # If the scope is missing from the run's scopes, deny the corresponding tool
            if scope not in run.scopes and tool_name not in effective_deny:
                effective_deny.append(tool_name)

    # Optimistic default — overridden by the appropriate exception handler below
    result_text: str | None = None
    outcome = RunOutcome(status=RunOutcomeStatus.OK)

    try:
        # Build the child LangGraph agent with scoped tools
        child_agent = await _build_child_agent(
            system_prompt=system_prompt,
            tools=tools,
            tool_allow=run.inherited_tool_allow,
            tool_deny=effective_deny,
            role=run.role,
            model_override=model_override,
        )

        # Assemble the full message list: forked context + the initial user task
        messages = list(forked_messages)
        messages.append(HumanMessage(content=user_message))

        # Build the LangGraph config dict (session, thinking tag, cwd)
        from pub_func import build_agent_config

        agent_config = build_agent_config(session_id=run.child_session_key)
        if run.thinking:
            agent_config["tags"] = agent_config.get("tags", [])
            # LangGraph inspects tags for the "thinking:<level>" pattern
            agent_config["tags"].append(f"thinking:{run.thinking}")
        if run.spawned_cwd:
            agent_config["cwd"] = run.spawned_cwd

        # Invoke the child agent under a wall-clock timeout
        agent_result = await asyncio.wait_for(
            child_agent.ainvoke(
                input={
                    "session_id": run.child_session_key,
                    "messages": messages,
                },
                config=agent_config,
            ),
            timeout=timeout_seconds,
        )

        # Extract the last assistant message as the result text
        if agent_result and "messages" in agent_result:
            last_msg = agent_result["messages"][-1] if agent_result["messages"] else None
            if last_msg and hasattr(last_msg, "content"):
                result_text = last_msg.content if isinstance(last_msg.content, str) else str(last_msg.content)

        # Validate structured output against the provided JSON Schema (swarm mode)
        if output_schema and result_text:
            from ..swarm.collector import validate_structured_output
            valid, err = validate_structured_output(result_text, output_schema)
            if not valid:
                logger.warning("Structured output validation failed for run {}: {}", run.run_id, err)

        if run.thread_binding_info and run.thread_binding_info.thread_id:
            refresh_thread_binding(run.thread_binding_info.thread_id)

    # --- Exception handling: map failures to RunOutcome statuses ---
    except asyncio.TimeoutError:
        outcome = RunOutcome(status=RunOutcomeStatus.TIMEOUT, error=f"Subagent timed out after {timeout_seconds}s")
    except asyncio.CancelledError:
        outcome = RunOutcome(status=RunOutcomeStatus.KILLED, error="Subagent was killed")
    except Exception as e:
        outcome = RunOutcome(status=RunOutcomeStatus.ERROR, error=str(e))

    # --- Lifecycle cleanup (always runs, even on cancellation) ---
    finally:
        from ..registry import remove_task
        remove_task(run.run_id)

        if run.thread_binding_info and run.thread_binding_info.thread_id:
            unbind_thread_on_cleanup(run.thread_binding_info.thread_id)

        try:
            await fire_progress_hook(run, "execution completed")
        except Exception:
            pass

        if outcome.status in (RunOutcomeStatus.ERROR, RunOutcomeStatus.TIMEOUT):
            from ..config import get_config
            grace = get_config().lifecycle_grace_period_seconds
            if grace > 0:
                # Give in-flight completion messages a chance to arrive before finalizing
                logger.info("Lifecycle grace: waiting {:.1f}s before finalizing run {} ({})",
                            grace, run.run_id, outcome.status.value)
                await asyncio.sleep(grace)
                from ..registry import get_run as _get_run
                latest = _get_run(run.run_id)
                if latest and latest.execution.status == ExecutionStatus.TERMINAL:
                    # Run was already completed (e.g. by a yield/settle callback) during grace period
                    logger.info("Lifecycle grace: run {} completed during grace period, skipping finalize", run.run_id)
                    try:
                        await fire_ended_hook(run)
                    except Exception:
                        pass
                    return

        from ..registry.lifecycle import complete_subagent_run
        await complete_subagent_run(run.run_id, outcome, result_text)

        try:
            await fire_ended_hook(run)
        except Exception:
            pass


async def _build_child_agent(
    system_prompt: str,
    tools: list | None,
    tool_allow: list[str],
    tool_deny: list[str],
    role: SubagentSessionRole = SubagentSessionRole.LEAF,
    model_override: str | None = None,
):
    """Construct a LangGraph agent for the child sub-agent with filtered tools and role-appropriate LLM.

    The LLM selection logic is:
      - If *model_override* is provided, attempt to resolve it by name; on failure fall through.
      - ORCHESTRATOR-role children get the main (larger) LLM.
      - LEAF-role children get the auxiliary (smaller / cheaper) LLM.

    Middleware stack (applied in order):
      - **Summarization** — condenses conversation when tokens/messages exceed threshold.
      - **IterationBudget(60)** — caps the agent to 60 reasoning/tool-call iterations.
      - **ToolGuardrails** — validates tool calls before dispatch.
      - **ToolCallNormalize** — normalises tool call format across LLM providers.
      - **HeartbeatStaleness** — detects and recovers from stalled agent loops.

    Args:
        system_prompt: System prompt for the child agent.
        tools: Tool list; ``None`` falls back to :func:`build_main_tools`.
        tool_allow: Explicit allow-list (empty = no restriction beyond deny-list).
        tool_deny: Explicit deny-list (always includes :data:`DEFAULT_SUBAGENT_BLOCKED_TOOLS`).
        role: :class:`SubagentSessionRole` — determines LLM selection.
        model_override: Optional model name string to override the default LLM.

    Returns:
        A fully-constructed LangGraph agent ready for ``ainvoke``.
    """
    from agent.core import StateSchema
    from langchain.agents import create_agent
    from models import build_main_llm, build_auxiliary_llm
    from agent.checkpointer import build_async_sqlite_checkpointer
    from agent.middlewares import IterationBudget, ToolGuardrails, ToolCallNormalize, Summarization, HeartbeatStaleness

    base_tools = tools if tools is not None else build_main_tools()
    filtered_tools = apply_tool_policy(base_tools, tool_allow, tool_deny)

    if model_override:
        try:
            from models import build_llm_by_name
            child_llm = build_llm_by_name(model_override)
        except (ImportError, AttributeError):
            if role == SubagentSessionRole.ORCHESTRATOR:
                child_llm = build_main_llm()
            else:
                child_llm = build_auxiliary_llm()
    elif role == SubagentSessionRole.ORCHESTRATOR:
        child_llm = build_main_llm()
    else:
        child_llm = build_auxiliary_llm()

    child_checkpointer = await build_async_sqlite_checkpointer()
    await child_checkpointer.setup()

    auxiliary_llm = child_llm
    child_agent = create_agent(
        model=child_llm,
        system_prompt=system_prompt,
        state_schema=StateSchema,
        checkpointer=child_checkpointer,
        tools=filtered_tools,
        middleware=[
            Summarization(
                model=auxiliary_llm,
                trigger=[
                    ("fraction", 0.5),
                    ("messages", 40),
                    ("tokens", 30000)
                ],
                keep=("messages", 10),
            ),
            IterationBudget(60),
            ToolGuardrails(),
            ToolCallNormalize(),
            HeartbeatStaleness()
        ],
    )

    return child_agent


async def _rollback_spawn(
    child_session_key: str,
    spawn_mode: SpawnMode,
    run_id: str | None,
    attachments_dir: str | None,
    attachments_root_dir: str | None,
) -> None:
    """Clean up partial spawn state (attachments, session, registry) after a failed spawn.

    Performs three cleanup steps in order:
      1. Remove materialised attachment files from disk.
      2. Delete the child session entry.
      3. Remove the registry run record (if one was created) and fire stop hooks.

    Args:
        child_session_key: Session key of the failed child.
        spawn_mode: The spawn mode that was attempted.
        run_id: Run identifier if registration succeeded before the failure.
        attachments_dir: Absolute path to the attachments directory (if created).
        attachments_root_dir: Root directory under which attachments live.
    """
    if attachments_dir:
        from ..registry.helpers import safe_remove_attachments_dir
        safe_remove_attachments_dir(attachments_dir, attachments_root_dir)

    await delete_subagent_session_for_cleanup(child_session_key, spawn_mode)

    if run_id:
        from ..registry import remove_run as _remove_run
        try:
            await _remove_run(run_id)
        except Exception:
            pass

        from ..hooks.base import fire_stop_hooks
        try:
            await fire_stop_hooks(SubagentRunRecord(
                run_id=run_id,
                child_session_key=child_session_key,
                requester_session_key="",
                task="",
            ))
        except Exception:
            pass
