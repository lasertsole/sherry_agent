"""RerankerProtocol conformance (DESIGN_PATTERN §3.3.4).

``CrossEncoderGGUF`` and ``CloudReranker`` must both satisfy the shared
``RerankerProtocol`` contract without changing their method signatures or
behavior. Instances are created via ``__new__`` so no model download / network
client is involved.
"""

import pytest

from models.reranker_model import reranker_model
from models.reranker_model.core import (
    CloudReranker,
    CrossEncoderGGUF,
    RerankerProtocol,
    reranker_conformance,
)

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

_METHODS = ("predict", "predict_scores", "rank", "filter")


def _bare(cls):
    return cls.__new__(cls)


class TestProtocolConformance:
    def test_both_backends_satisfy_protocol(self):
        assert isinstance(_bare(CrossEncoderGGUF), RerankerProtocol)
        assert isinstance(_bare(CloudReranker), RerankerProtocol)

    def test_unrelated_object_does_not_satisfy(self):
        assert not isinstance(object(), RerankerProtocol)

    def test_conformance_probe_returns_inputs(self):
        local = _bare(CrossEncoderGGUF)
        cloud = _bare(CloudReranker)
        assert reranker_conformance(local, cloud) == (local, cloud)

    @pytest.mark.parametrize("method", _METHODS)
    def test_both_backends_expose_every_contract_method(self, method):
        assert callable(getattr(_bare(CrossEncoderGGUF), method))
        assert callable(getattr(_bare(CloudReranker), method))

    def test_lazy_dispatch_singleton_exposes_the_same_methods(self):
        for method in _METHODS:
            assert callable(getattr(reranker_model, method))
