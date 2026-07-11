"""Module tests for agent/tools/pub_base/skill_usage.py — sidecar usage telemetry."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch
from agent.tools.pub_base.skill_usage import (
    _empty_record, latest_activity_at, activity_count,
    load_usage, save_usage, get_record, _mutate,
    bump_use, bump_view, bump_patch, mark_agent_created,
    set_state, set_pinned, forget,
    STATE_ACTIVE, STATE_STALE, STATE_ARCHIVED, _VALID_STATES,
)


class TestEmptyRecord:
    def test_fields(self):
        rec = _empty_record()
        assert rec["created_by"] is None
        assert rec["use_count"] == 0
        assert rec["view_count"] == 0
        assert rec["patch_count"] == 0
        assert rec["last_used_at"] is None
        assert rec["last_viewed_at"] is None
        assert rec["last_patched_at"] is None
        assert rec["state"] == STATE_ACTIVE
        assert rec["pinned"] is False
        assert rec["archived_at"] is None


class TestLatestActivityAt:
    def test_no_activity(self):
        rec = _empty_record()
        assert latest_activity_at(rec) is None

    def test_use_only(self):
        rec = _empty_record()
        rec["last_used_at"] = "2026-01-01T00:00:00+00:00"
        assert latest_activity_at(rec) == "2026-01-01T00:00:00+00:00"

    def test_newest_wins(self):
        rec = _empty_record()
        rec["last_used_at"] = "2026-01-01T00:00:00+00:00"
        rec["last_viewed_at"] = "2026-06-01T00:00:00+00:00"
        rec["last_patched_at"] = "2026-03-01T00:00:00+00:00"
        result = latest_activity_at(rec)
        assert result == "2026-06-01T00:00:00+00:00"


class TestActivityCount:
    def test_zero(self):
        rec = _empty_record()
        assert activity_count(rec) == 0

    def test_sum(self):
        rec = _empty_record()
        rec["use_count"] = 3
        rec["view_count"] = 5
        rec["patch_count"] = 2
        assert activity_count(rec) == 10

    def test_missing_fields(self):
        rec = {"use_count": 2}
        assert activity_count(rec) == 2


class TestGetRecord:
    def test_missing_returns_empty(self):
        with patch("agent.tools.pub_base.skill_usage.load_usage", return_value={}):
            rec = get_record("nonexistent")
            assert rec["use_count"] == 0
            assert rec["state"] == STATE_ACTIVE

    def test_existing_record(self):
        data = {"my_skill": {"use_count": 7, "state": STATE_STALE}}
        with patch("agent.tools.pub_base.skill_usage.load_usage", return_value=data):
            rec = get_record("my_skill")
            assert rec["use_count"] == 7
            assert rec["state"] == STATE_STALE


class TestMutate:
    def test_bundled_skill_skipped(self):
        """Bundled skills should not be mutated."""
        with patch("agent.tools.pub_base.skill_usage.is_agent_created", return_value=False):
            # Should not raise or write anything
            _mutate("bundled_skill", lambda rec: rec.__setitem__("use_count", 999))

    def test_empty_name_skipped(self):
        _mutate("", lambda rec: None)  # Should not raise


class TestBumpFunctions:
    def test_bump_use(self):
        data = {"test_skill": _empty_record()}
        data["test_skill"]["created_by"] = "agent"
        with patch("agent.tools.pub_base.skill_usage.is_agent_created", return_value=True), \
             patch("agent.tools.pub_base.skill_usage.load_usage", return_value=data), \
             patch("agent.tools.pub_base.skill_usage.save_usage") as mock_save:
            bump_use("test_skill")
            saved_data = mock_save.call_args[0][0]
            assert saved_data["test_skill"]["use_count"] == 1
            assert saved_data["test_skill"]["last_used_at"] is not None

    def test_bump_view(self):
        data = {"test_skill": _empty_record()}
        data["test_skill"]["created_by"] = "agent"
        with patch("agent.tools.pub_base.skill_usage.is_agent_created", return_value=True), \
             patch("agent.tools.pub_base.skill_usage.load_usage", return_value=data), \
             patch("agent.tools.pub_base.skill_usage.save_usage") as mock_save:
            bump_view("test_skill")
            saved_data = mock_save.call_args[0][0]
            assert saved_data["test_skill"]["view_count"] == 1

    def test_bump_patch(self):
        data = {"test_skill": _empty_record()}
        data["test_skill"]["created_by"] = "agent"
        with patch("agent.tools.pub_base.skill_usage.is_agent_created", return_value=True), \
             patch("agent.tools.pub_base.skill_usage.load_usage", return_value=data), \
             patch("agent.tools.pub_base.skill_usage.save_usage") as mock_save:
            bump_patch("test_skill")
            saved_data = mock_save.call_args[0][0]
            assert saved_data["test_skill"]["patch_count"] == 1


class TestMarkAgentCreated:
    def test_marks_created(self):
        data = {"test_skill": _empty_record()}
        with patch("agent.tools.pub_base.skill_usage.is_agent_created", return_value=True), \
             patch("agent.tools.pub_base.skill_usage.load_usage", return_value=data), \
             patch("agent.tools.pub_base.skill_usage.save_usage") as mock_save:
            mark_agent_created("test_skill")
            saved_data = mock_save.call_args[0][0]
            assert saved_data["test_skill"]["created_by"] == "agent"


class TestSetState:
    def test_valid_state(self):
        data = {"test_skill": _empty_record()}
        data["test_skill"]["created_by"] = "agent"
        with patch("agent.tools.pub_base.skill_usage.is_agent_created", return_value=True), \
             patch("agent.tools.pub_base.skill_usage.load_usage", return_value=data), \
             patch("agent.tools.pub_base.skill_usage.save_usage") as mock_save:
            set_state("test_skill", STATE_STALE)
            saved_data = mock_save.call_args[0][0]
            assert saved_data["test_skill"]["state"] == STATE_STALE

    def test_invalid_state_noop(self):
        """Invalid state should not trigger save."""
        with patch("agent.tools.pub_base.skill_usage.save_usage") as mock_save:
            set_state("test_skill", "invalid_state")
            mock_save.assert_not_called()

    def test_archived_sets_timestamp(self):
        data = {"test_skill": _empty_record()}
        data["test_skill"]["created_by"] = "agent"
        with patch("agent.tools.pub_base.skill_usage.is_agent_created", return_value=True), \
             patch("agent.tools.pub_base.skill_usage.load_usage", return_value=data), \
             patch("agent.tools.pub_base.skill_usage.save_usage") as mock_save:
            set_state("test_skill", STATE_ARCHIVED)
            saved_data = mock_save.call_args[0][0]
            assert saved_data["test_skill"]["archived_at"] is not None

    def test_active_clears_timestamp(self):
        data = {"test_skill": _empty_record()}
        data["test_skill"]["archived_at"] = "2026-01-01"
        data["test_skill"]["created_by"] = "agent"
        with patch("agent.tools.pub_base.skill_usage.is_agent_created", return_value=True), \
             patch("agent.tools.pub_base.skill_usage.load_usage", return_value=data), \
             patch("agent.tools.pub_base.skill_usage.save_usage") as mock_save:
            set_state("test_skill", STATE_ACTIVE)
            saved_data = mock_save.call_args[0][0]
            assert saved_data["test_skill"]["archived_at"] is None


class TestSetPinned:
    def test_pin(self):
        data = {"test_skill": _empty_record()}
        data["test_skill"]["created_by"] = "agent"
        with patch("agent.tools.pub_base.skill_usage.is_agent_created", return_value=True), \
             patch("agent.tools.pub_base.skill_usage.load_usage", return_value=data), \
             patch("agent.tools.pub_base.skill_usage.save_usage") as mock_save:
            set_pinned("test_skill", True)
            saved_data = mock_save.call_args[0][0]
            assert saved_data["test_skill"]["pinned"] is True


class TestForget:
    def test_removes_entry(self):
        data = {"test_skill": _empty_record(), "other": _empty_record()}
        with patch("agent.tools.pub_base.skill_usage.load_usage", return_value=data), \
             patch("agent.tools.pub_base.skill_usage.save_usage") as mock_save:
            forget("test_skill")
            saved_data = mock_save.call_args[0][0]
            assert "test_skill" not in saved_data
            assert "other" in saved_data

    def test_empty_name_noop(self):
        forget("")  # Should not raise
