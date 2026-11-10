"""Core entry point for sub-agent spawning — orchestrates validation, registration, and async execution.

Pipeline overview:
    1. Input validation (task, agent_id, depth, concurrency, runtime isolation, cwd)
    2. Ownership & capability resolution
    3. Model & thinking plan selection
    4. Origin routing
    5. Attachment materialization
    6. Registry registration (with terminal-gen tracking for orphan detection)
    7. Swarm group reservation (if applicable)
    8. System prompt & context assembly
    9. Background task launch via ``_execute_subagent_with_lane`` (SUBAGENT lane queue)
   10. Hook dispatch (spawned / progress / ended)

This module is the STABLE FACADE: it keeps :func:`spawn_subagent_direct` /
:func:`_spawn_subagent` and re-exports the pieces the split moved out (see
:mod:`.validation`, :mod:`.isolation`, :mod:`.child_agent_factory`,
:mod:`.ptc_prefill`, :mod:`.handoff`, :mod:`.lifecycle`) so existing imports keep
working. The execution entry points here also resolve the collaborators from
THIS module's globals, so a monkeypatched ``spawn.core._build_child_agent`` (or
``_execute_subagent``) still redirects a run exactly as it did before the split.
"""

import asyncio
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from config.features import LANE_SYSTEM as LANE_SYSTEM
from ..config import get_config
from ..types.capability import SubagentSessionRole as SubagentSessionRole
from ..types.functional_role import FunctionalRole as FunctionalRole
from ..types.registry import (
    ExecutionStatus as ExecutionStatus,
    RunOutcome as RunOutcome,
    RunOutcomeStatus as RunOutcomeStatus,
    SubagentRunRecord as SubagentRunRecord,
)
from ..roles import RoleDefinition as RoleDefinition
from ..roles import load_role_definition
from ..registry import register_run as register_run
from ..registry import get_run as get_run
from ..registry import mark_run_running as mark_run_running
from ..registry.reconciliation import resolve_run_orphan_reason
from ..capabilities import resolve_subagent_capabilities
from ..output_caps import resolve_child_max_output_tokens, resolve_subagent_output_caps
from ..hooks.progress import fire_ended_hook as fire_ended_hook
from ..hooks.progress import fire_progress_hook as fire_progress_hook
from .depth import (
    get_subagent_depth as get_subagent_depth,
    validate_spawn_depth as validate_spawn_depth,
    validate_concurrent_children as validate_concurrent_children,
    validate_total_spawns as validate_total_spawns,
)
from .target_policy import validate_target_policy as validate_target_policy
from .plan import (
    resolve_model_and_thinking_plan,
    resolve_role_model_profile,
    resolve_run_timeout_seconds,
)
from .task_name import normalize_subagent_task_name
from .request import SpawnRequest
from .system_prompt import SubagentPromptContext, build_subagent_system_prompt_from_context
from .initial_message import build_subagent_initial_user_message
from .inherited_tool_policy import apply_tool_policy as apply_tool_policy
from .attachments import materialize_subagent_attachments
from .ownership import resolve_spawn_ownership
from .accepted_note import resolve_spawn_accepted_note
from .runtime_isolation import (
    resolve_runtime_isolation,
    validate_runtime_isolation,
    validate_cwd_restriction,
)
from .origin_routing import resolve_requester_origin_for_child
from .gateway_dispatch import resolve_least_privilege_scopes
from ..swarm.collector import reserve_swarm_run, build_structured_output_prompt
from ..hooks.progress import fire_spawned_hook
from .handoff import (
    _HANDOFF_FIELDS as _HANDOFF_FIELDS,
    _HANDOFF_JSON_BLOCK as _HANDOFF_JSON_BLOCK,
    _normalise_handoff as _normalise_handoff,
    _extract_handoff_json as _extract_handoff_json,
    _extract_result_text as _extract_result_text,
)
from .validation import (
    _VALID_AGENT_ID as _VALID_AGENT_ID,
    _SPAWN_CONTROL_TOOLS as _SPAWN_CONTROL_TOOLS,
    SpawnResult,
    _validate_spawn_request,
    _resolve_functional_role,
    _check_role_containment,
    _resolve_child_tool_policy,
)
from .isolation import (
    _ISOLATION_PROMPT_NOTE,
    _is_inside_isolated_worktree as _is_inside_isolated_worktree,
    _create_isolated_child_workspace,
)
from .child_agent_factory import (
    _STRUCTURED_OUTPUT_REJECTION_MARKERS as _STRUCTURED_OUTPUT_REJECTION_MARKERS,
    _is_structured_output_rejection as _is_structured_output_rejection,
    _effective_child_deny as _effective_child_deny,
    _bind_child_run_context as _bind_child_run_context,
    _effective_child_summary_tokens as _effective_child_summary_tokens,
    _compression_threshold_for_children as _compression_threshold_for_children,
    _build_child_middlewares as _build_child_middlewares,
    _assert_child_token_floor as _assert_child_token_floor,
    _assemble_child_tools as _assemble_child_tools,
    _select_child_llm as _select_child_llm,
    _child_model_identity as _child_model_identity,
    _build_child_agent as _build_child_agent,
)
from . import ptc_prefill as _ptc_prefill_mod
from .ptc_prefill import (
    _ptc_preview as _ptc_preview,
    _render_ptc_progress as _render_ptc_progress,
    _ptc_tools_map,
)
from . import lifecycle as _lifecycle
from .lifecycle import (
    ExecutionSeams,
    default_seams as default_seams,
    _load_warned_runs as _load_warned_runs,
    _is_run_killed as _is_run_killed,
    _fail_run_load_timeout as _fail_run_load_timeout,
    _notify_load_warning as _notify_load_warning,
    _wait_for_load_gate as _wait_for_load_gate,
    _RunWallClock as _RunWallClock,
    _reset_child_attempt as _reset_child_attempt,
    release_child_session_state as release_child_session_state,
    _release_child_resources as _release_child_resources,
    _finalize_child_run as _finalize_child_run,
    _run_goal_loop as _run_goal_loop,
    _first_turn_self_evident as _first_turn_self_evident,
    _rollback_spawn,
)


def _execution_seams() -> ExecutionSeams:
    """The execution loop's collaborators, resolved from THIS module's globals.

    The tests drive the facade (they monkeypatch ``_build_child_agent``,
    ``_execute_subagent``, ``_finalize_child_run`` … here), so the bundle has to
    be rebuilt per call instead of bound once at import time.
    """
    return ExecutionSeams(
        execute_subagent=_execute_subagent,
        build_child_agent=_build_child_agent,
        try_ptc_prefill=_try_ptc_prefill,
        bind_child_run_context=_bind_child_run_context,
        run_goal_loop=_run_goal_loop,
        release_child_resources=_release_child_resources,
        finalize_child_run=_finalize_child_run,
    )


async def _execute_subagent_with_lane(
    run: SubagentRunRecord,
    system_prompt: str,
    user_message: str,
    tools: list | None,
    timeout_seconds: float,
    model_override: str | None = None,
    model_tier: str | None = None,
    model_profile: dict[str, str] | None = None,
    extra_tools: list[str] | None = None,
    output_schema: dict | None = None,
    goal_max_turns: int = 5,
) -> None:
    """Facade for :func:`lifecycle._execute_subagent_with_lane` (see there)."""
    await _lifecycle._execute_subagent_with_lane(
        run=run,
        system_prompt=system_prompt,
        user_message=user_message,
        tools=tools,
        timeout_seconds=timeout_seconds,
        model_override=model_override,
        model_tier=model_tier,
        model_profile=model_profile,
        extra_tools=extra_tools,
        output_schema=output_schema,
        goal_max_turns=goal_max_turns,
        seams=_execution_seams(),
    )


async def _execute_subagent(
    run: SubagentRunRecord,
    system_prompt: str,
    user_message: str,
    tools: list | None,
    timeout_seconds: float,
    model_override: str | None = None,
    model_tier: str | None = None,
    model_profile: dict[str, str] | None = None,
    extra_tools: list[str] | None = None,
    output_schema: dict | None = None,
    *,
    goal_max_turns: int = 5,
) -> None:
    """Facade for :func:`lifecycle._execute_subagent` (see there)."""
    await _lifecycle._execute_subagent(
        run,
        system_prompt=system_prompt,
        user_message=user_message,
        tools=tools,
        timeout_seconds=timeout_seconds,
        model_override=model_override,
        model_tier=model_tier,
        model_profile=model_profile,
        extra_tools=extra_tools,
        output_schema=output_schema,
        goal_max_turns=goal_max_turns,
        seams=_execution_seams(),
    )


async def _execute_subagent_scoped(
    run: SubagentRunRecord,
    system_prompt: str,
    user_message: str,
    tools: list | None,
    timeout_seconds: float,
    model_override: str | None = None,
    model_tier: str | None = None,
    model_profile: dict[str, str] | None = None,
    extra_tools: list[str] | None = None,
    output_schema: dict | None = None,
    *,
    goal_max_turns: int = 5,
) -> None:
    """Facade for :func:`lifecycle._execute_subagent_scoped` (see there)."""
    await _lifecycle._execute_subagent_scoped(
        run,
        system_prompt=system_prompt,
        user_message=user_message,
        tools=tools,
        timeout_seconds=timeout_seconds,
        model_override=model_override,
        model_tier=model_tier,
        model_profile=model_profile,
        extra_tools=extra_tools,
        output_schema=output_schema,
        goal_max_turns=goal_max_turns,
        seams=_execution_seams(),
    )


async def _try_ptc_prefill(
    run: SubagentRunRecord,
    tools: list | None,
    effective_deny: list[str],
    output_schema: dict | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Facade for :func:`ptc_prefill._try_ptc_prefill` (see there).

    The tool-map builder is resolved from THIS module's globals, so a
    monkeypatched ``spawn.core._ptc_tools_map`` still feeds the replay path.
    """
    return await _ptc_prefill_mod._try_ptc_prefill(
        run, tools, effective_deny, output_schema, tools_map_builder=_ptc_tools_map
    )


async def _materialize_child_attachments(
    attachments: list[dict] | None,
    cwd: str | None,
    config: Any,
) -> tuple[str | None, str | None, str, SpawnResult | None]:
    """Phase 7: materialize attachments into the child workspace.

    Returns ``(attachments_dir, attachments_root_dir, prompt_suffix, refusal)``.
    """
    if not attachments or not config.attachments_enabled:
        return None, None, "", None
    workspace = Path(cwd) if cwd else None
    mat_result = await materialize_subagent_attachments(
        attachments,
        child_workspace=workspace,
        max_files=config.attachments_max_files,
        max_file_bytes=config.attachments_max_file_bytes,
        max_total_bytes=config.attachments_max_total_bytes,
    )
    if mat_result.status == "error":
        return None, None, "", SpawnResult(status="error", error=mat_result.error)
    if mat_result.status == "ok" and mat_result.abs_dir:
        return mat_result.abs_dir, mat_result.root_dir, mat_result.system_prompt_suffix, None
    return None, None, "", None


def _assemble_child_prompts(
    run: SubagentRunRecord,
    *,
    task: str,
    role: SubagentSessionRole,
    functional_role: FunctionalRole,
    role_def: RoleDefinition | None,
    child_depth: int,
    max_depth: int,
    child_session_key: str,
    controller_session_key: str,
    requester_display_key: str,
    attachment_prompt_suffix: str,
    isolated: bool,
    output_schema: dict | None,
) -> tuple[str, str]:
    """Phase 10: the child's system prompt and initial user message."""
    system_prompt = build_subagent_system_prompt_from_context(
        SubagentPromptContext(
            role=role,
            task=task,
            functional_role=functional_role,
            role_description=role_def.description if role_def else "",
            role_prompt_body=role_def.prompt_body if role_def else "",
            requester_label=requester_display_key,
            depth=child_depth,
            max_depth=max_depth,
            child_session_key=child_session_key,
            requester_session_key=controller_session_key,
            can_spawn=role == SubagentSessionRole.ORCHESTRATOR,
        )
    )

    # Append attachment-aware and schema-constraint suffixes to the system prompt
    if attachment_prompt_suffix:
        system_prompt = f"{system_prompt}\n{attachment_prompt_suffix}"

    if isolated:
        system_prompt = f"{system_prompt}\n{_ISOLATION_PROMPT_NOTE}"

    if output_schema:
        schema_prompt = build_structured_output_prompt(output_schema)
        if schema_prompt:
            system_prompt = f"{system_prompt}\n{schema_prompt}"

    user_message = build_subagent_initial_user_message(
        task,
        depth=child_depth,
        max_depth=max_depth,
    )
    return system_prompt, user_message


async def _reserve_swarm_group(
    run: SubagentRunRecord,
    swarm_group_id: str | None,
    *,
    task: str,
    requester_session_key: str,
    normalized_task_name: str | None,
    launch_fingerprint: str | None,
) -> SubagentRunRecord:
    """Phase 9: reserve this run inside its swarm group (if any)."""
    if not swarm_group_id:
        return run
    from ..swarm.collector import configure_swarm_group, get_group_config

    group_cfg = get_group_config(swarm_group_id)
    if group_cfg is None:
        # First run in this group — create the group config on the fly
        from ..types.swarm import SwarmGroupConfig

        configure_swarm_group(SwarmGroupConfig(group_id=swarm_group_id))

    swarm_run = await reserve_swarm_run(
        swarm_group_id,
        task,
        requester_session_key,
        task_name=normalized_task_name,
        launch_fingerprint=launch_fingerprint,
    )
    if swarm_run is None:
        return run
    from ..registry.memory import get as get_run, update as update_run

    update_run(run.run_id, swarm_group_id=swarm_group_id, swarm_run_state=swarm_run.swarm_run_state)
    return get_run(run.run_id) or run  # refresh after in-place update


def _stamp_taskflow_context(
    child_session_key: str, flow_id: str | None, step_id: str | None
) -> None:
    """Stamp a TaskFlow step child with the flow/step it is executing."""
    if not flow_id:
        return
    from runtime import StateKey, state_register_mem

    state_register_mem.set_state(child_session_key, StateKey.FLOW_ID, flow_id)
    if step_id:
        state_register_mem.set_state(child_session_key, StateKey.STEP_ID, step_id)


async def spawn_subagent_direct(
    request: SpawnRequest | None = None,
    **legacy: Any,
) -> SpawnResult:
    """Validate, register, and launch a sub-agent as a background task.

    Accepts a :class:`SpawnRequest`; the historical keyword-argument shape is
    still supported through ``SpawnRequest.from_legacy`` (dozens of call sites
    and tests use it, and a hard cutover would be a compatibility break with no
    functional gain). Passing both forms at once is a caller bug.
    """
    if request is None:
        request = SpawnRequest.from_legacy(**legacy)
    elif legacy:
        raise TypeError(
            "spawn_subagent_direct() accepts a SpawnRequest OR keyword arguments, not both"
        )
    return await _spawn_subagent(request)


async def _spawn_subagent(request: SpawnRequest) -> SpawnResult:
    """The orchestration behind :func:`spawn_subagent_direct` (one request in)."""
    task = request.task
    requester_session_key = request.requester_session_key
    agent_id = request.agent_id
    task_name = request.task_name
    label = request.label
    thinking = request.thinking
    cleanup = request.cleanup
    attachments = request.attachments
    cwd = request.cwd
    completion_owner_key = request.completion_owner_key
    expects_completion_message = request.expects_completion_message
    run_timeout_seconds = request.run_timeout_seconds
    swarm_group_id = request.swarm_group_id
    output_schema = request.output_schema
    model = request.model
    launch_fingerprint = request.launch_fingerprint
    goal_max_turns = request.goal_max_turns
    functional_role_hint = request.functional_role_hint
    extra_tools = request.extra_tools
    isolation = request.isolation
    flow_id = request.flow_id
    step_id = request.step_id

    # --- Phases 1–2: input validation and the admission gate ---
    config = get_config()
    child_depth, refusal = _validate_spawn_request(task, agent_id, requester_session_key, config)
    if refusal is not None:
        logger.warning(
            "spawn refused: reason={} agent={} depth={} requester={}",
            refusal.error,
            agent_id,
            child_depth,
            requester_session_key,
        )
        return refusal

    normalized_task_name = normalize_subagent_task_name(task_name)

    # --- Phase 3: Runtime isolation & cwd ---
    isolation_cfg = resolve_runtime_isolation(requester_session_key, agent_id=agent_id, cwd=cwd)
    iso_ok, iso_reason = validate_runtime_isolation(isolation_cfg)
    if not iso_ok:
        logger.warning(
            "spawn refused: runtime isolation ({}) agent={} requester={}",
            iso_reason,
            agent_id,
            requester_session_key,
        )
        return SpawnResult(status="forbidden", error=iso_reason)

    # Inherit the parent's workspace when the caller does not specify one
    if cwd is None:
        from .runtime_isolation import resolve_spawned_workspace_inheritance

        cwd = resolve_spawned_workspace_inheritance(requester_session_key, agent_id)

    # Enforce directory-prefix allowlist from the isolation policy
    if cwd:
        cwd_ok, cwd_reason = validate_cwd_restriction(cwd, isolation_cfg.allowed_cwd_prefixes)
        if not cwd_ok:
            logger.warning(
                "spawn refused: cwd restriction ({}) agent={} cwd={}",
                cwd_reason,
                agent_id,
                cwd,
            )
            return SpawnResult(status="forbidden", error=cwd_reason)

    # --- Phase 4: Ownership & capability resolution ---
    ownership = resolve_spawn_ownership(
        requester_session_key=requester_session_key,
        completion_owner_key=completion_owner_key,
    )

    # Build a globally-unique session key for the child
    child_session_key = f"agent:{agent_id}:subagent:{uuid.uuid4()}"
    role, _ = resolve_subagent_capabilities(child_depth)

    # --- Phase 4.1: Workspace isolation (opt-in) ---
    cwd, isolated_workspace, refusal = _create_isolated_child_workspace(
        isolation, cwd, child_session_key
    )
    if refusal is not None:
        return refusal

    # --- Phase 4.5: Functional role resolution ---
    # GENERAL is the identity role: it loads no definition, so a spawn without a
    # functional-role hint keeps the pre-migration LLM/tool/prompt behavior.
    functional_role = _resolve_functional_role(functional_role_hint, agent_id)
    role_def: RoleDefinition | None = (
        load_role_definition(functional_role) if functional_role != FunctionalRole.GENERAL else None
    )

    # --- Phase 4.6: capability containment (role allow-list) ---
    # Specialized role → the roles its allow-list permits (its own role; an
    # executor also the read-only researcher / librarian). See
    # _check_role_containment and SPAWN_ALLOWED_CHILD_ROLES.
    # Refused here, before the model plan and the attachment materialization, and
    # the isolated workspace a refusal may have cut is discarded like any other.
    containment_error = _check_role_containment(requester_session_key, functional_role)
    if containment_error is not None:
        logger.warning(
            "spawn refused: capability containment child_role={} requester={}",
            functional_role,
            requester_session_key,
        )
        if isolated_workspace is not None:
            # A refused spawn must not leave its private copy behind: nothing
            # will ever merge it (no run record).
            from ..isolation import discard_isolated_workspace

            discard_isolated_workspace(isolated_workspace.meta_dir)
        return SpawnResult(status="forbidden", error=containment_error)

    # --- Phase 5: Model & thinking plan ---
    model_plan = resolve_model_and_thinking_plan(
        model_override=model,
        thinking_override_raw=thinking,
        requester_thinking=None,
        target_agent_thinking=None,
        model_tier=role_def.model_tier if role_def else None,
    )

    resolved_model = model_plan.resolved_model
    resolved_model_tier = model_plan.model_tier
    thinking_resolved = model_plan.thinking_override

    # The requester session may pin a MODEL per functional role (preset subagent models):
    # a profile descriptor that outranks the role's model_tier. None = tier.
    role_model_profile = resolve_role_model_profile(requester_session_key, functional_role)

    # --- Phase 6: Origin routing ---
    child_origin = resolve_requester_origin_for_child(requester_session_key, agent_id=agent_id)
    child_scopes = resolve_least_privilege_scopes(agent_id, role)

    # --- Phase 7: Attachment materialization ---
    (
        attachments_dir,
        attachments_root_dir,
        attachment_prompt_suffix,
        refusal,
    ) = await _materialize_child_attachments(attachments, cwd, config)
    if refusal is not None:
        await _rollback_spawn(child_session_key, None, attachments_dir, attachments_root_dir)
        if isolated_workspace is not None:
            # A failed spawn must not leave its copy behind: nothing will
            # ever merge it (no run record), so it is discarded here.
            from ..isolation import discard_isolated_workspace

            discard_isolated_workspace(isolated_workspace.meta_dir)
        return refusal

    # --- Phases 8–8.6: tool policy, then registry registration ---
    tool_allow, tool_deny = _resolve_child_tool_policy(role_def, role, extra_tools)
    extra_tool_names: list[str] = list(dict.fromkeys(extra_tools or []))

    run = register_run(
        child_session_key=child_session_key,
        requester_session_key=ownership.completion_requester_session_key,
        task=task,
        task_name=normalized_task_name,
        cleanup=cleanup,
        agent_id=agent_id,
        thinking=thinking_resolved or thinking,
        max_output_tokens=resolve_child_max_output_tokens(
            model_profile=role_model_profile,
            model_override=model,
            functional_role=functional_role,
            model_tier=resolved_model_tier,
            role=role,
        ),
        depth=child_depth,
        label=label,
        functional_role=functional_role,
        inherited_tool_allow=tool_allow,
        inherited_tool_deny=tool_deny,
        scopes=child_scopes,
        output_schema=output_schema,
        step_id=step_id,
        flow_id=flow_id,
        attachments_dir=attachments_dir,
        attachments_root_dir=attachments_root_dir,
        controller_session_key=ownership.controller_session_key,
        completion_owner_session_key=ownership.completion_requester_session_key,
        spawned_by=requester_session_key,
        spawned_cwd=cwd,
        expects_completion_message=expects_completion_message,
        wake_on_descendant_settle=True,  # auto-resume parent when child tree settles
    )

    # TaskFlow context is a read-only marker for this child: the evidence
    # recorder reads it when a terminal/python_repl verification runs, so a
    # flow's finish gate can query by flow_id instead of first reconstructing
    # the session set from its steps. Set AFTER registration (a refused spawn
    # leaves no run and must not leave a stale marker).
    _stamp_taskflow_context(child_session_key, flow_id, step_id)

    caps = resolve_subagent_output_caps(run.max_output_tokens)
    logger.info(
        "Subagent run {}: functional_role={} max_output_tokens={} "
        "result_caps(stored={}B delivery={}c summary={}c judge={}c transcript={}/{}t)",
        run.run_id,
        functional_role,
        run.max_output_tokens,
        caps.stored_bytes,
        caps.delivery_chars,
        caps.summary_chars,
        caps.judge_chars,
        caps.transcript_tokens,
        caps.per_message_tokens,
    )

    # Track the expected terminal generation so orphan detection can notice if the run stalls
    from ..registry.terminal_gen import get_terminal_gen_tracker

    get_terminal_gen_tracker().register_expected(run.run_id, run.generation)

    # --- Phase 9: Swarm group reservation (optional) ---
    run = await _reserve_swarm_group(
        run,
        swarm_group_id,
        task=task,
        requester_session_key=requester_session_key,
        normalized_task_name=normalized_task_name,
        launch_fingerprint=launch_fingerprint,
    )

    if child_origin.channel or child_origin.account_id:
        from ..registry.memory import update as update_run

        origin_data = child_origin.model_dump(exclude_none=True)
        update_run(run.run_id, origin_data=origin_data)

    # --- Phase 10: System prompt & context assembly ---
    logger.info(
        "Spawned subagent: run_id={}, child={}, role={}, depth={}, owner={}",
        run.run_id,
        child_session_key,
        role,
        child_depth,
        ownership.completion_requester_display_key,
    )

    system_prompt, user_message = _assemble_child_prompts(
        run,
        task=task,
        role=role,
        functional_role=functional_role,
        role_def=role_def,
        child_depth=child_depth,
        max_depth=config.max_spawn_depth,
        child_session_key=child_session_key,
        controller_session_key=ownership.controller_session_key,
        requester_display_key=ownership.completion_requester_display_key,
        attachment_prompt_suffix=attachment_prompt_suffix,
        isolated=isolated_workspace is not None,
        output_schema=output_schema,
    )
    timeout_seconds = resolve_run_timeout_seconds(run_timeout_seconds)

    # Warn if the run already looks orphaned at creation time (e.g. parent disconnected)
    orphan_reason = resolve_run_orphan_reason(run)
    if orphan_reason:
        logger.warning("Spawned run {} appears orphaned at creation: {}", run.run_id, orphan_reason)

    # --- Phase 11: Launch background execution ---
    from agent.tools import build_main_tools

    from config.features import COMPLETION_JUDGE

    effective_goal_max_turns = (
        int(goal_max_turns)
        if goal_max_turns is not None
        else int(COMPLETION_JUDGE["goal_max_turns"])
    )

    bg_task = asyncio.create_task(
        _execute_subagent_with_lane(
            run=run,
            system_prompt=system_prompt,
            user_message=user_message,
            tools=build_main_tools(),
            timeout_seconds=timeout_seconds,
            model_override=resolved_model,
            model_tier=resolved_model_tier,
            model_profile=role_model_profile,
            extra_tools=extra_tool_names,
            output_schema=output_schema,
            goal_max_turns=effective_goal_max_turns,
        )
    )

    from ..registry import register_task

    register_task(run.run_id, bg_task)

    # --- Phase 12: Hook dispatch & return ---
    try:
        await fire_spawned_hook(run)
    except Exception as e:
        logger.debug("fire_spawned_hook error for run {}: {}", run.run_id, e)

    accepted_note = resolve_spawn_accepted_note(requester_session_key)
    if isolated_workspace is not None:
        accepted_note = (
            f"{accepted_note} [isolated workspace: the run works in a private copy; "
            "its changes are merged back when it completes]"
        ).strip()

    return SpawnResult(
        status="accepted",
        child_session_key=child_session_key,
        run_id=run.run_id,
        task_name=normalized_task_name,
        note=accepted_note,
    )
