"""Behavior test for the memory-review nudge's editorial FACTS.md maintenance.

The nudge prompt asks the model to maintain FACTS.md like an editor: merge
entries that describe the same pitfall and replace/remove outdated ones. This
test runs the real ``_nudge_memory`` fork against an isolated memory directory
with a stub LLM that emits exactly those two calls through the real ``memory``
tool, then asserts the file reflects the merge and the prune.

A fresh ``MemoryStore`` replaces the process singleton so the real
``workspace/memory/`` files are never touched.
"""

from __future__ import annotations

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

import agent.middlewares.summarization.nudges as nudge_mod
import agent.tools.memory as memory_module
from agent.tools.memory import MemoryStore, build_memory_tool

pytestmark = [pytest.mark.unit, pytest.mark.timeout(120)]


class _FakeStateRegister:
    """Minimal in-memory stand-in for ``state_register_mem`` (lock only)."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], object] = {}

    def get_state(self, session_id: str, key: str, default: object = None) -> object:
        return self.data.get((session_id, key), default)

    def set_state(self, session_id: str, key: str, value: object) -> None:
        self.data[(session_id, key)] = value


class _EditorialModel(BaseChatModel):
    """Emits replace (merge) then remove (prune), then a final message."""

    @property
    def _llm_type(self) -> str:
        return "stub-facts-editor"

    def bind_tools(self, tools, **kwargs):  # noqa: ARG002
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ARG002
        tool_messages = sum(isinstance(m, ToolMessage) for m in messages)
        if tool_messages == 0:
            return _tool_call_result(
                {
                    "action": "replace",
                    "target": "facts",
                    "old_text": "pitfall B",
                    "content": "pitfall A and B merged",
                },
                "call-facts-replace",
            )
        if tool_messages == 1:
            return _tool_call_result(
                {"action": "remove", "target": "facts", "old_text": "outdated C"},
                "call-facts-remove",
            )
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="merged and pruned"))]
        )


def _tool_call_result(args: dict, call_id: str) -> ChatResult:
    return ChatResult(
        generations=[
            ChatGeneration(
                message=AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "memory", "args": args, "id": call_id, "type": "tool_call"}
                    ],
                )
            )
        ]
    )


@pytest.fixture
def isolated_facts(tmp_path, monkeypatch):
    """Point a fresh store at a tmp memory dir seeded with three facts entries."""
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    (mem_dir / "FACTS.md").write_text(
        "\n§\n".join(["pitfall A (old)", "pitfall B", "outdated C"]), encoding="utf-8"
    )
    monkeypatch.setattr(memory_module, "MEMORY_DIR", mem_dir)
    store = MemoryStore()
    store.load_from_disk()
    monkeypatch.setattr(memory_module, "memory_store", store)
    return mem_dir


@pytest.mark.asyncio
async def test_memory_review_merges_and_prunes_facts(isolated_facts, monkeypatch):
    monkeypatch.setattr("models.build_main_llm", lambda: _EditorialModel())
    monkeypatch.setattr("agent.get_agent_tools", lambda: [build_memory_tool()])
    monkeypatch.setattr(nudge_mod, "state_register_mem", _FakeStateRegister())

    await nudge_mod._nudge_memory("sess-facts-editor", "sys", [HumanMessage("hi")])

    text = (isolated_facts / "FACTS.md").read_text(encoding="utf-8")
    assert "pitfall A and B merged" in text
    assert "pitfall B" not in text
    assert "outdated C" not in text
