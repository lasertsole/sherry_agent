"""Context hit-rate analyzer — now a thin alias over the product implementation.

The analyzer used to be duplicated here and in ``pub.func.session_report``.
The product layer is now the single source of truth; this module keeps the
historical eval import path working (``from evals.shared.context_analyzer
import analyze_transcript`` in ``evals/gaia/suite.py``) and the eval-only
payload keys (``wasted_breakdown``, ``effective_tokens``, ...) which the
product payload already carries as backward-compatible aliases.
"""

from __future__ import annotations

from pub.func.session_report.context_analyzer import ContextAnalyzer, analyze_transcript

__all__ = ["ContextAnalyzer", "analyze_transcript"]
