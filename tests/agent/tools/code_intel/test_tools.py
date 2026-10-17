"""Unit tests for the LangChain code_intel tool wrappers."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agent.tools.code_intel import build_code_intel_tools
from config.features.agent_side import CODE_INTEL_SEMANTIC, CodeIntelConfig, CodeIntelSemanticConfig

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class _FakeEmbedding:
    """Deterministic stand-in; these tests never call the semantic tool."""

    dim = 8

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * self.dim for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        return [0.1] * self.dim


def _semantic_config(enabled: str = "on") -> CodeIntelSemanticConfig:
    config = CodeIntelSemanticConfig(**CODE_INTEL_SEMANTIC)
    config["code_intel_semantic_enabled"] = enabled
    return config


@pytest.fixture()
def tools(sample_repo: Path, index_db: Path, intel_config: CodeIntelConfig) -> list:
    return build_code_intel_tools(
        "sess-1",
        config=intel_config,
        semantic_config=_semantic_config(),
        root=sample_repo,
        db_path=str(index_db),
        embed_model=_FakeEmbedding(),
    )


def test_semantic_layer_can_be_disabled(
    sample_repo: Path, index_db: Path, intel_config: CodeIntelConfig
) -> None:
    tools = build_code_intel_tools(
        "sess-off",
        config=intel_config,
        semantic_config=_semantic_config("off"),
        root=sample_repo,
        db_path=str(index_db),
        embed_model=_FakeEmbedding(),
    )
    assert [tool.name for tool in tools] == [
        "explore",
        "callers",
        "callees",
        "impact",
    ]


def test_default_auto_stays_off_without_the_opt_in_marker(
    sample_repo: Path,
    index_db: Path,
    intel_config: CodeIntelConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agent.tools.code_intel.tools.code_embedder_enabled", lambda profile: False)
    tools = build_code_intel_tools(
        "sess-auto-off",
        config=intel_config,
        semantic_config=CodeIntelSemanticConfig(**CODE_INTEL_SEMANTIC),
        root=sample_repo,
        db_path=str(index_db),
    )
    assert [tool.name for tool in tools] == [
        "explore",
        "callers",
        "callees",
        "impact",
    ]


def test_bge_is_not_a_code_intel_embedder(
    sample_repo: Path, index_db: Path, intel_config: CodeIntelConfig
) -> None:
    semantic = _semantic_config()
    semantic["code_intel_semantic_model"] = "bge-m3"
    tools = build_code_intel_tools(
        "sess-bge",
        config=intel_config,
        semantic_config=semantic,
        root=sample_repo,
        db_path=str(index_db),
        embed_model=_FakeEmbedding(),
    )
    assert [tool.name for tool in tools] == [
        "explore",
        "callers",
        "callees",
        "impact",
    ]


def test_tool_names_and_scope_metadata(tools: list) -> None:
    assert [tool.name for tool in tools] == [
        "explore",
        "callers",
        "callees",
        "impact",
        "semantic_code_search",
    ]
    for tool in tools:
        assert tool.metadata["scope"] == "researcher_only"
        assert tool.description


def test_session_id_injected(tools: list) -> None:
    for tool in tools:
        assert tool._session_id == "sess-1"


def test_explore_tool_returns_json(tools: list) -> None:
    payload = json.loads(tools[0]._run(query="top_func"))
    assert payload["count"] > 0
    assert any(result["name"] == "top_func" for result in payload["results"])


def test_callers_tool_returns_json(tools: list) -> None:
    payload = json.loads(tools[1]._run(symbol="helper"))
    assert payload["count"] > 0
    assert any(caller["name"] == "top_func" for caller in payload["callers"])


def test_callees_tool_returns_json(tools: list) -> None:
    payload = json.loads(tools[2]._run(symbol="top_func"))
    assert any(callee["name"] == "helper" for callee in payload["callees"])


def test_impact_tool_returns_json(tools: list) -> None:
    payload = json.loads(tools[3]._run(symbol="helper"))
    assert payload["count"] > 0
    assert any(entry["name"] in {"top_func", "TopFunc"} for entry in payload["affected"])


def test_unknown_symbol_is_empty_not_error(tools: list) -> None:
    payload = json.loads(tools[1]._run(symbol="no_such_symbol_abc"))
    assert payload["count"] == 0
    assert payload["callers"] == []


def test_async_arun_returns_json(tools: list) -> None:
    payload = json.loads(asyncio.run(tools[0]._arun(query="top_func")))
    assert payload["count"] > 0


def test_args_schemas(tools: list) -> None:
    assert "query" in tools[0].args_schema.model_fields
    assert "symbol" in tools[1].args_schema.model_fields
    assert "symbol" in tools[2].args_schema.model_fields
    assert "symbol" in tools[3].args_schema.model_fields
    assert set(tools[4].args_schema.model_fields) == {"query", "top_k"}


# ---------------------------------------------------------------------------
# The index root is resolved PER CALL, like ast-grep and LSP do
# ---------------------------------------------------------------------------


class _InMemoryRegisterDB:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], object] = {}

    def get_state(self, session_id: str, key: str, default: object = None) -> object:
        return self.store.get((session_id, key), default)

    def set_state(self, session_id: str, key: str, value: object) -> bool:
        self.store[(session_id, key)] = value
        return True

    def delete_state(self, session_id: str, key: str) -> bool:
        self.store.pop((session_id, key), None)
        return True


@pytest.fixture()
def _isolated_register(monkeypatch):
    import runtime
    from runtime.session.state_register import state_register_mem

    monkeypatch.setattr(runtime, "state_register_db", _InMemoryRegisterDB())
    monkeypatch.delenv("SHERRY_CODE_INTEL_ROOT", raising=False)
    yield
    state_register_mem.clear_session("sess-bound")


def test_the_index_root_follows_the_session_binding(
    index_db: Path, intel_config: CodeIntelConfig, tmp_path: Path, _isolated_register
) -> None:
    """A session's project directory is the root, not the process cwd.

    The tools are built once per spawn, so a construction-time capture would pin
    every session (and every directory switch) to the first root — the same trap
    the LSP and ast-grep tools avoid.
    """
    from runtime.session.state_register import state_register_mem
    from runtime.session.state_keys import StateKey

    first = tmp_path / "proj-one"
    second = tmp_path / "proj-two"
    first.mkdir()
    second.mkdir()

    tools = build_code_intel_tools("sess-bound", config=intel_config, db_path=str(index_db))
    assert tools, "the tool family must build"

    state_register_mem.set_state("sess-bound", StateKey.PROJECT_DIR, str(first))
    assert tools[0]._session_root() == first.resolve()

    # A directory switch at the turn boundary is visible on the NEXT call.
    state_register_mem.set_state("sess-bound", StateKey.PROJECT_DIR, str(second))
    assert tools[0]._session_root() == second.resolve()
    assert all(tool._session_root() == second.resolve() for tool in tools)


def test_an_explicit_root_outranks_the_session_binding(
    sample_repo: Path,
    index_db: Path,
    intel_config: CodeIntelConfig,
    tmp_path: Path,
    _isolated_register,
) -> None:
    """Tests/drills pin the root; the binding cannot move it."""
    from runtime.session.state_register import state_register_mem
    from runtime.session.state_keys import StateKey

    other = tmp_path / "elsewhere"
    other.mkdir()
    state_register_mem.set_state("sess-bound", StateKey.PROJECT_DIR, str(other))

    tools = build_code_intel_tools(
        "sess-bound", config=intel_config, root=sample_repo, db_path=str(index_db)
    )
    assert tools[0]._session_root() == sample_repo.resolve()
