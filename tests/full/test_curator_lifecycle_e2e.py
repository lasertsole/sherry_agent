"""Live-network e2e: curator lifecycle on real components and a real skills tree.

LIVE-NETWORK, RUN EXPLICITLY (same policy as ``test_context_governance_e2e.py``):
a real sandbox skills tree, the real curator transitions/usage code, and the
real main LLM for umbrella generation. The sandbox is a tmp dir so no real
``skills/auto`` content is touched; ``tmp_path`` + ``monkeypatch`` remove it
after the test.

Marker policy: tagged ``llm_e2e``, so a bare ``pytest`` run deselects it via the
``pyproject.toml`` addopts and the hermetic CI gate never collects this file;
``tests/run_tests_split.py`` additionally ``--ignore``s ``tests/full/`` outright.

Run with::

    uv run --no-sync pytest -m llm_e2e tests/full/test_curator_lifecycle_e2e.py -v
"""

from __future__ import annotations

import re
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC

import pytest

import models
from agent.skill_write_provider import AgentSkillWriteProvider
from agent.tools.pub_base import skill_usage as agent_skill_usage
from agent.tools.skill_tools import skill_manage
from context_engine.curator import constants as curator_constants
from context_engine.curator import orchestrator
from context_engine.curator import usage as curator_usage
from context_engine.curator.transitions import apply_automatic_transitions
from runtime import data_provider

pytestmark = [pytest.mark.llm_e2e, pytest.mark.timeout(600)]


@dataclass(frozen=True)
class _Sandbox:
    auto: Path
    archive: Path
    usage_dir: Path


@pytest.fixture
def curator_sandbox(tmp_path, monkeypatch):
    auto = tmp_path / "skills" / "auto"
    usage_dir = auto / ".usage"
    archive = tmp_path / "skills" / ".archive"
    usage_dir.mkdir(parents=True)

    monkeypatch.setattr(curator_constants, "AUTO_SKILLS_DIR", auto)
    monkeypatch.setattr(curator_constants, "ARCHIVE_DIR", archive)
    monkeypatch.setattr(curator_usage, "USAGE_DIR", usage_dir)
    monkeypatch.setattr(skill_manage, "AUTO_SKILLS_DIR", auto)
    monkeypatch.setattr(agent_skill_usage, "AUTO_SKILLS_DIR", auto)
    monkeypatch.setattr(orchestrator, "_schedule_system_prompt_refresh", lambda: None)
    data_provider.clear_skill_write_provider()
    yield _Sandbox(auto=auto, archive=archive, usage_dir=usage_dir)
    data_provider.clear_skill_write_provider()
    orchestrator._provider_misses_logged.clear()


def _make_skill(sandbox: _Sandbox, name: str, body: str = "Body") -> None:
    skill_dir = sandbox.auto / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill\n---\n\n{body} of {name}.\n",
        encoding="utf-8",
    )


def _seed_record(sandbox: _Sandbox, name: str, **overrides: object) -> None:
    rec = curator_usage._default_record(name)
    rec.update(overrides)
    rec["_persisted"] = True
    curator_usage.save_record(name, rec)


class _CountingLLM:
    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.calls: list[object] = []

    def invoke(self, prompt: object, config: object = None) -> object:
        self.calls.append(prompt)
        return self._inner.invoke(prompt, config=config)  # type: ignore[attr-defined]


def _normalize_umbrella(name: str, content: str) -> str:
    body = content
    if content.startswith("---"):
        end = re.search(r"\n---\s*\n", content[3:])
        if end:
            body = content[end.end() + 3 :]
    return (
        f"---\nname: {name}\ndescription: umbrella generated from real LLM output\n"
        f"created_by: curator\n---\n\n{body.strip()}\n"
    )


def test_ninety_day_archive_then_restore_round_trip(curator_sandbox: _Sandbox) -> None:
    now = datetime.now(UTC)
    _make_skill(curator_sandbox, "docker", body="container workflow")
    _seed_record(
        curator_sandbox,
        "docker",
        use_count=4,
        last_activity_at=(now - timedelta(days=120)).isoformat(),
    )

    counts = apply_automatic_transitions(now=now)

    assert counts["archived"] == 1
    assert (curator_sandbox.archive / "docker" / "SKILL.md").is_file()
    assert not (curator_sandbox.auto / "docker").exists()

    ok, msg = agent_skill_usage.restore_skill("docker")

    assert ok, msg
    assert (curator_sandbox.auto / "docker" / "SKILL.md").is_file()
    assert not (curator_sandbox.archive / "docker").exists()


def test_real_llm_umbrella_merge_archives_source_with_provenance(
    curator_sandbox: _Sandbox, monkeypatch
) -> None:
    umbrella = "umbrella-e2e-merge"
    _make_skill(curator_sandbox, "alpha", body="Alpha reusable technique for parsing")
    _seed_record(
        curator_sandbox, "alpha", use_count=2, last_activity_at=datetime.now(UTC).isoformat()
    )
    data_provider.set_skill_write_provider(AgentSkillWriteProvider())

    counting = _CountingLLM(models.build_main_llm(temperature=0.3))
    monkeypatch.setattr(models, "build_main_llm", lambda temperature=0.3: counting)

    generated, supporting = orchestrator._generate_umbrella_skill(
        umbrella,
        ["- alpha: overlaps with the umbrella scope"],
        "Alpha body: reusable parsing technique.",
        "",
    )
    assert counting.calls, "the real main LLM must be invoked for umbrella generation"
    assert generated.strip(), "the real main LLM returned empty umbrella content"
    normalized = _normalize_umbrella(umbrella, generated)
    monkeypatch.setattr(
        orchestrator, "_generate_umbrella_skill", lambda *a, **k: (normalized, supporting)
    )

    summary = (
        f"```yaml\nconsolidations:\n  - from: alpha\n    into: {umbrella}\n    reason: overlap\n```"
    )
    orchestrator._apply_consolidation(summary)

    assert (curator_sandbox.auto / umbrella / "SKILL.md").is_file()
    assert not (curator_sandbox.auto / "alpha").exists()
    assert (curator_sandbox.archive / "alpha" / "SKILL.md").is_file()
    marker = curator_sandbox.archive / "alpha" / curator_constants.ABSORBED_INTO_FILE
    assert marker.read_text(encoding="utf-8") == f"{umbrella}\n"

    ok, msg = agent_skill_usage.restore_skill("alpha")

    assert ok, msg
    restored = curator_sandbox.auto / "alpha" / curator_constants.ABSORBED_INTO_FILE
    assert restored.read_text(encoding="utf-8") == f"{umbrella}\n"
