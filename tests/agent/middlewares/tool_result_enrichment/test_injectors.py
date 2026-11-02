"""Tests for the default tool-result injectors."""

from __future__ import annotations

import pytest
from langchain_core.messages import ToolMessage

from agent.middlewares.tool_result_enrichment import injectors
from agent.middlewares.tool_result_enrichment.injectors import (
    build_hook_context,
    clear_hook_context,
    stash_hook_context,
)


pytestmark = [pytest.mark.unit]


def _result() -> ToolMessage:
    return ToolMessage(content="ok", tool_call_id="call-1", name="terminal")


@pytest.fixture(autouse=True)
def _clean_hook_context():
    clear_hook_context("sess-1")
    yield
    clear_hook_context("sess-1")


def test_hook_context_is_drained_once():
    stash_hook_context("sess-1", "first")
    stash_hook_context("sess-1", "second")

    text = build_hook_context("write_file", {}, _result(), "sess-1")

    assert text == "[Hook context]: first\n[Hook context]: second"
    assert build_hook_context("write_file", {}, _result(), "sess-1") is None


def test_blank_hook_context_is_not_stashed():
    stash_hook_context("sess-1", "  ")

    assert build_hook_context("write_file", {}, _result(), "sess-1") is None


def test_clear_hook_context_drops_pending_text():
    stash_hook_context("sess-1", "pending")
    clear_hook_context("sess-1")

    assert build_hook_context("write_file", {}, _result(), "sess-1") is None


def test_injectors_module_exports_the_expected_helpers():
    assert injectors.stash_hook_context is stash_hook_context
    assert injectors.build_hook_context is build_hook_context
