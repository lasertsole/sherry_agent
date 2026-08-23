"""Swarm scheduler: reserve, activate, complete, structured-output prompt, and validation."""

import time
import json
from loguru import logger
from ..types.swarm import SwarmRunState, SwarmGroupConfig
from ..types.registry import SubagentRunRecord, ExecutionStatus, RunOutcome
from ..registry import register_run, get_run, set_run, all_runs
from ..registry.queries import count_active_runs_for_session
from .fifo import get_fifo


_group_configs: dict[str, SwarmGroupConfig] = {}
_launch_fingerprints: dict[str, str] = {}  # Idempotency: fingerprint → run_id


def configure_swarm_group(config: SwarmGroupConfig) -> None:
    """Register a swarm group configuration."""
    _group_configs[config.group_id] = config


def get_group_config(group_id: str) -> SwarmGroupConfig | None:
    """Retrieve the configuration for a swarm group, or None if not found."""
    return _group_configs.get(group_id)


async def reserve_swarm_run(
    group_id: str,
    task: str,
    requester_session_key: str,
    task_name: str | None = None,
    agent_id: str = "main",
    depth: int = 1,
    launch_fingerprint: str | None = None,
) -> SubagentRunRecord | None:
    """Reserve a swarm run slot, enqueue it, and pump the lane for activation."""
    if launch_fingerprint:
        fp_key = f"{group_id}:{launch_fingerprint}"  # Composite key prevents cross-group collisions
        existing_run_id = _launch_fingerprints.get(fp_key)
        if existing_run_id:
            existing = get_run(existing_run_id)
            if existing is not None:
                logger.info("reserve_swarm_run: idempotent hit for fingerprint {} → run {}", fp_key, existing_run_id)
                return existing

    config = _group_configs.get(group_id)
    if config is None:
        logger.warning("reserve_swarm_run: unknown group {}", group_id)
        return None

    total = _count_swarm_runs_by_group(group_id)
    if total >= config.max_children_per_group:
        logger.warning("reserve_swarm_run: group {} at capacity ({}/{})", group_id, total, config.max_children_per_group)
        return None

    if config.max_total_per_group > 0 and total >= config.max_total_per_group:
        logger.warning("reserve_swarm_run: group {} at total capacity ({}/{})", group_id, total, config.max_total_per_group)
        return None

    import uuid
    child_session_key = f"agent:{agent_id}:swarm:{group_id}:{uuid.uuid4()}"  # Deterministic prefix for identification

    run = register_run(
        child_session_key=child_session_key,
        requester_session_key=requester_session_key,
        task=task,
        task_name=task_name or f"swarm:{group_id}",
        depth=depth,
        agent_id=agent_id,
    )

    updated = run.model_copy(update={
        "swarm_group_id": group_id,
        "swarm_run_state": SwarmRunState.RESERVED.value,
    })
    set_run(updated)

    fifo = get_fifo()
    await fifo.enqueue(group_id, run.run_id)

    logger.info("Reserved swarm run: run_id={}, group={}", run.run_id, group_id)

    if launch_fingerprint:
        fp_key = f"{group_id}:{launch_fingerprint}"  # Same composite key for storing the mapping
        _launch_fingerprints[fp_key] = updated.run_id

    await _pump_lane(group_id)

    return updated


async def activate_swarm_run(run_id: str) -> SubagentRunRecord | None:
    """Transition a RESERVED swarm run to ACTIVE, respecting the group's concurrency cap."""
    run = get_run(run_id)
    if run is None:
        return None

    if run.swarm_run_state != SwarmRunState.RESERVED.value:
        logger.warning("activate_swarm_run: run {} not in RESERVED state", run_id)
        return run

    group_id = run.swarm_group_id
    if group_id is None:
        return None

    config = _group_configs.get(group_id)
    if config is None:
        return None

    active_count = _count_active_swarm_runs(group_id)
    if active_count >= config.max_concurrent:
        logger.info("activate_swarm_run: group {} at max concurrent ({}/{}), queued",
                    group_id, active_count, config.max_concurrent)
        return run

    updated = run.model_copy(update={
        "swarm_run_state": SwarmRunState.ACTIVE.value,
        "execution": run.execution.model_copy(update={
            "started_at": time.monotonic(),
        }),
    })
    set_run(updated)

    fifo = get_fifo()
    await fifo.remove(group_id, run_id)

    logger.info("Activated swarm run: run_id={}, group={}, started=true", run_id, group_id)

    try:
        await _on_swarm_run_started(updated)
    except Exception as e:
        logger.error("onStartFailure for swarm run {}: {}", run_id, e)
        failed = updated.model_copy(update={
            "swarm_run_state": SwarmRunState.FAILED.value,
        })
        set_run(failed)
        await _activate_next_in_group(group_id)
        return failed

    return updated


async def _on_swarm_run_started(run: SubagentRunRecord) -> None:
    """Fire the spawned hook when a swarm run starts."""
    try:
        from ..hooks.progress import fire_spawned_hook
        await fire_spawned_hook(run)
    except Exception:
        pass


async def complete_swarm_run(run_id: str, outcome: RunOutcome, result_text: str | None = None) -> SubagentRunRecord | None:
    """Complete a swarm run and activate the next queued run in the group."""
    run = get_run(run_id)
    if run is None:
        return None

    state = SwarmRunState.COMPLETED.value if outcome.status == "ok" else SwarmRunState.FAILED.value  # Map ok→COMPLETED, else→FAILED

    from ..registry.run_manager import complete_run
    updated = complete_run(run_id, outcome, result_text)
    if updated is None:
        return None

    updated = updated.model_copy(update={"swarm_run_state": state})
    set_run(updated)

    group_id = run.swarm_group_id
    if group_id:
        await _activate_next_in_group(group_id)

    return updated


async def _activate_next_in_group(group_id: str) -> None:
    """Pump the lane to activate the next queued run in the group."""
    await _pump_lane(group_id)


async def _pump_lane(group_id: str) -> None:
    """Fill available concurrency slots by activating queued runs from the FIFO."""
    config = _group_configs.get(group_id)
    if config is None:
        return
    active_count = _count_active_swarm_runs(group_id)
    while active_count < config.max_concurrent:
        fifo = get_fifo()
        next_run_id = await fifo.dequeue(group_id)
        if next_run_id is None:
            break
        activated = await activate_swarm_run(next_run_id)
        if activated is None or activated.swarm_run_state == SwarmRunState.FAILED.value:
            active_count = _count_active_swarm_runs(group_id)
            continue
        active_count += 1


def build_structured_output_prompt(output_schema: dict | None) -> str:
    """Build a prompt suffix requiring the sub-agent to output JSON matching the schema."""
    if output_schema is None:
        return ""

    schema_str = json.dumps(output_schema, indent=2, ensure_ascii=False)
    return (
        "\n\nIMPORTANT: Your output must conform to the following JSON schema:\n"
        f"```json\n{schema_str}\n```\n"
        "Return your result as valid JSON matching this schema."
    )


def validate_structured_output(result_text: str | None, output_schema: dict | None) -> tuple[bool, str | None]:
    """Validate that result_text parses as JSON and conforms to the given JSON schema."""
    if output_schema is None or result_text is None:
        return True, None

    try:
        parsed = json.loads(result_text)
    except json.JSONDecodeError as e:
        return False, f"Output is not valid JSON: {e}"

    return _validate_value_against_schema(parsed, output_schema, path="root")


def _validate_value_against_schema(value, schema: dict, path: str) -> tuple[bool, str | None]:
    """Recursively validate a parsed value against a JSON Schema subset."""
    if not isinstance(schema, dict):
        return True, None

    schema_type = schema.get("type")

    if schema_type == "object" or ("properties" in schema):  # Treat bare properties as object type
        if not isinstance(value, dict):
            return False, f"{path}: expected object, got {type(value).__name__}"

        required = schema.get("required", [])
        for field_name in required:
            if field_name not in value:
                return False, f"{path}: missing required field '{field_name}'"

        properties = schema.get("properties", {})
        for field_name, field_schema in properties.items():
            if field_name in value:
                ok, err = _validate_value_against_schema(value[field_name], field_schema, f"{path}.{field_name}")
                if not ok:
                    return False, err

        additional = schema.get("additionalProperties")
        if additional is False:  # Only enforce when explicitly set to false
            extra = set(value.keys()) - set(properties.keys())
            if extra:
                return False, f"{path}: additional properties not allowed: {extra}"

        pattern_props = schema.get("patternProperties", {})
        for pattern, pat_schema in pattern_props.items():
            import re
            try:
                pat_re = re.compile(pattern)
            except re.error:
                continue
            for key in value:
                if pat_re.search(key):
                    ok, err = _validate_value_against_schema(value[key], pat_schema, f"{path}.{key}")
                    if not ok:
                        return False, err

    elif schema_type == "array":
        if not isinstance(value, list):
            return False, f"{path}: expected array, got {type(value).__name__}"
        items_schema = schema.get("items")
        if items_schema:
            for i, item in enumerate(value):
                ok, err = _validate_value_against_schema(item, items_schema, f"{path}[{i}]")
                if not ok:
                    return False, err

    elif schema_type in ("string", "number", "integer", "boolean"):
        type_map = {
            "string": str, "number": (int, float),
            "integer": int, "boolean": bool,
        }
        python_type = type_map.get(schema_type)
        if python_type and not isinstance(value, python_type):
            return False, f"{path}: expected {schema_type}, got {type(value).__name__}"

    return True, None


def _count_swarm_runs_by_group(group_id: str) -> int:
    """Count all runs (any state) belonging to a swarm group."""
    return sum(1 for r in all_runs() if r.swarm_group_id == group_id)


def _count_active_swarm_runs(group_id: str) -> int:
    """Count currently running/interrupted runs in a swarm group."""
    return sum(
        1 for r in all_runs()
        if r.swarm_group_id == group_id
        and r.swarm_run_state == SwarmRunState.ACTIVE.value
        and r.execution.status in (ExecutionStatus.RUNNING, ExecutionStatus.INTERRUPTED)
    )


def list_swarm_runs_by_group(group_id: str) -> list[SubagentRunRecord]:
    """List all run records belonging to a swarm group."""
    return [r for r in all_runs() if r.swarm_group_id == group_id]


def count_pending_swarm_runs(group_id: str) -> int:
    """Count RESERVED (queued) runs in a swarm group."""
    return sum(
        1 for r in all_runs()
        if r.swarm_group_id == group_id
        and r.swarm_run_state == SwarmRunState.RESERVED.value
    )


def count_active_swarm_runs(group_id: str) -> int:
    """Public wrapper for _count_active_swarm_runs."""
    return _count_active_swarm_runs(group_id)
