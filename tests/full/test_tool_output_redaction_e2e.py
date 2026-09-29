"""Live-network e2e: a secret printed by a real tool never reaches the model (B1/B2).

LIVE-NETWORK, RUN EXPLICITLY. The production graph runs against the real main
LLM; the model is asked to call the real ``terminal`` tool and echo a
credential-shaped string. The assertion is on what the model's message list
actually holds afterwards — i.e. the model read «redacted», not the key.

The "secret" is a literal test string, so nothing real is involved: the point is
the pipeline, not the value.

Run it explicitly with::

    uv run --no-sync pytest -m llm_e2e tests/full/test_tool_output_redaction_e2e.py -v
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import uuid
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from loguru import logger

import agent.core as agent_core
from agent.checkpointer.async_sqlite_checkpointer import delete_thread_history
from agent.security.redact import REDACTED
from config import SESSIONS_DIR
from context_engine import delete_messages_by_session
from pub.func.build_agent_config import build_agent_config
from runtime import clear_all_register_sessions

pytestmark = [pytest.mark.llm_e2e, pytest.mark.timeout(600)]

#: A clearly fake key with a real vendor shape.
_FAKE_KEY = "sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


def _new_sid(tag: str) -> str:
    return f"e2e-redact-{tag}-{uuid.uuid4().hex[:8]}"


async def _purge_session(session_id: str) -> None:
    with contextlib.suppress(Exception):
        delete_messages_by_session(session_id)
    with contextlib.suppress(Exception):
        await delete_thread_history(session_id)
    with contextlib.suppress(Exception):
        shutil.rmtree(Path(SESSIONS_DIR) / session_id, ignore_errors=True)
    with contextlib.suppress(Exception):
        clear_all_register_sessions(session_id=session_id, clear_persistent_states=True)


async def _real_graph() -> Any:
    agent_core.init()
    return await agent_core.built_agent(temperature=0.0)


async def _invoke_with_retry(graph: Any, payload: dict, config: dict, attempts: int = 2) -> dict:
    for attempt in range(1, attempts + 1):
        try:
            return await graph.ainvoke(payload, config)
        except Exception as exc:  # noqa: BLE001 - live network boundary
            if attempt >= attempts:
                raise
            logger.warning("graph.ainvoke attempt {} failed ({}); retrying once", attempt, exc)
            await asyncio.sleep(5)
    raise AssertionError("unreachable")


def _tool_messages(out: dict, name: str) -> list[ToolMessage]:
    return [
        message
        for message in out.get("messages", [])
        if isinstance(message, ToolMessage) and getattr(message, "name", None) == name
    ]


def _describe(out: dict, name: str, found: list[ToolMessage]) -> str:
    names = [f"{type(m).__name__}:{getattr(m, 'name', None)}" for m in out.get("messages", [])]
    last_ai = next((m for m in reversed(out.get("messages", [])) if isinstance(m, AIMessage)), None)
    return (
        f"tool_messages={[str(m.content)[:120] for m in found]!r}; "
        f"message_names={names}; last_ai={getattr(last_ai, 'content', None)!r}"
    )


@pytest.mark.asyncio
async def test_a_key_printed_by_the_terminal_tool_reaches_the_model_masked() -> None:
    graph = await _real_graph()
    sid = _new_sid("terminal")
    prompt = (
        "You must call the tool `terminal` exactly once with these exact arguments and nothing "
        "else:\n"
        f"  command = {'echo MAIN_LLM_API_KEY=' + _FAKE_KEY!r}\n"
        "Use ONLY that command and do not call any other tool. After the tool returns, reply "
        "with the single word DONE."
    )

    last_note = "the model never called terminal"
    try:
        for attempt in range(1, 3):
            out = await _invoke_with_retry(
                graph,
                {
                    "messages": [HumanMessage(content=prompt, id=f"h-{sid}-{attempt}")],
                    "session_id": sid,
                },
                build_agent_config(sid),
            )
            found = _tool_messages(out, "terminal")
            if found:
                break
            last_note = _describe(out, "terminal", found)
            logger.warning("[redact e2e] attempt {}/2: {}", attempt, last_note)
        else:
            pytest.fail(f"the live model never called terminal after 2 attempts: {last_note}")

        content = str(found[-1].content)
        assert _FAKE_KEY not in content, f"the key reached the model verbatim: {content[:200]}"
        assert REDACTED in content, f"nothing was redacted: {content[:200]}"
        logger.info("[redact e2e] masked tool result: {}", content[:200])
    finally:
        await _purge_session(sid)
