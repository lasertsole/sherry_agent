import pytest
from future_subagent.announce.idempotency import build_idempotency_key


class TestBuildIdempotencyKey:
    def test_without_suffix(self):
        key = build_idempotency_key("r1", generation=0)
        assert key == "subagent_announce:r1:gen:0"

    def test_with_suffix(self):
        key = build_idempotency_key("r1", generation=0, suffix="wake")
        assert key == "subagent_announce:r1:gen:0:wake"

    def test_different_suffixes(self):
        k1 = build_idempotency_key("r1", 0)
        k2 = build_idempotency_key("r1", 0, suffix="wake")
        assert k1 != k2

    def test_different_generations(self):
        k1 = build_idempotency_key("r1", 0)
        k2 = build_idempotency_key("r1", 1)
        assert k1 != k2
