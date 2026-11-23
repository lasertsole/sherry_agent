"""Cross-benchmark helpers for the external eval suites (7 new benchmarks).

Everything here is dataset-optional by contract: a suite that cannot reach its
dataset SKIPS loudly (exit 3) instead of passing, per the plan's kill criteria.
None of these modules import the agent runtime or the server — they are plain
library code that unit tests can exercise without Docker, keys or a model.

The three post-task analyzers (``context_analyzer``, ``code_intel_tracker`` and
the token folding in ``coding_capability.suite._session_tokens``) now delegate
their shared algorithms to ``pub.func.session_report``, the product layer's
single source of truth. Eval-only report shapes stay here as thin compatibility
adapters; ``pub`` has no dependency back on ``evals`` or ``agent``.

The eval-control helpers (``eval_temperature``, ``eval_iteration_budget``,
``eval_session``, ``eval_checklist``) keep any agent/server import inside the
calling function, so importing this package never pulls in the runtime. They
give every coding benchmark the same deterministic decoding, iteration budget,
YOLO session grant and reviewer-checklist extraction.
"""
