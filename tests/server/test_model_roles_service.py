"""The effective judge/role model service (the Model Config inheritance labels).

The panel edits families; this service answers what each JUDGE / ROLE will
actually run on, and why: its own configured family, its local branch, or the
inherited tier (TaskFlow → auxiliary, a role → its definition's tier). Every
branch is fail-open, so a missing config file still yields one row per role.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import server.service.model_roles_service as service
from server.trigger.http import sherry_config

pytestmark = [pytest.mark.unit]


@pytest.fixture
def values(monkeypatch):
    """Replace the live .models read with a per-test mapping."""
    current: dict[str, str] = {}
    monkeypatch.setattr(service, "read_models_raw_values", lambda: dict(current))
    return current


def _rows() -> dict[str, dict]:
    return {row["role"]: row for row in service.read_model_roles()["roles"]}


def test_every_judge_and_role_has_a_row(values):
    rows = _rows()

    assert set(rows) == {
        "step_judge",
        "completion_judge",
        "executor",
        "researcher",
        "reviewer",
        "librarian",
    }


def test_an_empty_role_family_reports_the_definition_tier(values, monkeypatch):
    monkeypatch.setattr(service, "_role_tier", lambda _role: "auxiliary")
    values.update({"AUXILIARY_LLM_PROVIDER": "deepseek", "AUXILIARY_LLM_API_NAME": "chat"})

    row = _rows()["executor"]

    assert row["configured"] is False
    assert row["source"] == "auxiliary"
    assert row["effective_provider"] == "deepseek"
    assert row["effective_model"] == "chat"


def test_a_configured_role_family_reports_itself(values):
    values.update(
        {
            "EXECUTOR_LLM_PROVIDER": "anthropic",
            "EXECUTOR_LLM_API_NAME": "claude-sonnet",
            "EXECUTOR_LLM_MAX_OUTPUT_TOKEN": "16384",
        }
    )

    row = _rows()["executor"]

    assert row["configured"] is True
    assert row["source"] == "configured"
    assert row["effective_provider"] == "anthropic"
    assert row["effective_model"] == "claude-sonnet"
    assert row["max_output_tokens"] == 16384


def test_a_local_role_family_reports_the_main_model(values):
    values.update(
        {
            "REVIEWER_LLM_MODEL_LOCAL": "true",
            "MAIN_LLM_PROVIDER": "zhipu",
            "MAIN_LLM_NAME": "glm-5",
        }
    )

    row = _rows()["reviewer"]

    assert row["local"] is True
    assert row["source"] == "local"
    assert row["effective_model"] == "glm-5"


def test_an_inherit_role_reports_depth(values, monkeypatch):
    monkeypatch.setattr(service, "_role_tier", lambda _role: "inherit")

    row = _rows()["librarian"]

    assert row["source"] == "depth"
    assert row["effective_model"] == ""


def test_the_completion_judge_is_the_auxiliary_family(values):
    values.update({"AUXILIARY_LLM_PROVIDER": "deepseek", "AUXILIARY_LLM_API_NAME": "chat"})

    row = _rows()["completion_judge"]

    assert row["family"] == "AUXILIARY_LLM"
    assert row["source"] == "auxiliary"
    assert row["effective_model"] == "chat"


def test_the_step_judge_follows_auxiliary_when_taskflow_is_empty(values):
    values.update({"AUXILIARY_LLM_API_NAME": "aux-model"})

    row = _rows()["step_judge"]

    assert row["family"] == "TASKFLOW_LLM"
    assert row["source"] == "auxiliary"
    assert row["effective_model"] == "aux-model"


def test_the_route_answers_the_service_shape(monkeypatch):
    monkeypatch.setattr(
        sherry_config,
        "read_model_roles",
        lambda: {"roles": [{"role": "executor", "source": "auxiliary"}]},
    )

    response = asyncio.run(sherry_config.read_model_roles_handler(None))
    payload = json.loads(response.description)

    assert payload["roles"][0]["role"] == "executor"
