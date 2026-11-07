"""Compile one verified TaskFlow step into a PTC replay script.

The compiler is deliberately narrow: it receives ONE step's real evidence, the
owning skill's SKILL.md, and the tool signatures the PTC child can call. It
returns Python source (or ``skip``); it never writes files itself. The caller
validates the source and writes it with ``skill_manage`` into the owning
skill's top-level ``scripts/`` directory.

The generated script is executed later by the spawn-side prefill via
``run_ptc``. It is not executed during review.
"""

from __future__ import annotations

import ast
import json
from typing import Any

from langchain_core.messages import HumanMessage
from loguru import logger

from config.features import PTC

from .builtins import ALLOWED_IMPORT_MODULES

#: Helpers built into the generated ``sherry_tools`` module (not real tools).
_SHERRY_TOOL_HELPERS = frozenset({"json_parse", "shell_quote", "retry"})
_FORBIDDEN_CALL_NAMES = frozenset(
    {
        "__import__",
        "eval",
        "exec",
        "compile",
        "open",
        "input",
        "globals",
        "locals",
        "breakpoint",
        "execute_code",
    }
)


def _strip_json(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("compiler returned no JSON object")
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("compiler JSON must be an object")
    return parsed


def _message_text(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return str(content or "")


def validate_ptc_script(
    code: str,
    *,
    allowed_tools: set[str] | frozenset[str],
    max_bytes: int | None = None,
) -> str | None:
    """Return an error string, or ``None`` when the script satisfies the contract.

    This is a static gate: syntax, import surface, forbidden calls, a final
    ``print``, and a bounded size. It intentionally does not execute the code.
    """
    limit = int(max_bytes if max_bytes is not None else PTC["ptc_script_max_bytes"])
    if len(code.encode("utf-8")) > limit:
        return f"script exceeds {limit} bytes"
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"syntax error: {exc.msg} (line {exc.lineno})"

    has_sherry_import = False
    has_print = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root not in ALLOWED_IMPORT_MODULES:
                    return f"import not allowed: {alias.name}"
                if root == "sherry_tools":
                    has_sherry_import = True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            root = module.split(".", 1)[0]
            if root not in ALLOWED_IMPORT_MODULES:
                return f"import not allowed: {module}"
            if root == "sherry_tools":
                has_sherry_import = True
                for alias in node.names:
                    if alias.name == "*":
                        return "sherry_tools wildcard imports are not allowed"
                    if alias.name not in allowed_tools and alias.name not in _SHERRY_TOOL_HELPERS:
                        return f"tool not present in the step evidence: {alias.name}"
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else None
            if isinstance(func, ast.Attribute):
                name = func.attr
            if name == "print":
                has_print = True
            if name in _FORBIDDEN_CALL_NAMES:
                return f"forbidden call: {name}"
        elif isinstance(node, ast.Name) and node.id in _FORBIDDEN_CALL_NAMES:
            return f"forbidden name: {node.id}"

    if not has_sherry_import:
        return "script must import at least one tool from sherry_tools"
    if not has_print:
        return "script must print its final result"
    return None


_COMPILER_PROMPT = """You are compiling ONE verified TaskFlow step into a reusable PTC replay script.

The script will run later in the PTC child process. It must perform the SAME
tool calls that succeeded in the evidence, in the same order, and print the
final result. Do NOT invent tools, parameters, commands, or outcomes.

## Step

{step}

## Evidence (success_steps for this step)

{success_steps}

## Hard constraints (turn each verifiable one into an assert)

{hard_constraints}

## Owning skill excerpt

{skill_excerpt}

## Available PTC tools and signatures

{tool_signatures}

## Script rules

1. Use `from sherry_tools import ...` for every tool call; the wrappers are synchronous.
2. Import ONLY tools that appear in the evidence above. Never import `execute_code`, `os`, `subprocess`, or `sys`.
3. Allowed stdlib imports are limited to the PTC child allowlist (`json`, `re`, `math`, `time`, `csv`, `datetime`, `collections`, `itertools`, `functools`, `statistics`, `string`, `textwrap`, `decimal`, `random`, `fractions`, `heapq`, `bisect`).
4. Do NOT call `input()`, `open()`, `eval()`, `exec()`, or any interactive process.
5. Print one non-empty final result (prefer `print(json.dumps(...))`).
6. Keep the script small and deterministic; do not add sleeps unless the evidence used them.
7. If the evidence is too vague, contains dynamic values that cannot be reproduced, or would duplicate a non-idempotent side effect, return `{"status":"skip","reason":"..."}`.

Return ONLY JSON:
{"status":"compiled","code":"<python source>","reason":"what was compiled"}
or
{"status":"skip","reason":"why this step cannot be replayed"}

"""


async def compile_step_script(
    *,
    plan_key: str,
    plan_name: str,
    step_id: str,
    flow_id: str | None,
    task: str,
    success_steps: list[dict[str, Any]],
    hard_constraints: list[str],
    skill_excerpt: str,
    tool_signatures: list[str],
    allowed_tools: set[str] | frozenset[str],
) -> str | None:
    """Ask the auxiliary model for one script; return validated source or None."""
    from models import build_auxiliary_llm

    attempts = max(1, int(PTC["ptc_compile_max_attempts"]))
    base_prompt = (
        _COMPILER_PROMPT.replace(
            "{step}",
            json.dumps(
                {
                    "plan_key": plan_key,
                    "plan_name": plan_name,
                    "step_id": step_id,
                    "flow_id": flow_id or "",
                    "task": task[:4_000],
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        .replace(
            "{success_steps}",
            json.dumps(success_steps, ensure_ascii=False, indent=2)[:12_000],
        )
        .replace(
            "{hard_constraints}",
            json.dumps(hard_constraints, ensure_ascii=False, indent=2)[:6_000],
        )
        .replace(
            "{skill_excerpt}",
            skill_excerpt[:8_000],
        )
        .replace(
            "{tool_signatures}",
            "\n".join(tool_signatures) or "(none)",
        )
    )

    prompt = base_prompt
    for attempt in range(1, attempts + 1):
        try:
            response = await build_auxiliary_llm(temperature=0).ainvoke(
                [HumanMessage(content=prompt)]
            )
            text = _message_text(response)
            payload = _strip_json(text)
            if str(payload.get("status") or "").lower() == "skip":
                logger.info(
                    "PTC compile: skip plan={} step={}: {}",
                    plan_key,
                    step_id,
                    payload.get("reason") or "no reason",
                )
                return None
            code = str(payload.get("code") or "")
            error = validate_ptc_script(code, allowed_tools=allowed_tools)
            if error is None:
                logger.info(
                    "PTC compile: compiled plan={} step={} ({} bytes)",
                    plan_key,
                    step_id,
                    len(code.encode("utf-8")),
                )
                return code
            feedback = (
                f"\nThe previous attempt was rejected: {error}. Fix it and return JSON again.\n"
            )
        except Exception as exc:  # noqa: BLE001 - compilation is best-effort
            feedback = (
                f"\nThe previous attempt failed ({type(exc).__name__}: {exc}). "
                "Return valid JSON only.\n"
            )
        logger.debug(
            "PTC compile attempt {}/{} rejected for {}: {}", attempt, attempts, step_id, feedback
        )
        prompt = base_prompt + feedback
    logger.warning(
        "PTC compile: exhausted {} attempt(s) for plan={} step={}", attempts, plan_key, step_id
    )
    return None
