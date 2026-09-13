"""Regression tests: ContextEngineHook must import require_session_id.

The 1.1.10 refactor moved session-id extraction into the shared helper
``agent.middlewares.base.require_session_id``. ``context_engine/core.py``
was rewired to call it via ``_get_session_id_or_raise`` but the import was
missed, so every model call raised ``NameError`` and the whole agent turn
died before streaming anything (user-visible: no reply bubbles in the
client chat). These tests lock the wiring in.
"""

import pytest

from agent.middlewares.context_engine.core import ContextEngineHook

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class TestContextEngineSessionGuard:
    def test_returns_session_id(self):
        assert ContextEngineHook._get_session_id_or_raise({"session_id": "s1"}) == "s1"

    def test_missing_session_raises_runtime_error_not_name_error(self):
        """Must raise RuntimeError (guard) — a NameError means the shared
        helper import is missing from context_engine/core.py."""
        with pytest.raises(RuntimeError, match="Not pass session_id"):
            ContextEngineHook._get_session_id_or_raise({})
