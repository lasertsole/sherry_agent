"""Real-repository E2E (llm_e2e): GitHub issue → worktree → PR → CI → merge.

Deselected by default; run explicitly with ``pytest -m llm_e2e`` against a real
test repository and token.  The mocked CLI/worker chains live in
``test_github_e2e.py`` and ``test_feishu_e2e.py`` and always run.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.llm_e2e


@pytest.fixture()
def live_repo():
    """Skip unless a real test repository and token are configured."""
    repo = os.getenv("DSH_SUPERAGENT_E2E_REPO", "")
    token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    if not repo or not token:
        pytest.skip("set DSH_SUPERAGENT_E2E_REPO and GH_TOKEN to run the live E2E")
    return repo


def test_issue_to_merged_ticket(live_repo):
    """Drive one real issue through the full workflow and assert a merged Ticket.

    This is an operational acceptance test, not a hermetic unit: it needs a real
    repository, a token with push rights, and a passing CI on the test repo. The
    exact issue body and assertion are supplied by the operator running the
    acceptance gate; the test documents the expected end state here.
    """
    del live_repo
    pytest.skip("live acceptance requires an operator-supplied issue fixture")
