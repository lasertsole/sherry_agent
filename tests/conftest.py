"""Shared pytest fixtures for the EMA_AI_agent test suite."""

import pytest
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock


def pytest_configure(config):
    """Register custom markers and set asyncio mode."""
    config.addinivalue_line("markers", "asyncio: mark test as async")
    config.addinivalue_line("markers", "unit: mark test as unit test")
    config.addinivalue_line("markers", "module: mark test as module test")
    config.addinivalue_line("markers", "system: mark test as system test")


@pytest.fixture
def unit_test_config():
    """Patch ROOT_DIR and AUTO_SKILLS_DIR to temp directories for isolated tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        with patch("config.path.ROOT_DIR", tmp_path), \
             patch("config.path.SRC_DIR", tmp_path / "src"), \
             patch("config.path.AUTO_SKILLS_DIR", tmp_path / "skills" / "auto"), \
             patch("config.path.WORKSPACE_DIR", tmp_path / "workspace"), \
             patch("config.path.TEMP_DIR", tmp_path / "temp"), \
             patch("config.path.MODELS_DIR", tmp_path / "models"), \
             patch("config.path.SKILLS_DIR", tmp_path / "skills"):
            yield tmp_path


@pytest.fixture
def tmp_skills_dir(unit_test_config):
    """Create a temp auto-skills directory with test SKILL.md files."""
    skills_dir = unit_test_config / "skills" / "auto"
    skills_dir.mkdir(parents=True, exist_ok=True)

    # Create a test skill
    test_skill_dir = skills_dir / "test_skill"
    test_skill_dir.mkdir()
    (test_skill_dir / "SKILL.md").write_text(
        "---\nname: test_skill\ndescription: A test skill\n---\n\nThis is a test skill body.",
        encoding="utf-8"
    )

    return skills_dir


@pytest.fixture
def message_bus():
    """Create a fresh MessageBus instance for bus tests."""
    from bus.core import MessageBus
    return MessageBus()


@pytest.fixture(autouse=True)
def clean_registers():
    """Clear all register sessions before each test to prevent state leakage."""
    from runtime import clear_all_register_sessions
    yield
    # Cleanup after test if needed


@pytest.fixture
def mock_state_register_mem():
    """Provide a clean StateRegisterMeM instance for module tests."""
    from runtime.state_register import StateRegisterMeM
    # Force a fresh instance by clearing the singleton
    from runtime.core import Register
    if StateRegisterMeM in Register._instances:
        del Register._instances[StateRegisterMeM]
    reg = StateRegisterMeM()
    yield reg
    # Cleanup: clear all sessions
    for session_id in list(reg._states.keys()):
        reg.clear_session(session_id)


@pytest.fixture
def tmp_sqlite_db():
    """Create a temporary SQLite database for StateRegisterDB tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_state.db"
        yield db_path
