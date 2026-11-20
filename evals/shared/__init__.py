"""Cross-benchmark helpers for the external eval suites (7 new benchmarks).

Everything here is dataset-optional by contract: a suite that cannot reach its
dataset SKIPS loudly (exit 3) instead of passing, per the plan's kill criteria.
None of these modules import the agent runtime — they are plain library code
that unit tests can exercise without Docker, keys or a model.
"""
