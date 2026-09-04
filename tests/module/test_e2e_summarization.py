"""Hermetic end-to-end test: Summarization redesign STATIC-FALLBACK path (PART2 §14 adapted).

Task 10 of the summarization-redesign plan (``.omo/plans/summarization-redesign.md`` L886).

What this proves, end to end and without a single network byte:

  1. A REAL agent chain is built via ``langchain.agents.create_agent`` using the
     T7 integration form of ``agent/core.py:137-160`` — a ``StateSchema(AgentState)``
     carrying ``session_id`` (core.py:34-37) and a ``Summarization`` middleware
     instantiated with ``need_update_system_prompt=True`` (core.py:153), a stubbed
     auxiliary model (core.py:154), ``main_llm_context_window`` (core.py:155), the
     trigger-ratio form ``[("tokens", int(window * COMPRESSION_TRIGGER_RATIO))]``
     (core.py:156) and ``keep=("messages", 10)`` (core.py:157).
  2. Injecting PART2 §14's history (30 turns of Human+AI+Tool, ~5000 chars each,
     90 messages) drives the compiled graph's ``awrap_model_call`` hook through the
     full pipeline: preemptive truncate -> non-LLM strategies (dedup / prune /
     target truncate) -> LLM summary attempt (stub auxiliary FAILS) -> static
     fallback summary (``_build_static_fallback_summary``).
  3. Assertion (a) — post-compression message form (§14): the model call receives
     ``HumanMessage("What did we do so far?")`` followed by
     ``AIMessage(summary, additional_kwargs={"lc_source": "summarization"})``.
  4. Assertion (b) — need_update_system_prompt=True path (summarization.py
     L1141-1146): the model call's system message is the REBUILT system prompt
     (``memory_store.load_from_disk()`` + ``build_system_prompt(session_id=...)``)
     and the rebuilt prompt is double-written to ``state_register_mem`` AND
     ``state_register_db``.

ZERO NETWORK: the MAIN model is a capturing stub and the AUXILIARY model is a
failing stub — nothing in the chain touches HTTP. This test belongs to the
hermetic module group (B, ``tests/module``) and is explicitly NOT part of the
``llm_e2e`` marker system (see README "Testing": that marker is reserved for
real-LLM network e2e tests; this one is the static path only).

Skip guard: when the effective MAIN_LLM configuration is missing (no .env and no
``MAIN_LLM_*`` environment variables, or a blank injected value) the e2e class is
SKIPPED with an explicit reason — the suite must never FAIL on a machine that
merely lacks configuration.
"""

import asyncio
import os
import uuid
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# RAW environment snapshot — MUST run before any project import.
# Modules in the import chain below call ``load_dotenv(..., override=True)``
# (e.g. agent/tools/web_search.py:9, models/LLMs/main_llm.py:11), which would
# replace an injected blank MAIN_LLM_* value with the .env contents. The skip
# guard must judge the RAW process environment (QA scenario 2 injects a blank
# MAIN_LLM_PROVIDER into the subprocess), so it is snapshotted here first.
# ---------------------------------------------------------------------------
_RAW_MAIN_LLM_PROVIDER = os.environ.get("MAIN_LLM_PROVIDER")
_RAW_MAIN_LLM_NAME = os.environ.get("MAIN_LLM_NAME")

from langchain.agents import create_agent  # noqa: E402
from langchain.agents.middleware import AgentState  # noqa: E402
from langchain_core.messages import (  # noqa: E402
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

import agent.middlewares.summarization as summarization_module  # noqa: E402
from agent.middlewares.summarization import Summarization  # noqa: E402
from config import ENV_PATH  # noqa: E402
from config.num import COMPRESSION_TRIGGER_RATIO  # noqa: E402
from runtime import state_register_db, state_register_mem  # noqa: E402
from workspace.prompt_builder import build_system_prompt  # noqa: E402

# ======================================================================
# §14 fixture shape
# ======================================================================

_TURNS = 30          # §14: 30 turns of Human+AI+Tool = 90 messages
_TURN_CHARS = 5000   # §14: every message ~5000 chars (ASCII: the //4 estimator
                     # has a KNOWN CJK underestimation limitation — forbidden to fix)
_MAIN_LLM_CONTEXT_WINDOW = 32_000
# T7 ratio form with a scaled-down window: the real MAIN_LLM_MAX_TOKEN on this
# machine is 65_536_000 (uncapped), which would need a physically impossible
# ~52M-token history to reach the 0.80 compression pressure. The FORM
# int(window * COMPRESSION_TRIGGER_RATIO) is preserved verbatim (core.py:156).


def _build_history() -> list[BaseMessage]:
    """PART2 §14 step 1: 90 messages = 30 turns of Human+AI+Tool, ~5000 chars each."""
    history: list[BaseMessage] = []
    for i in range(_TURNS):
        history.append(
            HumanMessage(
                content=f"[turn {i:02d}] please investigate topic-{i}: "
                + "x" * (_TURN_CHARS - 40)
            )
        )
        history.append(
            AIMessage(
                content=f"[turn {i:02d}] working on topic-{i}: " + "y" * (_TURN_CHARS - 60),
                tool_calls=[
                    {
                        "name": "web_search",
                        "args": {"query": f"topic-{i} notes.md"},
                        "id": f"call_{i:02d}",
                        "type": "tool_call",
                    }
                ],
            )
        )
        history.append(
            ToolMessage(
                content=f"[turn {i:02d}] result for topic-{i}: " + "z" * (_TURN_CHARS - 60),
                tool_call_id=f"call_{i:02d}",
            )
        )
    return history


# ======================================================================
# Hermetic model stubs (network ban: NO real LLM client is ever built)
# ======================================================================


class _CapturingMainModel:
    """Stub for the MAIN model (replaces ``models.build_main_llm()``).

    Records every message list it is asked to answer — that list IS the
    post-compression view produced by the ``awrap_model_call`` override
    (system message + compressed messages), the end-to-end observable.
    """

    def __init__(self):
        self.calls: list[list[BaseMessage]] = []

    def bind(self, **kwargs):
        # create_agent binds via model.bind(**model_settings) with no tools
        # (factory.py:1378); a plain object needs no BaseChatModel machinery.
        return self

    async def ainvoke(self, messages, config=None, **kwargs):
        self.calls.append(list(messages))
        return AIMessage(content="Stub main-model reply: all topics investigated and closed.")


class _FailingAuxiliaryModel:
    """Stub for the AUXILIARY model (replaces ``models.build_auxiliary_llm()``).

    ALWAYS fails — the exception is swallowed inside ``_acreate_summary``
    (summarization.py:814-816) which then returns the static fallback summary.
    That is exactly the §14 "LLM summary attempt BLOCKED -> static fallback
    activated" semantics, realized hermetically.
    """

    def __init__(self):
        self.calls: list[object] = []

    def invoke(self, prompt, config=None):
        self.calls.append(prompt)
        raise RuntimeError("stub auxiliary LLM outage")

    async def ainvoke(self, prompt, config=None):
        self.calls.append(prompt)
        raise RuntimeError("stub auxiliary LLM outage")


# ======================================================================
# Real agent chain (T7 integration form)
# ======================================================================


class _E2EStateSchema(AgentState):
    """Mirrors agent/core.py:34-37: graph state carries session_id."""

    session_id: str


def _build_agent():
    """Real ``create_agent`` chain in the T7 instantiation form (agent/core.py:137-160).

    Both models are stubbed (zero-network requirement) and the context window is
    scaled to the §14 fixture size (see deviations log, task 10). Everything else —
    the factory, the StateSchema pattern and every Summarization kwarg — mirrors
    the production integration point verbatim.
    """
    main_model = _CapturingMainModel()
    aux_model = _FailingAuxiliaryModel()
    window = _MAIN_LLM_CONTEXT_WINDOW
    agent = create_agent(
        model=main_model,
        state_schema=_E2EStateSchema,
        tools=[],
        middleware=[
            Summarization(
                need_update_system_prompt=True,  # agent/core.py:153
                model=aux_model,                 # agent/core.py:154 — stubbed auxiliary
                main_llm_context_window=window,  # agent/core.py:155
                trigger=[("tokens", int(window * COMPRESSION_TRIGGER_RATIO))],  # core.py:156
                keep=("messages", 10),           # agent/core.py:157
            ),
        ],
    )
    return agent, main_model, aux_model


def _run_e2e(sid: str):
    """§14 steps 2-4: build the real chain, inject ≥90 messages, ainvoke once.

    The async graph run triggers the ``awrap_model_call`` hook naturally (no
    direct middleware calls — this is the end-to-end path).
    """
    agent, main_model, aux_model = _build_agent()
    history = _build_history()
    result = asyncio.run(agent.ainvoke({"messages": history, "session_id": sid}))
    return result, main_model, aux_model, history


# ======================================================================
# Skip guard: effective MAIN_LLM config probe (plan: .env existence is the
# fact source; .env contents are NEVER read)
# ======================================================================


def _probe_main_llm_config(provider=None, name=None, env_path: Path | None = None) -> tuple[bool, str]:
    """Return (config_present, reason).

    ``provider``/``name`` default to the RAW (pre-dotenv) environment snapshot —
    see the capture note at the top of this module. Judgement:

    - A blank/empty ``MAIN_LLM_PROVIDER`` or ``MAIN_LLM_NAME`` injection counts as
      MISSING — that is the env-injection QA scenario (subprocess with an
      empty-value provider). Note Win32 cannot carry zero-length env vars, so the
      injected "empty value" is whitespace-only; ``.strip()`` normalizes both.
    - Otherwise, explicit MAIN_LLM env config counts as present.
    - Otherwise, .env EXISTENCE at ENV_PATH counts as present (contents not read,
      per the plan). No .env and no env vars -> missing.
    """
    if provider is not None and provider.strip() == "":
        return False, (
            "skip guard: MAIN_LLM_PROVIDER is injected as an empty/blank value — "
            "effective MAIN_LLM config missing"
        )
    if name is not None and name.strip() == "":
        return False, (
            "skip guard: MAIN_LLM_NAME is injected as an empty/blank value — "
            "effective MAIN_LLM config missing"
        )
    if provider and name:
        return True, (
            f"MAIN_LLM config present via environment (provider={provider!r})"
        )
    path = env_path if env_path is not None else Path(ENV_PATH)
    if path.exists():
        return True, f"MAIN_LLM config assumed present: .env found at {path} (contents not read)"
    return False, (
        f"skip guard: no .env at {path} and no MAIN_LLM_PROVIDER/MAIN_LLM_NAME "
        "environment variables — MAIN_LLM config missing on this machine"
    )


_MAIN_LLM_CONFIG_OK, _MAIN_LLM_SKIP_REASON = _probe_main_llm_config(
    provider=_RAW_MAIN_LLM_PROVIDER, name=_RAW_MAIN_LLM_NAME
)


# ======================================================================
# Fixtures
# ======================================================================


@pytest.fixture
def sid():
    """Unique session id; cleans both registers afterwards (db has no clear_session)."""
    s = "t10-e2e-" + uuid.uuid4().hex[:8]
    yield s
    try:
        state_register_mem.clear_session(s)
    except Exception:
        pass
    for key in ("system_prompt", "workspace"):
        try:
            state_register_db.delete_state(s, key)
        except Exception:
            pass


# ======================================================================
# Skip-guard probe logic (pure logic — must run even on config-less machines)
# ======================================================================


class TestSkipGuardProbe:
    """The guard itself: blank/absent MAIN_LLM config must read as MISSING."""

    def test_blank_env_injection_counts_as_missing(self):
        ok, reason = _probe_main_llm_config(
            provider="   ", env_path=Path("Z:/nonexistent/.env")
        )
        assert ok is False
        assert "MAIN_LLM_PROVIDER" in reason and "missing" in reason

    def test_zero_length_env_injection_counts_as_missing(self):
        # POSIX machines CAN carry zero-length env vars; same verdict.
        ok, reason = _probe_main_llm_config(
            provider="", env_path=Path("Z:/nonexistent/.env")
        )
        assert ok is False
        assert "MAIN_LLM_PROVIDER" in reason and "missing" in reason

    def test_absent_env_and_no_dotenv_counts_as_missing(self):
        ok, reason = _probe_main_llm_config(
            provider=None, name=None, env_path=Path("Z:/nonexistent/.env")
        )
        assert ok is False
        assert "no .env" in reason and "missing" in reason

    def test_explicit_env_config_counts_as_present(self):
        ok, reason = _probe_main_llm_config(
            provider="openai", name="stub-model", env_path=Path("Z:/nonexistent/.env")
        )
        assert ok is True
        assert "present" in reason

    def test_raw_snapshot_probe_is_guard_source(self):
        """The module-level guard decision is computed from the RAW snapshot."""
        ok, reason = _probe_main_llm_config(
            provider=_RAW_MAIN_LLM_PROVIDER, name=_RAW_MAIN_LLM_NAME
        )
        assert ok == _MAIN_LLM_CONFIG_OK
        assert reason == _MAIN_LLM_SKIP_REASON


# ======================================================================
# The e2e itself (§14 semantics; guarded by the MAIN_LLM config probe)
# ======================================================================


@pytest.mark.skipif(not _MAIN_LLM_CONFIG_OK, reason=_MAIN_LLM_SKIP_REASON)
class TestE2ESummarizationStaticFallback:
    """Real agent chain -> compression triggered -> static fallback -> assertions."""

    def test_static_fallback_compression_pair_reaches_model(self, sid):
        """Assertion (a), §14: post-compression message form."""
        result, main_model, aux_model, history = _run_e2e(sid)

        # §14 step 5 pipeline: the auxiliary LLM was attempted exactly once and
        # its failure fell into the static fallback (no network, no retry storm).
        assert len(aux_model.calls) == 1
        # The real chain made exactly one model call (stub main model).
        assert len(main_model.calls) == 1
        received = main_model.calls[0]

        # (a) HumanMessage("What did we do so far?") directly after the system
        # message, followed by AIMessage(summary, lc_source="summarization").
        pair_question = received[1]
        summary_msg = received[2]
        assert isinstance(pair_question, HumanMessage)
        assert pair_question.content == "What did we do so far?"
        assert isinstance(summary_msg, AIMessage)
        assert (
            summary_msg.additional_kwargs.get("lc_source")
            == summarization_module._SUMMARY_LC_SOURCE
        )
        # Static-fallback structure markers — the summary is the deterministic
        # _build_static_fallback_summary output, not LLM prose.
        for marker in (
            summarization_module._SUMMARY_PREFIX,
            summarization_module._SUMMARY_OPEN_TAG,
            "## Latest Unresolved User Request",
            "### Completed",
            "## Next Steps",
            summarization_module._SUMMARY_CLOSE_TAG,
        ):
            assert marker in summary_msg.content

        # The preserved tail keeps the conversation end (cutoff never passes the
        # last user turn): the final turn's topic marker survives truncation.
        assert f"topic-{_TURNS - 1}" in received[-1].content

        # Heavy reduction: 90 input messages -> system + pair + small preserved tail.
        assert len(history) == _TURNS * 3
        assert len(received) - 1 < _TURNS

        # Task 5: T1 preflight compacts the PERSISTED state BEFORE the turn —
        # the graph state now holds the compressed pair + preserved tail +
        # this turn's reply. (The old T2-only wrap semantics kept the raw
        # history, because a wrap override never touched graph state.)
        final_state = result["messages"]
        assert len(final_state) < len(history) + 1
        assert any(
            isinstance(m, HumanMessage) and m.content == "What did we do so far?"
            for m in final_state
        )
        assert isinstance(final_state[-1], AIMessage)

        print(
            f"[e2e-t10] model received {len(received)} messages "
            f"(system + pair + {len(received) - 3} preserved) from {len(history)} input messages"
        )
        print(
            f"[e2e-t10] summary lc_source={summary_msg.additional_kwargs.get('lc_source')} "
            f"content_chars={len(summary_msg.content)}"
        )

    def test_rebuilt_system_prompt_on_need_update_system_prompt(self, sid):
        """Assertion (b): need_update_system_prompt=True path (summarization.py L1141-1146)."""
        _, main_model, _, _ = _run_e2e(sid)
        received = main_model.calls[0]

        system_msg = received[0]
        assert isinstance(system_msg, SystemMessage)

        # The rebuilt prompt: memory_store.load_from_disk() + build_system_prompt.
        expected = build_system_prompt(session_id=sid)
        assert system_msg.content == expected

        # The rebuilt prompt was double-written to BOTH state registers.
        assert state_register_mem.get_state(sid, "system_prompt") == expected
        assert state_register_db.get_state(sid, "system_prompt") == expected

        print(
            f"[e2e-t10] rebuilt system prompt delivered to the model "
            f"({len(system_msg.content)} chars) and written to mem+db registers"
        )
