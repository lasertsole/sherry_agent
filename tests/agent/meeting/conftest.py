"""Shared fixtures for the meeting-mode tests."""

from __future__ import annotations

from pathlib import Path

import pytest_asyncio

import agent.meeting.persistence as persistence


@pytest_asyncio.fixture
async def store(tmp_path: Path):
    """An isolated meeting store backed by a throwaway registry DB."""
    persistence._reset_store_for_tests(tmp_path / "registry.db")
    store = persistence.MeetingStore()
    await store.ensure_db()
    return store
