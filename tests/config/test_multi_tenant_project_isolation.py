"""Deployment-level isolation guarantees for per-tenant project directories.

The multi-tenant plan deliberately changes no code: ``coordination.db`` is
per-project by design, and an unconfigured session keeps the historical
ROOT_DIR fallback. These tests pin the two facts a deployment must rely on, so
a future change cannot silently move either path under the code directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.tools.pub_base.coordination import coordination_db_path
from config import path as config_path

pytestmark = [pytest.mark.unit]


def test_each_project_gets_its_own_coordination_db(tmp_path: Path):
    tenant_a = tmp_path / "cells" / "a" / "workspace"
    tenant_b = tmp_path / "cells" / "b" / "workspace"

    first = coordination_db_path(tenant_a)
    second = coordination_db_path(tenant_b)

    assert first == tenant_a / ".sherry" / "coordination.db"
    assert second == tenant_b / ".sherry" / "coordination.db"
    assert first != second


def test_project_dir_prefers_the_per_tenant_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tenant = tmp_path / "cells" / "a" / "workspace"
    tenant.mkdir(parents=True)
    monkeypatch.setenv("SHERRY_PROJECT_DIR", str(tenant))

    assert config_path.resolve_default_project_dir() == tenant.resolve()


def test_an_unconfigured_project_dir_keeps_the_documented_fallback(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SHERRY_PROJECT_DIR", raising=False)
    monkeypatch.setattr(config_path, "get_sherry_setting", lambda _key: "")

    assert config_path.resolve_default_project_dir() == config_path.ROOT_DIR
