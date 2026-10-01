"""The cron READMEs must stay faithful to the code they describe.

The four-language cron README documents a surface that moves: the payload
fields, the two `add_job` signatures, the REST body and the reference-
maintenance helpers. Every claim checked here is one this document has just
started making (the skill-binding feature), so a rename or a dropped parameter
would otherwise leave four translations confidently describing nothing.

Checked:

1. the ``skills`` payload field exists on ``CronPayload`` and in the store
   round-trip (``_save_store`` writes it, ``_load_store`` reads it);
2. both documented ``add_job`` signatures accept ``skills``;
3. the documented helpers exist where the document says they do;
4. every language states the same payload key, signatures and helper names.
"""

from __future__ import annotations

import inspect
import pathlib
import re

import pytest

from server.trigger.http import cron as cron_http
from skills.builtin.core.cron.scripts import base as cron_base
from skills.builtin.core.cron.scripts import core as cron_core
from skills.builtin.core.cron.scripts import skill_refs
from skills.builtin.core.cron.scripts.types import CronPayload

pytestmark = [pytest.mark.unit]

_GROUP = tuple(
    pathlib.Path(f"skills/builtin/core/cron/scripts/README{suffix}.md")
    for suffix in ("", ".zh", ".ja", ".ko")
)


def test_payload_field_matches_the_store_round_trip():
    assert "skills" in CronPayload.__dataclass_fields__

    source = inspect.getsource(cron_base)
    assert '"skills": j.payload.skills' in source, "_save_store must persist the field"
    assert 'get("skills")' in source, "_load_store must read it back"


def test_both_documented_add_job_signatures_accept_skills():
    service_sig = inspect.signature(cron_base.CronService.add_job)
    facade_sig = inspect.signature(cron_core.Cron.add_job)
    assert "skills" in service_sig.parameters, "CronService.add_job lost `skills`"
    assert "skills" in facade_sig.parameters, "Cron.add_job lost `skills`"


def test_reference_maintenance_helpers_exist_where_documented():
    assert callable(skill_refs.rewrite_skill_refs)
    assert callable(skill_refs.referenced_skill_names)
    assert cron_http._valid_skills(None) is None, "the HTTP layer validates the field"
    assert cron_http._valid_skills(["a", "a"]) == ["a"]


def test_every_language_documents_the_same_surface():
    for doc in _GROUP:
        text = doc.read_text(encoding="utf-8")
        for needle in (
            "`payload.skills`",
            "`skills`",
            "`_assemble_skill_prompt()`",
            "`rewrite_skill_refs(consolidated, pruned)`",
            "`referenced_skill_names()`",
            "`skill_refs.py`",
            "`skills.builtin.core.cron.scripts`",
        ):
            assert needle in text, f"{doc}: missing {needle}"
        # Both add_job signatures are documented with the parameter.
        assert re.search(r"cron\.add_job\([^)]*skills=None", text), f"{doc}: facade signature"
        assert re.search(r"add_job\(name, schedule, message[^)]*skills=None", text), (
            f"{doc}: service signature"
        )
        # The HTTP body documents the field, and the example store carries it.
        assert '"delete_after_run", "skills"' in text, f"{doc}: REST body"
        assert '"skills": ["news-digest"]' in text, f"{doc}: store example"
