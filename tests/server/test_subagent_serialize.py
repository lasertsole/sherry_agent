"""TDD tests for audit 2.1.5 — shared SubagentRunRecord serialization
(server/trigger/subagent_serialize.py).

Pins: the public-field tuple is identical across the HTTP and WS consumers,
and ``serialize_run`` emits only the public fields via ``model_dump(...,
mode="json")``.
"""

import pytest
from pydantic import BaseModel

from server.trigger.subagent_serialize import PUBLIC_FIELDS, serialize_run

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class _Nested(BaseModel):
    status: str


class _FakeRunRecord(BaseModel):
    """Minimal pydantic stand-in with a few public + private fields."""

    run_id: str
    task: str
    depth: int
    execution: _Nested
    internal_policy_vector: str = "secret"


class TestSharedSerialization:
    def test_public_fields_tuple_shape(self):
        assert len(PUBLIC_FIELDS) == 20
        assert "run_id" in PUBLIC_FIELDS
        assert "execution" in PUBLIC_FIELDS
        assert "delivery" in PUBLIC_FIELDS
        # no private fields leak into the tuple
        assert all(not f.startswith("_") for f in PUBLIC_FIELDS)

    def test_serialize_run_emits_only_public_fields(self):
        run = _FakeRunRecord(run_id="r1", task="t", depth=2, execution=_Nested(status="ok"))

        got = serialize_run(run)

        assert got == {
            "run_id": "r1",
            "task": "t",
            "depth": 2,
            "execution": {"status": "ok"},
        }
        assert "internal_policy_vector" not in got

    def test_http_and_ws_consumers_share_the_same_objects(self):
        from server.trigger.http import subagent as http_subagent
        from server.trigger.ws import subagent_ws

        assert http_subagent._PUBLIC_FIELDS is PUBLIC_FIELDS
        assert subagent_ws._PUBLIC_FIELDS is PUBLIC_FIELDS
        assert http_subagent._serialize_run is serialize_run
        assert subagent_ws._serialize_run is serialize_run
