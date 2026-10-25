"""Runtime gate for the optional internal families (T5 / C6).

`agent.core._assert_max_token` is the second line of defense: a mid-run edit
that drops a configured family below 200K must refuse to build the graph, just
like the main/auxiliary keys. An UNSET optional family is valid — it follows
the auxiliary model, whose own check already ran.
"""

import pytest

from agent.core import _assert_max_token
from config.features.agent_side.token_guard import TokenGuardError

pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def _valid_base_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MAIN_LLM_MAX_TOKEN", "200000")
    monkeypatch.setenv("AUXILIARY_LLM_MAX_TOKEN", "200000")
    for key in (
        "MAIN_LLM_MAX_OUTPUT_TOKEN",
        "AUXILIARY_LLM_MAX_OUTPUT_TOKEN",
        "TASKFLOW_LLM_MAX_OUTPUT_TOKEN",
        "COMPRESSION_LLM_MAX_OUTPUT_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)


def test_unset_optional_families_are_valid(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("TASKFLOW_LLM_MAX_TOKEN", raising=False)
    monkeypatch.delenv("COMPRESSION_LLM_MAX_TOKEN", raising=False)

    _assert_max_token()


@pytest.mark.parametrize("key", ["TASKFLOW_LLM_MAX_TOKEN", "COMPRESSION_LLM_MAX_TOKEN"])
def test_a_sub_200k_family_aborts(monkeypatch: pytest.MonkeyPatch, key):
    monkeypatch.setenv(key, "65536")

    with pytest.raises(TokenGuardError, match=key):
        _assert_max_token()


@pytest.mark.parametrize(
    "key", ["TASKFLOW_LLM_MAX_OUTPUT_TOKEN", "COMPRESSION_LLM_MAX_OUTPUT_TOKEN"]
)
def test_a_sub_8k_output_cap_aborts(monkeypatch: pytest.MonkeyPatch, key):
    monkeypatch.setenv(key, "4096")

    with pytest.raises(TokenGuardError, match=key):
        _assert_max_token()
