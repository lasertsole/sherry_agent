"""KnowledgeStore success-step layers (task structured + plan aggregate)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.tools.todolist.knowledge.identity import PlanIdentity
from agent.tools.todolist.knowledge.knowledge_store import KnowledgeStore

pytestmark = [pytest.mark.unit]

_KEY = "0123456789ab"


def _identity(plan_name: str = "p") -> PlanIdentity:
    return PlanIdentity(
        key=_KEY,
        plan_name=plan_name,
        plan_ref=Path("/repo/workspace/sessions/s/plans") / f"{plan_name}.md",
    )


@pytest.fixture()
def knowledge_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "plans"
    monkeypatch.setattr(
        "agent.tools.todolist.knowledge.knowledge_store._KNOWLEDGE_ROOT",
        root,
    )
    return root


@pytest.mark.asyncio
async def test_task_success_steps_render(knowledge_root: Path):
    identity = _identity()
    await KnowledgeStore.write(
        identity,
        layer="task",
        position=0,
        data={
            "method": "m",
            "success_steps": [
                {"step": "install", "tool": "terminal", "params": "uv add x", "outcome": "ok"},
                {
                    "step": "verify",
                    "tool": "terminal",
                    "params": "uv run pytest",
                    "outcome": "green",
                },
            ],
        },
    )

    text = KnowledgeStore.read_formatted(identity, layer="task", position=0)

    assert "## Success Steps" in text
    assert "install (terminal, uv add x) -> ok" in text
    assert "verify (terminal, uv run pytest) -> green" in text


@pytest.mark.asyncio
async def test_plan_aggregate_success_steps_are_readable(knowledge_root: Path):
    identity = _identity()
    await KnowledgeStore.write(
        identity,
        layer="plan",
        data={
            "method": "m",
            "aggregate_success_steps": [
                {"step": "install", "tool": "terminal", "params": "uv add x", "outcome": "ok"},
            ],
        },
    )

    overview = KnowledgeStore.read_formatted(identity)
    success = KnowledgeStore.read_formatted(identity, layer="success")

    assert "Aggregate success steps: 1" in overview
    assert "# Success Steps" in success
    assert "install (terminal, uv add x) -> ok" in success


@pytest.mark.asyncio
async def test_success_layer_file_is_supported(knowledge_root: Path):
    identity = _identity()
    await KnowledgeStore.write(
        identity,
        layer="success",
        data={
            "aggregate_success_steps": [
                {"step": "ship", "tool": "terminal", "params": "git push", "outcome": "ok"},
            ]
        },
    )

    assert (knowledge_root / _KEY / "success-steps.json").is_file()
    assert "ship (terminal, git push) -> ok" in KnowledgeStore.read_formatted(
        identity, layer="success"
    )


@pytest.mark.asyncio
async def test_old_failed_paths_tree_falls_back(knowledge_root: Path):
    identity = _identity()
    await KnowledgeStore.write(
        identity,
        layer="task",
        position=0,
        data={"failed_paths_tree": ["old: reason (source: depth-1:a)"]},
    )

    text = KnowledgeStore.read_formatted(identity, layer="task", position=0)

    assert "## Failed Branches (traceable)" in text
    assert "old: reason" in text
