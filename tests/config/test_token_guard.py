"""Unit contract for ``config/features/agent_side/token_guard.py``.

The 128K floor is the single source of truth shared by the server boot gate,
``built_agent()``, the subagent spawn path and the .env write path.
"""

import pytest

from config.features import (
    MIN_REQUIRED_MAX_TOKEN,
    TokenGuardError,
    assert_max_token_valid,
)
from config.features.agent_side import token_guard

pytestmark = [pytest.mark.unit]


def test_threshold_is_128k():
    assert token_guard.MIN_REQUIRED_MAX_TOKEN == 131_072
    assert MIN_REQUIRED_MAX_TOKEN == 131_072


class TestAssertMaxTokenValid:
    def test_none_value_raises(self):
        with pytest.raises(TokenGuardError, match="not set"):
            assert_max_token_valid("MAIN_LLM_MAX_TOKEN", None)

    def test_value_below_minimum_raises(self):
        with pytest.raises(TokenGuardError, match="below the minimum"):
            assert_max_token_valid("MAIN_LLM_MAX_TOKEN", MIN_REQUIRED_MAX_TOKEN - 1)

    def test_error_names_the_offending_key(self):
        with pytest.raises(TokenGuardError, match="AUXILIARY_LLM_MAX_TOKEN"):
            assert_max_token_valid("AUXILIARY_LLM_MAX_TOKEN", 65_536)

    def test_value_at_minimum_passes(self):
        assert_max_token_valid("MAIN_LLM_MAX_TOKEN", MIN_REQUIRED_MAX_TOKEN)

    def test_value_above_minimum_passes(self):
        assert_max_token_valid("AUXILIARY_LLM_MAX_TOKEN", MIN_REQUIRED_MAX_TOKEN * 2)
